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

**A hidden identity** (#171, FR-PROP-7). A ``same_as`` fact hidden from the speaker protects no
single name (Jenni may say "the stranger", and may say "Bobby"): it protects their **pairing**.
Its leak pattern is a clause (text between ``.``, ``;``, ``!``, ``?`` or a line break) holding a
name or alias of one side and one of the other ("the stranger is Bobby", "Bobby, the stranger,
…"); the hit claims the ``same_as``. The speaker's known entities are expanded through the
``same_as`` it holds (depth 1; ``core.resolution.identity.speaker_identities``, the mention
resolver's helper too, #546: the facts its viewpoint reads and its own ``same_as`` beliefs): who
knows Bobby and holds that the stranger is Bobby knows the stranger, and a hidden identity the
speaker itself believes protects nothing for it.

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

**Belief-only records** (#172, U-22 M5, FR-VERIFY-13). Another character's first-order belief
(its partition, #159) is hidden from the speaker unless the speaker may read it as canon. Its
terms are the names and aliases of its **object** people (``BELIEF_TERM_KINDS``) the speaker has
no other way to know (never a common thing such as "the text", never its bare subject: Bobby
may say "Marcus"; "Dana", whom only Marcus's belief that he is seeing her names, is protected);
a hit claims the belief's proposition, so #144's permission check denies it and #151 repairs
it. A hit restating a beat's declared attribution ("Bobby thinks …") is that attribution.

**Private thoughts** (#172, FR-TURN-3, NFR-PRIV-1; the secondary control). A side index, outside
the digest and the rebase re-check (it changes only as turns commit, which a rebase already
regenerates): ``PrivateRecord``s, every other character's ``think`` (its stored ``think``
memories of the last ``THOUGHT_WINDOW`` turns, ``load_private_records``, and this turn's
``think`` beats, which the pipeline adds after each render) and, for the inner-state check
(``core.verify.inner_state``), the statements of their belief records. ``match_thoughts`` finds
a text that repeats a thought: its ``SHINGLE``-word shingles (fewer for a shorter thought) hit
at least twice (once for a thought of one shingle), or one run of at least ``RUN`` words (the
whole thought, if shorter), found by a sequence matcher over folded words. A thinker's own
thoughts are never matched, and neither is the player's own input.

Nothing here calls a model; logs carry counts only (invariant 3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import weakref
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
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
from epistrel.core.readset import KeyedLookup, StructuredReadSet, register_rebase_check
from epistrel.core.resolution.identity import pair_of, speaker_identities
from epistrel.core.settings_schema import (
    CHARACTER_VIEWPOINT_PREFIX,
    DEFAULT_PROTECTED_INDEX_MAX,
    OMNISCIENT,
)

if TYPE_CHECKING:
    from epistrel.core.canon.facts import FactRef
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


def _alternation(folded: Iterable[str]) -> re.Pattern[str] | None:
    ordered = sorted(set(folded), key=lambda t: (-len(t), t))
    if not ordered:
        return None
    return re.compile(
        rf"(?<!{WORD_CHAR})(?:{'|'.join(_phrase(t) for t in ordered)})(?!{WORD_CHAR})"
    )


CLAUSE_END = re.compile(r"[.;!?\n]")
"""What ends a clause for a hidden identity's leak pattern (#171)."""


@dataclass(frozen=True, slots=True)
class ProtectedPair:
    """A hidden ``same_as`` (#171): the folded names of each side, the fact it protects and its
    proposition (what a hit claims). A hidden State's value (#191) is a pair too: the State's
    name and aliases on one side, its value's renderings on the other (``ref_kind``
    ``state``)."""

    left: tuple[str, ...]
    right: tuple[str, ...]
    ref_id: str
    proposition: Proposition
    ref_kind: str = "fact"

    @property
    def folded(self) -> str:
        return " | ".join(self.left) + " = " + " | ".join(self.right)


class ProtectedIndex:
    """A viewpoint's protected terms and their matcher (see the module docstring)."""

    def __init__(
        self,
        terms: Iterable[ProtectedTerm],
        *,
        truncated: bool = False,
        pairs: Iterable[ProtectedPair] = (),
    ) -> None:
        by_folded: dict[str, ProtectedTerm] = {}
        for term in terms:
            if len(term.folded) >= MIN_TERM_CHARS:
                by_folded.setdefault(term.folded, term)
        self.terms: tuple[ProtectedTerm, ...] = tuple(by_folded.values())
        self.truncated = truncated
        self._by_folded = by_folded
        self._pattern = _alternation(by_folded)
        self.pairs: tuple[ProtectedPair, ...] = tuple(
            {p.ref_id: p for p in pairs if p.left and p.right}.values()
        )
        self._pair_patterns = [
            (pair, _alternation(pair.left), _alternation(pair.right)) for pair in self.pairs
        ]

    def __len__(self) -> int:
        return len(self.terms) + len(self.pairs)

    @property
    def digest(self) -> str:
        """A hash of the terms and what they protect (the rebase re-check compares it)."""
        pairs = sorted((t.folded, t.ref_kind, t.ref_id) for t in self.terms)
        identities = sorted((p.folded, p.ref_id) for p in self.pairs)
        body = json.dumps(
            [pairs, self.truncated, *([identities] if identities else [])], separators=(",", ":")
        )
        return hashlib.sha256(body.encode()).hexdigest()

    def match(self, value: str | None) -> list[tuple[int, int, ProtectedTerm]]:
        """Every whole-word folded match in ``value`` (offsets in its NFC form), in order, and
        every clause pairing a hidden identity's two sides (#171)."""
        if not value or (self._pattern is None and not self.pairs):
            return []
        text = nfc(value)
        folded, starts, ends = fold_offsets(text)

        def spans(pattern: re.Pattern[str] | None) -> list[tuple[int, int, str]]:
            out: list[tuple[int, int, str]] = []
            if pattern is None:
                return out
            for match in pattern.finditer(folded):
                a, b = match.span()
                if starts is None or ends is None:
                    start, end = a, b
                else:
                    if (a and starts[a - 1] == starts[a]) or (
                        b < len(starts) and starts[b] == starts[b - 1]
                    ):
                        continue
                    start, end = starts[a], ends[b - 1]
                out.append((start, end, " ".join(match.group().split())))
            return out

        found: list[tuple[int, int, ProtectedTerm]] = []
        for start, end, words in spans(self._pattern):
            term = self._by_folded.get(words)
            if term is not None:
                found.append((start, end, term))
        if self._pair_patterns:
            clause = [0]
            for m in CLAUSE_END.finditer(text):
                clause.append(m.end())

            def clause_of(at: int) -> int:
                return sum(1 for c in clause[1:] if c <= at)

            for pair, left, right in self._pair_patterns:
                lefts, rights = spans(left), spans(right)
                for a_start, a_end, _ in lefts:
                    partner = [
                        r
                        for r in rights
                        if clause_of(r[0]) == clause_of(a_start)
                        and (r[1] <= a_start or r[0] >= a_end)
                    ]
                    if not partner:
                        continue
                    start = min(a_start, *(r[0] for r in partner))
                    end = max(a_end, *(r[1] for r in partner))
                    found.append(
                        (
                            start,
                            end,
                            ProtectedTerm(
                                pair.folded, pair.ref_kind, pair.ref_id, pair.proposition
                            ),
                        )
                    )
                    break
        return sorted(found, key=lambda f: (f[0], f[1]))


def build_index(
    candidates: Sequence[ProtectedTerm],
    hidden: Sequence[ProtectedTerm],
    *,
    max_terms: int,
    pairs: Sequence[ProtectedPair] = (),
) -> ProtectedIndex:
    """The index of ``candidates`` (thunk values, kept first) then ``hidden`` (canon, newest
    first), cut at ``max_terms`` distinct terms, with the hidden identities' ``pairs``."""
    kept: dict[str, ProtectedTerm] = {}
    truncated = False
    for term in (*candidates, *hidden):
        if term.folded in kept or len(term.folded) < MIN_TERM_CHARS:
            continue
        if len(kept) >= max_terms:
            truncated = True
            break
        kept[term.folded] = term
    return ProtectedIndex(kept.values(), truncated=truncated, pairs=pairs)


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


CanonReads = dict[tuple[str, tuple[int, int] | None], list["FactRef"]]
"""Canon read once for several indexes (#452): ``(story_id, through_position)`` to the
omniscient read every viewpoint's index is cut from (``canon.canon_refs``)."""


async def load_protected_index(
    conn: AsyncConnection,
    story_id: str,
    speaker: str,
    *,
    index: EntityIndex,
    through_position: tuple[int, int] | None = None,
    max_terms: int = DEFAULT_PROTECTED_INDEX_MAX,
    read_set: StructuredReadSet | None = None,
    reads: CanonReads | None = None,
) -> ProtectedIndex | None:
    """``speaker``'s protected index (see the module docstring); ``None`` for an omniscient
    speaker (nothing is hidden from it). Remembered for ``read_set``'s rebase re-check.

    The story's canon is read once, omniscient through ``through_position``, as
    ``canon.canon_refs`` (ids, visibility and the entities named, #452), and the viewpoint's
    share of it kept with ``visible_to`` (``visible_clause``'s rule in Python, as
    ``hidden_from``); only the versions hidden from the viewpoint are then read in full.
    ``reads`` shares the first read between the indexes of one snapshot (a turn's speakers,
    the rebase re-check's builds)."""
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
    await canon.check_viewpoint(conn, story_id, viewpoint)
    key = (story_id, through_position)
    every = None if reads is None else reads.get(key)
    if every is None:
        every = await canon.canon_refs(conn, story_id, through_position=through_position)
        if reads is not None:
            reads[key] = every
    seen = [f for f in every if canon.visible_to(f.visibility, viewpoint)]
    seen_ids = {f.id for f in seen}
    cast = {
        str(c)
        for c in await conn.scalars(
            select(characters.c.id).where(characters.c.story_id == story_id)
        )
    }
    known = cast | {e for f in seen for e in f.entities}
    if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX):
        known.add(viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX))
    hidden_refs = sorted(
        (f for f in every if f.id not in seen_ids), key=lambda f: (f.tx_from, f.id), reverse=True
    )
    decoded = (
        {
            f.id: f
            for f in await canon.canon(
                conn,
                story_id,
                through_position=through_position,
                ids=[f.id for f in hidden_refs],
            )
        }
        if hidden_refs
        else {}
    )
    held: frozenset[tuple[str, str]] = frozenset()
    if hidden_refs:
        # #171: who holds that two entities are one knows either through the other (depth 1).
        # #546: what the speaker holds is ``speaker_identities``' (the mention resolver's too):
        # the ``same_as`` facts its viewpoint reads and its own ``same_as`` beliefs.
        identities = await speaker_identities(
            KeyedLookup(conn, read_set if read_set is not None else StructuredReadSet()),
            story_id,
            speaker,
            player_id=None if player is None else str(player),
            through_position=through_position,
            viewpoint=viewpoint,
        )
        held = frozenset(identities.pairs())
        known |= {b for a, b in held if a in known} | {a for a, b in held if b in known}
    hidden_terms: list[ProtectedTerm] = []
    pairs: list[ProtectedPair] = []
    for ref in hidden_refs:
        fact = decoded.get(ref.id)
        if fact is None:  # closed between the two reads: no longer canon
            continue
        p = fact.proposition
        identity = pair_of(p)
        if identity is not None and identity in held:
            continue  # #546: the speaker holds it (its own belief): nothing to protect
        if identity is not None:
            # A hidden identity protects the pairing of its sides, not either name (#171).
            left, right = (
                tuple(term(n, "fact", fact.id, p).folded for n in _names(index, e))
                for e in identity
            )
            pairs.append(ProtectedPair(left, right, fact.id, p))
            continue
        texts: list[str] = []
        if p.literal is not None:
            texts += renderings(p.literal)
        for entity in _entities_of(p):
            if entity not in known:
                texts += _names(index, entity)
        hidden_terms += [term(t, "fact", fact.id, p) for t in texts]
    hidden_terms += await _belief_terms(conn, story_id, viewpoint, known, index, cast)
    pairs += await state_pairs(conn, story_id, viewpoint, through_position=through_position)
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
    built = build_index(candidate_terms, hidden_terms, max_terms=max_terms, pairs=pairs)
    if read_set is not None:
        _remember(
            read_set,
            _Built(read_set.attempt, story_id, speaker, through_position, max_terms, built.digest),
        )
    log.debug("protected index: %d terms (truncated=%s)", len(built), built.truncated)
    return built


async def state_pairs(
    conn: AsyncConnection,
    story_id: str,
    viewpoint: str,
    *,
    through_position: tuple[int, int] | None = None,
) -> list[ProtectedPair]:
    """#191 (U-22 M6, FR-VERIFY-13): each hidden State's current value the viewpoint's
    speaker holds no belief about (``core.states.verify.hidden_values``: the player's character
    and the narrator hold none), as a pair: one of the State's names (its name, ``_`` as
    spaces, and its aliases) and one of its value's renderings in one clause. A hit claims the
    value's ``state_value``. A ``player_visible`` State is not protected."""
    from epistrel.core.membership import bound_of
    from epistrel.core.states.verify import hidden_values, value_renderings

    speaker = (
        viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
        if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX)
        else None
    )
    through = None if through_position is None else bound_of(through_position)
    found: list[ProtectedPair] = []
    for hidden in await hidden_values(conn, story_id, speaker, through=through):
        p = hidden.proposition
        left = tuple(dict.fromkeys(term(n, "state", "", p).folded for n in hidden.state.names))
        right = tuple(
            dict.fromkeys(term(v, "state", "", p).folded for v in value_renderings(hidden.value))
        )
        ref = f"state:{hidden.state.id}:{p.subject_id}"
        found.append(ProtectedPair(left, right, ref, p, "state"))
    return found


BELIEF_TERM_KINDS = frozenset({"person"})
"""The entity kinds a belief-only record protects by name (#172): people the speaker has no
way to know ("Dana"), never a common thing ("the text") the speaker may name for itself."""


async def _belief_terms(
    conn: AsyncConnection,
    story_id: str,
    viewpoint: str,
    known: set[str],
    index: EntityIndex,
    cast: set[str],
) -> list[ProtectedTerm]:
    """The terms of every other character's belief-only records (see the module docstring):
    their object entities' names the speaker has no other way to know, newest first."""
    from epistrel.core.epistemic import partitions

    speaker = (
        viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
        if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX)
        else None
    )
    # #522: only a belief whose object is one of these can give a term, so only those are
    # read (none at all when the speaker knows every person the index names): reading and
    # decoding every other character's whole partition, for every speaker's index, was the
    # bulk of a multi-character turn's CPU.
    unknown = sorted(
        entity
        for entity, entry in index.entries.items()
        if entry.kind in BELIEF_TERM_KINDS and entity not in known
    )
    found: list[ProtectedTerm] = []
    if not unknown:
        return found
    for holder in sorted(cast):
        if holder == speaker:
            continue
        held = await partitions.partition(conn, story_id, holder, objects_=unknown)
        for belief in sorted(held, key=lambda b: b.id, reverse=True):
            p = belief.proposition
            objects = [p.object_id] if p.object_id is not None else []
            objects += list(p.object_set or ())
            for entity in objects:
                entry = index.get(entity)
                if entity in known or entry is None or entry.kind not in BELIEF_TERM_KINDS:
                    continue
                found += [term(n, "belief", belief.id, p) for n in _names(index, entity)]
    return found


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


# -- private thoughts (#172) -----------------------------------------------------------------

SHINGLE = 4
"""The word shingle a private thought is matched by (#152's)."""
RUN = 6
"""A run of this many words of a thought (the whole thought, if shorter) is a hit on its own."""
THOUGHT_WINDOW = 50
"""How many turns back the stored ``think`` memories are read."""
THOUGHTS_MAX = 400
"""The most private records one turn's side index holds (newest first)."""
_WORDS = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)
_THINKS_PREFIX = re.compile(r"^[^:\n]{1,200}? thinks: ")


def folded_words(text: str) -> list[tuple[int, int, str]]:
    """``text``'s words, folded, with their offsets in ``text``."""
    return [
        (m.start(), m.end(), fold(m.group()).replace("\u2019", "'")) for m in _WORDS.finditer(text)
    ]


@dataclass(frozen=True, slots=True)
class PrivateRecord:
    """One private record of a character (see the module docstring): its holder, a reference
    (``memory:<id>``, ``beat:<n>`` or ``belief:<id>``), its kind (``think`` or ``belief``)
    and its text (a thought's words, a belief's statement)."""

    holder: str
    ref: str
    kind: str
    text: str
    words: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def of(cls, holder: str, ref: str, kind: str, text: str) -> PrivateRecord:
        body = _THINKS_PREFIX.sub("", text.strip()) if kind == "think" else text
        return cls(holder, ref, kind, body, tuple(w for _, _, w in folded_words(body)))


def _shingles(words: Sequence[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def match_thoughts(
    text: str | None, records: Sequence[PrivateRecord], *, speaker: str
) -> list[tuple[int, int, PrivateRecord]]:
    """Every other character's ``think`` that ``text`` repeats (see the module docstring), as
    ``(start, end, record)`` over the matched words; ``speaker``'s own are never matched."""
    if not text or not records:
        return []
    said = folded_words(text)
    words = [w for _, _, w in said]
    found: list[tuple[int, int, PrivateRecord]] = []
    for record in records:
        if record.kind != "think" or record.holder == speaker or len(record.words) < 2:
            continue
        n = min(SHINGLE, len(record.words))
        own = _shingles(record.words, n)
        hits = own & _shingles(words, n)
        matcher = SequenceMatcher(None, record.words, words, autojunk=False)
        blocks = [b for b in matcher.get_matching_blocks() if b.size]
        longest = max((b.size for b in blocks), default=0)
        if len(hits) < min(2, len(own)) and longest < min(RUN, len(record.words)):
            continue
        spans = [b for b in blocks if b.size >= min(n, len(record.words))] or blocks
        first = min(b.b for b in spans)
        last = max(b.b + b.size for b in spans) - 1
        found.append((said[first][0], said[last][1], record))
    return sorted(found, key=lambda f: (f[0], f[1]))


async def load_private_records(
    conn: AsyncConnection,
    story_id: str,
    turn: int,
    *,
    names: Mapping[str, str] | None = None,
    predicates: Mapping[str, str] | None = None,
) -> tuple[PrivateRecord, ...]:
    """The story's stored private records a turn's scan reads (see the module docstring): every
    character's active ``think`` memories of the last ``THOUGHT_WINDOW`` turns and, with
    ``names``/``predicates`` given, every character's belief statements."""
    from epistrel.core.epistemic import partitions
    from epistrel.core.membership import active_only
    from epistrel.core.tables import characters
    from epistrel.core.tables.memories import memories

    m = memories.c
    rows = await conn.execute(
        select(m.id, m.holder_character_id, m.text)
        .where(
            m.story_id == story_id,
            m.channel == "think",
            m.holder_character_id.is_not(None),
            m.narrative_turn >= turn - THOUGHT_WINDOW,
            m.narrative_turn <= turn,
            active_only(memories),
        )
        .order_by(m.narrative_turn.desc(), m.id.desc())
        .limit(THOUGHTS_MAX)
    )
    found = [
        PrivateRecord.of(str(r.holder_character_id), f"memory:{r.id}", "think", str(r.text))
        for r in rows
    ]
    if names is not None and predicates is not None:
        cast = [
            str(c)
            for c in await conn.scalars(
                select(characters.c.id).where(
                    characters.c.story_id == story_id, characters.c.is_player.is_(False)
                )
            )
        ]
        for holder in cast:
            for belief in await partitions.partition(conn, story_id, holder):
                p = belief.proposition
                words = [names.get(p.subject_id, ""), predicates.get(p.predicate_id, "")]
                if p.object_id is not None:
                    words.append(names.get(p.object_id, ""))
                if p.literal is not None:
                    words.append(str(p.literal.value))
                text = " ".join(w.replace("_", " ") for w in words if w)
                if text:
                    found.append(PrivateRecord.of(holder, f"belief:{belief.id}", "belief", text))
    return tuple(found[:THOUGHTS_MAX])


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
    changed.

    It runs under the story lock, so its cost is the lock's hold time (#452): each distinct
    build (story, speaker, position, cap) is rebuilt once and compared with every digest the
    attempt recorded for it (a turn builds the same index at its check, its composition and
    its repair re-checks), the entity index is read once per story, and the canon once per
    snapshot."""
    from epistrel.core.resolution.context import load_index

    source = op.read_set_source
    if not isinstance(source, StructuredReadSet):
        return True
    wanted: dict[tuple[str, str, tuple[int, int] | None, int], set[str]] = {}
    for built in recorded(source):
        key = (built.story_id, built.speaker, built.through_position, built.max_terms)
        wanted.setdefault(key, set()).add(built.digest)
    indexes: dict[str, EntityIndex] = {}
    reads: CanonReads = {}
    for (story_id, speaker, through_position, max_terms), digests in wanted.items():
        if story_id not in indexes:
            indexes[story_id] = await load_index(conn, story_id)
        again = await load_protected_index(
            conn,
            story_id,
            speaker,
            index=indexes[story_id],
            through_position=through_position,
            max_terms=max_terms,
            reads=reads,
        )
        digest = None if again is None else again.digest
        if digests != {digest}:
            log.debug("rebase re-check: a protected index changed")
            return False
    return True


register_rebase_check(recheck)


__all__ = [
    "DEFAULT_PROTECTED_INDEX_MAX",
    "CanonReads",
    "PrivateRecord",
    "ProtectedIndex",
    "ProtectedPair",
    "ProtectedTerm",
    "build_index",
    "folded_words",
    "load_private_records",
    "load_protected_index",
    "match_thoughts",
    "number_words",
    "ordinal_words",
    "recheck",
    "recorded",
    "renderings",
    "state_pairs",
    "term",
    "viewpoint_of",
]
