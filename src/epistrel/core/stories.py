"""Story creation: the minimal genesis helper (#45). #50 builds story creation on it."""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.events.types import StoryCreated
from epistrel.core.ids import new_ulid
from epistrel.core.tables import stories


async def create_story(
    db: AsyncEngine | AsyncConnection,
    *,
    principal: str,
    title: str | None = None,
    story_id: str | None = None,
) -> str:
    """Insert a story row and commit its genesis operation (``story_created``); return its id.

    ``timeline_revision`` goes 0 → 1 with genesis; ``next_turn_index`` stays 1.
    """
    story_id = story_id or new_ulid()
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.GENESIS,
        principal=principal,
        provenance=Provenance.CONSUMER,
    )
    if isinstance(db, AsyncConnection):
        async with db.begin():
            await db.execute(insert(stories).values(id=story_id))
    else:
        async with db.begin() as conn:
            await conn.execute(insert(stories).values(id=story_id))
    async with op:
        op.append("story_created", StoryCreated(title=title))
        await op.commit()
    return story_id
