"""The hot set: its counters, overflow episodes, ranking, retirement and the vectors only hot
records keep (FR-MEM-8, NFR-SCALE-2, FR-STORE-30; docs/02 §4.2; #147).

A memory's **retrieval tier** (``hot``/``retired``) is independent of its timeline membership:
retirement takes a record out of what retrieval searches and keeps it in history, in keyed
lookups and in the audit views. The budgets are the story's profile dials ``memory.hot_set.*``
(prospective; the configuration the committing transaction reads under the story lock):

- a character's **unpinned hot memories** (``memories_per_character``, 2,000): active, hot,
  unpinned rows whose ``holder_character_id`` is the character. A holder-less record counts
  toward the story's vector budget only;
- the story's **unpinned hot vectors** (``vectors_per_story``, 50,000): active, hot, unpinned
  rows with a vector. A row waiting for its embedding does not count yet; the commit of the
  embed job that stamps it does (``after_vectors``).

Pinned memories (``core.memory.pins``) are outside both and admission-limited, so a story's hot
set is bounded by the budgets plus the pin ceilings. A quarantined memory still counts.

**Counters** (``stories.hot_counts``, ``HotCounts``): ``{characters: {id: n}, story_vectors:
n}``, kept in the transaction that changes them: a memory write (``MEMORY_HOOKS``), a vector
stamp, a pin or unpin and a retirement add or take their delta; a truncation, untruncation,
swap or regeneration (whose rows moved wholesale) recounts exactly. ``NULL`` means not computed
yet (a migrated, forked or erased story): the next reader recounts. An exact recount is two
index-only reads of the counted rows (``ix_memories_hot_unpinned``, ``ix_memories_hot_vectors``),
so its cost is the hot set's size, which is what the budgets bound. The daily reconcile
(``reconcile``, the orchestrator's ``hot_counts_reconcile`` task) recounts every story with
memories and reports a correction (``epistrel_hot_counts_corrected_total``).

**Overflow episodes** (``overflow_episodes``, ``evaluate``): the transaction that takes a set
from within its budget to over it opens one: ``opened_turn`` is the story's head turn (committed
turns of the active chain; a write positioned at the turn being committed counts as that turn),
``deadline_turn = opened_turn + memory.hot_set.retirement_max_lag`` (50) and never moves while
the episode lasts; ``EPISODE_HOOKS`` get ``opened`` (the orchestrator enqueues priority
consolidation and the episode's retirement job), and every later commit that adds to the excess
``overflowing`` (the retirement job again, coalesced). An episode closes when its set is back
within budget (``within_budget``: retirement, a truncation, an unpin or a raised budget) or when
the deadline's forced retirement brought it back (``forced``); a later overflow opens a new
one. Each open episode's current ``excess`` is what ``epistrel_hot_overflow{scope}`` sums.

**Ranking and retirement** (``ranked``, ``run_episode``): the candidates are the set's own rows
(a character's unpinned hot memories; the story's unpinned hot rows with a vector), ordered by
whether they are **retirable now** — covered by an active hot consolidation output
(``covered_by``, #143) or with ``decayed_importance()`` (#135) below
``memory.hot_set.importance_floor`` (0.2) — then lowest decayed importance, then oldest last
access (``last_accessed_at``, else ``created_at``), then id. The ranking is one statement that
keeps only the batch a run may retire (#444): Postgres computes the order, so a run's cost in
Python does not grow with the set. Before the deadline a run retires
only retirable records, as many as the excess, and leaves the episode open while it is still
over. In the commit whose turn reaches the deadline (``on_turn_committed``, a ``TURN_HOOKS``
hook, synchronously) the run also retires the lowest-ranked uncovered records,
``retired_reason = uncovered``, and ``EPISODE_HOOKS`` get ``forced`` (consolidation is queued to
cover them; a later cover turns the reason to ``covered``). One run retires at most
``RETIREMENT_BATCH_SIZE`` (500) records; a forced run with more to do signals ``follow_up``
(the retirement job finishes it). Retiring sets ``retrieval_tier = retired``,
``retired_reason``, ``retired_at`` and ``retired_turn``, and drops the vector and its
``embedding_model_version`` unless ``memory.hot_set.retain_retired_vectors``. Pinned records
are never candidates. Retirement writes no events: it is derivable maintenance state, run as a
``maintenance`` operation by the job or inside the turn's commit, and bumps no revision.

**Vectors only for active hot records** (FR-STORE-30; ``on_membership_change``,
``on_turn_committed``): when a truncation, swap or regeneration moves memories off the active
timeline their vectors are dropped in the same transaction unless
``memory.hot_set.retain_dormant_vectors``; when one brings memories back, those without a vector
are re-embedded by #138's embed job (``VECTOR_HOOKS``) with the deployment's pinned
embedding-model version. Rehydration (``core.retrieval.pipeline``) stores the vector it
computes only under ``retain_retired_vectors``, and never returns a record to the hot set: M4
does not re-promote a record that is re-accessed repeatedly (FR-MEM-8 allows it to stay
retired); pinning it (``core.memory.pins``) brings it back.

Everything is inert unless the story's configuration keeps episodic memory
(``memory.episodic.enabled``, #129): counters are still kept (a profile switch then finds them
right), but no episode opens and nothing retires. Logs carry identifiers and counts only
(invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    Integer,
    any_,
    case,
    cast,
    exists,
    func,
    insert,
    literal,
    null,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import timeline
from epistrel.core.config_changes import read_section
from epistrel.core.enums import Membership, RetrievalTier
from epistrel.core.epistemic import modeling
from epistrel.core.events.operation import register_turn_hook
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_only
from epistrel.core.memory import records
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.settings_schema import HotSetSettings
from epistrel.core.tables import stories
from epistrel.core.tables.hot_set import overflow_episodes
from epistrel.core.tables.memories import HOT_UNPINNED_WHERE, HOT_VECTORS_WHERE, memories
from epistrel.observability.metrics import RETIREMENTS

log = logging.getLogger(__name__)

Scope = Literal["character", "story"]
CHARACTER: Scope = "character"
STORY: Scope = "story"

COVERED = "covered"
FLOOR = "floor"
UNCOVERED = "uncovered"
WITHIN_BUDGET = "within_budget"
FORCED = "forced"

RETIREMENT_BATCH_SIZE = 500
"""The most records one retirement run retires (a forced run with more signals ``follow_up``)."""
RECENT_RETIREMENTS = 20
"""How many of the latest retirements ``GET /admin/stories/{id}/hot-set`` lists."""

_COUNT_BY_HOLDER = text(
    "SELECT holder_character_id AS holder, count(*) AS n FROM memories "  # noqa: S608
    f"WHERE story_id = :story AND {HOT_UNPINNED_WHERE} AND holder_character_id IS NOT NULL "
    "GROUP BY holder_character_id"
)
_COUNT_VECTORS = text(
    f"SELECT count(*) FROM memories WHERE story_id = :story AND {HOT_VECTORS_WHERE}"  # noqa: S608
)
_COUNTED = text(HOT_UNPINNED_WHERE)


# -- counters ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HotCounts:
    """A story's hot-set counters: each character's unpinned hot memories and the story's
    unpinned hot vectors."""

    characters: Mapping[str, int] = field(default_factory=dict)
    story_vectors: int = 0

    def document(self) -> dict[str, Any]:
        """The ``stories.hot_counts`` document (characters at zero left out)."""
        return {
            "characters": {c: n for c, n in sorted(self.characters.items()) if n > 0},
            "story_vectors": max(0, self.story_vectors),
        }

    @classmethod
    def of(cls, document: Mapping[str, Any]) -> HotCounts:
        characters = document.get("characters") or {}
        return cls(
            {str(c): int(n) for c, n in characters.items()},
            int(document.get("story_vectors") or 0),
        )

    def plus(self, characters: Mapping[str, int], vectors: int) -> HotCounts:
        merged = dict(self.characters)
        for character, n in characters.items():
            merged[character] = max(0, merged.get(character, 0) + n)
        return HotCounts(
            {c: n for c, n in merged.items() if n > 0}, max(0, self.story_vectors + vectors)
        )

    def count(self, scope: str, scope_id: str) -> int:
        return self.story_vectors if scope == STORY else int(self.characters.get(scope_id, 0))


async def exact_counts(conn: AsyncConnection, story_id: str) -> HotCounts:
    """The story's counters recounted from its rows (two index-only reads)."""
    holders = await conn.execute(_COUNT_BY_HOLDER, {"story": story_id})
    vectors = await conn.scalar(_COUNT_VECTORS, {"story": story_id})
    return HotCounts({str(r.holder): int(r.n) for r in holders}, int(vectors or 0))


async def _stored(conn: AsyncConnection, story_id: str) -> HotCounts | None:
    found = await conn.scalar(select(stories.c.hot_counts).where(stories.c.id == story_id))
    return None if found is None else HotCounts.of(found)


async def store_counts(conn: AsyncConnection, story_id: str, counts: HotCounts) -> None:
    await conn.execute(
        update(stories).where(stories.c.id == story_id).values(hot_counts=counts.document())
    )


async def invalidate(conn: AsyncConnection, story_id: str) -> None:
    """Mark the story's counters not computed (the next reader recounts): for a write that
    changed counted rows in ways it does not track itself (an erasure)."""
    await conn.execute(update(stories).where(stories.c.id == story_id).values(hot_counts=None))


async def counts(conn: AsyncConnection, story_id: str) -> HotCounts:
    """The story's counters: as stored, or recounted (and stored) when not computed yet."""
    found = await _stored(conn, story_id)
    if found is not None:
        return found
    exact = await exact_counts(conn, story_id)
    await store_counts(conn, story_id, exact)
    return exact


async def recount(conn: AsyncConnection, story_id: str) -> HotCounts:
    """Recount the story's counters exactly and store them."""
    exact = await exact_counts(conn, story_id)
    await store_counts(conn, story_id, exact)
    return exact


async def add(
    conn: AsyncConnection, story_id: str, characters: Mapping[str, int], vectors: int
) -> HotCounts:
    """Add a change the caller has just written to the story's counters (already counted when
    they were not computed yet, since the recount reads the rows as they now are)."""
    found = await _stored(conn, story_id)
    if found is None:
        return await recount(conn, story_id)
    changed = found.plus(characters, vectors)
    await store_counts(conn, story_id, changed)
    return changed


# -- budgets and the turn clock -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budgets:
    """The story's ``memory.hot_set.*`` dials."""

    memories_per_character: int
    vectors_per_story: int
    importance_floor: float
    max_lag: int
    retain_retired: bool
    retain_dormant: bool

    @classmethod
    def of(cls, settings: ProfileSettings) -> Budgets:
        h = settings.memory.hot_set
        return cls(
            memories_per_character=h.memories_per_character,
            vectors_per_story=h.vectors_per_story,
            importance_floor=h.importance_floor,
            max_lag=h.retirement_max_lag,
            retain_retired=h.retain_retired_vectors,
            retain_dormant=h.retain_dormant_vectors,
        )

    minors: frozenset[str] = frozenset()
    """The story's ``minor`` characters (#163), held to ``minor_fraction`` of the budget."""
    minor_fraction: float = 1.0

    def character_budget(self, character_id: str) -> int:
        """A character's unpinned hot budget: ``memories_per_character``, scaled by the
        deployment's ``minor_hot_budget_fraction`` for a ``minor`` (FR-EPI-10; at least 1)."""
        if character_id in self.minors:
            return max(1, int(self.memories_per_character * self.minor_fraction))
        return self.memories_per_character

    def budget(self, scope: str, scope_id: str | None = None) -> int:
        if scope == STORY:
            return self.vectors_per_story
        return self.memories_per_character if scope_id is None else self.character_budget(scope_id)


async def budgets_of(conn: AsyncConnection, story_id: str, settings: ProfileSettings) -> Budgets:
    """The story's budgets under ``settings``, its minors' scaled by the deployment's
    ``minor_hot_budget_fraction`` (#163)."""
    minors = await modeling.minor_ids(conn, story_id)
    if not minors:
        return Budgets.of(settings)
    caps = await read_section(conn, HotSetSettings)
    return replace(
        Budgets.of(settings), minors=minors, minor_fraction=caps.minor_hot_budget_fraction
    )


async def _settings(conn: AsyncConnection, story_id: str) -> ProfileSettings:
    return (await effective_configuration(conn, story_id, validate=False)).settings


async def head_turn(conn: AsyncConnection, story_id: str) -> int:
    """The story's head turn: its committed turns on the active chain (the chain is turns 1..k,
    one active variant each, FR-STORE-28)."""
    found = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    return max(0, int(found or 1) - 1)


# -- episodes --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Episode:
    """One ``overflow_episodes`` row."""

    id: str
    story_id: str
    scope: str
    scope_id: str
    opened_turn: int
    deadline_turn: int
    closed_turn: int | None
    closed_reason: str | None
    excess_at_open: int
    excess: int
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.closed_turn is None


def _episode(row: Any) -> Episode:
    return Episode(
        id=str(row.id),
        story_id=str(row.story_id),
        scope=str(row.scope),
        scope_id=str(row.scope_id),
        opened_turn=int(row.opened_turn),
        deadline_turn=int(row.deadline_turn),
        closed_turn=row.closed_turn,
        closed_reason=row.closed_reason,
        excess_at_open=int(row.excess_at_open),
        excess=int(row.excess),
        opened_at=row.opened_at,
        closed_at=row.closed_at,
    )


async def open_episodes(conn: AsyncConnection, story_id: str) -> list[Episode]:
    """The story's open episodes, oldest first."""
    e = overflow_episodes.c
    found = await conn.execute(
        select(overflow_episodes)
        .where(e.story_id == story_id, e.closed_turn.is_(None))
        .order_by(e.opened_turn, e.scope, e.scope_id)
    )
    return [_episode(r) for r in found]


async def episodes(conn: AsyncConnection, story_id: str) -> list[Episode]:
    """Every episode of the story, oldest first."""
    e = overflow_episodes.c
    found = await conn.execute(
        select(overflow_episodes)
        .where(e.story_id == story_id)
        .order_by(e.opened_turn, e.opened_at, e.scope, e.scope_id)
    )
    return [_episode(r) for r in found]


async def episode(conn: AsyncConnection, episode_id: str) -> Episode | None:
    row = (
        await conn.execute(select(overflow_episodes).where(overflow_episodes.c.id == episode_id))
    ).one_or_none()
    return None if row is None else _episode(row)


EpisodeSignal = Literal["opened", "overflowing", "forced", "follow_up"]
EpisodeHook = Callable[[AsyncConnection, Episode, EpisodeSignal], Awaitable[None]]
"""Called in the transaction that changed an episode: ``opened`` (priority consolidation and the
retirement job), ``overflowing`` (a later commit added to the excess: the retirement job again),
``forced`` (the deadline retired uncovered records: consolidation to cover them) and
``follow_up`` (a forced run left more to retire). The orchestrator registers the handler."""

EPISODE_HOOKS: list[EpisodeHook] = []


def register_episode_hook(hook: EpisodeHook) -> EpisodeHook:
    if hook not in EPISODE_HOOKS:
        EPISODE_HOOKS.append(hook)
    return hook


def remove_episode_hook(hook: EpisodeHook) -> None:
    if hook in EPISODE_HOOKS:
        EPISODE_HOOKS.remove(hook)


async def _signal(conn: AsyncConnection, ep: Episode, signal: EpisodeSignal) -> None:
    for hook in list(EPISODE_HOOKS):
        await hook(conn, ep, signal)


VectorHook = Callable[[AsyncConnection, str], Awaitable[None]]
"""Called in the transaction that left a story's active hot memories without vectors to be
re-embedded (a reactivation, a pin's rehydration): #138's embed job registers here."""

VECTOR_HOOKS: list[VectorHook] = []


def register_vector_hook(hook: VectorHook) -> VectorHook:
    if hook not in VECTOR_HOOKS:
        VECTOR_HOOKS.append(hook)
    return hook


async def needs_vectors(conn: AsyncConnection, story_id: str) -> None:
    for hook in list(VECTOR_HOOKS):
        await hook(conn, story_id)


async def _close(conn: AsyncConnection, ep: Episode, turn: int, reason: str) -> Episode:
    e = overflow_episodes.c
    await conn.execute(
        update(overflow_episodes)
        .where(e.id == ep.id, e.closed_turn.is_(None))
        .values(
            closed_turn=turn,
            closed_reason=reason,
            closed_at=func.transaction_timestamp(),
            excess=0,
        )
    )
    log.info(
        "hot-set episode closed story=%s scope=%s episode=%s reason=%s turn=%d",
        ep.story_id,
        ep.scope,
        ep.id,
        reason,
        turn,
    )
    return Episode(
        ep.id,
        ep.story_id,
        ep.scope,
        ep.scope_id,
        ep.opened_turn,
        ep.deadline_turn,
        turn,
        reason,
        ep.excess_at_open,
        0,
        ep.opened_at,
    )


async def _set_excess(conn: AsyncConnection, ep: Episode, excess: int) -> None:
    if excess != ep.excess:
        await conn.execute(
            update(overflow_episodes)
            .where(overflow_episodes.c.id == ep.id)
            .values(excess=max(0, excess))
        )


def overflow(counts: HotCounts, budgets: Budgets, story_id: str) -> dict[tuple[str, str], int]:
    """Every set over its budget, with its excess: ``{(scope, scope_id): excess}``."""
    found: dict[tuple[str, str], int] = {}
    for character, n in counts.characters.items():
        allowed = budgets.character_budget(character)
        if n > allowed:
            found[(CHARACTER, character)] = n - allowed
    if counts.story_vectors > budgets.vectors_per_story:
        found[(STORY, story_id)] = counts.story_vectors - budgets.vectors_per_story
    return found


async def evaluate(
    conn: AsyncConnection,
    story_id: str,
    counts: HotCounts,
    budgets: Budgets,
    turn: int,
    *,
    keep: Iterable[str] = (),
) -> list[Episode]:
    """Open an episode for every set over budget without one, update the open ones' excess
    (``overflowing`` when it grew) and close every open one whose set is back within budget
    (except ``keep``, which the caller closes itself). Answers the episodes opened."""
    over = overflow(counts, budgets, story_id)
    current = {(ep.scope, ep.scope_id): ep for ep in await open_episodes(conn, story_id)}
    opened: list[Episode] = []
    for (scope, scope_id), excess in sorted(over.items()):
        ep = current.get((scope, scope_id))
        if ep is None:
            ep = Episode(
                new_ulid(),
                story_id,
                scope,
                scope_id,
                turn,
                turn + budgets.max_lag,
                None,
                None,
                excess,
                excess,
            )
            await conn.execute(
                insert(overflow_episodes).values(
                    id=ep.id,
                    story_id=story_id,
                    scope=scope,
                    scope_id=scope_id,
                    opened_turn=ep.opened_turn,
                    deadline_turn=ep.deadline_turn,
                    excess_at_open=excess,
                    excess=excess,
                )
            )
            log.info(
                "hot-set episode opened story=%s scope=%s episode=%s excess=%d deadline=%d",
                story_id,
                scope,
                ep.id,
                excess,
                ep.deadline_turn,
            )
            opened.append(ep)
            await _signal(conn, ep, "opened")
            continue
        await _set_excess(conn, ep, excess)
        if excess > ep.excess:
            await _signal(conn, ep, "overflowing")
    kept = set(keep)
    for key, ep in current.items():
        if key not in over and ep.id not in kept:
            await _close(conn, ep, turn, WITHIN_BUDGET)
    return opened


# -- ranking and retirement ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One record a retirement may take: why it is retirable now (``covered``, ``floor`` or
    ``None``), its decayed importance and last access, its holder and whether it has a vector."""

    id: str
    holder: str | None
    has_vector: bool
    retirable: str | None
    decayed: float
    accessed: datetime

    @property
    def key(self) -> tuple[int, float, datetime, str]:
        return (0 if self.retirable else 1, self.decayed, self.accessed, self.id)


def _covered(m: Any) -> Any:
    o = memories.alias("covering")
    return exists(
        select(o.c.id).where(
            o.c.id == any_(m.covered_by),
            active_only(o),
            o.c.retrieval_tier == RetrievalTier.HOT.value,
        )
    )


async def ranked(
    conn: AsyncConnection,
    story_id: str,
    scope: str,
    scope_id: str,
    budgets: Budgets,
    *,
    now: int,
    half_life: float,
    limit: int | None = None,
) -> list[Candidate]:
    """The set's candidates in retirement order (see the module docstring), the first ``limit``
    of them (all with ``None``).

    The ranking is one statement (#444): the decayed importance (``records.decayed_importance``
    as an expression), the reason a record is retirable now and the order are computed by
    Postgres, which keeps only the first ``limit`` rows (a bounded top-N sort over the set's
    rows, read through its partial index), so a run brings no more rows into Python than it
    may retire."""
    if half_life <= 0:
        raise ValueError("an importance half-life is a positive number of turns")
    m = memories.c
    where: list[Any] = [m.story_id == story_id, _COUNTED]
    if scope == CHARACTER:
        where.append(m.holder_character_id == scope_id)
    else:
        where.append(m.embedding.is_not(None))
    value = func.coalesce(cast(m.importance, DOUBLE_PRECISION), records.DEFAULT_IMPORTANCE)
    age = func.greatest(0, literal(now, Integer) - func.coalesce(m.importance_at, now))
    decayed = value * func.power(
        literal(0.5, DOUBLE_PRECISION),
        cast(age, DOUBLE_PRECISION) / literal(float(half_life), DOUBLE_PRECISION),
    )
    rows = (
        select(
            m.id,
            m.holder_character_id.label("holder"),
            m.embedding.is_not(None).label("has_vector"),
            decayed.label("decayed"),
            func.coalesce(m.last_accessed_at, m.created_at).label("accessed"),
            _covered(m).label("covered"),
        )
        .where(*where)
        .subquery("candidates")
    )
    retirable = case(
        (rows.c.covered, COVERED),
        (rows.c.decayed < literal(budgets.importance_floor, DOUBLE_PRECISION), FLOOR),
        else_=null(),
    )
    query = (
        select(
            rows.c.id,
            rows.c.holder,
            rows.c.has_vector,
            rows.c.decayed,
            rows.c.accessed,
            retirable.label("retirable"),
        )
        .order_by(
            retirable.is_(None),
            rows.c.decayed,
            rows.c.accessed,
            rows.c.id.collate("C"),
        )
        .limit(limit)
    )
    return [
        Candidate(
            id=str(r.id),
            holder=None if r.holder is None else str(r.holder),
            has_vector=bool(r.has_vector),
            retirable=r.retirable,
            decayed=float(r.decayed),
            accessed=r.accessed,
        )
        for r in await conn.execute(query)
    ]


async def retire(
    conn: AsyncConnection,
    story_id: str,
    picks: Sequence[tuple[Candidate, str]],
    *,
    turn: int,
    retain_vectors: bool,
) -> tuple[dict[str, int], int]:
    """Retire ``picks`` (each with its reason), dropping their vectors unless
    ``retain_vectors``; answers the counters' change ``(characters, vectors)``."""
    m = memories.c
    by_reason: dict[str, list[str]] = {}
    for candidate, reason in picks:
        by_reason.setdefault(reason, []).append(candidate.id)
    for reason, ids in sorted(by_reason.items()):
        values: dict[str, Any] = {
            "retrieval_tier": RetrievalTier.RETIRED.value,
            "retired_reason": reason,
            "retired_at": func.transaction_timestamp(),
            "retired_turn": turn,
        }
        if not retain_vectors:
            values.update(embedding=None, embedding_model_version=None)
        await conn.execute(
            update(memories)
            .where(m.story_id == story_id, m.id.in_(sorted(ids)), _COUNTED)
            .values(**values)
        )
    characters: dict[str, int] = {}
    vectors = 0
    for candidate, _ in picks:
        if candidate.holder is not None:
            characters[candidate.holder] = characters.get(candidate.holder, 0) - 1
        if candidate.has_vector:
            vectors -= 1
    return characters, vectors


@dataclass(frozen=True, slots=True)
class EpisodeRun:
    """What one retirement run did for an episode: the records it retired by reason, whether it
    was forced (the deadline had come), whether it closed the episode, the excess left and
    whether a forced run left more to do."""

    episode_id: str
    retired: Mapping[str, int] = field(default_factory=dict)
    forced: bool = False
    closed: bool = False
    excess: int = 0
    more: bool = False
    skipped: str | None = None

    def result(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "retired": dict(self.retired),
            "forced": self.forced,
            "closed": self.closed,
            "excess": self.excess,
            "more": self.more,
            **({"skipped": self.skipped} if self.skipped else {}),
        }


async def run_episode(
    conn: AsyncConnection,
    story_id: str,
    episode_id: str,
    *,
    settings: ProfileSettings | None = None,
    limit: int = RETIREMENT_BATCH_SIZE,
) -> EpisodeRun:
    """One retirement run for an open episode, under the story lock (see the module
    docstring): a closed or unknown episode, or a story without episodic memory, is skipped."""
    ep = await episode(conn, episode_id)
    if ep is None or ep.story_id != story_id or not ep.open:
        return EpisodeRun(episode_id, skipped="closed")
    settings = settings or await _settings(conn, story_id)
    if not await records.episodic_on(conn, story_id, settings):
        return EpisodeRun(episode_id, skipped="module_off")
    budgets = await budgets_of(conn, story_id, settings)
    turn = await head_turn(conn, story_id)
    forced = turn >= ep.deadline_turn
    current = await recount(conn, story_id)
    budget = budgets.budget(ep.scope, ep.scope_id)
    excess = current.count(ep.scope, ep.scope_id) - budget
    retired: dict[str, int] = {}
    wanted = 0
    picks: list[tuple[Candidate, str]] = []
    if excess > 0:
        half_life = await records.half_life_turns(conn)
        wanted = min(excess, limit)
        candidates = await ranked(
            conn,
            story_id,
            ep.scope,
            ep.scope_id,
            budgets,
            now=turn,
            half_life=half_life,
            limit=wanted,
        )
        picks = [(c, c.retirable) for c in candidates if c.retirable][:wanted]
        if forced and len(picks) < wanted:
            uncovered = [c for c in candidates if not c.retirable][: wanted - len(picks)]
            picks.extend((c, UNCOVERED) for c in uncovered)
        if picks:
            characters, vectors = await retire(
                conn, story_id, picks, turn=turn, retain_vectors=budgets.retain_retired
            )
            current = current.plus(characters, vectors)
            await store_counts(conn, story_id, current)
            for _, reason in picks:
                retired[reason] = retired.get(reason, 0) + 1
            for reason, n in retired.items():
                RETIREMENTS.labels(reason=reason).inc(n)
        excess = current.count(ep.scope, ep.scope_id) - budget
    closed = excess <= 0
    more = forced and not closed and len(picks) == wanted and wanted == limit
    if closed:
        ep = await _close(conn, ep, turn, FORCED if UNCOVERED in retired else WITHIN_BUDGET)
    else:
        await _set_excess(conn, ep, excess)
    # The other sets may have moved too (a character's retirement drops the story's vectors).
    await evaluate(conn, story_id, current, budgets, turn, keep=() if closed else (ep.id,))
    if retired.get(UNCOVERED):
        await _signal(conn, ep, "forced")
    if more:
        await _signal(conn, ep, "follow_up")
    log.info(
        "hot-set retirement story=%s scope=%s episode=%s retired=%d forced=%s closed=%s",
        story_id,
        ep.scope,
        ep.id,
        sum(retired.values()),
        forced,
        closed,
    )
    return EpisodeRun(ep.id, retired, forced, closed, max(0, excess), more)


# -- the hooks -------------------------------------------------------------------------------------


async def on_memories_written(
    conn: AsyncConnection, story_id: str, memory_ids: tuple[str, ...]
) -> None:
    """``core.memory.records``' hook, in the writing transaction: count the new rows and
    evaluate the budgets when they changed a counter. A write of nothing counted (an episodic
    memory: shared, and without a vector until the embed job) costs one statement."""
    if not memory_ids:
        return
    m = memories.c
    found = (
        await conn.execute(
            select(
                m.holder_character_id,
                m.embedding.is_not(None).label("has_vector"),
                m.narrative_turn,
            ).where(m.story_id == story_id, m.id.in_(list(memory_ids)), _COUNTED)
        )
    ).all()
    characters: dict[str, int] = {}
    vectors = 0
    for r in found:
        if r.holder_character_id is not None:
            holder = str(r.holder_character_id)
            characters[holder] = characters.get(holder, 0) + 1
        vectors += 1 if r.has_vector else 0
    if not characters and not vectors:
        return
    current = await add(conn, story_id, characters, vectors)
    written = max((int(r.narrative_turn) for r in found if r.narrative_turn is not None), default=0)
    await reevaluate(conn, story_id, current, at_least=written)


async def reevaluate(
    conn: AsyncConnection,
    story_id: str,
    current: HotCounts,
    *,
    at_least: int = 0,
    settings: ProfileSettings | None = None,
) -> None:
    """``evaluate`` with the story's own budgets at its head turn (or ``at_least``), unless the
    story keeps no episodic memory."""
    settings = settings or await _settings(conn, story_id)
    if not await records.episodic_on(conn, story_id, settings):
        return
    turn = max(await head_turn(conn, story_id), at_least)
    await evaluate(conn, story_id, current, await budgets_of(conn, story_id, settings), turn)


async def counted_without_vectors(
    conn: AsyncConnection, story_id: str, memory_ids: Iterable[str]
) -> list[str]:
    """Of ``memory_ids``, the counted rows that have no vector yet (read before a vector
    stamp, so ``after_vectors`` knows which stamps add to the vector counter)."""
    ids = sorted(set(memory_ids))
    if not ids:
        return []
    m = memories.c
    found = await conn.scalars(
        select(m.id).where(m.story_id == story_id, m.id.in_(ids), _COUNTED, m.embedding.is_(None))
    )
    return [str(i) for i in found]


async def after_vectors(conn: AsyncConnection, story_id: str, newly: Sequence[str]) -> None:
    """After a vector stamp (#138's embed and re-embed jobs, in their commit): the counted rows
    of ``newly`` (``counted_without_vectors``) that have a vector now add to the story's vector
    counter, and the budgets are evaluated."""
    if not newly:
        return
    m = memories.c
    stamped = await conn.scalar(
        select(func.count())
        .select_from(memories)
        .where(m.story_id == story_id, m.id.in_(list(newly)), _COUNTED, m.embedding.is_not(None))
    )
    if not stamped:
        return
    current = await add(conn, story_id, {}, int(stamped))
    await reevaluate(conn, story_id, current)


async def _memories_of(conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]) -> bool:
    m = memories.c
    found = await conn.scalar(
        select(m.id).where(m.story_id == story_id, m.operation_id.in_(list(operation_ids))).limit(1)
    )
    return found is not None


async def _left_active(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str], budgets: Budgets
) -> None:
    """Memories of ``operation_ids`` left the active timeline: drop their vectors unless
    ``retain_dormant_vectors`` (FR-STORE-30)."""
    if budgets.retain_dormant:
        return
    m = memories.c
    await conn.execute(
        update(memories)
        .where(
            m.story_id == story_id,
            m.operation_id.in_(list(operation_ids)),
            ~active_only(memories),
            m.embedding.is_not(None),
        )
        .values(embedding=None, embedding_model_version=None)
    )


async def _came_back(conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]) -> None:
    """Memories of ``operation_ids`` are active again: those hot without a vector re-embed."""
    m = memories.c
    missing = await conn.scalar(
        select(m.id)
        .where(
            m.story_id == story_id,
            m.operation_id.in_(list(operation_ids)),
            active_only(memories),
            m.retrieval_tier == RetrievalTier.HOT.value,
            m.embedding.is_(None),
        )
        .limit(1)
    )
    if missing is not None:
        await needs_vectors(conn, story_id)


async def on_membership_change(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
    """In a truncation's, untruncation's or swap's transaction, after the derived rows followed
    their operations: their vectors follow FR-STORE-30, the counters are recounted and the
    episodes re-evaluated (one whose set is back within budget closes ``within_budget``; the
    others keep their deadlines)."""
    if not change.operation_ids or not await _memories_of(
        conn, change.story_id, change.operation_ids
    ):
        return
    settings = await _settings(conn, change.story_id)
    budgets = await budgets_of(conn, change.story_id, settings)
    if change.new is Membership.ACTIVE:
        await _came_back(conn, change.story_id, change.operation_ids)
    else:
        await _left_active(conn, change.story_id, change.operation_ids, budgets)
    current = await recount(conn, change.story_id)
    await reevaluate(conn, change.story_id, current, settings=settings)


async def on_turn_committed(
    conn: AsyncConnection, story_id: str, turn: int, made_dormant: tuple[str, ...]
) -> None:
    """In every turn's commit (``TURN_HOOKS``): what the turn made dormant (a regenerated
    variant, a shelved tail) follows FR-STORE-30 and is recounted; then every open episode whose
    deadline this turn reaches is enforced (forced retirement, synchronously). A turn of a story
    with nothing open and nothing made dormant costs one statement."""
    if made_dormant and await _memories_of(conn, story_id, made_dormant):
        settings = await _settings(conn, story_id)
        await _left_active(conn, story_id, made_dormant, await budgets_of(conn, story_id, settings))
        current = await recount(conn, story_id)
        await reevaluate(conn, story_id, current, settings=settings, at_least=turn)
    e = overflow_episodes.c
    due = list(
        await conn.scalars(
            select(e.id)
            .where(e.story_id == story_id, e.closed_turn.is_(None), e.deadline_turn <= turn)
            .order_by(e.deadline_turn, e.scope, e.scope_id)
        )
    )
    for episode_id in due:
        await run_episode(conn, story_id, str(episode_id))


# -- reads and the reconcile -----------------------------------------------------------------------


async def reconcile(conn: AsyncConnection, story_id: str) -> bool:
    """Recount the story's counters, store them, and evaluate the budgets; answers whether the
    stored counters were wrong (a missing document is not a correction)."""
    stored = await _stored(conn, story_id)
    exact = await recount(conn, story_id)
    corrected = stored is not None and stored.document() != exact.document()
    if corrected:
        log.warning("hot_counts_corrected story=%s", story_id)
    await reevaluate(conn, story_id, exact)
    return corrected


async def stories_with_memories(conn: AsyncConnection) -> list[str]:
    """Every story that has a memory or an open episode (the reconcile's fan-out)."""
    found = await conn.scalars(
        select(memories.c.story_id)
        .group_by(memories.c.story_id)
        .union(
            select(overflow_episodes.c.story_id).where(overflow_episodes.c.closed_turn.is_(None))
        )
    )
    return sorted({str(s) for s in found})


@dataclass(frozen=True, slots=True)
class Retirement:
    """One retired record as the admin view lists it (identifiers only)."""

    memory_id: str
    reason: str | None
    retired_turn: int | None
    retired_at: datetime | None
    holder_character_id: str | None


async def recent_retirements(
    conn: AsyncConnection, story_id: str, limit: int = RECENT_RETIREMENTS
) -> list[Retirement]:
    m = memories.c
    found = await conn.execute(
        select(m.id, m.retired_reason, m.retired_turn, m.retired_at, m.holder_character_id)
        .where(m.story_id == story_id, m.retrieval_tier == RetrievalTier.RETIRED.value)
        .order_by(m.retired_at.desc().nulls_last(), m.id.desc())
        .limit(limit)
    )
    return [
        Retirement(
            str(r.id),
            r.retired_reason,
            r.retired_turn,
            r.retired_at,
            None if r.holder_character_id is None else str(r.holder_character_id),
        )
        for r in found
    ]


records.register_memory_hook(on_memories_written)
timeline.on_membership_change(on_membership_change)
register_turn_hook(on_turn_committed)

__all__ = [
    "CHARACTER",
    "COVERED",
    "EPISODE_HOOKS",
    "FLOOR",
    "FORCED",
    "RECENT_RETIREMENTS",
    "RETIREMENT_BATCH_SIZE",
    "STORY",
    "UNCOVERED",
    "VECTOR_HOOKS",
    "WITHIN_BUDGET",
    "Budgets",
    "Candidate",
    "Episode",
    "EpisodeHook",
    "EpisodeRun",
    "EpisodeSignal",
    "HotCounts",
    "Retirement",
    "Scope",
    "add",
    "after_vectors",
    "counted_without_vectors",
    "counts",
    "episode",
    "episodes",
    "evaluate",
    "exact_counts",
    "head_turn",
    "invalidate",
    "needs_vectors",
    "on_membership_change",
    "on_memories_written",
    "on_turn_committed",
    "open_episodes",
    "overflow",
    "ranked",
    "recent_retirements",
    "reconcile",
    "recount",
    "reevaluate",
    "register_episode_hook",
    "register_vector_hook",
    "remove_episode_hook",
    "retire",
    "run_episode",
    "store_counts",
    "stories_with_memories",
]
