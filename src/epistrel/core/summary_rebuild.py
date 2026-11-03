"""A downgrade to Lite rebuilds the rolling summary from the transcript (FR-LITE-6; #232).

A confirmed change whose effective ``pipeline`` moves from ``structured`` to ``lite`` keeps the
structured data dormant (#231's decrease: coverage closed, nothing deleted) and starts a
**summarization backfill**: a ``story_backfills`` row of kind ``resummarize`` (capabilities
``["rolling_summary"]``, its range the turns the summary does not cover yet) and the story's
``lite_summary`` job (#72), which compresses those turns on its normal grid. The story is not
paused (the owner's round-two decision): turns go on under Lite with the segments written so
far and the verbatim window, and each reply carries ``degradation: ["summary_rebuilding"]``
until the summary has caught up. The row then ends ``completed`` (``settle``); ``DELETE
/stories/{id}/backfill`` cancels it (the segments already written stay, as the incremental job
would have written them).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, summary
from epistrel.core.ids import new_ulid
from epistrel.core.profiles.capabilities import level_at_least
from epistrel.core.tables import story_backfills
from epistrel.core.tables.backfill import ACTIVE_STATUSES
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

KIND = "resummarize"
REBUILDING = "summary_rebuilding"
"""The degradation step a Lite reply carries while the summary is rebuilt."""


def downgraded(before: Any, after: Any) -> bool:
    """Whether a change from ``before`` to ``after`` moves the pipeline from structured to
    lite."""
    return (
        before is not None
        and after is not None
        and level_at_least(before, "pipeline", "structured")
        and not level_at_least(after, "pipeline", "structured")
    )


async def _row(conn: AsyncConnection, story_id: str) -> Any:
    t = story_backfills.c
    return (
        await conn.execute(
            select(story_backfills).where(
                t.story_id == story_id, t.kind == KIND, t.status.in_(ACTIVE_STATUSES)
            )
        )
    ).one_or_none()


async def start(conn: AsyncConnection, story_id: str, principal: str) -> str | None:
    """Start the summarization backfill in ``conn``'s transaction when the summary has turns
    to cover (see the module docstring); its row's id, or ``None``. The ``lite_summary`` job is
    enqueued after the commit (``core.summary_port.notify``)."""
    state = await summary.summary_state(conn, story_id)
    if not state.settings.enabled or not state.chunks:
        return None
    from epistrel.core.backfill import running

    if await running(conn, story_id) is not None:
        return None
    backfill_id = new_ulid()
    await conn.execute(
        story_backfills.insert().values(
            id=backfill_id,
            story_id=story_id,
            kind=KIND,
            status="extracting",
            capabilities=["rolling_summary"],
            range_from_turn=state.chunks[0].covers_from,
            range_to_turn=state.chunks[-1].covers_to,
            chunks_total=len(state.chunks),
            requested_by=principal,
            started_at=clock.now(),
        )
    )
    log.info("summary rebuild started", backfill_id=backfill_id, chunks=len(state.chunks))
    return backfill_id


async def estimate(conn: AsyncConnection, story_id: str) -> dict[str, Any]:
    """A downgrade's ``summary_backfill``: whether the summary has turns to cover and its
    estimate, one ``lite_summary`` call per chunk of the uncovered turns."""
    from epistrel.core.backfill.coverage import active_turns

    segments = await summary.included_segments(conn, story_id)
    covered = summary.covered_to(segments)
    turns = [t for t in await active_turns(conn, story_id) if t > covered]
    settings = await summary.read_summary_settings(conn, story_id)
    calls = -(-len(turns) // settings.chunk_turns) if turns else 0
    return {"needed": bool(turns), "estimate": {"model_calls": calls}}


async def on_configuration(
    conn: AsyncConnection, story_id: str, before: Any, principal: str
) -> str | None:
    """After a configuration change applied: start the summarization backfill when it moved
    the story from structured to lite."""
    from epistrel.core.profiles import effective

    current = await effective.effective_configuration(conn, story_id, validate=False)
    if not downgraded(before, current.settings):
        return None
    return await start(conn, story_id, principal)


async def settle(conn: AsyncConnection, story_id: str) -> bool:
    """End the story's summarization backfill once the summary has no turn left to cover;
    whether one is still rebuilding."""
    found = await _row(conn, story_id)
    if found is None:
        return False
    if await summary.needs_work(conn, story_id):
        return True
    await conn.execute(
        update(story_backfills)
        .where(story_backfills.c.id == found.id)
        .values(status="completed", finished_at=clock.now())
    )
    log.info("summary rebuild completed", backfill_id=str(found.id))
    return False


async def rebuilding(db: AsyncConnection | AsyncEngine, story_id: str) -> bool:
    """Whether the story's summary is being rebuilt (a reply's ``summary_rebuilding``)."""
    if isinstance(db, AsyncConnection):
        return await settle(db, story_id)
    async with db.begin() as conn:
        return await settle(conn, story_id)


__all__ = [
    "KIND",
    "REBUILDING",
    "downgraded",
    "estimate",
    "on_configuration",
    "rebuilding",
    "settle",
    "start",
]
