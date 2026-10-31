"""Participation roles and the bounds of reactive narration (FR-MULTI-7,9,10, NFR-PERF-7 (6);
docs/02 §4.4; #176). Pure: nothing here reads the database or calls a model.

**Roles** (FR-MULTI-7). Each present character has a role for the turn: ``primary`` (it renders),
``reactive`` (another render may narrate its visible reaction) or ``silent`` (nothing is narrated
for it). A character's stored role (``characters.participation_role``, ``participation_fixed``) is
a **default** the engine may change for a turn (escalation, budget reduction) or **fixed**, a bound
the engine never crosses on its own. ``resolve_roles`` gives the turn's roles: the named
(``render_character_ids``) are ``primary``; every other present character takes the request's
override (``roles``), else its stored role; a stored ``primary`` that the request did not name is
``reactive`` for the turn (a turn renders only the characters it names); a minor (#163) with no
explicit role is ``reactive``. The player's character and a non-player actor have no role. Each
entry carries its ``source``: ``default``, ``fixed``, ``request``, ``escalated`` or ``reduced``.

``validate_overrides`` is the request's ``roles`` rules (422 ``validation_error``): an unknown
character, the player's or the actor's, or a named character overridden down are refused; a
``primary`` override of an unnamed character names it (``promoted``).

**Budget reduction** (NFR-PERF-7 (6), ``reduce``): when the safety reservation does not fit every
named primary, the extra ones (the last named first) become reactive narration where the story
allows (a role that is not a fixed ``primary``), otherwise they are deferred to a later turn.

**Reactions** (FR-MULTI-9,10, ``judge``). A narrated reaction is attributed and observable only.
Its text is classified by the reaction lexicon (``detectors/reaction.json``) and the claim
detectors' tables: a thought (``inner_state.json``'s cues) is never narrated for another character
(dropped); speech is allowed only as a brief interjection when the story allows it (at most
``interjection_max_words`` whitespace tokens); speech beyond that, a decision, or an effect
(possession, transfer, placement, movement: the detectors' verb tables), or #187 (FR-MULTI-9,
FR-STATE-5) a mention of a tracked State (its name, an alias, its unit: ``Bounds.state_terms``),
**escalates** the
character to a primary render, or, when escalation is unavailable (a fixed role; the budget, which
the caller decides), is **limited** to a non-verbal reaction (``limited_text``) and the character
is listed in ``escalation_deferred``. A ``silent`` character gets no narrated reaction at all, a
primary renders itself, and the player's character is never a reaction's subject.

``prose_reactions`` finds a reaction narrated in a primary's ``do`` prose: a sentence whose subject
is another present character (its name first) and whose verb is one of the lexicon's (or an effect
or thought verb), or which names a tracked State (#187); every other sentence stays the
primary's (an action by another character is #144's denial).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any, Literal

from epistrel.core.errors import InvalidRequest

PRIMARY, REACTIVE, SILENT = "primary", "reactive", "silent"
ROLES: tuple[str, ...] = (PRIMARY, REACTIVE, SILENT)
Role = Literal["primary", "reactive", "silent"]
DEFAULT_ROLE: Role = "reactive"
DEFAULT, FIXED, REQUEST, ESCALATED, REDUCED = "default", "fixed", "request", "escalated", "reduced"
SOURCES: tuple[str, ...] = (DEFAULT, FIXED, REQUEST, ESCALATED, REDUCED)

NONVERBAL, INTERJECTION = "nonverbal", "interjection"
ReactionKind = Literal["nonverbal", "interjection"]
INTERJECTION_MAX_WORDS = 6
"""The longest interjection: the default of the profile dial
``multi_character.interjection_max_words`` (#181)."""
MAX_ESCALATIONS_PER_TURN = 2
"""The most reactive characters one turn escalates to a primary render (no recursion)."""

FIXED_ROLE, BUDGET = "fixed_role", "budget"
"""Why an escalation was unavailable (``escalation_deferred[].reason``)."""


@dataclass(frozen=True, slots=True)
class Participation:
    """A character's stored role: the role and whether it is fixed."""

    role: str = DEFAULT_ROLE
    fixed: bool = False

    def payload(self) -> dict[str, Any]:
        return {"role": self.role, "fixed": self.fixed}


@dataclass(frozen=True, slots=True)
class RoleOf:
    """A character's role for one turn and where it came from."""

    role: str
    source: str
    fixed: bool = False

    def payload(self) -> dict[str, Any]:
        return {"role": self.role, "source": self.source}


def resolve_roles(
    *,
    present: Sequence[str],
    named: Sequence[str],
    stored: Mapping[str, Participation],
    overrides: Mapping[str, str] | None = None,
    excluded: Iterable[str] = (),
    minors: Iterable[str] = (),
) -> dict[str, RoleOf]:
    """The turn's roles (see the module docstring), keyed by character id: every named
    character and every present one but the ``excluded`` (the player's character, the actor)."""
    gone = set(excluded)
    lazy = set(minors)
    wanted = dict(overrides or {})
    found: dict[str, RoleOf] = {}
    for cid in named:
        if cid in gone:
            continue
        own = stored.get(cid, Participation())
        found[cid] = RoleOf(PRIMARY, REQUEST if cid in wanted else DEFAULT, own.fixed)
    for cid in present:
        if cid in gone or cid in found:
            continue
        mine = stored.get(cid)
        if cid in wanted:
            found[cid] = RoleOf(wanted[cid], REQUEST, mine.fixed if mine else False)
            continue
        if mine is None:
            found[cid] = RoleOf(REACTIVE if cid in lazy else DEFAULT_ROLE, DEFAULT)
            continue
        role = REACTIVE if mine.role == PRIMARY else mine.role
        found[cid] = RoleOf(role, FIXED if mine.fixed else DEFAULT, mine.fixed)
    return found


def _invalid(message: str, field_: str, reason: str) -> InvalidRequest:
    return InvalidRequest(
        message,
        {
            "field": field_,
            "reason": reason,
            "errors": [{"field": field_, "message": message, "type": "value_error"}],
        },
    )


def validate_overrides(
    overrides: Mapping[str, str] | None,
    *,
    cast: Sequence[str],
    named: Sequence[str],
    excluded: Iterable[str],
) -> tuple[str, ...]:
    """The request's ``roles`` (see the module docstring); answers the characters a ``primary``
    override names (to render, after the named ones). Raises 422."""
    if not overrides:
        return ()
    gone = set(excluded)
    known = set(cast)
    promoted: list[str] = []
    for cid, role in overrides.items():
        where = f"body.roles.{cid}"
        if role not in ROLES:
            raise _invalid(f"{role!r} is not a participation role", where, "unknown_role")
        if cid not in known:
            raise _invalid("roles names a character the story does not have", where, "unknown")
        if cid in gone:
            raise _invalid("the player's character has no participation role", where, "no_role")
        if cid in named and role != PRIMARY:
            raise _invalid(
                "a character the request names renders as primary", where, "named_overridden"
            )
        if role == PRIMARY and cid not in named:
            promoted.append(cid)
    return tuple(promoted)


# -- budget reduction (NFR-PERF-7 (6)) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reduction:
    """The named primaries that fit the budget, those turned into reactive narration and those
    deferred (``(id, fixed)``), in request order."""

    kept: tuple[str, ...]
    reduced: tuple[str, ...] = ()
    deferred: tuple[tuple[str, bool], ...] = ()


def reduce(
    named: Sequence[str], fit: int, stored: Mapping[str, Participation], *, reactive_ok: bool = True
) -> Reduction:
    """Keep the first ``fit`` named primaries (at least one); each extra one becomes reactive
    narration unless its role is a fixed ``primary`` (or the story allows no reactive narration),
    which defers it (see the module docstring)."""
    count = max(1, fit)
    kept = tuple(named[:count])
    reduced: list[str] = []
    deferred: list[tuple[str, bool]] = []
    for cid in named[count:]:
        mine = stored.get(cid, Participation())
        fixed_primary = mine.fixed and mine.role == PRIMARY
        if reactive_ok and not fixed_primary:
            reduced.append(cid)
        else:
            deferred.append((cid, fixed_primary))
    return Reduction(kept, tuple(reduced), tuple(deferred))


# -- reactions ------------------------------------------------------------------------------


@cache
def lexicon() -> Mapping[str, Any]:
    """``detectors/reaction.json``."""
    source = resources.files("epistrel.core.verify").joinpath("detectors/reaction.json")
    loaded: dict[str, Any] = json.loads(source.read_text("utf-8"))
    return loaded


def _alternation(words: Iterable[str]) -> str:
    ordered = sorted(set(words), key=lambda w: (-len(w), w))
    return "|".join(r"\s+".join(re.escape(p) for p in w.casefold().split()) for w in ordered)


def _pattern(words: Iterable[str]) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w'\u2019])(?:{_alternation(words)})(?![\w'\u2019])")


@cache
def _effect_forms() -> tuple[str, ...]:
    from epistrel.core.verify.detect import tables

    return tuple(f for table in tables().verbs for lemma in table.lemmas for f in lemma.forms)


@cache
def _inner_forms() -> tuple[str, ...]:
    from epistrel.core.verify.inner_state import lexicon as inner

    table = inner()
    return tuple(str(w) for w in (*table["verbs"], *table["cognition"]))


@cache
def _patterns() -> dict[str, re.Pattern[str]]:
    table = lexicon()
    return {
        "inner": _pattern(_inner_forms()),
        "speech": _pattern(table["speech"]),
        "decision": _pattern(table["decision"]),
        "effect": _pattern(_effect_forms()),
        "nonverbal": _pattern(table["nonverbal"]),
    }


def words(text: str) -> int:
    """An interjection's length: its whitespace tokens."""
    return len(text.split())


def quoted(text: str) -> bool:
    return any(q in text for q in lexicon()["quotes"])


CATEGORIES = ("inner", "speech", "decision", "effect", "nonverbal")
"""What a reaction's text is, most restrictive first."""


def classify(text: str, kind: str = NONVERBAL) -> str:
    """What ``text`` narrates: ``inner`` (a thought), ``speech``, ``decision``, ``effect``, or
    ``nonverbal`` (anything else: an observable reaction)."""
    folded = " ".join(text.casefold().split())
    patterns = _patterns()
    if patterns["inner"].search(folded):
        return "inner"
    if kind == INTERJECTION or quoted(text) or patterns["speech"].search(folded):
        return "speech"
    for category in ("decision", "effect"):
        if patterns[category].search(folded):
            return category
    return "nonverbal"


def reaction_verb(text: str) -> bool:
    """Whether ``text`` holds a verb any of the categories names (a narrated reaction's)."""
    folded = " ".join(text.casefold().split())
    return any(p.search(folded) for p in _patterns().values())


@dataclass(frozen=True, slots=True)
class Reaction:
    """One narrated reaction: its reactor, text and kind, who narrated it, the beat it reacts to
    (``None``: the immediately preceding perceivable beat), and where it was found (``declared``
    on the render's ``reactions`` channel, or ``prose`` in the primary's beat ``beat``)."""

    character_id: str
    text: str
    narrated_by: str
    kind: str = NONVERBAL
    reacts_to: int | None = None
    source: str = "declared"
    beat: int | None = None


class Outcome(StrEnum):
    ACCEPT = "accept"
    ESCALATE = "escalate"
    LIMIT = "limit"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class Bound:
    """What reactive narration may make of one reaction: ``accept`` it (as ``kind``), escalate
    the reactor, limit it to a non-verbal reaction (``reason``: why escalation was unavailable),
    or drop it (``reason``)."""

    outcome: Outcome
    reason: str = ""
    kind: str = NONVERBAL
    category: str = "nonverbal"


@dataclass(frozen=True, slots=True)
class Bounds:
    """The turn's reactive-narration settings and who is who."""

    roles: Mapping[str, RoleOf]
    present: frozenset[str]
    interjections: bool = False
    max_words: int = INTERJECTION_MAX_WORDS
    player_id: str | None = None
    primaries: frozenset[str] = field(default_factory=frozenset)
    state_terms: tuple[str, ...] = ()
    """#187: how prose names the story's tracked States (case-folded names, aliases, units): a
    reaction that names one would change it, which only a primary render may declare."""


STATE = "state"
"""#187: a reaction's category when it names a tracked State (it escalates like an effect)."""


def names_state(text: str, terms: Sequence[str]) -> bool:
    """Whether ``text`` names one of the tracked States' ``terms``."""
    return bool(terms) and _pattern(terms).search(" ".join(text.casefold().split())) is not None


def judge(reaction: Reaction, bounds: Bounds) -> Bound:
    """The reaction's fate under ``bounds`` (see the module docstring); the caller turns an
    ``escalate`` it cannot afford into a ``limit`` (``budget``)."""
    cid = reaction.character_id
    category = classify(reaction.text, reaction.kind)
    if bounds.player_id is not None and cid == bounds.player_id:
        return Bound(Outcome.DROP, "player", category=category)
    role = bounds.roles.get(cid)
    if role is None:
        return Bound(Outcome.DROP, "not_present", category=category)
    if cid in bounds.primaries or role.role == PRIMARY:
        return Bound(Outcome.DROP, "primary", category=category)
    if cid not in bounds.present:
        return Bound(Outcome.DROP, "not_present", category=category)
    if role.role == SILENT:
        return Bound(Outcome.DROP, FIXED_ROLE if role.fixed else SILENT, category=category)
    if category == "inner":
        return Bound(Outcome.DROP, "inner_state", category=category)
    if category == "speech":
        if bounds.interjections and words(reaction.text) <= bounds.max_words:
            return Bound(Outcome.ACCEPT, kind=INTERJECTION, category=category)
        return _trigger(role, "speech", category)
    if category in ("decision", "effect"):
        return _trigger(role, category, category)
    if names_state(reaction.text, bounds.state_terms):
        # #187 (FR-MULTI-9): a reaction that would change a tracked State escalates.
        return _trigger(role, STATE, STATE)
    return Bound(Outcome.ACCEPT, category=category)


def _trigger(role: RoleOf, reason: str, category: str) -> Bound:
    if role.fixed:
        return Bound(Outcome.LIMIT, FIXED_ROLE, category=category)
    return Bound(Outcome.ESCALATE, reason, category=category)


def limited_text(name: str) -> str:
    """A reaction limited to what reactive narration allows: a non-verbal one."""
    return f"{name} looks up but says nothing."


_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")


def prose_reactions(
    text: str, others: Mapping[str, str], state_terms: Sequence[str] = ()
) -> list[tuple[str, str, int, int]]:
    """The reactions ``text`` (a primary's ``do``) narrates: ``(character id, sentence, start,
    end)`` for each sentence whose first words are another present character's name (``others``:
    name → id) and which holds a reaction, speech, decision, effect or thought verb, or names a
    tracked State (``state_terms``, #187)."""
    found: list[tuple[str, str, int, int]] = []
    by_name = sorted(others.items(), key=lambda kv: -len(kv[0]))
    for match in _SENTENCE.finditer(text):
        sentence = match.group(0)
        stripped = sentence.strip()
        if not stripped:
            continue
        folded = stripped.casefold()
        for name, cid in by_name:
            lead = name.casefold()
            if folded.startswith(lead) and (
                len(folded) == len(lead) or not folded[len(lead)].isalnum()
            ):
                rest = stripped[len(name) :]
                if reaction_verb(rest) or names_state(rest, state_terms):
                    start = match.start() + (len(sentence) - len(sentence.lstrip()))
                    found.append((cid, stripped, start, start + len(stripped)))
                break
    return found


def without(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """``text`` with the ``spans`` removed (whitespace closed up)."""
    kept: list[str] = []
    at = 0
    for start, end in sorted(spans):
        kept.append(text[at:start])
        at = end
    kept.append(text[at:])
    return " ".join("".join(kept).split())


__all__ = [
    "BUDGET",
    "CATEGORIES",
    "DEFAULT",
    "DEFAULT_ROLE",
    "ESCALATED",
    "FIXED",
    "FIXED_ROLE",
    "INTERJECTION",
    "INTERJECTION_MAX_WORDS",
    "MAX_ESCALATIONS_PER_TURN",
    "NONVERBAL",
    "PRIMARY",
    "REACTIVE",
    "REDUCED",
    "REQUEST",
    "ROLES",
    "SILENT",
    "SOURCES",
    "STATE",
    "Bound",
    "Bounds",
    "Outcome",
    "Participation",
    "Reaction",
    "Reduction",
    "RoleOf",
    "classify",
    "judge",
    "lexicon",
    "limited_text",
    "names_state",
    "prose_reactions",
    "quoted",
    "reaction_verb",
    "reduce",
    "resolve_roles",
    "validate_overrides",
    "without",
    "words",
]
