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

A reactivation check (#120: an untruncate, swap or fork of a dormant continuation whose turns
lack a policy now in effect) pauses the story the same way, with ``cause: reactivation_check``.
When its turns pass, the job re-runs the edit it held back while the story is still paused:
``holding(job_id)`` lets the calls made inside it through when ``job_id`` is what paused the
story, and nothing else.

The refusal names what paused the story: ``details {story_id, job_id, cause, progress {done,
total}}``. This module reads only tables, so ``core.events.operation`` can use it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import StoryStatus
from epistrel.core.errors import StoryPaused
from epistrel.core.tables import rating_check_jobs, stories

RATING_CHECK = "rating_check"
"""The ``cause`` of a pause by a rating tightening's history check."""
REACTIVATION_CHECK = "reactivation_check"
"""The ``cause`` of a pause by a reactivation check (#120)."""
REACTIVATION_KIND = "reactivation"
"""``rating_check_jobs.kind`` of a reactivation check."""

_holding: ContextVar[str | None] = ContextVar("epistrel_pause_holding", default=None)


@contextmanager
def holding(job_id: str) -> Iterator[None]:
    """Inside the block, a story paused by ``job_id`` is not refused (the job's own edit)."""
    token = _holding.set(job_id)
    try:
        yield
    finally:
        _holding.reset(token)


def is_paused(status: str | StoryStatus | None) -> bool:
    return status is not None and StoryStatus(status) is StoryStatus.PAUSED


async def details(conn: AsyncConnection, story_id: str) -> dict[str, Any]:
    """What paused ``story_id``: the check's job id, its cause and its progress."""
    row = (
        await conn.execute(
            select(stories.c.paused_by, rating_check_jobs.c.progress, rating_check_jobs.c.kind)
            .outerjoin(rating_check_jobs, rating_check_jobs.c.id == stories.c.paused_by)
            .where(stories.c.id == story_id)
        )
    ).one_or_none()
    progress = dict(row.progress or {}) if row is not None else {}
    return {
        "story_id": story_id,
        "job_id": row.paused_by if row is not None else None,
        "cause": (
            REACTIVATION_CHECK
            if row is not None and row.kind == REACTIVATION_KIND
            else RATING_CHECK
        ),
        "progress": {
            "done": int(progress.get("done", 0)),
            "total": int(progress.get("total", 0)),
        },
    }


async def paused_error(conn: AsyncConnection, story_id: str) -> StoryPaused:
    found = await details(conn, story_id)
    what = (
        "its rating is checked"
        if found["cause"] == RATING_CHECK
        else "the turns an edit would reactivate are checked"
    )
    return StoryPaused(f"story {story_id} is paused while {what}; try again when it resumes", found)


async def refuse(
    db: AsyncEngine | AsyncConnection, story_id: str, status: str | StoryStatus | None
) -> None:
    """409 ``story_paused`` when ``status`` (the story's, as the caller read it) is paused."""
    if not is_paused(status):
        return
    if isinstance(db, AsyncConnection):
        await _refuse_in(db, story_id)
        return
    async with db.connect() as conn:
        await _refuse_in(conn, story_id)


async def _refuse_in(conn: AsyncConnection, story_id: str) -> None:
    held = _holding.get()
    if held is not None:
        paused_by = await conn.scalar(select(stories.c.paused_by).where(stories.c.id == story_id))
        if paused_by == held:
            return
    raise await paused_error(conn, story_id)


__all__ = [
    "RATING_CHECK",
    "REACTIVATION_CHECK",
    "REACTIVATION_KIND",
    "details",
    "holding",
    "is_paused",
    "paused_error",
    "refuse",
]
