"""Bounds are not permission (FR-STATE-10,12; docs/02 §7.1's effect rules; #188): who may change a
State, which events justify each direction, the caps on a render's changes, and transfers that
balance between tracked parties. Pure: nothing here reads the database or calls a model;
``core.states.effects.dry_run`` runs it over a turn, and ``core.states.api`` (#186's consumer
writes) and #192's rules share its source rule.

**The declaration** (``authorization_of``, ``transfer_of``; checked by ``check_effect_rules``
at definition time and on ``update_definition``):

- ``authorization = {sources, justifications: {increase, decrease}, caps: {per_effect,
  per_turn}}``. ``sources`` lists ``character:<id>``, ``any_character``, ``director`` (the
  narrator's world pass), ``author``, ``consumer`` and ``rule``. A State with no
  ``authorization`` (or no ``sources``) has the default sources ``DEFAULT_SOURCES`` (all five
  kinds), unrestricted justifications and no caps. A list of justification labels (free author
  strings) restricts that direction; an empty or absent one leaves it open. A cap is a positive
  number (number States only); ``null`` is none.
- ``transfer = {conserved, counterparties}``: whether value of this State is conserved (moves
  between parties, never appears from nothing in a render), and the States a transfer may name
  as its other side besides the State itself. A counterparty that names no State of the story
  is **untracked**; one owned by an external provider (#204) is **external**.
- A definition with ``rules`` (#192) whose sources omit ``rule`` is refused (422
  ``state_unpermitted``, ``reason: source``; owner decision 2026-10-30). That check is
  ``model.check_rules_shape``'s (after these shapes), through ``source_permitted``.

**The decision** (``authorize``), in docs/02's validation order, for one effect (``StateEffect``)
of a source (``Source``), given the turn's effects already authorized (``Authorized``):

1. **Source.** A ``reactive`` asserter never authorizes an effect (FR-MULTI-9:
   ``reactive_asserter``); a ``co_author``-narrated beat carries none (``source``). A source the
   State's ``sources`` do not admit is ``source``; a transfer naming a counterparty the State's
   ``transfer.counterparties`` do not list is ``counterparty``. A **world gain** (a character's
   increase of a ``conserved`` State with no counterparty: value appearing from nothing, "found
   treasure") is a world fact: ``world_gain_needs_director`` (FR-VERIFY-11).
2. **Justification** (a render's effects only). An increase or a decrease of a direction whose
   list is non-empty names a ``justification`` from it and a ``justified_by`` beat of the same
   turn that verification kept (``Evidence``: present, not an attempt). The cited beat must fit
   the label: ``sale`` and ``theft`` need a ``transfer`` or ``possession`` category (#148's
   verb tables, ``categories_of``); ``loot`` and ``reward`` a ``possession`` category or a
   narration beat; any other label only the beat. A cited beat that does not exist or does not
   fit is ``justification`` whatever the list.
3. **Preconditions** (#189, ``core.states.preconditions``): counterparty consent, the item's
   availability, co-location and ownership, verified as propositions; every hook in
   ``PRECONDITIONS`` may refuse (``precondition:<name>``).
4. **Balance.** A transfer of a ``conserved`` State whose other side is tracked must balance:
   the other side's delta, when declared, is the negation of this one's, else
   ``unbalanced_transfer``. A transfer that is not conserved may be unbalanced.
5. **Caps** (a render's effects only: the consumer, the author and rules are never capped). An
   effect larger than ``per_effect``, or than what ``per_turn`` leaves after the turn's earlier
   effects on the same value (repairs' included), is ``Capped`` to that amount; nothing left is
   ``cap``.

The bounds (``model.violation``) come after, in the dry-run. Codes only; this module logs
nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any, Final, Literal

from epistrel.core.enums import StateOwner, StateType
from epistrel.core.errors import InvalidRequest
from epistrel.core.states.model import (
    NAME_PATTERN,
    StateDefinition,
    Undefined,
    UndefinedType,
    Value,
    number,
)

ANY_CHARACTER: Final = "any_character"
CHARACTER_PREFIX: Final = "character:"
DIRECTOR: Final = "director"
AUTHOR: Final = "author"
CONSUMER: Final = "consumer"
RULE: Final = "rule"
DEFAULT_SOURCES: tuple[str, ...] = (ANY_CHARACTER, DIRECTOR, AUTHOR, CONSUMER, RULE)
"""A State's sources when its definition names none (FR-STATE-10's default)."""
SOURCE_KINDS: frozenset[str] = frozenset({ANY_CHARACTER, DIRECTOR, AUTHOR, CONSUMER, RULE})

Direction = Literal["increase", "decrease"]
INCREASE: Final = "increase"
DECREASE: Final = "decrease"
CHANGE, TRANSFER = "change", "transfer"
SALE, THEFT, LOOT, REWARD = "sale", "theft", "loot", "reward"
TRANSFER_LABELS: frozenset[str] = frozenset({SALE, THEFT})
"""Labels whose cited beat must be a ``transfer`` or ``possession`` one."""
WORLD_LABELS: frozenset[str] = frozenset({LOOT, REWARD})
"""Labels whose cited beat must be a ``possession`` or a narration one."""
LABEL_MAX = 80

# Refusal reasons (``Unpermitted.reason``).
SOURCE: Final = "source"
REACTIVE_ASSERTER: Final = "reactive_asserter"
COUNTERPARTY: Final = "counterparty"
WORLD_GAIN: Final = "world_gain_needs_director"
JUSTIFICATION: Final = "justification"
UNBALANCED: Final = "unbalanced_transfer"
CAP: Final = "cap"
REASONS: frozenset[str] = frozenset(
    {SOURCE, REACTIVE_ASSERTER, COUNTERPARTY, WORLD_GAIN, JUSTIFICATION, UNBALANCED, CAP}
)

UNTRACKED, EXTERNAL = "untracked", "external"
"""How a transfer's other side is reported when it is not a story-owned State."""

SourceKind = Literal["character", "director", "author", "consumer", "rule", "reactive", "co_author"]
RENDER_KINDS: frozenset[str] = frozenset({"character", "director"})
"""The sources whose effects need a justification and stay under the caps."""


# -- the declaration -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Authorization:
    """A State's ``authorization`` with its defaults (see the module docstring)."""

    sources: tuple[str, ...] = DEFAULT_SOURCES
    increase: tuple[str, ...] = ()
    decrease: tuple[str, ...] = ()
    per_effect: Decimal | None = None
    per_turn: Decimal | None = None

    def labels(self, direction: Direction) -> tuple[str, ...]:
        return self.increase if direction == INCREASE else self.decrease


@dataclass(frozen=True, slots=True)
class TransferSettings:
    """A State's ``transfer``: conserved, and the other States a transfer may name."""

    conserved: bool = False
    counterparties: tuple[str, ...] = ()


def _strings(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list | tuple):
        return tuple(str(s) for s in raw)
    return ()


def _cap(raw: Any) -> Decimal | None:
    return None if raw is None or isinstance(raw, bool) else number(raw)


def authorization_of(definition: StateDefinition) -> Authorization:
    """``definition``'s authorization, its defaults filled in."""
    doc = definition.authorization or {}
    justifications = doc.get("justifications") or {}
    caps = doc.get("caps") or {}
    return Authorization(
        sources=_strings(doc.get("sources")) or DEFAULT_SOURCES,
        increase=_strings(justifications.get(INCREASE)),
        decrease=_strings(justifications.get(DECREASE)),
        per_effect=_cap(caps.get("per_effect")),
        per_turn=_cap(caps.get("per_turn")),
    )


def transfer_of(definition: StateDefinition) -> TransferSettings:
    """``definition``'s transfer settings (``conserved: false`` and no counterparties by
    default)."""
    doc = definition.transfer or {}
    return TransferSettings(
        conserved=bool(doc.get("conserved", False)),
        counterparties=_strings(doc.get("counterparties")),
    )


def _invalid(message: str, field: str, reason: str) -> InvalidRequest:
    return InvalidRequest(
        message,
        {
            "field": field,
            "reason": reason,
            "errors": [{"field": field, "message": message, "type": "value_error"}],
        },
    )


def _valid_source(source: str) -> bool:
    if source in SOURCE_KINDS:
        return True
    return source.startswith(CHARACTER_PREFIX) and len(source) > len(CHARACTER_PREFIX)


def _check_labels(raw: Any, field: str) -> None:
    if raw is None:
        return
    if not isinstance(raw, list | tuple) or not all(isinstance(s, str) for s in raw):
        raise _invalid("justification labels are a list of strings", field, "type")
    labels = [s.strip() for s in raw]
    if any(not s or len(s) > LABEL_MAX for s in labels):
        raise _invalid(f"a justification label is 1 to {LABEL_MAX} characters", field, "length")
    if len(set(labels)) != len(labels):
        raise _invalid("justification labels are unique", field, "duplicate")


def _check_cap(raw: Any, field: str, numeric: bool) -> None:
    if raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, int | float | Decimal):
        raise _invalid("a cap is a positive number or null", field, "type")
    if not numeric:
        raise _invalid("only a number State has caps", field, "not_number")
    if number(raw) is None or Decimal(str(raw)) <= 0:
        raise _invalid("a cap is a positive number or null", field, "positive")


def _check_authorization(d: StateDefinition) -> None:
    doc = d.authorization
    unknown = sorted(set(doc) - {"sources", "justifications", "caps"})
    if unknown:
        raise _invalid(f"unknown authorization member {unknown[0]!r}", "authorization", "unknown")
    if "sources" in doc and doc["sources"] is not None:
        sources = doc["sources"]
        if not isinstance(sources, list | tuple) or not all(isinstance(s, str) for s in sources):
            raise _invalid("sources is a list of strings", "authorization.sources", "type")
        if not sources:
            raise _invalid(
                "sources names at least one source (omit it for the default)",
                "authorization.sources",
                "empty",
            )
        bad = [s for s in sources if not _valid_source(s)]
        if bad:
            raise _invalid(
                "a source is character:<id>, any_character, director, author, consumer or rule",
                "authorization.sources",
                "unknown_source",
            )
        if len(set(sources)) != len(sources):
            raise _invalid("sources are unique", "authorization.sources", "duplicate")
    justifications = doc.get("justifications")
    if justifications is not None:
        if not isinstance(justifications, Mapping) or set(justifications) - {INCREASE, DECREASE}:
            raise _invalid(
                "justifications has increase and decrease lists",
                "authorization.justifications",
                "shape",
            )
        for direction in (INCREASE, DECREASE):
            _check_labels(
                justifications.get(direction), f"authorization.justifications.{direction}"
            )
    caps = doc.get("caps")
    if caps is not None:
        if not isinstance(caps, Mapping) or set(caps) - {"per_effect", "per_turn"}:
            raise _invalid("caps has per_effect and per_turn", "authorization.caps", "shape")
        numeric = d.type is StateType.NUMBER
        for name in ("per_effect", "per_turn"):
            _check_cap(caps.get(name), f"authorization.caps.{name}", numeric)


def _check_transfer(d: StateDefinition) -> None:
    doc = d.transfer
    if not doc:
        return
    unknown = sorted(set(doc) - {"conserved", "counterparties"})
    if unknown:
        raise _invalid(f"unknown transfer member {unknown[0]!r}", "transfer", "unknown")
    if d.type is not StateType.NUMBER:
        raise _invalid("only a number State transfers", "transfer", "not_number")
    if "conserved" in doc and not isinstance(doc["conserved"], bool):
        raise _invalid("conserved is a boolean", "transfer.conserved", "type")
    counterparties = doc.get("counterparties")
    if counterparties is None:
        return
    if not isinstance(counterparties, list | tuple) or not all(
        isinstance(s, str) and NAME_PATTERN.match(s) for s in counterparties
    ):
        raise _invalid("counterparties is a list of State names", "transfer.counterparties", "name")
    if len(set(counterparties)) != len(counterparties):
        raise _invalid("counterparties are unique", "transfer.counterparties", "duplicate")


def check_effect_rules(definition: StateDefinition) -> None:
    """``definition``'s ``authorization`` and ``transfer`` checked (422 ``validation_error``
    with ``details.reason``). Its ``rules`` against its sources (422 ``state_unpermitted``,
    ``reason: source``) are ``model.check_rules_shape``'s, which runs after this and asks
    ``source_permitted``."""
    _check_authorization(definition)
    _check_transfer(definition)


def source_permitted(definition: StateDefinition, source: str) -> bool:
    """Whether ``definition``'s sources admit ``source`` (``consumer``, ``author``, ``rule``,
    ``director``, or ``character:<id>``, which ``any_character`` also admits)."""
    sources = authorization_of(definition).sources
    if source in sources:
        return True
    return source.startswith(CHARACTER_PREFIX) and ANY_CHARACTER in sources


# -- the decision --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """Who asserts an effect: a render's character or the director (the narrator), the author,
    the consumer, a rule, or a ``reactive``/``co_author`` narration, which carries none."""

    kind: SourceKind
    character_id: str | None = None

    @property
    def name(self) -> str:
        """The ``authorization.sources`` entry this source is (``character:<id>`` for a
        character's render)."""
        if self.kind == "character":
            return f"{CHARACTER_PREFIX}{self.character_id}"
        return self.kind

    @property
    def render(self) -> bool:
        return self.kind in RENDER_KINDS


@dataclass(frozen=True, slots=True)
class Counterparty:
    """A transfer's other side: the State, its scope key and its delta (``None``: mirrors)."""

    state: str
    scope_key: str | None = None
    delta: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StateEffect:
    """One effect to authorize (#187's typed effect, as Core takes it): the State and scope key,
    its kind (``change`` or ``transfer``), its ``delta`` or new ``value``, the counterparty, the
    justification label and the beat that justifies it; ``consideration`` is #189's."""

    state: str
    scope_key: str | None = None
    kind: str = CHANGE
    delta: Decimal | None = None
    value: Value | None = None
    counterparty: Counterparty | None = None
    justification: str | None = None
    justified_by: int | None = None
    consideration: str | None = None
    beat: int | None = None
    """The number of the beat that declares it (#189: the same beat's possession change)."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """A beat of the turn a justification may cite: its number, kind, the #148 categories its
    text holds and whether it is narration."""

    beat: int
    kind: str
    categories: frozenset[str] = frozenset()
    narration: bool = False


@dataclass(frozen=True, slots=True)
class Authorized:
    """An effect the turn already authorized: the value it changes and by how much (its
    magnitude; a transfer's tracked other side is one too)."""

    key: tuple[str, str | None]
    magnitude: Decimal


@dataclass(frozen=True, slots=True)
class Permitted:
    """The effect may go on to the bounds."""


@dataclass(frozen=True, slots=True)
class Unpermitted:
    """The effect is refused (unpermitted novelty, repaired): ``reason`` is one of ``REASONS``."""

    reason: str


@dataclass(frozen=True, slots=True)
class Capped:
    """The effect is larger than a cap allows: repaired down to ``amount`` (the signed delta the
    cap leaves); ``declared`` is the signed delta it asked for."""

    amount: Decimal
    declared: Decimal


Decision = Permitted | Unpermitted | Capped
Precondition = Callable[[StateEffect, Source, StateDefinition, Any], Unpermitted | None]
PRECONDITIONS: list[Precondition] = []
"""#189's seam: action preconditions verified as propositions, after the justification
(``core.states.preconditions.evaluate``; its last argument is ``authorize``'s ``context``, the
turn's beats and the canon the attempt read, ``None`` when there is none to check against)."""


def direction_of(delta: Decimal | None) -> Direction | None:
    if delta is None or delta == 0:
        return None
    return INCREASE if delta > 0 else DECREASE


def change_of(effect: StateEffect, current: Value | UndefinedType) -> Decimal | None:
    """The signed amount ``effect`` changes a number by (``None``: not a number change)."""
    if effect.delta is not None:
        return effect.delta
    value = effect.value
    if isinstance(value, Decimal) and isinstance(current, Decimal):
        return value - current
    return None


@cache
def _category_patterns() -> dict[str, re.Pattern[str]]:
    from epistrel.core.verify.detect import tables

    forms: dict[str, list[str]] = {}
    for table in tables().verbs:
        for lemma in table.lemmas:
            forms.setdefault(table.category, []).extend(lemma.forms)
    edge = "\\w'\u2019"
    return {
        category: re.compile(
            f"(?<![{edge}])(?:"
            + "|".join(
                r"\s+".join(re.escape(p) for p in w.split())
                for w in sorted(set(words), key=lambda w: (-len(w), w))
            )
            + f")(?![{edge}])",
            re.IGNORECASE,
        )
        for category, words in forms.items()
        if words
    }


def categories_of(text: str) -> frozenset[str]:
    """The #148 verb categories (``possession``, ``transfer``, ``consumption``, …) ``text``
    holds."""
    return frozenset(c for c, p in _category_patterns().items() if p.search(text))


def _fits(label: str, cited: Evidence) -> bool:
    if label in TRANSFER_LABELS:
        return bool(cited.categories & {"transfer", "possession"})
    if label in WORLD_LABELS:
        return "possession" in cited.categories or cited.narration
    return True


def _justified(
    effect: StateEffect,
    labels: Sequence[str],
    evidence: Mapping[int, Evidence],
) -> bool:
    if not labels and effect.justification is None and effect.justified_by is None:
        return True
    if labels and (effect.justification is None or effect.justification not in labels):
        return False
    if effect.justified_by is None:
        return not labels
    cited = evidence.get(effect.justified_by)
    if cited is None:
        return False
    return effect.justification is None or _fits(effect.justification, cited)


def tracked(definitions: Mapping[str, StateDefinition], name: str) -> bool:
    """Whether the State ``name`` is story-owned and the story's (a transfer's tracked side)."""
    found = definitions.get(name)
    return found is not None and found.owner is StateOwner.STORY


def counterparty_kind(definitions: Mapping[str, StateDefinition], name: str) -> str | None:
    """``untracked`` or ``external`` for a transfer's other side that is not a story-owned
    State, ``None`` for one that is."""
    found = definitions.get(name)
    if found is None:
        return UNTRACKED
    return EXTERNAL if found.owner is StateOwner.EXTERNAL else None


def authorize(
    effect: StateEffect,
    asserter: Source,
    turn_effects_so_far: Sequence[Authorized],
    *,
    definition: StateDefinition,
    definitions: Mapping[str, StateDefinition] | None = None,
    current: Value | UndefinedType | None = None,
    evidence: Mapping[int, Evidence] | None = None,
    context: Any = None,
) -> Decision:
    """Whether ``asserter`` may make ``effect`` (see the module docstring): ``definitions``
    the story's States (a transfer's other side), ``current`` the value it finds, ``evidence``
    the turn's beats a justification may cite (``None``: not checked, as for a source that needs
    none), ``context`` what the ``PRECONDITIONS`` hooks read (#189)."""
    found = definitions or {definition.name: definition}
    if asserter.kind == "reactive":
        return Unpermitted(REACTIVE_ASSERTER)
    if asserter.kind == "co_author" or not source_permitted(definition, asserter.name):
        return Unpermitted(SOURCE)
    settings = transfer_of(definition)
    other = effect.counterparty
    if (
        effect.kind == TRANSFER
        and other is not None
        and other.state != definition.name
        and other.state not in settings.counterparties
    ):
        return Unpermitted(COUNTERPARTY)
    amount = change_of(effect, Undefined if current is None else current)
    direction = direction_of(amount)
    if (
        asserter.kind == "character"
        and settings.conserved
        and direction == INCREASE
        and (effect.kind != TRANSFER or other is None)
    ):
        return Unpermitted(WORLD_GAIN)
    rules = authorization_of(definition)
    if asserter.render and evidence is not None:
        labels = () if direction is None else rules.labels(direction)
        if not _justified(effect, labels, evidence):
            return Unpermitted(JUSTIFICATION)
    for hook in PRECONDITIONS:
        refused = hook(effect, asserter, definition, context)
        if refused is not None:
            return refused
    if (
        effect.kind == TRANSFER
        and other is not None
        and settings.conserved
        and tracked(found, other.state)
        and other.delta is not None
        and amount is not None
        and other.delta != -amount
    ):
        return Unpermitted(UNBALANCED)
    if not asserter.render or amount is None or amount == 0:
        return Permitted()
    magnitude = abs(amount)
    allowed = magnitude
    if rules.per_effect is not None:
        allowed = min(allowed, rules.per_effect)
    if rules.per_turn is not None:
        key = (definition.name, effect.scope_key)
        used = sum((a.magnitude for a in turn_effects_so_far if a.key == key), Decimal(0))
        allowed = min(allowed, max(Decimal(0), rules.per_turn - used))
    if allowed == magnitude:
        return Permitted()
    if allowed <= 0:
        return Unpermitted(CAP)
    return Capped(allowed if amount > 0 else -allowed, amount)


def authorized(
    effect: StateEffect,
    amount: Decimal | None,
    definitions: Mapping[str, StateDefinition],
) -> list[Authorized]:
    """What an effect that passed adds to the turn's per-turn tally: its own value's change and
    a tracked other side's."""
    if amount is None or amount == 0:
        return []
    found = [Authorized((effect.state, effect.scope_key), abs(amount))]
    other = effect.counterparty
    if effect.kind == TRANSFER and other is not None and tracked(definitions, other.state):
        moved = other.delta if other.delta is not None else -amount
        found.append(Authorized((other.state, other.scope_key), abs(moved)))
    return found


__all__ = [
    "ANY_CHARACTER",
    "AUTHOR",
    "CAP",
    "CHANGE",
    "CHARACTER_PREFIX",
    "CONSUMER",
    "COUNTERPARTY",
    "DECREASE",
    "DEFAULT_SOURCES",
    "DIRECTOR",
    "EXTERNAL",
    "INCREASE",
    "JUSTIFICATION",
    "PRECONDITIONS",
    "REACTIVE_ASSERTER",
    "REASONS",
    "RULE",
    "SOURCE",
    "TRANSFER",
    "UNBALANCED",
    "UNTRACKED",
    "WORLD_GAIN",
    "Authorization",
    "Authorized",
    "Capped",
    "Counterparty",
    "Decision",
    "Evidence",
    "Permitted",
    "Precondition",
    "Source",
    "StateEffect",
    "TransferSettings",
    "Unpermitted",
    "authorization_of",
    "authorize",
    "authorized",
    "categories_of",
    "change_of",
    "check_effect_rules",
    "counterparty_kind",
    "direction_of",
    "source_permitted",
    "tracked",
    "transfer_of",
]
