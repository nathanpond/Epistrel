"""A writer the subject-index coverage test registers (#123): it writes a story-content row and
never calls the indexer, so the coverage check must report it. Never run."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def write_note(conn: AsyncConnection, story_id: str, note: str) -> None:
    await conn.execute(
        text("INSERT INTO story_notes (story_id, note) VALUES (:s, :n)"), {"s": story_id, "n": note}
    )
