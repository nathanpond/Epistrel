"""The interim history read: a story's active transcript, from the projection (FR-ADMIN-3).

Reads ``transcript_messages`` only, never the log, which is what makes a projection rebuild
observable (NFR-REL-2). Items use #59's shape; #59 adds the output viewpoint, ``as_of`` and
paging; #57 adds the render characters of each reply; #58 each beat's ``origin``
(``generated`` or ``ingested``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership
from epistrel.core.errors import StoryNotFound
from epistrel.core.tables import operations, stories, transcript_messages


@dataclass(frozen=True, slots=True)
class HistoryItem:
    turn: int | None
    variant: int | None
    beat: int | None
    kind: str
    actor_character_id: str | None
    render_character_ids: list[str] | None
    channel: str
    text: str
    operation_id: str
    message_id: str
    origin: str = "generated"
    """``ingested`` when a consumer recorded the beat through ``ingest`` (#58)."""


@dataclass(frozen=True, slots=True)
class History:
    items: list[HistoryItem]
    timeline_revision: int


async def read_history(
    db: AsyncEngine | AsyncConnection, story_id: str, *, operation_id: str | None = None
) -> History:
    """The active transcript of ``story_id`` in narrative order (only ``operation_id``'s beats,
    when given: a turn's ``beats``)."""
    t = transcript_messages
    async with _connection(db) as conn:
        revision = await conn.scalar(
            select(stories.c.timeline_revision).where(stories.c.id == story_id)
        )
        if revision is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        found = await conn.execute(
            select(t, operations.c.origin)
            .join(operations, operations.c.operation_id == t.c.operation_id)
            .where(
                t.c.story_id == story_id,
                t.c.membership == Membership.ACTIVE.value,
                *([t.c.operation_id == operation_id] if operation_id is not None else []),
            )
            .order_by(
                t.c.narrative_turn.asc().nulls_first(),
                t.c.turn_variant.asc().nulls_first(),
                t.c.seq,
            )
        )
        rows = found.all()
    return History(
        items=[
            HistoryItem(
                turn=row.narrative_turn,
                variant=row.turn_variant,
                beat=row.beat,
                kind=row.kind,
                actor_character_id=row.actor_character_id,
                render_character_ids=row.render_character_ids,
                channel=row.channel,
                text=row.text,
                operation_id=row.operation_id,
                message_id=row.message_id,
                origin=row.origin,
            )
            for row in rows
        ],
        timeline_revision=int(revision),
    )


@asynccontextmanager
async def _connection(db: AsyncEngine | AsyncConnection) -> AsyncIterator[AsyncConnection]:
    if isinstance(db, AsyncConnection):
        yield db
        return
    async with db.connect() as conn:
        yield conn
