"""The two ways projections are written: incrementally in the commit, and by refolding the log.

``apply_events`` runs inside ``Operation.commit()``'s transaction, before COMMIT. For each
projection it loads the rows the new events can touch, applies the events, and writes only the
difference. ``fold`` replays every event of the story's committed operations in ``seq`` order
through the same ``apply``, in memory, and ``rebuild`` replaces the stored rows with the result
under the story lock as a ``maintenance`` operation, so it bumps no revision (FR-CONC-1) and
writes no event.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, and_, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.enums import (
    EventCategory,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
)
from epistrel.core.errors import StoryNotFound
from epistrel.core.events.registry import validate
from epistrel.core.projections.base import Key, LogEvent, Row, Rows, registered_projections
from epistrel.core.tables import events, operations, stories


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """What a rebuild wrote: the row count per projection table, its operation, and the story's
    timeline revision (unchanged by the rebuild)."""

    operation_id: str
    counts: dict[str, int] = field(default_factory=dict)
    timeline_revision: int = 0


async def apply_events(
    conn: AsyncConnection, story_id: str, new_events: Sequence[LogEvent]
) -> None:
    """Maintain every projection for ``new_events`` inside the caller's transaction."""
    if not new_events:
        return
    for projection in registered_projections():
        rows = await projection.load(conn, story_id, new_events)
        before = rows.snapshot()
        for event in new_events:
            projection.apply(rows, event)
        await _write_difference(conn, rows, before)


async def fold(conn: AsyncConnection, story_id: str) -> dict[str, Rows]:
    """Every projection of ``story_id`` as the log says it should be (nothing is written)."""
    found = await conn.execute(
        select(events, operations.c.kind, operations.c.membership)
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(
            events.c.story_id == story_id,
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        .order_by(events.c.seq)
    )
    log = [
        LogEvent(
            id=row.id,
            story_id=row.story_id,
            seq=row.seq,
            operation_id=row.operation_id,
            kind=OperationKind(row.kind),
            membership=Membership(row.membership),
            category=EventCategory(row.category),
            narrative_turn=row.narrative_turn,
            turn_variant=row.turn_variant,
            beat=row.beat,
            character_id=row.character_id,
            provenance=Provenance(row.provenance),
            type=row.type,
            payload=validate(row.type, row.payload),
        )
        for row in found
    ]
    folded: dict[str, Rows] = {}
    for projection in registered_projections():
        rows = Rows(projection.table)
        for event in log:
            projection.apply(rows, event)
        folded[projection.name] = rows
    return folded


async def replace_with_fold(conn: AsyncConnection, story_id: str) -> dict[str, int]:
    """Delete the story's projection rows and insert the fold; returns rows per table.

    The caller holds the story lock (``rebuild`` runs this as an operation commit step).
    """
    folded = await fold(conn, story_id)
    counts: dict[str, int] = {}
    for projection in registered_projections():
        table = projection.table
        await conn.execute(delete(table).where(table.c.story_id == story_id))
        rows = folded[projection.name]
        if len(rows):
            await conn.execute(insert(table), list(rows))
        counts[projection.name] = len(rows)
    return counts


async def rebuild(
    db: AsyncEngine | AsyncConnection, story_id: str, *, principal: str = SYSTEM_PRINCIPAL
) -> RebuildResult:
    """Truncate and refold every projection of ``story_id`` (FR-ADMIN-3, NFR-REL-2).

    Runs as one ``maintenance`` operation holding the story row lock, so no commit to the story
    interleaves with the fold; it records no event and bumps no revision. Synchronous in M2
    (#70 moves stories over ``rebuild_async_threshold`` events to a job).
    """
    from epistrel.core.events.operation import Operation  # the commit path imports this module

    await _require_story(db, story_id)
    counts: dict[str, int] = {}

    async def step(conn: AsyncConnection) -> None:
        counts.update(await replace_with_fold(conn, story_id))

    async with Operation(
        db, story_id=story_id, kind=OperationKind.MAINTENANCE, principal=principal
    ) as op:
        op.add_commit_step(step)
        await op.commit()
    return RebuildResult(
        operation_id=op.operation_id, counts=counts, timeline_revision=op.timeline_revision or 0
    )


async def _require_story(db: AsyncEngine | AsyncConnection, story_id: str) -> None:
    query = select(stories.c.id).where(stories.c.id == story_id)
    if isinstance(db, AsyncConnection):
        found = await db.scalar(query)
        if db.in_transaction():
            await db.rollback()
    else:
        async with db.connect() as conn:
            found = await conn.scalar(query)
    if found is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})


async def _write_difference(conn: AsyncConnection, rows: Rows, before: dict[Key, Row]) -> None:
    """Insert the rows ``apply`` added and update the ones it changed (``apply`` never removes)."""
    table = rows.table
    after = rows.snapshot()

    def where(key: Key) -> ColumnElement[bool]:
        pairs = zip(rows.key_columns, key, strict=True)
        return and_(*(table.c[name] == value for name, value in pairs))

    added = sorted(after.keys() - before.keys())
    if added:
        await conn.execute(insert(table), [after[key] for key in added])
    for key in before.keys() & after.keys():
        if before[key] != after[key]:
            changed = {k: v for k, v in after[key].items() if before[key].get(k) != v}
            await conn.execute(update(table).where(where(key)).values(**changed))
