"""The pure half of a turn's State effects (FR-TURN-11, FR-STATE-5,6, FR-VERIFY-12; #187): the
dry-run of declared effects, the State changes narrated without one, what each finding asks of
the repair ladder, and the deterministic refusal. Nothing here reads the database or calls a
model; ``orchestrator.structured.effects`` runs it over a turn.

**The dry-run** (``dry_run``). Each declared effect (``core.turns.beats.DeclaredEffect``) is
folded in beat order over the snapshot's values (the State's folded value when the turn
started) plus the turn's earlier effects that passed: a ``delta`` adds, a ``value`` sets. An
effect with neither is ``effect_unresolved`` (FR-STATE-5: a vague amount); one whose result
breaks the State's constraints (``model.violation``) is ``state_constraint`` (FR-STATE-6: the
action was an attempt), and so is a delta on a State with no value yet (``undefined``). A
refused effect changes nothing the later ones see. #188: an effect with a ``source`` (its
beat's asserter) is first authorized (``core.states.authorize``: source, justification,
preconditions, transfer balance, caps): an unpermitted one is ``state_unpermitted`` (the reason
its ``constraint``), repaired like a refused one; one above a cap is ``state_capped`` with the
``amount`` the cap leaves. A ``transfer`` folds its tracked other side too. #189: the action
preconditions (``core.states.preconditions``, given as ``dry_run``'s ``preconditions``) refuse
an effect as ``state_unpermitted`` with ``precondition:<name>``; a sale whose item is named
nowhere is ``effect_unresolved`` with the constraint ``consideration``.

**Narrated changes** (``mentions``). A ``do`` or ``narration`` sentence that names a tracked
State (its name, an alias, its unit, or the name of the entity it tracks) and holds a change
verb of #148's possession, transfer or consumption tables changes that State. A question, a
negation, a conditional or a modal (#148's cues) suppresses the sentence. When the beat
declares an effect on that State the sentence is covered; otherwise it is ``state_undeclared``
(FR-VERIFY-12's ``denied:state_change``) when the sentence names an amount ("hands over 500
gold"), and ``effect_unresolved`` when it names none ("spends most of his gold").

**Findings** (``Finding``) carry their repair tag, the State and the constraint, and the line
the repair ladder hands the render (``constraint_line``): for a character's beat it names the
State and the rule it would break, never a value (FR-STATE-8: a value reaches a character only
through its beliefs); the narrator's names the value too. ``refusal_text`` is the deterministic
rung's answer to a refused effect: the attempt narrated as failing, with nothing changed.

Pure: no I/O, no story text in any log (this module logs nothing).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cache
from typing import Any, Final, Literal

from epistrel.core.enums import StateType, StateVisibility
from epistrel.core.repair.rungs import sentences
from epistrel.core.states import authorize as permission
from epistrel.core.states.authorize import (
    Authorized,
    Capped,
    Counterparty,
    Evidence,
    Source,
    StateEffect,
    Unpermitted,
)
from epistrel.core.states.model import (
    StateDefinition,
    Undefined,
    UndefinedType,
    Value,
    number,
    to_json,
    violation,
)
from epistrel.core.turns.beats import DeclaredEffect

STATE_CONSTRAINT: Final = "state_constraint"
"""A declared effect breaks its State's constraints (FR-STATE-6): repaired in character."""
EFFECT_UNRESOLVED: Final = "effect_unresolved"
"""A State change with no concrete amount (FR-STATE-5): repaired to one, or removed."""
STATE_UNDECLARED: Final = "state_undeclared"
"""A narrated State change with no validated effect (FR-VERIFY-12, #144's ``denied:state_change``;
#190's ``undeclared`` is the same finding): declare it, or remove the sentence."""
STATE_UNPERMITTED: Final = "state_unpermitted"
"""A declared effect its State does not authorize (FR-STATE-10, #188: a source, justification,
transfer or cap refusal; ``constraint`` the reason): unpermitted novelty, repaired."""
STATE_CAPPED: Final = "state_capped"
"""A declared effect larger than its State's caps allow (FR-STATE-10, #188): repaired down to
the cap deterministically (``amount``)."""
TIME_STOP: Final = "time_stop"
"""#196 (FR-CLOCK-16): a beat placed after a due scheduled event's stop: regenerated to end at
or before the cutoff (the repair reason ``time_stop``); its deterministic rung is the fallback
line, never the beat as it was."""
UNPROMPTED_SKIP: Final = "unprompted_skip"
"""#199 (FR-CLOCK-8): a model-initiated skip that narrates no established activity, exceeds
``max_unprompted_skip_minutes`` or meets a cap of 0: its advance is removed and the beat
repaired to stay in the present (the repair reason ``unprompted_skip``; its deterministic rung
removes the skip's sentence)."""
UNESTIMATED_SKIP: Final = "unestimated_skip"
"""#199 (NFR-PERF-7 (5)): an undeclared skip the turn could not estimate (no budget, no answer,
a confidence under ``core.world_clock.skips.MIN_CONFIDENCE``): its sentence is removed
deterministically, no model repair and no guessed duration (degradation
``unestimated_skip_removed``)."""
INVALID_EFFECT = "invalid_effect"
"""The constraint of a repaired render's effect that names no State it may change."""
UNDEFINED = "undefined"
"""The constraint of a delta on a State with no value yet."""
CONSIDERATION = "consideration"
"""#189: the constraint of an ``effect_unresolved`` sale whose item is named nowhere."""
PRECONDITION_PREFIX = "precondition:"
"""#189: the prefix of an unmet action precondition's reason (``core.states.preconditions``)."""
_PRECONDITION_RULES: dict[str, str] = {
    "consent": "the other side has not agreed to it",
    "availability": "what it pays for is not there to be had",
    "co_location": "the two sides are not in the same place",
    "ownership": "what it pays for is not the seller's to sell",
}
"""How a repair line and a refusal name an unmet precondition (no name, no value)."""
STATE_UNNARRATED: Final = "state_unnarrated"
"""#190 (FR-STATE-13): a declared effect on a player-visible State that nothing in the turn
narrates (``core.states.completeness``): narrated by the render, else by one appended line."""
AMOUNT: Final = "amount"
"""#190: the constraint of a ``state_undeclared`` finding whose beat declares an effect on the
State that does not cover the narrated change (another amount, or the other direction)."""
EXTERNAL_OUTCOME_UNCONFIRMED: Final = "external_outcome_unconfirmed"
"""#209 (FR-EXT-9,15): a beat asserts the outcome of a change the turn asks an external value's
owner to apply ("the purchase went through", "your balance is now 250") while no outcome is
known: repaired to the attempt ("you hand over the coins"); the effect stays requested."""
Tag = Literal[
    "state_constraint",
    "effect_unresolved",
    "state_undeclared",
    "state_unpermitted",
    "state_capped",
    "state_unnarrated",
    "time_stop",
    "external_outcome_unconfirmed",
    "unprompted_skip",
    "unestimated_skip",
]
TAGS: frozenset[str] = frozenset(
    {
        STATE_CONSTRAINT,
        EFFECT_UNRESOLVED,
        STATE_UNDECLARED,
        STATE_UNPERMITTED,
        STATE_CAPPED,
        STATE_UNNARRATED,
        TIME_STOP,
        EXTERNAL_OUTCOME_UNCONFIRMED,
        UNPROMPTED_SKIP,
        UNESTIMATED_SKIP,
    }
)
PROSE_TAGS: frozenset[str] = frozenset({STATE_UNNARRATED, EXTERNAL_OUTCOME_UNCONFIRMED})
"""The tags whose repair rewrites the prose only: the beat keeps its declared effects."""
REFUSED_TAGS: frozenset[str] = frozenset({STATE_CONSTRAINT, STATE_UNPERMITTED})
"""The tags of a refused effect: the deterministic rung narrates the attempt as failing."""

NARRATED_KINDS: frozenset[str] = frozenset({"do", "narration"})
"""The beats whose prose is read for an undeclared State change."""
_LOOKBACK = 4
"""How many words before a State's term an amount may span ("two hundred and six gold")."""

Key = tuple[str, str | None]
"""A State's value in a turn: ``(name, scope key)``."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One effect or sentence the repair ladder must fix (see the module docstring)."""

    tag: Tag
    beat: int
    state: str
    state_id: str | None = None
    scope_key: str | None = None
    constraint: str | None = None
    """The constraint a refused effect breaks (``state_constraint``)."""
    effect: int | None = None
    """The index of the beat's effect it is about (``None``: a narrated sentence)."""
    span: tuple[int, int] | None = None
    """The sentence of the beat it is about, when one names the State."""
    current: Value | UndefinedType = Undefined
    attempted: Value | UndefinedType = Undefined
    line: str = field(default="", compare=False)
    """The repair constraint the ladder hands the render (``constraint_line``)."""
    refusal: str = field(default="", compare=False)
    """The deterministic rung's text for a refused effect (``refusal_text``)."""
    amount: Decimal | None = None
    """``state_capped``: the signed delta the cap leaves (#188)."""
    claim_id: str | None = field(default=None, compare=False)
    """#190: the detected claim a ``state_undeclared`` finding merges (``completeness.diff``)."""
    narration: str = field(default="", compare=False)
    """#190: a ``state_unnarrated`` effect's line, which the deterministic rung appends."""

    def payload(self) -> dict[str, object]:
        """The verdict payload's part: codes and ids only."""
        return {
            "tag": self.tag,
            "state_id": self.state_id,
            "constraint": self.constraint,
            "effect": self.effect,
        }


@dataclass(frozen=True, slots=True)
class Passed:
    """A declared effect the dry-run passed: the value it found and the one it leaves."""

    beat: int
    effect: int
    key: Key
    old: Value | UndefinedType
    new: Value | UndefinedType


@dataclass(frozen=True, slots=True)
class Planned:
    """One declared effect in the turn's order: its beat, index, the beat's actor (a
    character-scoped State defaults to it) and the effect; ``source`` who asserts it (#188:
    ``None`` skips ``authorize``)."""

    beat: int
    index: int
    actor: str | None
    effect: DeclaredEffect
    source: Source | None = None


def scope_of(effect: DeclaredEffect, definition: StateDefinition, actor: str | None) -> str | None:
    """The effect's scope key: its own, else the acting character's for a character-scoped
    State (``None`` for the story's)."""
    if definition.scope.value == "story":
        return None
    return effect.scope_key if effect.scope_key is not None else actor


def _typed(
    definition: StateDefinition, effect: DeclaredEffect
) -> tuple[Value | None, Decimal | None]:
    if effect.delta is not None:
        return None, number(effect.delta)
    raw = effect.value
    if definition.type is StateType.NUMBER:
        return number(raw), None
    return raw, None


def effect_of(
    effect: DeclaredEffect, definition: StateDefinition, scope_key: str | None
) -> StateEffect:
    """``effect`` as ``core.states.authorize`` takes it (#188)."""
    value, delta = _typed(definition, effect)
    other = None
    if effect.counterparty is not None:
        name, other_scope, other_delta = effect.counterparty
        other = Counterparty(
            name, other_scope, None if other_delta is None else number(other_delta)
        )
    return StateEffect(
        state=definition.name,
        scope_key=scope_key,
        kind=effect.kind,
        delta=delta,
        value=value,
        counterparty=other,
        justification=effect.justification,
        justified_by=effect.justified_by,
        consideration=effect.consideration,
    )


def _current(
    definition: StateDefinition,
    key: Key,
    running: Mapping[Key, Value | UndefinedType],
    values: Mapping[Key, Value | UndefinedType],
) -> Value | UndefinedType:
    if key in running:
        return running[key]
    if key in values:
        return values[key]
    return Undefined if definition.initial_value is None else definition.initial_value


def dry_run(
    definitions: Mapping[str, StateDefinition],
    values: Mapping[Key, Value | UndefinedType],
    planned: Sequence[Planned],
    *,
    evidence: Mapping[int, Evidence] | None = None,
    preconditions: Any = None,
) -> tuple[list[Passed], list[Finding]]:
    """Fold ``planned`` (in order) over ``values`` (each key's value at the turn's start; a key
    not given is its State's initial value): what passes and what is refused (see the module
    docstring). #188: an effect with a ``source`` is authorized first (``core.states.authorize``,
    ``evidence`` the turn's beats a justification may cite): refused as ``state_unpermitted``,
    or ``state_capped`` with the amount the caps leave; a transfer's tracked other side is
    folded and bounded too. #189: ``preconditions`` (``core.states.preconditions.Context``) is
    what the action-precondition hook reads; a sale whose item is named nowhere is
    ``effect_unresolved`` (constraint ``consideration``)."""
    running: dict[Key, Value | UndefinedType] = {}
    passed: list[Passed] = []
    found: list[Finding] = []
    so_far: list[Authorized] = []
    for p in planned:
        definition = definitions.get(p.effect.state)
        if definition is None:
            found.append(
                Finding(
                    STATE_CONSTRAINT,
                    p.beat,
                    p.effect.state,
                    constraint=INVALID_EFFECT,
                    effect=p.index,
                )
            )
            continue
        key = (definition.name, scope_of(p.effect, definition, p.actor))
        current = _current(definition, key, running, values)
        refused = _refused(definition, p, key, current)
        if refused is not None:
            found.append(refused)
            continue
        value, delta = _typed(definition, p.effect)
        typed = replace(effect_of(p.effect, definition, key[1]), beat=p.beat)
        if p.source is not None:
            decision = permission.authorize(
                typed,
                p.source,
                so_far,
                definition=definition,
                definitions=definitions,
                current=current,
                evidence=evidence,
                context=preconditions,
            )
            if isinstance(decision, Unpermitted) and decision.reason == EFFECT_UNRESOLVED:
                # #189: a sale whose item is named nowhere; rung 1 asks the render to name it.
                found.append(
                    replace(
                        _finding(definition, p, key, CONSIDERATION, current, Undefined),
                        tag=EFFECT_UNRESOLVED,
                    )
                )
                continue
            if isinstance(decision, Unpermitted):
                found.append(
                    _finding(
                        definition, p, key, decision.reason, current, Undefined, STATE_UNPERMITTED
                    )
                )
                continue
            if isinstance(decision, Capped):
                cut: Value | UndefinedType = (
                    current + decision.amount if isinstance(current, Decimal) else Undefined
                )
                found.append(
                    replace(
                        _finding(definition, p, key, permission.CAP, current, cut, STATE_CAPPED),
                        amount=decision.amount,
                    )
                )
                continue
        attempted: Value | UndefinedType
        if delta is not None:
            if isinstance(current, UndefinedType) or not isinstance(current, Decimal):
                found.append(_finding(definition, p, key, UNDEFINED, current, Undefined))
                continue
            attempted = current + delta
        else:
            assert value is not None  # noqa: S101 - an unresolved effect was refused above
            attempted = value
        broken = violation(definition, attempted)
        if broken is not None:
            found.append(_finding(definition, p, key, broken, current, attempted))
            continue
        other = _other_side(definitions, typed, delta, running, values)
        if isinstance(other, str):
            found.append(_finding(definition, p, key, other, current, attempted))
            continue
        running[key] = attempted
        passed.append(Passed(p.beat, p.index, key, current, attempted))
        if other is not None:
            other_key, other_old, other_new = other
            running[other_key] = other_new
            passed.append(Passed(p.beat, p.index, other_key, other_old, other_new))
        so_far += permission.authorized(typed, permission.change_of(typed, current), definitions)
    return passed, found


def _other_side(
    definitions: Mapping[str, StateDefinition],
    effect: StateEffect,
    delta: Decimal | None,
    running: Mapping[Key, Value | UndefinedType],
    values: Mapping[Key, Value | UndefinedType],
) -> tuple[Key, Value | UndefinedType, Value] | str | None:
    """A transfer's tracked other side folded: its key, old and new value; the constraint it
    would break; ``None`` when there is none (a change, or an untracked or external side)."""
    other = effect.counterparty
    if effect.kind != permission.TRANSFER or other is None or delta is None:
        return None
    if not permission.tracked(definitions, other.state):
        return None
    definition = definitions[other.state]
    if definition.scope.value == "character" and other.scope_key is None:
        return INVALID_EFFECT
    key = (definition.name, None if definition.scope.value == "story" else other.scope_key)
    current = _current(definition, key, running, values)
    moved = other.delta if other.delta is not None else -delta
    if not isinstance(current, Decimal):
        return UNDEFINED
    attempted = current + moved
    broken = violation(definition, attempted)
    return broken if broken is not None else (key, current, attempted)


def _refused(
    definition: StateDefinition, p: Planned, key: Key, current: Value | UndefinedType
) -> Finding | None:
    if p.effect.unresolved:
        return Finding(
            EFFECT_UNRESOLVED,
            p.beat,
            definition.name,
            definition.id,
            key[1],
            effect=p.index,
            current=current,
        )
    if definition.scope.value == "character" and key[1] is None:
        return _finding(definition, p, key, INVALID_EFFECT, current, Undefined)
    value, delta = _typed(definition, p.effect)
    if (p.effect.delta is not None and delta is None) or (
        p.effect.value is not None and value is None
    ):
        return _finding(definition, p, key, INVALID_EFFECT, current, Undefined)
    return None


def _finding(
    definition: StateDefinition,
    p: Planned,
    key: Key,
    constraint: str,
    current: Value | UndefinedType,
    attempted: Value | UndefinedType,
    tag: Tag = STATE_CONSTRAINT,
) -> Finding:
    return Finding(
        tag,
        p.beat,
        definition.name,
        definition.id,
        key[1],
        constraint=constraint,
        effect=p.index,
        current=current,
        attempted=attempted,
    )


# -- narrated changes ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Terms:
    """How prose names one State: its name (underscores read as spaces), aliases, unit and the
    name of the entity it tracks, case-folded."""

    state: str
    words: tuple[str, ...]


def terms_of(definition: StateDefinition, entity_names: Mapping[str, str] | None = None) -> Terms:
    """``definition``'s terms (see ``Terms``)."""
    found = [definition.name.replace("_", " "), *definition.aliases]
    if definition.unit:
        found.append(definition.unit)
    if definition.entity_id is not None and entity_names:
        name = entity_names.get(definition.entity_id)
        if name:
            found.append(name)
    return Terms(definition.name, tuple(dict.fromkeys(w.casefold() for w in found if w.strip())))


def _alternation(words: Iterable[str]) -> str:
    ordered = sorted(set(words), key=lambda w: (-len(w), w))
    return "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in ordered)


def _word_pattern(words: Iterable[str]) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w'\u2019])(?:{_alternation(words)})(?![\w'\u2019])", re.IGNORECASE)


@cache
def _change_verbs() -> re.Pattern[str]:
    from epistrel.core.verify.detect import tables

    forms = [
        f
        for table in tables().verbs
        if table.category in ("possession", "transfer", "consumption")
        for lemma in table.lemmas
        for f in lemma.forms
    ]
    return _word_pattern(forms)


@cache
def _suppressors() -> re.Pattern[str]:
    from epistrel.core.verify.detect import tables

    cues = tables().cues
    return _word_pattern([*cues["negation"], *cues["conditionals"], *cues["modals"]])


NOUN_CUES: tuple[str, ...] = (
    "a",
    "an",
    "the",
    "this",
    "his",
    "her",
    "their",
    "my",
    "your",
    "our",
    "its",
    "final",
    "best",
    "last",
    "first",
)
"""#190: a word just before a change verb's form that makes it a noun ("final offer", "her
pay"): no change."""
_NOUN_BEFORE = re.compile(rf"(?<![\w'\u2019])(?:{'|'.join(NOUN_CUES)})\s+$", re.IGNORECASE)


def verb_hit(pattern: re.Pattern[str], text: str) -> bool:
    """Whether ``pattern`` (a verb's forms) matches ``text`` as a verb: a match right after a
    determiner or an adjective of ``NOUN_CUES`` is the noun ("a final offer"), not a change."""
    return any(_NOUN_BEFORE.search(text[: m.start()]) is None for m in pattern.finditer(text))


def change_cue(text: str) -> bool:
    """Whether ``text`` holds a change verb (possession, transfer or consumption), as a verb."""
    return verb_hit(_change_verbs(), text)


def suppressed(sentence: str) -> bool:
    """Whether ``sentence`` holds a negation, a conditional or a modal (#148's cues)."""
    return _suppressors().search(sentence) is not None


@dataclass(frozen=True, slots=True)
class Mention:
    """A sentence that changes a State: the State, the sentence's span and the amount it names
    (``None``: none)."""

    state: str
    span: tuple[int, int]
    amount: Decimal | None


def amount_before(sentence: str, at: int) -> Decimal | None:
    """The number just before offset ``at`` of ``sentence`` ("two hundred and six gold"),
    or ``None``."""
    from epistrel.core.verify.detect import parse_number

    before = re.findall(r"[\w,.'-]+", sentence[:at])[-_LOOKBACK:]
    for size in range(len(before), 0, -1):
        found = parse_number(" ".join(before[-size:]).strip(",."))
        if found is not None:
            return Decimal(str(found))
    return None


def mentions(text: str, terms: Sequence[Terms]) -> list[Mention]:
    """The sentences of ``text`` that change one of the States ``terms`` names (see the module
    docstring), in order."""
    found: list[Mention] = []
    patterns = [(t, _word_pattern(t.words)) for t in terms if t.words]
    for start, end in sentences(text):
        sentence = text[start:end]
        if sentence.rstrip().endswith("?") or suppressed(sentence):
            continue
        if not change_cue(sentence):
            continue
        for term, pattern in patterns:
            hit = pattern.search(sentence)
            if hit is not None:
                found.append(
                    Mention(term.state, (start, end), amount_before(sentence, hit.start()))
                )
    return found


def narrated(
    beat: int,
    kind: str,
    text: str,
    effects: Sequence[DeclaredEffect],
    terms: Sequence[Terms],
    definitions: Mapping[str, StateDefinition],
) -> list[Finding]:
    """The undeclared State changes ``text`` (beat ``beat`` of ``kind``) narrates: one finding
    per State it changes that none of ``effects`` covers (see the module docstring)."""
    if kind not in NARRATED_KINDS:
        return []
    covered = {e.state for e in effects}
    found: dict[str, Finding] = {}
    for m in mentions(text, terms):
        if m.state in covered or m.state in found:
            continue
        definition = definitions.get(m.state)
        # #190: a boolean, enum or text State changes with no amount ("pockets the key").
        amountless = definition is not None and definition.type is not StateType.NUMBER
        found[m.state] = Finding(
            STATE_UNDECLARED if m.amount is not None or amountless else EFFECT_UNRESOLVED,
            beat,
            m.state,
            None if definition is None else definition.id,
            span=m.span,
        )
    return list(found.values())


def span_of(text: str, state: str, terms: Sequence[Terms]) -> tuple[int, int] | None:
    """The first sentence of ``text`` naming ``state`` (whatever its verb), or ``None``."""
    wanted = [t for t in terms if t.state == state and t.words]
    if not wanted:
        return None
    pattern = _word_pattern(wanted[0].words)
    for start, end in sentences(text):
        if pattern.search(text[start:end]):
            return start, end
    return None


# -- an external change's outcome (#209) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pending:
    """A change the turn asks an external value's owner to apply (FR-EXT-9): the State, its
    scope key, the words prose names it by, its value as pinned for the turn and the value the
    change would leave (``Undefined``: not known)."""

    state: str
    state_id: str | None
    scope_key: str | None
    words: tuple[str, ...]
    current: Value | UndefinedType = Undefined
    leaves: Value | UndefinedType = Undefined


OUTCOME_NOUNS: tuple[str, ...] = (
    "purchase",
    "payment",
    "transaction",
    "sale",
    "transfer",
    "trade",
    "deal",
    "order",
    "balance",
    "account",
    "withdrawal",
    "deposit",
)
"""What a sentence names when it speaks of an exchange rather than of the value itself."""
OUTCOME_VERBS: tuple[str, ...] = (
    "succeeded",
    "succeeds",
    "successful",
    "successfully",
    "went through",
    "goes through",
    "gone through",
    "completed",
    "completes",
    "is complete",
    "confirmed",
    "approved",
    "processed",
    "cleared",
    "deducted",
    "debited",
    "credited",
    "is done",
)
"""The words of an exchange's outcome ("the purchase went through")."""
NOW_CUES: tuple[str, ...] = ("now", "left", "remaining", "remains", "down to", "up to")
"""The words of a value's state after a change ("your balance is now 250")."""
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def _numbers(sentence: str) -> list[Decimal]:
    found: list[Decimal] = []
    for hit in _NUMBER.finditer(sentence):
        try:
            found.append(Decimal(hit.group(0).replace(",", "")))
        except ArithmeticError:  # pragma: no cover - the pattern is a decimal literal
            continue
    return found


def _asserts_outcome(sentence: str, request: Pending) -> bool:
    """Whether ``sentence`` says ``request``'s change happened (see ``unconfirmed_outcomes``)."""
    names = _word_pattern(request.words).search(sentence) is not None if request.words else False
    exchange = _word_pattern(OUTCOME_NOUNS).search(sentence) is not None
    if (names or exchange) and _word_pattern(OUTCOME_VERBS).search(sentence) is not None:
        return True
    if not (names or exchange):
        return False
    amounts = [n for n in _numbers(sentence) if n != request.current]
    if not amounts:
        return False
    if isinstance(request.leaves, Decimal) and request.leaves in amounts:
        return True
    return _word_pattern(NOW_CUES).search(sentence) is not None


OUTCOME_KINDS: frozenset[str] = frozenset({"say", "do", "narration"})
"""The beats whose prose is read for an external change's outcome."""


def outcome_spans(text: str, pending: Sequence[Pending]) -> list[tuple[Pending, tuple[int, int]]]:
    """The sentences of ``text`` asserting the outcome of one of ``pending``'s changes, each with
    the change it is about: a sentence naming the State (or an exchange: a purchase, a payment,
    the balance) with an outcome word ("went through", "succeeded", "confirmed"), or with an
    amount other than the pinned value that is the value the change would leave, or that a
    "now"/"left" cue gives as the value after it. A question, a negation, a conditional or a
    modal (#148's cues) suppresses the sentence; so it never fires on an attempt ("you hand over
    the coins") or a hope ("if the payment goes through")."""
    found: list[tuple[Pending, tuple[int, int]]] = []
    if not pending:
        return found
    for start, end in sentences(text):
        sentence = text[start:end]
        if sentence.rstrip().endswith("?") or suppressed(sentence):
            continue
        named = [
            p for p in pending if p.words and _word_pattern(p.words).search(sentence) is not None
        ]
        for request in named or list(pending):
            if _asserts_outcome(sentence, request):
                found.append((request, (start, end)))
                break
    return found


def unconfirmed_outcomes(
    beat: int, kind: str, text: str, pending: Sequence[Pending]
) -> list[Finding]:
    """#209 (FR-EXT-15): one ``external_outcome_unconfirmed`` finding per sentence of ``text``
    (beat ``beat`` of ``kind``) that asserts a requested change's outcome (``outcome_spans``):
    no outcome is known while the turn is generated, so the beat may narrate the attempt only."""
    if kind not in OUTCOME_KINDS:
        return []
    return [
        Finding(
            EXTERNAL_OUTCOME_UNCONFIRMED,
            beat,
            request.state,
            request.state_id,
            request.scope_key,
            span=span,
            current=request.current,
            attempted=request.leaves,
        )
        for request, span in outcome_spans(text, pending)
    ]


def attempt_text(kind: str, actor: str | None, definition: StateDefinition | None) -> str:
    """The deterministic rung's text for a beat whose every sentence asserted an unconfirmed
    external outcome: the exchange narrated as an attempt, nothing said about how it ends."""
    name = "it" if definition is None else f"the {label(definition)}"
    if kind == "say":
        return "Here, take it."
    if kind == "do" and actor:
        return f"{actor} offers {name}."
    return f"{name[0].upper()}{name[1:]} is offered."


def pending_line(payload: Mapping[str, Any], holder: str | None = None) -> str:
    """One ``pending_outcomes`` line of the narrator's inputs (#209): a change asked of the game
    (a ``requested_effect`` event's payload) whose outcome it has not reported, with whose value
    it is (``holder``, a character's name) and why."""
    name = str(payload.get("state", "")).replace("_", " ")
    reason = str(payload.get("reason", ""))
    if payload.get("kind") == "time_advance":
        change = f"the clock forward by {payload.get('minutes')} minutes"
    elif payload.get("delta") is not None:
        delta = number(payload["delta"])
        sign = "+" if isinstance(delta, Decimal) and delta > 0 else ""
        change = f"{name} {sign}{payload['delta']}"
    else:
        change = f"{name} set to {payload.get('value')}"
    held = f" ({holder}'s)" if holder else ""
    return f"{change}{held}: {reason}" if reason else f"{change}{held}"


# -- what a finding asks -----------------------------------------------------------------------


def label(definition: StateDefinition) -> str:
    """How a line names the State: its name, underscores read as spaces."""
    return definition.name.replace("_", " ")


def _rule(definition: StateDefinition, constraint: str | None) -> str:
    c = definition.constraints
    name = label(definition)
    if constraint in ("non_negative", UNDEFINED) or (constraint == "min" and c.min == 0):
        return f"there is not enough {name}"
    if constraint == "min" and c.min is not None:
        return f"{name} cannot go below {to_json(c.min)}"
    if constraint == "max" and c.max is not None:
        return f"{name} cannot go above {to_json(c.max)}"
    if constraint == "allowed_values":
        return f"{name} cannot become that"
    if constraint == INVALID_EFFECT:
        return f"{name} cannot change that way"
    if constraint in (permission.SOURCE, permission.REACTIVE_ASSERTER):
        return f"this cannot change {name}"
    if constraint == permission.COUNTERPARTY:
        return f"{name} cannot move to or from there"
    if constraint == permission.WORLD_GAIN:
        return f"{name} cannot simply appear: only the world grants it"
    if constraint == permission.JUSTIFICATION:
        return f"nothing in this turn justifies that change to {name}"
    if constraint == permission.UNBALANCED:
        return f"both sides of a {name} transfer must match"
    if constraint == permission.CAP:
        return f"{name} cannot change that much at once"
    if constraint is not None and constraint.startswith(PRECONDITION_PREFIX):
        rule = _PRECONDITION_RULES.get(constraint[len(PRECONDITION_PREFIX) :])
        if rule is not None:
            return rule
    return f"{name} cannot change like that"


def _shown(value: Value | UndefinedType, definition: StateDefinition) -> str:
    if isinstance(value, UndefinedType):
        return "unset"
    shown = str(to_json(value))
    return f"{shown} {definition.unit}" if definition.unit else shown


def constraint_line(finding: Finding, definition: StateDefinition | None, *, narrator: bool) -> str:
    """The repair constraint of ``finding`` (see the module docstring)."""
    name = finding.state if definition is None else label(definition)
    if finding.tag == STATE_CAPPED and finding.amount is not None:
        return (
            f"{name} can change by at most {to_json(abs(finding.amount))} here: declare that "
            "amount and narrate no more than it."
        )
    if finding.tag in REFUSED_TAGS:
        rule = name if definition is None else _rule(definition, finding.constraint)
        known = (
            f" ({finding.state} is {_shown(finding.current, definition)})"
            if narrator and definition is not None
            else ""
        )
        justify = ""
        if finding.constraint == permission.JUSTIFICATION and definition is not None:
            labels = [
                *permission.authorization_of(definition).increase,
                *permission.authorization_of(definition).decrease,
            ]
            named = f" (one of: {', '.join(dict.fromkeys(labels))})" if labels else ""
            justify = (
                f" Or declare the event that justifies it: `justification`{named} and "
                "`justified_by`, the number of the beat that narrates it."
            )
        return (
            f"The action is only an attempt and it fails: {rule}{known}. Narrate it falling "
            "short in character (someone says so, or it simply does not happen), and declare "
            f"no effect on it, or a smaller one.{justify}"
        )
    if finding.tag == EFFECT_UNRESOLVED and finding.constraint == CONSIDERATION:
        return (
            f"Name what this {name} pays for: declare the effect's `consideration` (the item "
            "the payment buys), or narrate the exchange of the item in the same beat, or remove "
            "the payment."
        )
    if finding.tag == EXTERNAL_OUTCOME_UNCONFIRMED:
        return (
            f"The game decides whether this change to {name} happens, and it has not said yet: "
            "narrate only the attempt (handing it over, asking for it), never that it went "
            f"through or what {name} is now. Keep the declared effect."
        )
    if finding.tag == STATE_UNNARRATED:
        return (
            f"This beat changes {name}, but nothing in the turn says so: narrate the change "
            "in this beat (say what happens to it), or declare no effect on it."
        )
    if finding.tag == STATE_UNDECLARED and finding.constraint == AMOUNT:
        return (
            f"This beat narrates a change to {name} that its declared effect does not match: "
            "make the effect's amount and direction match what the beat says happens, or "
            "remove the sentence that narrates it."
        )
    if finding.tag == EFFECT_UNRESOLVED:
        return (
            f"Say exactly how much {name} changes: declare an effect with a concrete delta or "
            "value, or remove the sentence that changes it."
        )
    return (
        f"This beat changes {name}: declare the change as an effect (with its amount and a "
        "reason), or remove the sentence that changes it."
    )


def refusal_text(
    kind: str, actor: str | None, definition: StateDefinition | None, constraint: str | None
) -> str:
    """The deterministic rung's text for a refused effect: the attempt narrated as failing
    (nothing changes)."""
    rule = "it cannot be done" if definition is None else _rule(definition, constraint)
    if kind == "say":
        return f"That won't work: {rule}."
    if kind == "do" and actor:
        return f"{actor} tries, but it cannot be done: {rule}."
    return f"The attempt fails: {rule}."


def visible_to_characters(definition: StateDefinition) -> bool:
    return definition.visibility is StateVisibility.PLAYER_VISIBLE


# -- the render prompts' ``## Tracked values`` -----------------------------------------------------

ANY_CHARACTER = "any_character"
"""``authorization.sources`` entry: every character's render may change the State."""
CHARACTER_SOURCE = "character:"
"""``authorization.sources`` entry prefix: ``character:<id>``'s render may change it."""


def may_declare(definition: StateDefinition, character_id: str) -> bool:
    """Whether ``character_id``'s render is told it may change ``definition`` (owner decision
    2026-10-30): a player-visible State whose ``authorization.sources`` admit
    ``character:<id>`` (``any_character``, or the default sources; #188's
    ``core.states.authorize`` enforces them)."""
    if not visible_to_characters(definition):
        return False
    return permission.source_permitted(definition, f"{CHARACTER_SOURCE}{character_id}")


def _shape(definition: StateDefinition) -> list[str]:
    parts = [
        definition.type.value,
        "one per character" if definition.scope.value == "character" else "the story's",
    ]
    if definition.unit:
        parts.append(f"unit {definition.unit}")
    if definition.aliases:
        parts.append("also called " + ", ".join(definition.aliases))
    if definition.constraints.allowed_values:
        parts.append("one of " + ", ".join(definition.constraints.allowed_values))
    return parts


def character_line(definition: StateDefinition) -> str:
    """A character render's line for a State it may change: its name, type, scope, unit,
    aliases and allowed values; never its value."""
    return f"{definition.name} ({'; '.join(_shape(definition))})"


def _bounds(definition: StateDefinition) -> list[str]:
    c = definition.constraints
    found = []
    if c.non_negative:
        found.append("never below 0")
    if c.min is not None:
        found.append(f"at least {to_json(c.min)}")
    if c.max is not None:
        found.append(f"at most {to_json(c.max)}")
    return found


EXTERNAL_VALUE = "read from a provider: see its block under External data"
"""#208: what an externally owned State's narrator line shows instead of its value."""


def narrator_line(
    definition: StateDefinition,
    values: Mapping[str | None, Value | UndefinedType],
    names: Mapping[str, str],
    *,
    external: bool = False,
) -> str:
    """The narrator's line for a State: its shape, constraints, visibility and its value at the
    turn's start (per character, by name, for a character-scoped one). An ``external`` one's
    value is not shown: the line points at its data block (#208)."""
    parts = _shape(definition) + _bounds(definition)
    if definition.visibility is StateVisibility.HIDDEN:
        parts.append("hidden from the characters")

    def shown_value(v: Value | UndefinedType) -> str:
        return EXTERNAL_VALUE if external else _shown(v, definition)

    if definition.scope.value == "character":
        shown = [
            f"{names.get(k, k)} (id {k}) {shown_value(v)}"
            for k, v in sorted(values.items(), key=lambda kv: names.get(kv[0] or "", "") or "")
            if k is not None
        ]
        now = "; ".join(shown) if shown else _shown(_initial(definition), definition) + " each"
    else:
        now = shown_value(values.get(None, _initial(definition)))
    return f"{definition.name} ({'; '.join(parts)}): {now}"


def _initial(definition: StateDefinition) -> Value | UndefinedType:
    return Undefined if definition.initial_value is None else definition.initial_value


def syntax(definitions: Sequence[StateDefinition], *, character: bool) -> str:
    """How to declare an effect, with one example over the first State listed."""
    first = definitions[0] if definitions else None
    example = '{"kind": "change", "state": "gold", "delta": -5, "reason": "paid for a meal"}'
    if first is not None:
        amount: str
        if first.type is StateType.NUMBER:
            amount = '"delta": -1'
        elif first.type is StateType.BOOLEAN:
            amount = '"value": true'
        elif first.type is StateType.ENUM and first.constraints.allowed_values:
            amount = f'"value": "{first.constraints.allowed_values[0]}"'
        else:
            amount = '"value": "new text"'
        scope = ', "scope_key": "<character id>"' if first.scope.value == "character" else ""
        example = (
            f'{{"kind": "change", "state": "{first.name}"{scope}, {amount}, '
            '"reason": "why it changes"}'
        )
    whose = (
        "A per-character value is your own unless you name another character's id in `scope_key`."
        if character
        else "A per-character value names the character's id in `scope_key`."
    )
    return (
        "When a beat changes one of these values, declare it on that beat in `effects`: "
        '`{"kind": "change", "state": <name>, "delta": <amount> or "value": <new value>, '
        f'"reason": <why>}}`. {whose} For example: `{example}`. Never change a value only in '
        "prose. An action the value cannot cover is an attempt that fails: narrate it failing "
        "and declare nothing."
    )


__all__ = [
    "AMOUNT",
    "ANY_CHARACTER",
    "CHARACTER_SOURCE",
    "EFFECT_UNRESOLVED",
    "EXTERNAL_OUTCOME_UNCONFIRMED",
    "EXTERNAL_VALUE",
    "INVALID_EFFECT",
    "NARRATED_KINDS",
    "NOW_CUES",
    "OUTCOME_KINDS",
    "OUTCOME_NOUNS",
    "OUTCOME_VERBS",
    "PROSE_TAGS",
    "REFUSED_TAGS",
    "STATE_CAPPED",
    "STATE_CONSTRAINT",
    "STATE_UNDECLARED",
    "STATE_UNNARRATED",
    "STATE_UNPERMITTED",
    "TAGS",
    "TIME_STOP",
    "UNDEFINED",
    "UNESTIMATED_SKIP",
    "UNPROMPTED_SKIP",
    "Finding",
    "Key",
    "Mention",
    "Passed",
    "Pending",
    "Planned",
    "Tag",
    "Terms",
    "amount_before",
    "attempt_text",
    "change_cue",
    "character_line",
    "constraint_line",
    "dry_run",
    "effect_of",
    "label",
    "may_declare",
    "mentions",
    "narrated",
    "narrator_line",
    "outcome_spans",
    "pending_line",
    "refusal_text",
    "scope_of",
    "span_of",
    "suppressed",
    "syntax",
    "terms_of",
    "unconfirmed_outcomes",
    "verb_hit",
    "visible_to_characters",
]
