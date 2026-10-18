"""Author pins: memories that are never retired, admission-limited, and always in the structured
context (FR-MEM-8, FR-LITE-3; docs/02 §4.2; #147).

- **Pin and unpin** (``pin``, ``unpin``; ``POST|DELETE /stories/{id}/memories/{mid}/pin``):
  one position-less ``control`` operation each that bumps the story's revision (FR-CONC-1) and
  records ``memory_pinned``/``memory_unpinned``; ``pinned`` and ``pinned_by`` are set in its
  commit. Pinning a pinned memory, or unpinning one that is not, answers unchanged (no
  operation). Pinning a retired memory rehydrates it to the hot set (its vector is re-embedded
  by #138's embed job when it has none); pinning takes the memory out of the budgets' counts
  and unpinning puts it back, so an unpin above budget opens an overflow episode at its commit
  (``core.memory.retirement``). Any memory of the story may be pinned, in any membership (a
  dormant pin comes back pinned with its continuation). Pins are floor behaviour: they work
  under every profile; retirement itself is what the ``episodic`` module gates.
- **Admission** (``admit``): a character may hold at most the deployment's
  ``max_pinned_per_character`` (200) pinned memories (those it holds) and a story at most
  ``max_pinned_per_story`` (2,000): its pinned memories, in any membership, and its Lite text
  pins (#60's ``PUT /pinned-memories``) share that counter. Past either, 422
  ``pin_limit_exceeded`` with ``details {scope, limit, actual}`` (the character's limit is
  checked first, so the first limit hit names its scope); unpinning admits again. The check
  runs before the operation and again under the story lock, in its commit, so two concurrent
  pins never both take the last place. A text-pin edit that would cross the story ceiling is
  refused whole (``check_text_pins``).
- **Context** (``pinned_context``): a context source of #141's retrieval
  (``retrieval.pipeline.CONTEXT_SOURCES``, tier 1): each pinned, active memory positioned at or
  before the query, **quarantined pins left out** (#145: the source is outside retrieval's
  quarantine filter), as a ``pin`` item whose ``item_id`` is the memory's id (#145's implication
  hook implicates it as an author pin), by decayed importance (best first), then id. The pins'
  share of the token budget is ``assemble``'s. Retrieval runs only with episodic memory on, so
  a Lite prompt never sees it.

Logs carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.concurrency import check_expected_revision
from epistrel.core.config_changes import read_section
from epistrel.core.enums import Membership, OperationKind, Provenance, RetrievalTier
from epistrel.core.errors import MemoryNotFound, PinLimitExceeded
from epistrel.core.events import Operation
from epistrel.core.events.types import MemoryPinned, MemoryUnpinned
from epistrel.core.membership import active_only
from epistrel.core.memory import records, retirement
from epistrel.core.memory.records import Memory
from epistrel.core.retrieval.assemble import ContextItem, Tier
from epistrel.core.retrieval.pipeline import SourceRequest, register_context_source
from epistrel.core.settings_schema import HotSetSettings
from epistrel.core.stories import require_writable
from epistrel.core.tables import stories, story_texts
from epistrel.core.tables.memories import memories

if TYPE_CHECKING:
    from epistrel.core.mutation import MemoryWrite

log = logging.getLogger(__name__)

CHARACTER_CAP = "max_pinned_per_character"
STORY_CAP = "max_pinned_per_story"


# -- admission -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PinCounts:
    """What a story's pins hold: its pinned memories (all, and per holder) and its text pins."""

    memory_pins: int
    by_character: dict[str, int]
    text_pins: int

    @property
    def story_pins(self) -> int:
        return self.memory_pins + self.text_pins


async def text_pin_count(conn: AsyncConnection, story_id: str) -> int:
    """The story's Lite text pins (#60's ``story_texts`` projection)."""
    found = await conn.scalar(
        select(func.coalesce(func.jsonb_array_length(story_texts.c.pinned), 0)).where(
            story_texts.c.story_id == story_id
        )
    )
    return int(found or 0)


async def pin_counts(conn: AsyncConnection, story_id: str) -> PinCounts:
    """The story's pins, counted from the rows (``ix_memories_pinned``) and its text pins."""
    m = memories.c
    rows = await conn.execute(
        select(m.holder_character_id, func.count())
        .where(m.story_id == story_id, m.pinned.is_(True))
        .group_by(m.holder_character_id)
    )
    by_character: dict[str, int] = {}
    total = 0
    for holder, n in rows:
        total += int(n)
        if holder is not None:
            by_character[str(holder)] = int(n)
    return PinCounts(total, by_character, await text_pin_count(conn, story_id))


def _refused(scope: str, cap: str, limit: int, actual: int, **extra: str) -> PinLimitExceeded:
    return PinLimitExceeded(
        f"{cap} is {limit}; the pin would make it {actual}",
        {"scope": scope, "cap": cap, "limit": limit, "actual": actual, **extra},
    )


async def admit(
    conn: AsyncConnection,
    story_id: str,
    holder: str | None,
    *,
    caps: HotSetSettings | None = None,
) -> None:
    """Refuse one more memory pin held by ``holder`` past a ceiling (422
    ``pin_limit_exceeded``; the character's first)."""
    caps = caps or await read_section(conn, HotSetSettings)
    held = await pin_counts(conn, story_id)
    if holder is not None:
        mine = held.by_character.get(holder, 0) + 1
        if mine > caps.max_pinned_per_character:
            raise _refused(
                retirement.CHARACTER,
                CHARACTER_CAP,
                caps.max_pinned_per_character,
                mine,
                character_id=holder,
            )
    story = held.story_pins + 1
    if story > caps.max_pinned_per_story:
        raise _refused(retirement.STORY, STORY_CAP, caps.max_pinned_per_story, story)


async def check_text_pins(conn: AsyncConnection, story_id: str, items: int) -> None:
    """Refuse a text-pin edit that would leave the story with ``items`` text pins past
    ``max_pinned_per_story`` (its memory pins share the counter); 422 ``pin_limit_exceeded``."""
    caps = await read_section(conn, HotSetSettings)
    held = await pin_counts(conn, story_id)
    story = held.memory_pins + items
    if items > held.text_pins and story > caps.max_pinned_per_story:
        raise _refused(retirement.STORY, STORY_CAP, caps.max_pinned_per_story, story)


# -- pin and unpin -------------------------------------------------------------------------------


async def _memory(conn: AsyncConnection, story_id: str, memory_id: str) -> Memory:
    found = await records.memories_by_id(conn, story_id, [memory_id])
    if not found:
        raise MemoryNotFound(
            f"memory {memory_id} is not one of the story's",
            {"story_id": story_id, "memory_id": memory_id},
        )
    return found[0]


async def read_memory(db: AsyncEngine, story_id: str, memory_id: str) -> Memory:
    async with db.connect() as conn:
        return await _memory(conn, story_id, memory_id)


def _written(memory: Memory, revision: int, operation_id: str | None = None) -> MemoryWrite:
    """The answer ``core.mutation``'s memory primitives give: the memory as the admin reads it
    (its pin and tier included), the revision and the operation."""
    from epistrel.core.mutation import MemoryView, MemoryWrite  # here: it imports retrieval

    return MemoryWrite(MemoryView.of(memory), revision, operation_id)


def _counted(memory: Memory) -> bool:
    return (
        memory.retrieval_tier is RetrievalTier.HOT
        and memory.membership is Membership.ACTIVE
        and not memory.pinned
    )


def _delta(memory: Memory, sign: int) -> tuple[dict[str, int], int]:
    characters = {memory.holder_character_id: sign} if memory.holder_character_id else {}
    return characters, sign if memory.embedded else 0


def _control(
    db: AsyncEngine, story_id: str, principal: str, idempotency_key: str | None
) -> Operation:
    return Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    )


async def pin(
    db: AsyncEngine,
    story_id: str,
    memory_id: str,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> MemoryWrite:
    """Pin memory ``memory_id`` (see the module docstring)."""
    await require_writable(db, story_id)
    revision = await check_expected_revision(db, story_id, expected_revision)
    current = await read_memory(db, story_id, memory_id)
    if current.pinned:
        return _written(current, revision)
    async with db.connect() as conn:
        await admit(conn, story_id, current.holder_character_id)
    async with _control(db, story_id, principal, idempotency_key) as op:
        op.append("memory_pinned", MemoryPinned(memory_id=memory_id))

        async def step(conn: AsyncConnection) -> None:
            row = await _memory(conn, story_id, memory_id)
            if row.pinned:
                return
            await admit(conn, story_id, row.holder_character_id)
            await conn.execute(
                update(memories)
                .where(memories.c.story_id == story_id, memories.c.id == memory_id)
                .values(
                    pinned=True,
                    pinned_by=principal,
                    retrieval_tier=RetrievalTier.HOT.value,
                    retired_reason=None,
                    retired_at=None,
                    retired_turn=None,
                )
            )
            if _counted(row):
                characters, vectors = _delta(row, -1)
                current_counts = await retirement.add(conn, story_id, characters, vectors)
                await retirement.reevaluate(conn, story_id, current_counts)
            if (
                row.retrieval_tier is RetrievalTier.RETIRED
                and row.membership is Membership.ACTIVE
                and not row.embedded
            ):
                await retirement.needs_vectors(conn, story_id)

        op.add_commit_step(step)
        await op.commit()
    log.info("memory pinned story=%s memory=%s", story_id, memory_id)
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return _written(
        await read_memory(db, story_id, memory_id), int(op.timeline_revision), op.operation_id
    )


async def unpin(
    db: AsyncEngine,
    story_id: str,
    memory_id: str,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> MemoryWrite:
    """Remove a memory's pin (a no-op for a memory that is not pinned)."""
    await require_writable(db, story_id)
    revision = await check_expected_revision(db, story_id, expected_revision)
    current = await read_memory(db, story_id, memory_id)
    if not current.pinned:
        return _written(current, revision)
    async with _control(db, story_id, principal, idempotency_key) as op:
        op.append("memory_unpinned", MemoryUnpinned(memory_id=memory_id))

        async def step(conn: AsyncConnection) -> None:
            row = await _memory(conn, story_id, memory_id)
            if not row.pinned:
                return
            await conn.execute(
                update(memories)
                .where(memories.c.story_id == story_id, memories.c.id == memory_id)
                .values(pinned=False, pinned_by=None)
            )
            after = row.model_copy(update={"pinned": False})
            if _counted(after):
                characters, vectors = _delta(after, 1)
                current_counts = await retirement.add(conn, story_id, characters, vectors)
                await retirement.reevaluate(conn, story_id, current_counts)

        op.add_commit_step(step)
        await op.commit()
    log.info("memory unpinned story=%s memory=%s", story_id, memory_id)
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return _written(
        await read_memory(db, story_id, memory_id), int(op.timeline_revision), op.operation_id
    )


# -- the context source --------------------------------------------------------------------------


async def pinned_context(conn: AsyncConnection, request: SourceRequest) -> Sequence[ContextItem]:
    """The story's pinned memories as tier-1 ``pin`` items (see the module docstring)."""
    m = memories.c
    turn = request.position[0]
    rows = (
        await conn.execute(
            select(m.id, m.text, m.importance, m.importance_at)
            .where(
                m.story_id == request.story_id,
                m.pinned.is_(True),
                active_only(memories),
                m.quarantined_at.is_(None),
                or_(m.narrative_turn.is_(None), m.narrative_turn <= turn),
            )
            .order_by(m.id)
        )
    ).all()
    if not rows:
        return []
    half_life = await records.half_life_turns(conn)
    scored = sorted(
        rows,
        key=lambda r: (
            -records.decayed_importance(r.importance, r.importance_at, turn, half_life),
            str(r.id),
        ),
    )
    return [ContextItem(Tier.LEDGER_AND_PINS, "pin", str(r.id), r.text) for r in scored]


# -- the admin view ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HotSetView:
    """``GET /admin/stories/{id}/hot-set``: the story's counters (recounted now, and as
    stored), its budgets and pin ceilings, its pins, its open episodes and its latest
    retirements. Identifiers and counts only."""

    story_id: str
    episodic: bool
    counters: retirement.HotCounts
    stored: retirement.HotCounts | None
    budgets: retirement.Budgets
    caps: HotSetSettings
    pins: PinCounts
    head_turn: int
    timeline_revision: int
    open_episodes: tuple[retirement.Episode, ...] = ()
    recent_retirements: tuple[retirement.Retirement, ...] = ()


async def hot_set_view(db: AsyncEngine, story_id: str) -> HotSetView:
    """The story's hot set as ``GET /admin/stories/{id}/hot-set`` shows it (a read: the
    counters are recounted, never stored); 404 for an unknown story."""
    from epistrel.core.profiles.effective import effective_configuration

    revision = await check_expected_revision(db, story_id, None)
    async with db.connect() as conn:
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
        exact = await retirement.exact_counts(conn, story_id)
        stored = await conn.scalar(select(stories.c.hot_counts).where(stories.c.id == story_id))
        return HotSetView(
            story_id=story_id,
            episodic=await records.episodic_on(conn, story_id, settings),
            counters=exact,
            stored=None if stored is None else retirement.HotCounts.of(stored),
            budgets=retirement.Budgets.of(settings),
            caps=await read_section(conn, HotSetSettings),
            pins=await pin_counts(conn, story_id),
            head_turn=await retirement.head_turn(conn, story_id),
            timeline_revision=revision,
            open_episodes=tuple(await retirement.open_episodes(conn, story_id)),
            recent_retirements=tuple(await retirement.recent_retirements(conn, story_id)),
        )


register_context_source(pinned_context)

__all__ = [
    "CHARACTER_CAP",
    "STORY_CAP",
    "HotSetView",
    "PinCounts",
    "admit",
    "check_text_pins",
    "hot_set_view",
    "pin",
    "pin_counts",
    "pinned_context",
    "read_memory",
    "text_pin_count",
    "unpin",
]
