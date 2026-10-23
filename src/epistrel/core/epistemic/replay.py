"""Could-have-known by acquisition replay (FR-EPI-4, FR-BELIEF-2,11; docs/02 §4.4; #165).

A character may reference or know a fact at narrative position T only if replaying its dated
acquisition records up to T yields a belief in it. ``could_have_known(conn, story_id, character,
proposition, T)`` answers ``Known(path)`` (the earliest record that licenses it, usable as an
antecedent), ``NoPath(nearest)`` (no legitimate path by T; ``nearest`` is a path that exists only
after T, if any, for a repair's constraint) or ``NotApplicable`` (the gate is off). The callers
are #166's hallucination rule (``NoPath``: the claim is a hallucination, repaired), #176's
reactive bounds (a denied reaction; it passes ``min_level = believed``) and #154's thunk
reconciliation, through the soft flag (``core.commit.epistemic``).

**What counts as a path**, each matched on the proposition's normalized key (subject,
predicate, object and qualifiers, canonical direction) and its polarity:

- **a belief the character held at T** (the bitemporal read ``held_from ≤ T < held_to``: a
  belief closed by T does not count, a decayed one does), whose transitions up to T leave it at
  ``min_level`` or above (default ``suspected``). Every acquisition is a transition (#160):
  observation (seeing is believing, FR-BELIEF-2), testimony, inference, discovery, correction,
  revision, author direction (#173's seeds and author acts) and a thunk resolution's knowledge
  obligations (``resolution``, which holds from the thunk's time: its ``held_from``). A belief in
  ¬P licenses asking about P, never asserting it. A lazily resolved belief is in the record only
  from the turn that resolved it (#164's ``included_through``);
- **canon the character may read** at T (``public`` and ``objective`` facts, and those whose
  visibility lists it), read through ``core.readset.KeyedLookup`` (never ``facts`` itself:
  FR-EPI-1);
- **an implication** of what it held at T under the story's declared rules (#131's ``imply``,
  over its beliefs about the same subject, at the registry's depth);
- **an unresolved exposure** at or before T (#164): dry-run in its own frame by
  ``exposure.decide`` (the default, nothing persisted). It licenses when the default accepts at
  ``min_level`` or above (``Known(path = exposure)``).

An **envelope** perception (a whisper heard as a whisper, a language not understood) is an
observation of ``spoke_to``/``spoke`` only (``core.epistemic.observation``). It is no exposure and
no path to the words (FR-BELIEF-11): it may seed a low-confidence ``inference``, and a claim to
know the words finds ``NoPath`` until a genuine later acquisition (told, read) exists.

**Who.** The player's character is always ``Known`` (FR-EPI-12: the player knows what the player
knows). ``None`` (the narrator) is ``NotApplicable``, as is every character when the story's
``partitions`` module is off (callers treat ``NotApplicable`` as ``Known``: FR-EPI-4 is inert
without per-character partitions). An entity no character is (an unmodeled person) has no
partition: ``NoPath``.

**Pure core.** ``replay(acquisitions, key, polarity, at)`` decides from in-memory records, so the
COULD-HAVE-KNOWN property runs without a database. ``could_have_known`` gathers the records and
asks it.

**Cost.** The reads are bounded: the holder's beliefs about the proposition's subject and
predicate (one keyed ``beliefs`` read at T, then those beliefs' transitions), one keyed canon
read, the holder's beliefs about the subject at T for the implication closure, and its unresolved
exposures on the functional key before T (#164's cap bounds how many there can be). Every keyed
read joins the caller's read set. Within a ``checking()`` block (a turn's), answers are memoized
per (holder, proposition, T, level), and ``turn_report.epistemic.could_have_known`` counts the
checks and the ``no_path`` answers. ``epistrel_could_have_known_checks_total{outcome}`` counts
every answer. Same-turn staged beats and render steps (#174, #168) license only once those
stories' oracles exist; nested propositions (#167) replay against nested rows then.

Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import BeliefConfidenceLevel, ExposureState
from epistrel.core.epistemic import exposure as exposures
from epistrel.core.epistemic import partitions, testimony, transitions
from epistrel.core.membership import Position
from epistrel.core.propositions.codec import proposition_from_row
from epistrel.core.propositions.model import Instant, Polarity, Proposition
from epistrel.core.propositions.registry import Registry, canonical, imply, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX
from epistrel.core.tables import characters
from epistrel.observability.metrics import COULD_HAVE_KNOWN

log = logging.getLogger(__name__)

Level = BeliefConfidenceLevel
PathKind = Literal["transition", "canon", "implied", "exposure", "player"]
DEFAULT_MIN_LEVEL = Level.SUSPECTED
"""The weakest level that licenses a reference (#176 passes ``believed``)."""

KNOWN = "known"
NO_PATH = "no_path"
NOT_APPLICABLE = "not_applicable"


# -- answers -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathRef:
    """One acquisition path: what it is (``kind``), the record (a transition, a fact, the
    premise belief of an implication, an exposure; ``None`` for the player), the position it
    licenses from (``None``: position-less, before everything) and, for a transition, its
    kind."""

    kind: PathKind
    ref_id: str | None = None
    position: Position | None = None
    transition_kind: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "position": None if self.position is None else self.position.document(),
            "transition_kind": self.transition_kind,
        }


@dataclass(frozen=True, slots=True)
class Known:
    """A legitimate path by T: the earliest record that licenses the reference."""

    path: PathRef


@dataclass(frozen=True, slots=True)
class NoPath:
    """No legitimate path by T; ``nearest`` is the earliest path that exists only after T."""

    nearest: PathRef | None = None


@dataclass(frozen=True, slots=True)
class NotApplicable:
    """The check does not apply (``reason``: ``narrator`` or ``partitions_off``); callers treat
    it as ``Known``."""

    reason: str


Answer = Known | NoPath | NotApplicable


def outcome_of(answer: Answer) -> str:
    if isinstance(answer, Known):
        return KNOWN
    if isinstance(answer, NoPath):
        return NO_PATH
    return NOT_APPLICABLE


# -- the pure replay -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Acquisition:
    """One dated acquisition record as the replay reads it: the proposition key and polarity it
    is about, the level it leaves the holder at, the position from which it holds (``None``:
    before everything), the position at which its lineage stops holding (``until``: a closed
    belief's ``held_to``) and its lineage (a belief id: a later record of the same lineage
    supersedes an earlier one's level, as a forgetting or a revision does)."""

    key: tuple[Any, ...]
    polarity: Polarity
    level: Level
    since: Position | None
    ref: PathRef
    until: Position | None = None
    lineage: str = ""


def _rank(level: Level) -> int:
    return partitions.LEVELS.index(Level(level))


def _at_or_before(since: Position | None, at: Position) -> bool:
    return since is None or since.at_or_before(at)


def _order(a: Acquisition) -> tuple[tuple[int, ...], str]:
    return ((-1,) if a.since is None else a.since.key(), a.ref.ref_id or "")


def replay(
    acquisitions: Iterable[Acquisition],
    key: tuple[Any, ...],
    polarity: Polarity,
    at: Position,
    *,
    min_level: Level = DEFAULT_MIN_LEVEL,
) -> Known | NoPath:
    """Whether ``acquisitions`` yield a belief in (``key``, ``polarity``) at ``at`` at
    ``min_level`` or above (see the module docstring). Each lineage's state at ``at`` is its
    latest record at or before ``at`` (unless the lineage stopped holding by then); the answer's
    path is the earliest record of a licensing lineage that reached ``min_level`` and that every
    later record up to ``at`` kept there. ``NoPath.nearest`` is the earliest licensing record
    after ``at``."""
    floor = _rank(min_level)
    matching = sorted(
        (a for a in acquisitions if a.key == key and a.polarity is polarity), key=_order
    )
    by_lineage: dict[str, list[Acquisition]] = {}
    for a in matching:
        by_lineage.setdefault(a.lineage or (a.ref.ref_id or ""), []).append(a)
    found: list[Acquisition] = []
    for records in by_lineage.values():
        upto = [a for a in records if _at_or_before(a.since, at)]
        if not upto:
            continue
        if any(a.until is not None and not at.before(a.until) for a in upto):
            continue
        if _rank(upto[-1].level) < floor:
            continue
        start = len(upto) - 1
        while start > 0 and _rank(upto[start - 1].level) >= floor:
            start -= 1
        found.append(upto[start])
    if found:
        return Known(min(found, key=_order).ref)
    later = [
        a
        for a in matching
        if a.since is not None and at.before(a.since) and _rank(a.level) >= floor
    ]
    return NoPath(min(later, key=_order).ref if later else None)


# -- the turn's checks ---------------------------------------------------------------------------


@dataclass(slots=True)
class Checks:
    """The could-have-known checks one turn made (``turn_report.epistemic.could_have_known``)
    and its memo."""

    checks: int = 0
    no_path: int = 0
    memo: dict[tuple[Any, ...], Answer] = field(default_factory=dict)

    def report(self) -> dict[str, int]:
        return {"checks": self.checks, "no_path": self.no_path}


_CHECKS: ContextVar[Checks | None] = ContextVar("epistrel_could_have_known", default=None)


@contextmanager
def checking() -> Iterator[Checks]:
    """Within the block, every ``could_have_known`` answer is counted in the yielded ``Checks``
    and memoized per (holder, proposition, T, level)."""
    found = Checks()
    token = _CHECKS.set(found)
    try:
        yield found
    finally:
        _CHECKS.reset(token)


def _answered(memo_key: tuple[Any, ...] | None, answer: Answer) -> Answer:
    COULD_HAVE_KNOWN.labels(outcome=outcome_of(answer)).inc()
    checks = _CHECKS.get()
    if checks is not None:
        checks.checks += 1
        if isinstance(answer, NoPath):
            checks.no_path += 1
        if memo_key is not None:
            checks.memo[memo_key] = answer
    return answer


# -- gathering the records -----------------------------------------------------------------------


def _key(proposition: Proposition) -> tuple[Any, ...]:
    return proposition.normalized_key()


def _window(p: Proposition) -> tuple[int | None, int | None] | None:
    vt = p.valid_time
    if isinstance(vt, Instant):
        return (vt.at, vt.until)
    start = None if vt.start is None else vt.start.outer
    end = None if vt.end is None else vt.end.outer
    return None if start is None and end is None else (start, end)


def _since(belief: partitions.Belief, kind: transitions.Kind, at: Position) -> Position:
    """Where a transition's acquisition holds from: a backdating kind (a resolution, an author
    act) and a lazy resolution hold from the belief's ``held_from``, the rest from their own
    position (``at``)."""
    if kind in transitions.BACKDATING_KINDS or belief.resolution_operation_id is not None:
        return belief.held_from
    return at


async def _belief_records(
    conn: AsyncConnection,
    lookup: KeyedLookup,
    story_id: str,
    holder: str,
    target: Proposition,
    at: Position,
) -> list[Acquisition]:
    """The holder's beliefs in the target's key, held at ``at`` and now, with their transitions
    as dated records."""
    held: dict[str, partitions.Belief] = {}
    for through in (at, None):
        for b in await lookup.beliefs(
            story_id,
            holder=holder,
            predicate=target.predicate_id,
            subjects=target.subject_id,
            object=target.object_id,
            through=through,
        ):
            if _key(b.proposition) == _key(target):
                held.setdefault(b.id, b)
    if not held:
        return []
    found: list[Acquisition] = []
    for t in await transitions.transitions_of(conn, story_id, holder=holder, belief_ids=list(held)):
        belief = held[t.belief_id]
        found.append(
            Acquisition(
                key=_key(belief.proposition),
                polarity=Polarity(belief.polarity),
                level=t.confidence_level,
                since=_since(belief, t.kind, t.position),
                until=belief.held_to,
                lineage=belief.id,
                ref=PathRef(
                    "transition",
                    t.id,
                    _since(belief, t.kind, t.position),
                    t.kind.value,
                ),
            )
        )
    return found


async def _canon(
    lookup: KeyedLookup, story_id: str, holder: str, target: Proposition, at: Position
) -> Known | None:
    facts = await lookup.canon_at(
        story_id,
        predicate=target.predicate_id,
        subjects=target.subject_id,
        object=target.object_id if target.object_id is not None else target.literal,
        window=_window(target),
        viewpoint=f"{CHARACTER_VIEWPOINT_PREFIX}{holder}",
        through_position=at,
    )
    for f in facts:
        if _key(f.proposition) == _key(target) and f.proposition.polarity is target.polarity:
            return Known(PathRef("canon", f.id, None))
    return None


async def _implied(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    target: Proposition,
    at: Position,
    registry: Registry,
    min_level: Level,
) -> Known | None:
    rules = registry.rules()
    if not rules:
        return None
    premises = await partitions.partition(
        conn,
        story_id,
        holder,
        through=at,
        subjects=[target.subject_id],
        levels=partitions.at_least(min_level),
    )
    affirmed = [b for b in premises if Polarity(b.polarity) is Polarity.AFFIRMED]
    if not affirmed:
        return None
    closure = imply([b.proposition for b in affirmed], rules)
    for d in closure.derived:
        if _key(canonical(d, registry)) == _key(target) and d.polarity is target.polarity:
            return Known(PathRef("implied", affirmed[0].id, affirmed[0].held_from))
    return None


async def _exposed(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    target: Proposition,
    at: Position,
    registry: Registry,
    min_level: Level,
    read_set: StructuredReadSet,
) -> tuple[Known | None, PathRef | None]:
    """An unresolved exposure at or before ``at`` the default would accept (``Known``), and the
    earliest after ``at`` (the nearest path)."""
    floor = _rank(min_level)
    later: PathRef | None = None
    for e in await exposures.exposures_of(
        conn, story_id, holder, state=ExposureState.UNRESOLVED, as_of_turn=at.turn
    ):
        stored = canonical(e.proposition, registry)
        if _key(stored) != _key(target) or stored.polarity is not target.polarity:
            continue
        if at.before(e.position):
            later = later or PathRef("exposure", e.id, e.position)
            continue
        outcome = await exposures.decide(conn, e, registry=registry, read_set=read_set)
        if (
            outcome.decision == testimony.ACCEPT
            and outcome.confidence is not None
            and _rank(outcome.confidence) >= floor
        ):
            return Known(PathRef("exposure", e.id, e.position)), None
    return None, later


async def _character(conn: AsyncConnection, story_id: str, character_id: str) -> Any:
    return (
        await conn.execute(
            select(characters.c.id, characters.c.is_player).where(
                characters.c.story_id == story_id, characters.c.id == character_id
            )
        )
    ).one_or_none()


async def could_have_known(
    conn: AsyncConnection,
    story_id: str,
    character_id: str | None,
    proposition: Proposition,
    at: Position,
    *,
    min_level: Level = DEFAULT_MIN_LEVEL,
    settings: Any = None,
    registry: Registry | None = None,
    read_set: StructuredReadSet | None = None,
) -> Answer:
    """Whether ``character_id`` could have known ``proposition`` at ``at``: see the module
    docstring."""
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration

    if character_id is None:
        return _answered(None, NotApplicable("narrator"))
    current = settings
    if current is None:
        current = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(current, transitions.PARTITIONS_MODULE):
        return _answered(None, NotApplicable("partitions_off"))
    found = registry or await load(conn, story_id)
    target = canonical(proposition, found)
    memo_key = (story_id, character_id, _key(target), target.polarity, at.key(), min_level)
    checks = _CHECKS.get()
    if checks is not None and memo_key in checks.memo:
        return checks.memo[memo_key]
    row = await _character(conn, story_id, character_id)
    if row is None:
        return _answered(memo_key, NoPath())
    if bool(row.is_player):
        return _answered(memo_key, Known(PathRef("player")))
    reads = read_set if read_set is not None else StructuredReadSet()
    lookup = KeyedLookup(conn, reads)
    records = await _belief_records(conn, lookup, story_id, character_id, target, at)
    answer: Known | NoPath = replay(records, _key(target), target.polarity, at, min_level=min_level)
    if isinstance(answer, NoPath):
        answer = (await _canon(lookup, story_id, character_id, target, at)) or answer
    if isinstance(answer, NoPath):
        implied = await _implied(conn, story_id, character_id, target, at, found, min_level)
        answer = implied or answer
    if isinstance(answer, NoPath):
        exposed, later = await _exposed(
            conn, story_id, character_id, target, at, found, min_level, reads
        )
        if exposed is not None:
            answer = exposed
        elif answer.nearest is None and later is not None:
            answer = NoPath(later)
    log.debug("could_have_known: %s for %s", outcome_of(answer), character_id)
    return _answered(memo_key, answer)


async def _stated(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    target: Proposition,
    at: Position,
    registry: Registry,
    *,
    own: bool,
) -> bool:
    """Whether a proposition with ``target``'s key and polarity is held (another character's
    belief, ``own`` false) or was said (an utterance: by another speaker, or by
    ``character_id`` itself when ``own``) by ``at``, on the active timeline."""
    from sqlalchemy import and_, or_

    from epistrel.core.membership import active_only, position_key, position_literal
    from epistrel.core.tables import beliefs, propositions, utterances

    key = _key(target)
    p = propositions.c
    found: list[Any] = []
    if not own:
        b = beliefs.c
        found += list(
            await conn.execute(
                select(propositions)
                .select_from(beliefs.join(propositions, p.id == b.proposition_id))
                .where(
                    b.story_id == story_id,
                    b.holder_character_id != character_id,
                    active_only(beliefs),
                    b.tx_to.is_(None),
                    p.subject_id == target.subject_id,
                    p.predicate_id == target.predicate_id,
                    position_key(beliefs, "held_from") <= position_literal(at),
                    or_(
                        b.held_to_turn.is_(None),
                        position_key(beliefs, "held_to") > position_literal(at),
                    ),
                )
            )
        )
    u = utterances.c
    speaker = (
        u.speaker_character_id == character_id if own else u.speaker_character_id != character_id
    )
    found += list(
        await conn.execute(
            select(propositions)
            .select_from(utterances.join(propositions, p.id == func.any(u.proposition_ids)))
            .where(
                u.story_id == story_id,
                speaker,
                active_only(utterances),
                and_(u.narrative_turn.is_not(None), u.narrative_turn <= at.turn),
                p.subject_id == target.subject_id,
                p.predicate_id == target.predicate_id,
                *(
                    [
                        u.intent.in_(("sincere", "unspecified")),
                        u.classification.is_distinct_from("hallucination_repaired"),
                    ]
                    if own
                    else []
                ),
            )
        )
    )
    for row in found:
        stored = canonical(proposition_from_row(row), registry)
        if _key(stored) == key and stored.polarity is target.polarity:
            return True
    return False


async def held_elsewhere(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    proposition: Proposition,
    at: Position,
    *,
    registry: Registry | None = None,
) -> bool:
    """Whether the record holds ``proposition`` beyond ``character_id``'s own knowledge at
    ``at`` (#166): another character's belief (first-order or ``said``) held by then, or another
    speaker's utterance of it by then's turn, on the active timeline. A first-person claim of
    such withheld content needs a path of the speaker's own (FR-BELIEF-11: a whisper's words
    overheard only as an envelope); content no one holds is new, and is not asked about."""
    found = registry or await load(conn, story_id)
    target = canonical(proposition, found)
    return await _stated(conn, story_id, character_id, target, at, found, own=False)


async def said_before(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    proposition: Proposition,
    at: Position,
    *,
    registry: Registry | None = None,
) -> bool:
    """Whether ``character_id`` itself sincerely said ``proposition`` by ``at``'s turn (an
    utterance not repaired away): repeating one's own earlier word is no new claim (#166)."""
    found = registry or await load(conn, story_id)
    target = canonical(proposition, found)
    return await _stated(conn, story_id, character_id, target, at, found, own=True)


def acquisitions_of(beliefs_: Sequence[partitions.Belief]) -> list[Acquisition]:
    """The beliefs as single-record lineages at their ``held_from`` (a convenience for callers
    holding rows already, such as a context assembly)."""
    return [
        Acquisition(
            key=_key(b.proposition),
            polarity=Polarity(b.polarity),
            level=b.confidence_level,
            since=b.held_from,
            until=b.held_to,
            lineage=b.id,
            ref=PathRef("transition", b.provenance, b.held_from),
        )
        for b in beliefs_
    ]


__all__ = [
    "DEFAULT_MIN_LEVEL",
    "KNOWN",
    "NOT_APPLICABLE",
    "NO_PATH",
    "Acquisition",
    "Answer",
    "Checks",
    "Known",
    "NoPath",
    "NotApplicable",
    "PathRef",
    "acquisitions_of",
    "checking",
    "could_have_known",
    "held_elsewhere",
    "outcome_of",
    "replay",
    "said_before",
]
