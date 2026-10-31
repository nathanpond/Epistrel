"""State values as objective truth, not knowledge (FR-STATE-8, FR-VERIFY-7,13; #191).

A State's value is a proposition of the core predicate ``state_value`` (``01JPRED…35``):
``state_value(subject, state = <State id>) = <value>``, the subject the character a per-character
State belongs to (its scope key), or the State's own id for a story State, and the value a
typed literal (a number as an ``int`` or a ``float``, a boolean, an enum value or text as a
``str``). Its valid-time is open: a value is what holds at a narrative position, which the
fold and a holder's ``held_from``/``held_to`` bound, so a later observation of another value
closes the earlier belief (#160's functional correction).

**The owner lookup** (``STATE_LOOKUP``, registered for ``state_value`` with
``core.propositions.registry.register_owner_lookup``). ``state_value`` takes part in #140's rules
only through it (ADR-160's owner paths):

- ``read`` (``core.readset.KeyedLookup.owned``, a query key of the turn's read set; a changed
  value regenerates through #140's rebase re-check, which re-reads it): each State's fold
  (``core.states.folded_at``) on the active timeline through the claim's position, for the
  subjects asked (every story State, and the per-character States of the subjects that are
  characters), as the viewpoint may read it: a ``hidden`` State only ``omniscient``.
  An externally owned State's value is its pinned read (#205, ``core.states.external``): the
  current turn's pin, else the recorded version at or before the position (a frame-bounded read
  of an earlier turn included: never a later or a live one); one with none is left out (a
  claim on it is ``novel``). A value whose provider is registered ``trust_tier = advisory``
  (#204's ``external_providers``) is ``unsettled`` (#207).
- ``verdict`` (pure): over a canon holder's values, a claim is ``supported`` when its value
  matches the fold (a number within the State's ``constraints.tolerance``; an enum and a boolean
  exactly; text case-insensitively) and ``contradicted`` otherwise (``functional_key``; a
  negated claim the other way round, ``polarity``), citing the value's ``owner_ref``
  (``state:<id>``). No value the viewpoint may read (a hidden State, a State the story no
  longer has) is ``novel``: the claim's permission decides (#148's protected content denies
  it), and a ``state_value`` claim is never canonized. A State with no value yet, or a claim
  of the wrong type, is ``uncertain`` (``unresolved_entity``, ``argument_type``): the safe
  disposition, with no model call. An advisory provider's value settles nothing (#207, the
  owner's decision): a claim on it is ``uncertain(advisory)`` whether it matches or not, so an
  equal claim is not ``supported`` and a differing one is hedged (#151), not repaired.
- ``implied`` (pure): a declared rule's consequence on ``state_value`` (``holds ⇒
  state_value``: the possession of the entity a possession-style State tracks) as the value
  its cause states (the ``holds`` claim's ``quantity``), which ``verdict`` then checks.

A ``belief(A)`` holder never consults the lookup: A's ``state_value`` beliefs are ordinary
beliefs (ADR-167), acquired only through ``core.epistemic.transitions.transition()`` (ADR-168):
an observation of a beat that states the value (``observation.observe`` over
``RouteBeat.claims``), a verified #168 ``observe`` step, testimony (#162). For an externally
owned State (``external_claim``, the lookup's ``belief_only`` test; #207) A's partition alone
decides: ADR-167's canon fallback (``check_with`` under ``character:A``) is not consulted, so
neither the pin nor a fact (an external reference #205 committed, a stale planted one) answers
for A, and a value A holds no belief about is ``novel`` to A (hidden to #166's classification).

**Detection** (``state_claim``, called by #148's ``core.verify.detect`` with the story's
``TrackedState``s). A lone quantity whose unit names a number State (its name, an alias or its
unit, singular or plural) in a sentence that states a balance (``VALUE_CUES``: "has", "left",
"in all", "counts", …) and holds no price or change cue (``PRICE_CUES``, #148's possession,
transfer and consumption verbs) is a ``state_value`` claim: the subject the sentence names
before the amount (#139's resolution), which must be one of the story's characters for a
per-character State. Anything else stays the lone quantity it was (``uncertain``).

**Hidden States in the protected index** (#148, U-22 M6). ``hidden_pairs`` gives each hidden
State's current values: the speaker who holds no belief about one (no ``state_value`` belief of
its key; the player and the narrator never do) may not say it. Its term is a pairing, as #171's
hidden identities are: the value's renderings in one clause with the State's name or an alias
("trust … three"), so "three" alone is no hit. A ``player_visible`` State is not protected.

Pure but for ``read`` and ``hidden_pairs``; no story text in any log (this module logs nothing).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import StateOwner, StateScope, StateType, StateVisibility
from epistrel.core.propositions.model import (
    Interval,
    LiteralKind,
    Polarity,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.registry import register_owner_lookup
from epistrel.core.propositions.vocabulary import STATE_VALUE
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.states.model import StateDefinition, UndefinedType, Value, from_json, number
from epistrel.core.verify.rules import (
    Evidence,
    OwnedValue,
    Rule,
    Verdict,
    VerdictKind,
    VerdictReason,
    uncertain,
)

if TYPE_CHECKING:
    from epistrel.core.membership import Position

STATE_QUALIFIER: Final = "state"
"""The ``state_value`` qualifier naming the State (its id): with the subject, the key."""
REF_PREFIX: Final = "state:"
"""An owner value's ``ref``: ``state:<State id>``."""
ADVISORY_TIER: Final = "advisory"
"""#204's ``trust_tier`` whose values settle no claim (#207; ``VerdictReason.ADVISORY``)."""

VALUE_CUES: tuple[str, ...] = (
    "has",
    "have",
    "had",
    "having",
    "got",
    "left",
    "in all",
    "all told",
    "in total",
    "total",
    "totals",
    "count",
    "counts",
    "counted",
    "counting",
    "balance",
)
"""Words in a sentence that make an amount naming a State its balance (see the module
docstring): what someone has, has left, or counts."""
PRICE_CUES: tuple[str, ...] = (
    "for",
    "cost",
    "costs",
    "price",
    "priced",
    "pay",
    "pays",
    "paid",
    "paying",
    "worth",
    "charge",
    "charges",
    "charged",
    "owe",
    "owes",
    "owed",
    "spend",
    "spends",
    "spent",
    "more",
    "less",
    "another",
    "extra",
)
"""Words that make an amount a price, a payment or a change, never a balance."""


def _words_pattern(words: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted(set(words), key=lambda w: (-len(w), w))
    body = "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in ordered)
    return re.compile(rf"(?<![\w'\u2019])(?:{body})(?![\w'\u2019])", re.IGNORECASE)


_VALUE = _words_pattern(VALUE_CUES)
_PRICE = _words_pattern(PRICE_CUES)


# -- the tracked States detection reads -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackedState:
    """A State as detection and the protected index read it: its id, name, type, scope and
    visibility, its aliases and unit, and the entity a possession-style State tracks."""

    id: str
    name: str
    type: StateType
    scope: StateScope
    visibility: StateVisibility
    aliases: tuple[str, ...] = ()
    unit: str | None = None
    entity_id: str | None = None

    @classmethod
    def of(cls, definition: StateDefinition) -> TrackedState:
        assert definition.id is not None  # noqa: S101 - a stored definition has its id
        return cls(
            definition.id,
            definition.name,
            StateType(definition.type),
            StateScope(definition.scope),
            StateVisibility(definition.visibility),
            tuple(definition.aliases),
            definition.unit,
            definition.entity_id,
        )

    @property
    def names(self) -> tuple[str, ...]:
        """What prose calls it: its name (``_`` as spaces) and its aliases."""
        return tuple(dict.fromkeys((self.name.replace("_", " "), *self.aliases)))

    @property
    def words(self) -> tuple[str, ...]:
        """Its names and its unit, case-folded: what a quantity's unit may name it by."""
        found = [*self.names, *([self.unit] if self.unit else [])]
        return tuple(dict.fromkeys(w.strip().casefold() for w in found if w.strip()))

    def subject(self, scope_key: str | None) -> str | None:
        """The ``state_value`` subject of the value under ``scope_key``: the State's id for a
        story State, the character for a per-character one (``None`` without one)."""
        if self.scope is StateScope.STORY:
            return self.id
        return scope_key


def tracked(definitions: Iterable[StateDefinition]) -> tuple[TrackedState, ...]:
    """``definitions`` (stored, so with ids) as detection reads them."""
    return tuple(TrackedState.of(d) for d in definitions if d.id is not None)


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def names(state: TrackedState, word: str) -> bool:
    """Whether ``word`` (a quantity's unit, say) names ``state``: one of its words, singular or
    plural."""
    wanted = _singular(word.strip().casefold())
    return any(_singular(w) == wanted for w in state.words)


# -- values as literals -----------------------------------------------------------------------


def value_literal(value: Value) -> TypedLiteral:
    """A State value as a ``state_value`` literal: a number as an ``int`` (integral) or a
    ``float``, a boolean as a ``bool``, an enum value or text as a ``str``."""
    if isinstance(value, bool):
        return TypedLiteral(kind=LiteralKind.BOOL, value=value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return TypedLiteral(kind=LiteralKind.INT, value=int(value))
        return TypedLiteral(kind=LiteralKind.FLOAT, value=float(value))
    return TypedLiteral(kind=LiteralKind.STR, value=str(value))


def _claimed_number(literal: TypedLiteral) -> Decimal | None:
    if literal.kind not in (LiteralKind.INT, LiteralKind.FLOAT, LiteralKind.QUANTITY):
        return None
    return number(literal.value)


def number_literal(amount: Decimal | int | float) -> TypedLiteral:
    """A detected amount as a ``state_value`` literal (its unit is the State's, dropped)."""
    found = number(amount)
    if found is None:
        raise ValueError("an amount is a finite number")
    return value_literal(found)


def proposition(
    state: TrackedState | str,
    subject: str,
    literal: TypedLiteral | None,
    *,
    polarity: Polarity = Polarity.AFFIRMED,
) -> Proposition:
    """``state_value(subject, state = <id>) = literal`` over open valid-time (see the module
    docstring)."""
    state_id = state if isinstance(state, str) else state.id
    return Proposition(
        subject_id=subject,
        predicate_id=STATE_VALUE,
        literal=literal,
        qualifiers={STATE_QUALIFIER: state_id},
        polarity=polarity,
        valid_time=Interval.of(None, None),
    )


def state_of(p: Proposition) -> str | None:
    """The State id a ``state_value`` proposition names."""
    found = p.qualifiers.get(STATE_QUALIFIER)
    return found if isinstance(found, str) else None


# -- detection --------------------------------------------------------------------------------


def balance_sentence(sentence: str) -> bool:
    """Whether ``sentence`` states a balance (a ``VALUE_CUES`` word) and no price or change
    (``PRICE_CUES``, #148's possession, transfer and consumption verbs)."""
    from epistrel.core.states.effects import change_cue

    if _VALUE.search(sentence) is None:
        return False
    return _PRICE.search(sentence) is None and not change_cue(sentence)


def state_claim(
    states: Sequence[TrackedState],
    unit: str | None,
    amount: Decimal | int | float,
    *,
    subject: str | None,
    scope_keys: frozenset[str],
    sentence: str,
    polarity: Polarity = Polarity.AFFIRMED,
) -> Proposition | None:
    """A detected lone quantity (``amount`` of ``unit``) as a ``state_value`` claim, or
    ``None`` (see the module docstring's detection): exactly one number State names the unit,
    ``sentence`` states a balance, and a per-character State's subject is one of the story's
    characters (``scope_keys``)."""
    if not unit or not balance_sentence(sentence):
        return None
    named = [s for s in states if s.type is StateType.NUMBER and names(s, unit)]
    if len(named) != 1:
        return None
    (state,) = named
    owner = state.subject(subject if subject in scope_keys else None)
    if owner is None:
        return None
    return proposition(state, owner, number_literal(amount), polarity=polarity)


# -- the comparator ---------------------------------------------------------------------------


def matches(owned: OwnedValue, claimed: TypedLiteral) -> bool | None:
    """Whether ``claimed`` is ``owned``'s value (see the module docstring), or ``None`` when it
    cannot be compared (no value, or a literal of another type)."""
    value = owned.proposition.literal
    if value is None:
        return None
    kind = StateType(owned.kind)
    if kind is StateType.NUMBER:
        mine, theirs = _claimed_number(claimed), number(value.value)
        if mine is None or theirs is None:
            return None
        try:
            tolerance = Decimal(str(owned.tolerance))
        except InvalidOperation:  # pragma: no cover - a stored tolerance is a number
            tolerance = Decimal(0)
        return abs(mine - theirs) <= tolerance
    if kind is StateType.BOOLEAN:
        if claimed.kind is not LiteralKind.BOOL:
            return None
        return claimed.value is value.value
    if claimed.kind is not LiteralKind.STR or not isinstance(claimed.value, str):
        return None
    if kind is StateType.ENUM:
        return claimed.value == value.value
    return claimed.value.casefold() == str(value.value).casefold()


def _of(claim: Proposition, owned: Sequence[OwnedValue]) -> OwnedValue | None:
    key = (claim.subject_id, state_of(claim))
    for found in owned:
        if (found.proposition.subject_id, state_of(found.proposition)) == key:
            return found
    return None


def verdict(claim: Proposition, owned: Sequence[OwnedValue]) -> Verdict:
    """A canon holder's verdict on a ``state_value`` claim over the values ``owned`` (see the
    module docstring). Pure."""
    found = _of(claim, owned)
    if found is None:
        return Verdict(VerdictKind.NOVEL)
    if found.unsettled is not None:
        # #207: an advisory provider's value settles nothing either way (the owner's decision,
        # 2026-10-30): an equal claim is not supported, a differing one takes the safe, hedged
        # disposition (#151), never contradiction and repair. #206 adds ``provider_unknown``
        # (an ``Unknown`` pinned value) through the same branch.
        return uncertain(found.unsettled)
    if found.proposition.literal is None or claim.literal is None:
        return uncertain(VerdictReason.UNRESOLVED_ENTITY)
    same = matches(found, claim.literal)
    if same is None:
        return uncertain(VerdictReason.ARGUMENT_TYPE)
    affirmed = claim.polarity is Polarity.AFFIRMED
    if same is affirmed:
        return Verdict(
            VerdictKind.SUPPORTED, evidence=(Evidence(Rule.COVERAGE, owner_ref=found.ref),)
        )
    rule = Rule.FUNCTIONAL_KEY if affirmed else Rule.POLARITY
    return Verdict(VerdictKind.CONTRADICTED, evidence=(Evidence(rule, owner_ref=found.ref),))


def _stated_amount(cause: Proposition) -> Decimal | None:
    if cause.literal is not None:
        return _claimed_number(cause.literal)
    stated = cause.qualifiers.get("quantity")
    if not isinstance(stated, str):
        return None
    head = stated.strip().split(" ", 1)[0]
    try:
        found = Decimal(head)
    except InvalidOperation:
        return None
    return found if found.is_finite() else None


def implied(
    consequence: Proposition, cause: Proposition, owned: Sequence[OwnedValue]
) -> Proposition | None:
    """A declared rule's consequence on ``state_value`` (its object the entity a State tracks)
    as the value ``cause`` states, or ``None`` when no State tracks that entity for the
    consequence's subject or the cause states no amount. Pure."""
    entity = consequence.object_id
    amount = _stated_amount(cause)
    if entity is None or amount is None:
        return None
    for found in owned:
        held = found.proposition
        if found.entity_id != entity:
            continue
        if held.subject_id != consequence.subject_id and held.subject_id != state_of(held):
            continue  # a per-character State of someone else
        state = state_of(held)
        if state is None:
            continue
        return proposition(state, held.subject_id, number_literal(amount))
    return None


# -- the read ---------------------------------------------------------------------------------


async def _characters(conn: AsyncConnection, story_id: str) -> frozenset[str]:
    from epistrel.core.tables import characters

    return frozenset(
        str(c)
        for c in await conn.scalars(
            select(characters.c.id).where(characters.c.story_id == story_id)
        )
    )


def _visible(state: StateDefinition, viewpoint: str) -> bool:
    return viewpoint == OMNISCIENT or state.visibility is StateVisibility.PLAYER_VISIBLE


async def read(
    conn: AsyncConnection,
    story_id: str,
    subjects: Sequence[str] | None,
    through: Position | None,
    viewpoint: str = OMNISCIENT,
) -> list[OwnedValue]:
    """The owner values the lookup reads (see the module docstring): every story State and the
    per-character States of ``subjects`` (``None``: every character), folded through
    ``through``, as ``viewpoint`` may read them, ordered by State and subject."""
    from epistrel.core import states
    from epistrel.core.states import external

    defined = [s for s in await states.definitions(conn, story_id) if _visible(s, viewpoint)]
    if not defined:
        return []
    cast = await _characters(conn, story_id)
    wanted = cast if subjects is None else cast & frozenset(subjects)
    owned = [s for s in defined if s.owner is not StateOwner.EXTERNAL]
    folded = await states.folded_at(conn, story_id, owned, through=through)

    def keys_of(state: StateDefinition) -> list[str | None]:
        return [None] if state.scope is StateScope.STORY else sorted(wanted, key=str)

    # #205: an externally owned State's value is its pinned read, never a fold; one with no
    # pin (not read this turn, or never recorded) has no value here.
    remote = external.external(defined)
    advisory = await _advisory(conn, remote)
    pinned = await external.pinned_values(
        conn,
        story_id,
        remote,
        [(str(s.id), k) for s in remote for k in keys_of(s)],
        through=through,
    )
    found: list[OwnedValue] = []
    for state in defined:
        assert state.id is not None  # noqa: S101 - a stored definition has its id
        is_external = state.owner is StateOwner.EXTERNAL
        for scope_key in keys_of(state):
            value: Value | UndefinedType | None
            if is_external:
                pin = pinned.get((state.id, scope_key))
                if pin is None:
                    continue
                value = pin.value
            else:
                fold = folded.get((state.id, scope_key))
                value = fold.value if fold is not None else state.initial_value
            literal = (
                None if value is None or isinstance(value, UndefinedType) else value_literal(value)
            )
            subject = state.id if scope_key is None else scope_key
            found.append(
                OwnedValue(
                    proposition=proposition(state.id, subject, literal),
                    ref=f"{REF_PREFIX}{state.id}",
                    kind=state.type.value,
                    tolerance=float(state.constraints.tolerance),
                    entity_id=state.entity_id,
                    unsettled=VerdictReason.ADVISORY
                    if is_external and state.provider_id in advisory
                    else None,
                )
            )
    return found


async def _advisory(conn: AsyncConnection, remote: Sequence[StateDefinition]) -> frozenset[str]:
    """The providers among ``remote``'s registered with ``trust_tier = advisory`` (#204's
    ``external_providers``, a Core table: Core never imports ``orchestrator.providers``)."""
    from epistrel.core.tables.providers import external_providers

    named = sorted({str(s.provider_id) for s in remote if s.provider_id is not None})
    if not named:
        return frozenset()
    p = external_providers.c
    found = await conn.scalars(
        select(p.name).where(p.name.in_(named), p.trust_tier == ADVISORY_TIER)
    )
    return frozenset(str(name) for name in found)


async def external_claim(conn: AsyncConnection, story_id: str, claim: Proposition) -> bool:
    """Whether ``claim`` (a ``state_value``) names an externally owned State: a value a
    character knows only as its own belief (#207; ``register_owner_lookup``'s
    ``belief_only``), so ADR-167's canon fallback never answers it."""
    from epistrel.core.tables.states import state_definitions

    state_id = state_of(claim) if claim.predicate_id == STATE_VALUE else None
    if state_id is None:
        return False
    d = state_definitions.c
    owner = await conn.scalar(select(d.owner).where(d.story_id == story_id, d.id == state_id))
    return owner is not None and str(getattr(owner, "value", owner)) == StateOwner.EXTERNAL.value


class _StateLookup:
    """``state_value``'s owner lookup (``core.propositions.registry.OwnerLookup``)."""

    async def read(
        self,
        conn: AsyncConnection,
        story_id: str,
        subjects: Sequence[str] | None,
        through: Position | None,
        viewpoint: str,
    ) -> list[OwnedValue]:
        return await read(conn, story_id, subjects, through, viewpoint)

    def verdict(self, claim: Proposition, owned: Sequence[OwnedValue]) -> Verdict:
        return verdict(claim, owned)

    def implied(
        self, consequence: Proposition, cause: Proposition, owned: Sequence[OwnedValue]
    ) -> Proposition | None:
        return implied(consequence, cause, owned)


STATE_LOOKUP = register_owner_lookup(STATE_VALUE, _StateLookup(), belief_only=external_claim)


# -- hidden States in the protected index -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class HiddenValue:
    """One hidden State's current value under a scope key: the State, the ``state_value``
    proposition a leak of it claims, and its value."""

    state: TrackedState
    proposition: Proposition
    value: Value


async def hidden_values(
    conn: AsyncConnection,
    story_id: str,
    speaker: str | None,
    *,
    through: Position | None = None,
) -> list[HiddenValue]:
    """Every hidden State's current value (through ``through``) that ``speaker`` (a character
    id; ``None``: the narrator) holds no ``state_value`` belief about (see the module
    docstring): the player's character and the narrator hold none."""
    from epistrel.core import states
    from epistrel.core.epistemic import partitions

    hidden = [
        s
        for s in await states.definitions(conn, story_id)
        if s.visibility is StateVisibility.HIDDEN and s.id is not None
    ]
    if not hidden:
        return []
    held: set[tuple[str, str | None]] = set()
    if speaker is not None and not await partitions.is_player(conn, story_id, speaker):
        for belief in await partitions.partition(conn, story_id, speaker):
            p = belief.proposition
            if p.predicate_id == STATE_VALUE:
                held.add((p.subject_id, state_of(p)))
    found: list[HiddenValue] = []
    for owned in await read(conn, story_id, None, through, OMNISCIENT):
        p = owned.proposition
        state_id = state_of(p)
        definition = next((s for s in hidden if s.id == state_id), None)
        if definition is None or p.literal is None or (p.subject_id, state_id) in held:
            continue
        value = from_json(definition.type, p.literal.value)
        if value is None:  # pragma: no cover - a literal holds a value
            continue
        found.append(HiddenValue(TrackedState.of(definition), p, value))
    return found


def value_renderings(value: Value) -> list[str]:
    """The ways a hidden State's value may be written (#148's ``renderings`` of its literal;
    an enum value or text as itself), a single digit included: paired with the State's name, a
    "3" is no common word."""
    from epistrel.core.verify.protected import renderings

    literal = value_literal(value)
    found = renderings(literal)
    if literal.kind in (LiteralKind.INT, LiteralKind.FLOAT):
        found.insert(0, str(literal.value))
    return list(dict.fromkeys(found))


__all__ = [
    "ADVISORY_TIER",
    "PRICE_CUES",
    "REF_PREFIX",
    "STATE_LOOKUP",
    "STATE_QUALIFIER",
    "VALUE_CUES",
    "HiddenValue",
    "TrackedState",
    "balance_sentence",
    "external_claim",
    "hidden_values",
    "implied",
    "matches",
    "names",
    "number_literal",
    "proposition",
    "read",
    "state_claim",
    "state_of",
    "tracked",
    "value_literal",
    "value_renderings",
    "verdict",
]
