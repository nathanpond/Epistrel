"""The protected-content index: what a speaker may not say because its viewpoint does not hold
it (FR-VERIFY-13 (b), FR-VERIFY-12; ADR-065; #148).

**What is protected** (M4): the current canon facts hidden from the speaker's viewpoint
(``latent``-scoped and ``characters``-scoped facts it is not named in, ``engine``-scoped ones:
``core.canon.facts.hidden_from``'s rule, read through the speaker's position), and the
candidate values of every open (undetermined) thunk (#146). A narrator's index is the union of
every character-hidden fact (``narrator`` reads only shared scopes). The terms are

- for a hidden fact, its literal value as it may be written (digits and number words, ordinals,
  ISO and spelled dates, a quantity with its unit) and the names and aliases of the entities it
  names that the speaker has no other way to know (no fact the speaker may read names them,
  they are not in the cast, and they are not the speaker);
- for a thunk, each candidate's text and, for an entity candidate the speaker does not know,
  its names and aliases.

Each term carries what a hit claims: the hidden fact's proposition, or the thunk's proposition
with the candidate in its open slot (``None`` for a text candidate with no entity: that claim
cannot normalize and is ``uncertain``, never silently allowed).

**Matching** folds the text and the terms with the erasure fold (``core.erasure.matcher``: full
case-folding then NFC, ``ı`` as ``i``, every dash as ``-``, no soft hyphen; ADR-155/156) and
matches whole words (``matcher.WORD_CHAR`` boundaries, so a script's combining marks stay inside
a word, #414) with one compiled alternation, longest term first. Unlike erasure it applies no
common-word or candidate rule (ADR-154): every whole-word folded match of a protected term is a
hit, a lowercase common word included.

**Once per turn.** ``load_protected_index`` builds a viewpoint's index at plan time, capped at
the deployment's ``protected_index_max`` terms (thunk candidates first, then hidden canon,
newest first; ``truncated`` reports the cut). The index's ``digest`` is remembered for the
turn's read set, and ``recheck`` (a ``core.readset.REBASE_CHECKS`` entry) rebuilds it under
the story lock at a rebase: a changed index (a fact newly hidden or revealed, a thunk resolved)
regenerates the turn, so a turn never commits text checked against stale protected content.

Nothing here calls a model; logs carry counts only (invariant 3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import weakref
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.erasure.matcher import WORD_CHAR, fold, fold_offsets, nfc
from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    Interval,
    LiteralKind,
    Proposition,
    TypedLiteral,
)
from epistrel.core.readset import StructuredReadSet, register_rebase_check
from epistrel.core.settings_schema import (
    CHARACTER_VIEWPOINT_PREFIX,
    DEFAULT_PROTECTED_INDEX_MAX,
    OMNISCIENT,
)

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation
    from epistrel.core.resolution.entities import EntityIndex

log = logging.getLogger(__name__)

MIN_TERM_CHARS = 2

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORDINAL_WORDS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def number_words(n: int) -> str | None:
    """``n`` (0 to 999,999) in English words, ``None`` outside that range."""
    if n < 0 or n >= 1_000_000:
        return None
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if not ones else f"-{_ONES[ones]}")
    if n < 1_000:
        hundreds, rest = divmod(n, 100)
        head = f"{_ONES[hundreds]} hundred"
        return head if not rest else f"{head} and {number_words(rest)}"
    thousands, rest = divmod(n, 1_000)
    head = f"{number_words(thousands)} thousand"
    return head if not rest else f"{head} {number_words(rest)}"


def ordinal_words(n: int) -> str | None:
    words = number_words(n)
    if words is None:
        return None
    last = re.split(r"([ -])", words)
    tail = last[-1]
    if tail in _ORDINAL_WORDS:
        last[-1] = _ORDINAL_WORDS[tail]
    elif tail.endswith("y"):
        last[-1] = tail[:-1] + "ieth"
    else:
        last[-1] = tail + "th"
    return "".join(last)


def ordinal_digits(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def renderings(literal: TypedLiteral) -> list[str]:
    """The ways a hidden literal may be written (see the module docstring)."""
    value = literal.value
    found: list[str] = []
    if literal.kind in (LiteralKind.INT, LiteralKind.DURATION) and isinstance(value, int):
        found += [str(value), number_words(value) or "", ordinal_digits(value)]
        found.append(ordinal_words(value) or "")
    elif literal.kind is LiteralKind.FLOAT:
        found.append(str(value))
    elif literal.kind is LiteralKind.QUANTITY and isinstance(value, int | float):
        number = int(value) if float(value).is_integer() else value
        unit = literal.unit or ""
        found.append(f"{number} {unit}".strip())
        if isinstance(number, int) and (words := number_words(number)):
            found.append(f"{words} {unit}".strip())
    elif literal.kind is LiteralKind.DATE and isinstance(value, str):
        found.append(value)
        try:
            day = date.fromisoformat(value)
        except ValueError:
            day = None
        if day is not None:
            month = _MONTHS[day.month - 1]
            for name in (month, month[:3]):
                found += [
                    f"{name} {day.day}",
                    f"{name} {ordinal_digits(day.day)}",
                    f"{day.day} {name}",
                    f"{ordinal_digits(day.day)} of {name}",
                    f"{name} {day.day}, {day.year}",
                    f"{day.day} {name} {day.year}",
                ]
    elif literal.kind is LiteralKind.STR and isinstance(value, str):
        found.append(value)
    return [t for t in dict.fromkeys(found) if len(t.strip()) >= MIN_TERM_CHARS]


# -- the index -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtectedTerm:
    """One protected term: its folded form, what it protects (``fact`` or ``thunk`` and the
    id) and the proposition a hit claims (``None``: a candidate with no entity)."""

    folded: str
    ref_kind: str
    ref_id: str
    proposition: Proposition | None


def _phrase(term: str) -> str:
    return r"\s+".join(re.escape(part) for part in term.split())


class ProtectedIndex:
    """A viewpoint's protected terms and their matcher (see the module docstring)."""

    def __init__(self, terms: Iterable[ProtectedTerm], *, truncated: bool = False) -> None:
        by_folded: dict[str, ProtectedTerm] = {}
        for term in terms:
            if len(term.folded) >= MIN_TERM_CHARS:
                by_folded.setdefault(term.folded, term)
        self.terms: tuple[ProtectedTerm, ...] = tuple(by_folded.values())
        self.truncated = truncated
        self._by_folded = by_folded
        ordered = sorted(by_folded, key=lambda t: (-len(t), t))
        self._pattern = (
            re.compile(
                rf"(?<!{WORD_CHAR})(?:{'|'.join(_phrase(t) for t in ordered)})(?!{WORD_CHAR})"
            )
            if ordered
            else None
        )

    def __len__(self) -> int:
        return len(self.terms)

    @property
    def digest(self) -> str:
        """A hash of the terms and what they protect (the rebase re-check compares it)."""
        pairs = sorted((t.folded, t.ref_kind, t.ref_id) for t in self.terms)
        body = json.dumps([pairs, self.truncated], separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def match(self, value: str | None) -> list[tuple[int, int, ProtectedTerm]]:
        """Every whole-word folded match in ``value`` (offsets in its NFC form), in order."""
        if not value or self._pattern is None:
            return []
        text = nfc(value)
        folded, starts, ends = fold_offsets(text)
        found: list[tuple[int, int, ProtectedTerm]] = []
        for match in self._pattern.finditer(folded):
            a, b = match.span()
            if starts is None or ends is None:
                start, end = a, b
            else:
                if (a and starts[a - 1] == starts[a]) or (
                    b < len(starts) and starts[b] == starts[b - 1]
                ):
                    continue
                start, end = starts[a], ends[b - 1]
            term = self._by_folded.get(" ".join(match.group().split()))
            if term is not None:
                found.append((start, end, term))
        return found


def build_index(
    candidates: Sequence[ProtectedTerm], hidden: Sequence[ProtectedTerm], *, max_terms: int
) -> ProtectedIndex:
    """The index of ``candidates`` (thunk values, kept first) then ``hidden`` (canon, newest
    first), cut at ``max_terms`` distinct terms."""
    kept: dict[str, ProtectedTerm] = {}
    truncated = False
    for term in (*candidates, *hidden):
        if term.folded in kept or len(term.folded) < MIN_TERM_CHARS:
            continue
        if len(kept) >= max_terms:
            truncated = True
            break
        kept[term.folded] = term
    return ProtectedIndex(kept.values(), truncated=truncated)


def term(text: str, ref_kind: str, ref_id: str, proposition: Proposition | None) -> ProtectedTerm:
    return ProtectedTerm(" ".join(fold(text).split()), ref_kind, ref_id, proposition)


# -- loading ---------------------------------------------------------------------------------


def viewpoint_of(speaker: str, player_id: str | None = None) -> str:
    """The canon viewpoint of a speaker: ``character:<id>``, ``narrator`` or ``omniscient``."""
    from epistrel.core.resolution.entities import NARRATOR, PLAYER

    if speaker == NARRATOR:
        return "narrator"
    if speaker == OMNISCIENT:
        return OMNISCIENT
    if speaker == PLAYER:
        return OMNISCIENT if player_id is None else f"{CHARACTER_VIEWPOINT_PREFIX}{player_id}"
    return f"{CHARACTER_VIEWPOINT_PREFIX}{speaker}"


def _entities_of(p: Proposition) -> list[str]:
    found = [p.subject_id]
    if p.object_id is not None:
        found.append(p.object_id)
    found += list(p.object_set or ())
    found += [v for v in p.qualifiers.values() if isinstance(v, str)]
    return found


def _names(index: EntityIndex, entity_id: str) -> list[str]:
    entry = index.get(entity_id)
    if entry is None:
        return []
    return [entry.canonical_name, *(alias for alias, _ in entry.aliases)]


async def load_protected_index(
    conn: AsyncConnection,
    story_id: str,
    speaker: str,
    *,
    index: EntityIndex,
    through_position: tuple[int, int] | None = None,
    max_terms: int = DEFAULT_PROTECTED_INDEX_MAX,
    read_set: StructuredReadSet | None = None,
) -> ProtectedIndex | None:
    """``speaker``'s protected index (see the module docstring); ``None`` for an omniscient
    speaker (nothing is hidden from it). Remembered for ``read_set``'s rebase re-check."""
    from epistrel.core.canon import facts as canon
    from epistrel.core.commit.lifecycle import open_clause, thunk_columns, thunk_of
    from epistrel.core.tables import characters
    from epistrel.core.tables.commitments import thunks

    player = await conn.scalar(
        select(characters.c.id).where(
            characters.c.story_id == story_id, characters.c.is_player.is_(True)
        )
    )
    viewpoint = viewpoint_of(speaker, None if player is None else str(player))
    if viewpoint == OMNISCIENT:
        return None
    every = await canon.canon(conn, story_id, through_position=through_position)
    seen = await canon.canon(conn, story_id, through_position=through_position, viewpoint=viewpoint)
    seen_ids = {f.id for f in seen}
    cast = {
        str(c)
        for c in await conn.scalars(
            select(characters.c.id).where(characters.c.story_id == story_id)
        )
    }
    known = cast | {e for f in seen for e in _entities_of(f.proposition)}
    if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX):
        known.add(viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX))
    hidden_terms: list[ProtectedTerm] = []
    hidden = sorted(
        (f for f in every if f.id not in seen_ids), key=lambda f: (f.tx_from, f.id), reverse=True
    )
    for fact in hidden:
        p = fact.proposition
        texts: list[str] = []
        if p.literal is not None:
            texts += renderings(p.literal)
        for entity in _entities_of(p):
            if entity not in known:
                texts += _names(index, entity)
        hidden_terms += [term(t, "fact", fact.id, p) for t in texts]
    candidate_terms: list[ProtectedTerm] = []
    t = thunks.alias("t")
    rows = await conn.execute(
        select(*thunk_columns(t))
        .where(t.c.story_id == story_id, open_clause(t))
        .order_by(t.c.id.desc())
    )
    for thunk in (thunk_of(r) for r in rows):
        sketch = thunk.sketch
        for candidate in sketch.candidates:
            proposition = None
            if candidate.entity_id is not None:
                proposition = _filled(sketch, candidate.entity_id, thunk.valid_time)
            texts = [candidate.text] if candidate.text else []
            if candidate.entity_id is not None and candidate.entity_id not in known:
                texts += _names(index, candidate.entity_id)
            candidate_terms += [term(x, "thunk", thunk.id, proposition) for x in texts]
    built = build_index(candidate_terms, hidden_terms, max_terms=max_terms)
    if read_set is not None:
        _remember(
            read_set,
            _Built(read_set.attempt, story_id, speaker, through_position, max_terms, built.digest),
        )
    log.debug("protected index: %d terms (truncated=%s)", len(built), built.truncated)
    return built


def _filled(sketch: object, entity_id: str, valid_time: int | None) -> Proposition | None:
    from epistrel.core.commit.sketch import ThunkSketch
    from epistrel.core.enums import OpenSlot

    if not isinstance(sketch, ThunkSketch):  # pragma: no cover - typed
        return None
    when: Interval | Instant = (
        Interval.of(None, None)
        if valid_time is None
        else Instant.of(valid_time, Granularity.MINUTE)
    )
    subject = entity_id if sketch.open_slot is OpenSlot.SUBJECT else sketch.subject_id
    obj = entity_id if sketch.open_slot is OpenSlot.OBJECT else sketch.object_id
    if subject is None or sketch.open_slot is OpenSlot.LITERAL:
        return None
    try:
        return Proposition(
            subject_id=subject,
            predicate_id=sketch.predicate_id,
            object_id=obj,
            literal=sketch.literal,
            polarity=sketch.polarity,
            valid_time=when,
        )
    except ValueError:
        return None


# -- the rebase re-check ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Built:
    attempt: int
    story_id: str
    speaker: str
    through_position: tuple[int, int] | None
    max_terms: int
    digest: str


_BUILT: dict[int, tuple[weakref.ref[StructuredReadSet], list[_Built]]] = {}


def _remember(read_set: StructuredReadSet, built: _Built) -> None:
    key = id(read_set)
    entry = _BUILT.get(key)
    if entry is None or entry[0]() is not read_set:
        entry = (weakref.ref(read_set, lambda _: _BUILT.pop(key, None)), [])
        _BUILT[key] = entry
    entry[1].append(built)


def recorded(read_set: StructuredReadSet) -> list[_Built]:
    """The indexes the read set's current attempt built."""
    entry = _BUILT.get(id(read_set))
    if entry is None or entry[0]() is not read_set:
        return []
    return [b for b in entry[1] if b.attempt == read_set.attempt]


async def recheck(conn: AsyncConnection, op: Operation) -> bool:
    """The rebase re-check (``core.readset.REBASE_CHECKS``): every protected index the turn's
    current attempt built, rebuilt against current state; ``False`` (regenerate) when one
    changed."""
    from epistrel.core.resolution.context import load_index

    source = op.read_set_source
    if not isinstance(source, StructuredReadSet):
        return True
    for built in recorded(source):
        again = await load_protected_index(
            conn,
            built.story_id,
            built.speaker,
            index=await load_index(conn, built.story_id),
            through_position=built.through_position,
            max_terms=built.max_terms,
        )
        digest = None if again is None else again.digest
        if digest != built.digest:
            log.debug("rebase re-check: a protected index changed")
            return False
    return True


register_rebase_check(recheck)


__all__ = [
    "DEFAULT_PROTECTED_INDEX_MAX",
    "ProtectedIndex",
    "ProtectedTerm",
    "build_index",
    "load_protected_index",
    "number_words",
    "ordinal_words",
    "recheck",
    "recorded",
    "renderings",
    "term",
    "viewpoint_of",
]
