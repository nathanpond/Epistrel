"""The ``importing`` story status (#250, FR-STORE-7): nobody reads or plays a story whose
import has not finished.

A story imported from a bundle exists from the import request on (``stories.status =
importing``), while its ``story_import`` job inserts its rows, runs #251's gates and rebuilds
its projections. Until the job ends:

- every Engine call naming the story is 409 ``story_importing`` (``refuse``, from the Engine's
  story gate beside the erasure block, ``core.erasure.ledger.check_story``), with the job's
  ``stage`` and ``progress``;
- every operation on it is refused at ``begin`` and again at commit (``Operation``'s backstop);
- background work skips it in its claim (``orchestrator.jobs.queue.claim``).

The import job's own work passes (``passing``), as the erasure reapply passes its blocked
story (``core.erasure.ledger.reapplying``). Log lines carry ids only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import StoryStatus
from epistrel.core.errors import StoryImporting

IMPORTING = StoryStatus.IMPORTING.value

_PASSING: ContextVar[str | None] = ContextVar("epistrel_importing_story", default=None)


@contextmanager
def passing(story_id: str) -> Iterator[None]:
    """Let the import job's own reads and operations on ``story_id`` through."""
    token = _PASSING.set(story_id)
    try:
        yield
    finally:
        _PASSING.reset(token)


def passes(story_id: str) -> bool:
    """Whether the work under way is ``story_id``'s import job."""
    return _PASSING.get() == story_id


async def details(conn: AsyncConnection, story_id: str) -> dict[str, Any]:
    """The 409's details: the import job, its stage and its progress."""
    from epistrel.core.tables import bundle_jobs

    b = bundle_jobs.c
    row = (
        await conn.execute(
            select(b.id, b.stage, b.progress)
            .where(b.story_id == story_id, b.kind == "import")
            .order_by(b.created_at.desc())
            .limit(1)
        )
    ).one_or_none()
    progress = dict(row.progress or {}) if row is not None else {}
    return {
        "story_id": story_id,
        "job_id": None if row is None else str(row.id),
        "stage": None if row is None else row.stage,
        "progress": {
            "done": int(progress.get("done", 0)),
            "total": int(progress.get("total", 0)),
        },
    }


async def refuse(conn: AsyncConnection, story_id: str, status: str | None) -> None:
    """409 ``story_importing`` for an importing story, unless this is its import job."""
    if status == IMPORTING and not passes(story_id):
        raise StoryImporting(
            "the story is being imported; it is served once its import has finished",
            await details(conn, story_id),
        )


__all__ = ["IMPORTING", "details", "passes", "passing", "refuse"]
