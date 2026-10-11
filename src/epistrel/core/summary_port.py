"""The port through which Core asks for the Lite rolling summary to be brought up to date (#72).

Core records turns and timeline edits but never enqueues background work itself (it does not
import the Orchestrator). The Orchestrator's ``lite_summary`` job module sets the scheduler at
import (``set_scheduler``); ``Operation`` calls ``notify`` after a ``turn`` operation commits, and
the timeline's post-commit membership hook does after truncate, untruncate, swap and fork. The
scheduler decides whether there is work (``core.summary.needs_work``) and enqueues one coalesced
job per story. A failure here is logged (class name only) and never fails the commit that
already happened.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

Scheduler = Callable[[AsyncEngine, str], Awaitable[bool]]
"""``scheduler(db, story_id)``: enqueue the story's summary job when it has work; ``True`` when
it did."""

_SCHEDULER: list[Scheduler] = []


def set_scheduler(scheduler: Scheduler | None) -> None:
    """Make ``scheduler`` the one ``notify`` calls (``None``: nothing is scheduled)."""
    _SCHEDULER.clear()
    if scheduler is not None:
        _SCHEDULER.append(scheduler)


def scheduler() -> Scheduler | None:
    return _SCHEDULER[0] if _SCHEDULER else None


async def notify(db: AsyncEngine, story_id: str) -> bool:
    """Ask the scheduler to bring ``story_id``'s summary up to date; ``False`` when nothing was
    enqueued (no scheduler, no work, or a failure, which is logged and swallowed)."""
    found = scheduler()
    if found is None:
        return False
    try:
        return await found(db, story_id)
    except Exception as exc:
        logger.warning("summary scheduling failed for story %s: %s", story_id, type(exc).__name__)
        return False


__all__ = ["Scheduler", "notify", "scheduler", "set_scheduler"]
