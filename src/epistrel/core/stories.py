"""Story creation: the minimal genesis helper (#45). #50 builds story creation on it.

Every story has exactly one rating (FR-RATE-1): the one the request names, which must be
assignable (422 ``rating_retired`` otherwise), or the deployment default (#48).
"""

from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.events.types import StoryCreated
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import deployment_state, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID


async def create_story(
    db: AsyncEngine | AsyncConnection,
    *,
    principal: str,
    title: str | None = None,
    story_id: str | None = None,
    content_rating_id: str | None = None,
) -> str:
    """Insert a story row and commit its genesis operation (``story_created``); return its id.

    ``timeline_revision`` goes 0 → 1 with genesis; ``next_turn_index`` stays 1. Without
    ``content_rating_id`` the story gets the deployment's default rating.
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
            await _insert_story(db, story_id, content_rating_id)
    else:
        async with db.begin() as conn:
            await _insert_story(conn, story_id, content_rating_id)
    async with op:
        op.append("story_created", StoryCreated(title=title))
        await op.commit()
    return story_id


async def resolve_story_rating(conn: AsyncConnection, content_rating_id: str | None) -> str:
    """The rating a new story gets: the named one if assignable, else the deployment default.

    Holds the deployment row and the rating's head ``FOR SHARE`` until the caller commits, so
    neither the default nor the rating can change underneath the insert.
    """
    if content_rating_id is None:
        content_rating_id = (
            await conn.execute(
                select(deployment_state.c.default_content_rating_id)
                .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
                .with_for_update(read=True)
            )
        ).scalar_one()
    return await require_assignable(conn, content_rating_id)


async def _insert_story(
    conn: AsyncConnection, story_id: str, content_rating_id: str | None
) -> None:
    rating_id = await resolve_story_rating(conn, content_rating_id)
    await conn.execute(insert(stories).values(id=story_id, content_rating_id=rating_id))
