"""A paused story refuses truth-changing calls (FR-CONC-5, FR-RATE-7; #118).

A tightening of a story's rating pauses it (``stories.status = 'paused'``, ``paused_by`` = the
``rating_check_jobs`` row) until its history check ends. While it is paused, every call that
would change its story truth is refused with 409 ``story_paused`` and records nothing:
``respond``, ``regenerate``, a queued turn when it would start, ``ingest``, pinned memories and
custom instructions, truncate, untruncate and swap, an added character, a fork, archive and
unarchive, the story's own policies, and another rating change. ``Operation.begin`` refuses
every revision-bumping operation of a paused story, and each entry point checks before its
gate or its operation, right after ``story_archived`` (#44's order). Reads are served, and so
are ``PATCH /stories/{id}/settings`` without ``policies`` (prospective), the story's engine
profile and overrides (prospective), owner and relation changes, feedback and deletion (which
ends the check: its job finds the row gone). Work that started before the pause finishes: a
turn in flight commits, and the check waits for it.

The refusal names what paused the story: ``details {story_id, job_id, cause, progress {done,
total}}``. This module reads only tables, so ``core.events.operation`` can use it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import StoryStatus
from epistrel.core.errors import StoryPaused
from epistrel.core.tables import rating_check_jobs, stories

RATING_CHECK = "rating_check"
"""The ``cause`` of a pause by a rating tightening's history check."""


def is_paused(status: str | StoryStatus | None) -> bool:
    return status is not None and StoryStatus(status) is StoryStatus.PAUSED


async def details(conn: AsyncConnection, story_id: str) -> dict[str, Any]:
    """What paused ``story_id``: the check's job id, its cause and its progress."""
    row = (
        await conn.execute(
            select(stories.c.paused_by, rating_check_jobs.c.progress)
            .outerjoin(rating_check_jobs, rating_check_jobs.c.id == stories.c.paused_by)
            .where(stories.c.id == story_id)
        )
    ).one_or_none()
    progress = dict(row.progress or {}) if row is not None else {}
    return {
        "story_id": story_id,
        "job_id": row.paused_by if row is not None else None,
        "cause": RATING_CHECK,
        "progress": {
            "done": int(progress.get("done", 0)),
            "total": int(progress.get("total", 0)),
        },
    }


async def paused_error(conn: AsyncConnection, story_id: str) -> StoryPaused:
    return StoryPaused(
        f"story {story_id} is paused while its rating is checked; try again when it resumes",
        await details(conn, story_id),
    )


async def refuse(
    db: AsyncEngine | AsyncConnection, story_id: str, status: str | StoryStatus | None
) -> None:
    """409 ``story_paused`` when ``status`` (the story's, as the caller read it) is paused."""
    if not is_paused(status):
        return
    if isinstance(db, AsyncConnection):
        raise await paused_error(db, story_id)
    async with db.connect() as conn:
        raise await paused_error(conn, story_id)


__all__ = ["RATING_CHECK", "details", "is_paused", "paused_error", "refuse"]
