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
refused effect changes nothing the later ones see.

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
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache
from typing import Final, Literal

from epistrel.core.enums import StateType, StateVisibility
from epistrel.core.repair.rungs import sentences
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
INVALID_EFFECT = "invalid_effect"
"""The constraint of a repaired render's effect that names no State it may change."""
UNDEFINED = "undefined"
"""The constraint of a delta on a State with no value yet."""
Tag = Literal["state_constraint", "effect_unresolved", "state_undeclared"]
TAGS: frozenset[str] = frozenset({STATE_CONSTRAINT, EFFECT_UNRESOLVED, STATE_UNDECLARED})

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
    character-scoped State defaults to it) and the effect."""

    beat: int
    index: int
    actor: str | None
    effect: DeclaredEffect


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


def dry_run(
    definitions: Mapping[str, StateDefinition],
    values: Mapping[Key, Value | UndefinedType],
    planned: Sequence[Planned],
) -> tuple[list[Passed], list[Finding]]:
    """Fold ``planned`` (in order) over ``values`` (each key's value at the turn's start; a key
    not given is its State's initial value): what passes and what is refused (see the module
    docstring)."""
    running: dict[Key, Value | UndefinedType] = {}
    passed: list[Passed] = []
    found: list[Finding] = []
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
        if key in running:
            current = running[key]
        elif key in values:
            current = values[key]
        else:
            current = Undefined if definition.initial_value is None else definition.initial_value
        refused = _refused(definition, p, key, current)
        if refused is not None:
            found.append(refused)
            continue
        value, delta = _typed(definition, p.effect)
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
        running[key] = attempted
        passed.append(Passed(p.beat, p.index, key, current, attempted))
    return passed, found


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
) -> Finding:
    return Finding(
        STATE_CONSTRAINT,
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


def change_cue(text: str) -> bool:
    """Whether ``text`` holds a change verb (possession, transfer or consumption)."""
    return _change_verbs().search(text) is not None


@dataclass(frozen=True, slots=True)
class Mention:
    """A sentence that changes a State: the State, the sentence's span and the amount it names
    (``None``: none)."""

    state: str
    span: tuple[int, int]
    amount: Decimal | None


def _amount(sentence: str, at: int) -> Decimal | None:
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
        if sentence.rstrip().endswith("?") or _suppressors().search(sentence):
            continue
        if not change_cue(sentence):
            continue
        for term, pattern in patterns:
            hit = pattern.search(sentence)
            if hit is not None:
                found.append(Mention(term.state, (start, end), _amount(sentence, hit.start())))
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
        found[m.state] = Finding(
            STATE_UNDECLARED if m.amount is not None else EFFECT_UNRESOLVED,
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
    return f"{name} cannot change like that"


def _shown(value: Value | UndefinedType, definition: StateDefinition) -> str:
    if isinstance(value, UndefinedType):
        return "unset"
    shown = str(to_json(value))
    return f"{shown} {definition.unit}" if definition.unit else shown


def constraint_line(finding: Finding, definition: StateDefinition | None, *, narrator: bool) -> str:
    """The repair constraint of ``finding`` (see the module docstring)."""
    name = finding.state if definition is None else label(definition)
    if finding.tag == STATE_CONSTRAINT:
        rule = name if definition is None else _rule(definition, finding.constraint)
        known = (
            f" ({finding.state} is {_shown(finding.current, definition)})"
            if narrator and definition is not None
            else ""
        )
        return (
            f"The action is only an attempt and it fails: {rule}{known}. Narrate it falling "
            "short in character (someone says so, or it simply does not happen), and declare "
            "no effect on it, or a smaller one."
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
    2026-10-30): a player-visible State whose ``authorization.sources`` names
    ``any_character`` or ``character:<id>`` (#188 enforces the sources; until then they only
    shape the prompt)."""
    if not visible_to_characters(definition):
        return False
    sources = definition.authorization.get("sources") or ()
    if isinstance(sources, str):
        sources = (sources,)
    return ANY_CHARACTER in sources or f"{CHARACTER_SOURCE}{character_id}" in sources


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


def narrator_line(
    definition: StateDefinition,
    values: Mapping[str | None, Value | UndefinedType],
    names: Mapping[str, str],
) -> str:
    """The narrator's line for a State: its shape, constraints, visibility and its value at the
    turn's start (per character, by name, for a character-scoped one)."""
    parts = _shape(definition) + _bounds(definition)
    if definition.visibility is StateVisibility.HIDDEN:
        parts.append("hidden from the characters")
    if definition.scope.value == "character":
        shown = [
            f"{names.get(k, k)} (id {k}) {_shown(v, definition)}"
            for k, v in sorted(values.items(), key=lambda kv: names.get(kv[0] or "", "") or "")
            if k is not None
        ]
        now = "; ".join(shown) if shown else _shown(_initial(definition), definition) + " each"
    else:
        now = _shown(values.get(None, _initial(definition)), definition)
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
    "ANY_CHARACTER",
    "CHARACTER_SOURCE",
    "EFFECT_UNRESOLVED",
    "INVALID_EFFECT",
    "NARRATED_KINDS",
    "STATE_CONSTRAINT",
    "STATE_UNDECLARED",
    "TAGS",
    "UNDEFINED",
    "Finding",
    "Key",
    "Mention",
    "Passed",
    "Planned",
    "Tag",
    "Terms",
    "change_cue",
    "character_line",
    "constraint_line",
    "dry_run",
    "label",
    "may_declare",
    "mentions",
    "narrated",
    "narrator_line",
    "refusal_text",
    "scope_of",
    "span_of",
    "syntax",
    "terms_of",
    "visible_to_characters",
]
