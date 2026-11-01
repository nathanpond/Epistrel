"""Completeness in both directions between narrated changes and declared effects (FR-STATE-13;
#190): nothing is paid in prose without being paid in the ledger, and nothing moves in the
ledger without the story saying so.

**``diff``** compares a turn's detected claims (#148's ``DetectedClaim``s, each with the id the
turn gave it) and its beats' prose with the effects its beats declare (#188's ``StateEffect``,
wrapped in ``Declared`` with its beat, index, reason and ``silent`` flag):

- **``undeclared``**: a ``do`` or narration sentence that changes a tracked State with no
  effect of its beat covering it. The sentence changes a State when it holds a change verb
  (#148's possession, transfer and consumption tables; a question, a negation, a conditional
  or a modal suppresses it, #187's ``mentions``) and names the State: a detected claim of the
  sentence whose subject or object resolves (#139) to the State's ``entity_id``; a quantity
  whose unit names the State (its name, an alias or its ``unit``); or #187's terms (the name,
  an alias, the unit or the tracked entity's name). Each is a ``StateRef``: the State, the
  sentence, the direction the verb moves it for the beat's actor (``give`` and consumption
  verbs out, ``take`` and possession verbs in; ``None`` when unknown) and the amount
  narrated.
  An effect **covers** it when it is on the same State (its own side, or a transfer's other
  side), in the same direction (checked only when the effect is on the beat's actor's own
  value and both directions are known), and its amount is within the claim's tolerance:
  exact for a stated amount, ±10 % for an approximation ("about fifty"), any amount when the
  sentence states none ("some gold"). A set (``value``) covers any amount; a boolean set
  covers a change of the matching direction. A number State's change with no amount and no
  effect is #187's ``effect_unresolved``, not ``undeclared``.
  When the beat declares an effect on the State that does not cover the sentence (the
  narrated amount differs, as when a cap repaired the effect but the prose spells the old
  amount in words), the entry is a ``mismatch``.
- **``unnarrated``**: a declared effect on a ``player_visible`` State that no beat of the turn
  mentions: none names the State (its name, an alias, its unit, its tracked entity) or the
  change (the amount of its delta, as a number). An effect on a ``silent`` State, or a
  ``silent`` effect, needs no narration.

``undeclared`` and #187's ``denied:state_change`` (``core.states.effects.narrated``) are one
finding: ``orchestrator.structured.effects.findings`` merges them by the beat and State (the
detected claim's id kept on the finding), so one claim yields one ``state_undeclared`` repair.
``unnarrated`` is the ``state_unnarrated`` finding (``STATE_UNNARRATED``): the ladder asks the
render to narrate the change, and the deterministic rung appends one line built from the
effect's reason (``narration_line``).

- **``skips``** (#199, FR-CLOCK-7): ``undeclared_skips`` over the turn's beats, a ``do`` or
  narration beat that narrates elapsed time (the ``time`` detector's ``elapsed`` hit, in an
  asserted sentence) with no time advance declared: the Orchestrator's ``time_estimation``
  proposes its duration (``orchestrator.structured.time_estimation``).

Pure: no I/O, no story text in any log (this module logs nothing).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import TYPE_CHECKING, Final

from epistrel.core.enums import StateType, StateVisibility
from epistrel.core.repair.rungs import sentences
from epistrel.core.states import effects as rules
from epistrel.core.states.authorize import StateEffect
from epistrel.core.states.effects import AMOUNT, STATE_UNNARRATED
from epistrel.core.states.model import StateDefinition, to_json

if TYPE_CHECKING:
    from epistrel.core.verify.detect import DetectedClaim

APPROXIMATION: Final = Decimal("0.10")
"""How far an approximate amount ("about fifty") may be from the declared one."""
APPROXIMATE_CUES: tuple[str, ...] = (
    "about",
    "around",
    "roughly",
    "nearly",
    "almost",
    "approximately",
    "some",
    "maybe",
    "perhaps",
    "close to",
    "or so",
)
"""Words just before (or, for "or so", after) an amount that make it an approximation."""
GAIN, LOSS = 1, -1


@dataclass(frozen=True, slots=True)
class Beat:
    """A beat of the turn as ``diff`` reads it: its number, kind, text, the acting character
    (``None`` for the narrator's) and whether it is the narrator's (who may name every State;
    a character names only the player-visible ones)."""

    beat: int
    kind: str
    text: str
    actor: str | None = None
    narrator: bool = False
    attempt: bool = False
    advances: int = 0
    """#199: the time advances the beat declares (a skip it declares is not undeclared)."""
    flashback: bool = False
    """#199: a flashback's elapsed time is not the present's (no skip)."""


@dataclass(frozen=True, slots=True)
class Declared:
    """One declared effect: its beat and index there, the effect as Core takes it, its reason
    and whether it is ``silent``."""

    beat: int
    index: int
    effect: StateEffect
    reason: str = ""
    silent: bool = False


@dataclass(frozen=True, slots=True)
class StateRef:
    """A narrated change to a State: the State, the beat and sentence, the direction the verb
    moves it for the beat's actor (``GAIN``, ``LOSS``, ``None``), the amount narrated (``None``:
    none) and whether it is an approximation."""

    state: str
    beat: int
    span: tuple[int, int]
    direction: int | None = None
    amount: Decimal | None = None
    approximate: bool = False


@dataclass(frozen=True, slots=True)
class Undeclared:
    """A narrated change no declared effect covers: the detected claim it came from (``None``:
    #187's sentence scan alone found it), its ``StateRef``, and whether its beat declares an
    effect on the State that does not cover it (``mismatch``)."""

    claim_id: str | None
    ref: StateRef
    mismatch: bool = False


@dataclass(frozen=True, slots=True)
class Diff:
    """``diff``'s answer (see the module docstring)."""

    undeclared: tuple[Undeclared, ...] = ()
    unnarrated: tuple[Declared, ...] = ()
    skips: tuple[UndeclaredSkip, ...] = ()
    """#199 (FR-CLOCK-7): ``undeclared_skips`` over the turn's beats."""


@dataclass(frozen=True, slots=True)
class UndeclaredSkip:
    """#199 (FR-CLOCK-7): a beat narrating elapsed time (the ``time`` detector's ``elapsed``
    hit, ``core.verify.detect.elapsed_spans``) with no time advance declared: the beat, the
    sentence holding the hit (``span``) and the hit itself (``hit``)."""

    beat: int
    span: tuple[int, int]
    hit: tuple[int, int]


UNDECLARED_SKIP: Final = "undeclared_skip"
"""#199: what ``diff`` calls an ``UndeclaredSkip`` (FR-CLOCK-7's flag)."""


# -- reading prose -----------------------------------------------------------------------------


def _pattern(words: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted({w for w in words if w}, key=lambda w: (-len(w), w))
    body = "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in ordered)
    return re.compile(rf"(?<![\w'\u2019])(?:{body})(?![\w'\u2019])", re.IGNORECASE)


@cache
def _directions() -> tuple[tuple[re.Pattern[str], int], ...]:
    from epistrel.core.verify.detect import tables

    found: list[tuple[re.Pattern[str], int]] = []
    for table in tables().verbs:
        for lemma in table.lemmas:
            if table.category == "transfer":
                way = LOSS if lemma.options.get("direction") == "give" else GAIN
            elif table.category == "possession":
                way = GAIN
            elif table.category == "consumption":
                way = LOSS
            else:
                continue
            found.append((_pattern(lemma.forms), way))
    return tuple(found)


def direction_of(sentence: str) -> int | None:
    """The way ``sentence``'s change verbs move a value for its actor: ``LOSS`` (give, pay,
    spend), ``GAIN`` (take, pocket, receive), ``None`` when it holds none or both."""
    ways = {way for pattern, way in _directions() if rules.verb_hit(pattern, sentence)}
    return ways.pop() if len(ways) == 1 else None


@cache
def _approximate() -> re.Pattern[str]:
    return _pattern(APPROXIMATE_CUES)


def _approximate_at(sentence: str, start: int, end: int) -> bool:
    before = " ".join(re.findall(r"[\w']+", sentence[:start])[-2:])
    after = " ".join(re.findall(r"[\w']+", sentence[end:])[:2])
    return bool(_approximate().search(before)) or after.casefold().startswith("or so")


def _quantities(sentence: str) -> list[tuple[int, int, Decimal, str]]:
    from epistrel.core.verify.detect import quantity_spans

    found = []
    for start, end, literal in quantity_spans(sentence):
        value = literal.value
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        found.append((start, end, Decimal(str(value)), str(literal.unit or "")))
    return found


def _names(definition: StateDefinition, word: str) -> bool:
    from epistrel.core.states.verify import TrackedState, names

    if definition.id is None:
        return word.strip().casefold() in rules.terms_of(definition).words
    return names(TrackedState.of(definition), word)


def change_sentence(sentence: str) -> bool:
    """Whether ``sentence`` asserts a change: a change verb, and no question, negation,
    conditional or modal (#187's ``mentions`` rule)."""
    if sentence.rstrip().endswith("?") or rules.suppressed(sentence):
        return False
    return rules.change_cue(sentence)


def _entity_of(claim: DetectedClaim) -> set[str]:
    found: set[str] = set()
    p = claim.proposition
    if p is not None:
        found.add(p.subject_id)
        if p.object_id is not None:
            found.add(p.object_id)
    sketch = claim.sketch
    if sketch is not None:
        found |= {i for i in (sketch.subject_id, sketch.object_id) if i is not None}
    return found


def _claim_states(
    claim: DetectedClaim, definitions: Sequence[StateDefinition]
) -> list[StateDefinition]:
    """The States a detected claim names: by #139's resolution to a State's ``entity_id``, or
    by the unit of its quantity (a lone one, or one a change claim carries)."""
    entities = _entity_of(claim)
    found = [d for d in definitions if d.entity_id is not None and d.entity_id in entities]
    units: list[str] = []
    sketch = claim.sketch
    if sketch is not None and sketch.literal is not None and sketch.literal.unit:
        units.append(str(sketch.literal.unit))
    if sketch is not None and isinstance(sketch.qualifiers.get("quantity"), str):
        units += [q[3] for q in _quantities(str(sketch.qualifiers["quantity"]))]
    for unit in units:
        found += [d for d in definitions if d.type is StateType.NUMBER and _names(d, unit)]
    return list({d.name: d for d in found}.values())


def _amount(
    definition: StateDefinition, sentence: str, terms: rules.Terms
) -> tuple[Decimal | None, bool]:
    """The amount ``sentence`` narrates for ``definition`` (a quantity whose unit names it,
    else a number just before one of its terms) and whether it is approximate."""
    for start, end, value, unit in _quantities(sentence):
        if _names(definition, unit):
            return value, _approximate_at(sentence, start, end)
    if terms.words:
        hit = _pattern(terms.words).search(sentence)
        if hit is not None:
            found = rules.amount_before(sentence, hit.start())
            if found is not None:
                return found, _approximate_at(sentence, hit.start(), hit.end())
    return None, False


def _refs(
    beat: Beat,
    claims: Sequence[tuple[str | None, DetectedClaim]],
    definitions: Sequence[StateDefinition],
    entity_names: Mapping[str, str],
) -> list[tuple[str | None, StateRef]]:
    """The changes ``beat`` narrates, one per sentence and State, each with the first detected
    claim of the sentence that names it."""
    terms = {d.name: rules.terms_of(d, entity_names) for d in definitions}
    found: dict[tuple[tuple[int, int], str], str | None] = {}
    for start, end in sentences(beat.text):
        sentence = beat.text[start:end]
        if not change_sentence(sentence):
            continue
        named: dict[str, str | None] = {}
        for claim_id, claim in claims:
            at = claim.span[1]
            if not start <= at < end:
                continue
            for d in _claim_states(claim, definitions):
                if named.get(d.name) is None:
                    named[d.name] = claim_id
        for _, _, _, unit in _quantities(sentence):
            for d in definitions:
                if d.type is StateType.NUMBER and _names(d, unit):
                    named.setdefault(d.name, None)
        for mention in rules.mentions(sentence, list(terms.values())):
            named.setdefault(mention.state, None)
        for state, claim_id in named.items():
            found[((start, end), state)] = claim_id
    out: list[tuple[str | None, StateRef]] = []
    by_name = {d.name: d for d in definitions}
    for (span, state), claim_id in found.items():
        sentence = beat.text[span[0] : span[1]]
        amount, approximate = _amount(by_name[state], sentence, terms[state])
        out.append(
            (
                claim_id,
                StateRef(state, beat.beat, span, direction_of(sentence), amount, approximate),
            )
        )
    return out


# -- covering ----------------------------------------------------------------------------------


def _sides(d: Declared, state: str) -> list[tuple[str | None, Decimal | None, object]]:
    """``d``'s sides on ``state``: ``(scope key, delta, value)``, its own and a transfer's
    other side (whose delta mirrors the effect's own unless it names one)."""
    e = d.effect
    found: list[tuple[str | None, Decimal | None, object]] = []
    if e.state == state:
        found.append((e.scope_key, e.delta, e.value))
    other = e.counterparty
    if other is not None and other.state == state:
        delta = other.delta if other.delta is not None else (None if e.delta is None else -e.delta)
        found.append((other.scope_key, delta, None))
    return found


def _within(amount: Decimal, delta: Decimal, approximate: bool) -> bool:
    size = abs(delta)
    if not approximate:
        return size == amount
    return abs(size - amount) <= amount * APPROXIMATION


def covers(d: Declared, ref: StateRef, actor: str | None) -> bool:
    """Whether ``d`` covers ``ref`` (see the module docstring); ``actor`` the character whose
    beat narrates it."""
    for scope_key, delta, value in _sides(d, ref.state):
        own = actor is not None and scope_key == actor and ref.direction is not None
        if delta is None:
            if own and isinstance(value, bool) and value is not (ref.direction == GAIN):
                continue
            return True
        if own and (delta > 0) != (ref.direction == GAIN):
            continue
        if ref.amount is None or _within(ref.amount, delta, ref.approximate):
            return True
    return False


# -- the diff ----------------------------------------------------------------------------------


def _visible(definition: StateDefinition) -> bool:
    return definition.visibility is StateVisibility.PLAYER_VISIBLE


def _numbers(text: str) -> set[Decimal]:
    from epistrel.core.verify.detect import parse_number

    found = {q[2] for q in _quantities(text)}
    for word in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        parsed = parse_number(word)
        if parsed is not None:
            found.add(Decimal(str(parsed)))
    return found


def narrated_by(
    d: Declared,
    definitions: Mapping[str, StateDefinition],
    texts: Sequence[str],
    entity_names: Mapping[str, str],
) -> bool:
    """Whether one of ``texts`` mentions ``d``'s State (its name, an alias, its unit, its
    tracked entity; a transfer's other side too) or its change (the delta's amount)."""
    words: list[str] = []
    for name in {
        d.effect.state,
        *([] if d.effect.counterparty is None else [d.effect.counterparty.state]),
    }:
        definition = definitions.get(name)
        if definition is not None:
            words += rules.terms_of(definition, entity_names).words
    pattern = _pattern(words) if words else None
    delta = d.effect.delta
    for text in texts:
        if pattern is not None and pattern.search(text):
            return True
        if delta is not None and delta != 0 and abs(delta) in _numbers(text):
            return True
    return False


def needs_narration(d: Declared, definitions: Mapping[str, StateDefinition]) -> bool:
    """Whether ``d`` must be narrated: an effect on a player-visible State, neither the State
    nor the effect ``silent``."""
    definition = definitions.get(d.effect.state)
    if definition is None or d.silent or definition.silent:
        return False
    return _visible(definition)


def diff(
    detected_claims: Sequence[tuple[str | None, DetectedClaim]],
    declared_effects: Sequence[Declared],
    definitions: Mapping[str, StateDefinition],
    *,
    beats: Sequence[Beat],
    texts: Sequence[str] = (),
    entity_names: Mapping[str, str] | None = None,
) -> Diff:
    """The two directions (see the module docstring): ``detected_claims`` and ``beats``' prose
    against ``declared_effects``; ``texts`` the turn's other prose an effect's narration may sit
    in (an earlier primary's staged beats)."""
    names = entity_names or {}
    by_beat: dict[int, list[tuple[str | None, DetectedClaim]]] = {}
    for claim_id, claim in detected_claims:
        if claim.beat is not None:
            by_beat.setdefault(claim.beat, []).append((claim_id, claim))
    undeclared: list[Undeclared] = []
    for b in beats:
        if b.kind not in rules.NARRATED_KINDS or b.attempt:
            continue
        known = [d for d in definitions.values() if b.narrator or _visible(d)]
        own = [d for d in declared_effects if d.beat == b.beat]
        for claim_id, ref in _refs(b, by_beat.get(b.beat, []), known, names):
            on_state = [d for d in own if _sides(d, ref.state)]
            if any(covers(d, ref, b.actor) for d in on_state):
                continue
            definition = definitions[ref.state]
            if not on_state and ref.amount is None and definition.type is StateType.NUMBER:
                continue  # #187's effect_unresolved: a change with no amount
            undeclared.append(Undeclared(claim_id, ref, mismatch=bool(on_state)))
    prose = [b.text for b in beats if not b.attempt] + list(texts)
    unnarrated = tuple(
        d
        for d in declared_effects
        if needs_narration(d, definitions) and not narrated_by(d, definitions, prose, names)
    )
    return Diff(tuple(undeclared), unnarrated, undeclared_skips(beats))


SKIP_KINDS: Final = frozenset({"do", "narration"})
"""#199: the beats whose elapsed time moves the clock: a ``say``'s or a ``think``'s is talk
("we could talk until six"), never the scene's time passing."""


def undeclared_skips(beats: Sequence[Beat]) -> tuple[UndeclaredSkip, ...]:
    """#199 (FR-CLOCK-7): each ``do`` or narration beat that narrates elapsed time
    (``elapsed_spans``) in an asserted sentence (no question, negation, conditional or modal)
    and declares no time advance: one ``UndeclaredSkip`` per beat, at its first such sentence.
    A flashback and an attempt narrate no skip."""
    from epistrel.core.verify.detect import elapsed_spans

    found: list[UndeclaredSkip] = []
    for beat in beats:
        if beat.kind not in SKIP_KINDS or beat.advances or beat.flashback or beat.attempt:
            continue
        for start, end in sentences(beat.text):
            sentence = beat.text[start:end]
            if sentence.rstrip().endswith("?") or rules.suppressed(sentence):
                continue
            hits = elapsed_spans(sentence)
            if hits:
                hit = (start + hits[0][0], start + hits[0][1])
                found.append(UndeclaredSkip(beat.beat, (start, end), hit))
                break
    return tuple(found)


# -- the deterministic narration ---------------------------------------------------------------


def narration_line(d: Declared, definition: StateDefinition | None) -> str:
    """The one line the deterministic rung appends for an unnarrated effect: its reason, and
    the change ("Paid the toll (gold -50).")."""
    reason = " ".join(d.reason.split()).rstrip(".!?") or "A change"
    reason = reason[0].upper() + reason[1:]
    name = d.effect.state.replace("_", " ") if definition is None else rules.label(definition)
    e = d.effect
    if e.delta is not None:
        change = f"{name} {'+' if e.delta > 0 else '-'}{to_json(abs(e.delta))}"
    elif e.value is not None:
        shown = to_json(e.value)
        change = f"{name}: {str(shown).lower() if isinstance(shown, bool) else shown}"
    else:
        change = name
    return f"{reason} ({change})."


__all__ = [
    "AMOUNT",
    "APPROXIMATE_CUES",
    "APPROXIMATION",
    "GAIN",
    "LOSS",
    "SKIP_KINDS",
    "STATE_UNNARRATED",
    "UNDECLARED_SKIP",
    "Beat",
    "Declared",
    "Diff",
    "StateRef",
    "Undeclared",
    "UndeclaredSkip",
    "change_sentence",
    "covers",
    "diff",
    "direction_of",
    "narrated_by",
    "narration_line",
    "needs_narration",
    "undeclared_skips",
]
