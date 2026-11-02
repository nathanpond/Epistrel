"""A worker loop's own record of what it did (#446; shared by X-07 and the jobs tests, #609):
checks that a job was taken by a particular pass of the loop read this record, not the wall
clock, so they hold however slow the host is."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock
from epistrel.orchestrator.jobs import timers as timer_api
from epistrel.orchestrator.jobs.worker import Worker


class LoopLog:
    """What a worker's loop did, in order (#446): ``("poll", claimed)`` after each
    ``run_one``, ``("wait", seconds)`` for each idle wait it computed, and ``("fire", moment,
    timer kinds)`` for each timer pass (``moment`` read just before it). "Within one polling
    interval" is checked against this record rather than the wall clock: the work is taken by
    the first pass that could see it, after at most one idle wait no longer than the interval,
    so the check holds however slow the host is."""

    def __init__(self, worker: Worker, patch: pytest.MonkeyPatch) -> None:
        self.entries: list[tuple[Any, ...]] = []
        run_one, idle, fire_due = worker.run_one, worker._idle_timeout, timer_api.fire_due

        async def polled(worker_id: str) -> bool:
            claimed = await run_one(worker_id)
            self.entries.append(("poll", claimed))
            return claimed

        async def waited() -> float:
            seconds = await idle()
            self.entries.append(("wait", seconds))
            return seconds

        async def fired(db: AsyncEngine, now: datetime | None = None) -> timer_api.Fired:
            moment = clock.now()
            result = await fire_due(db, now)
            self.entries.append(("fire", moment, tuple(result.timers)))
            return result

        patch.setattr(worker, "run_one", polled)
        patch.setattr(worker, "_idle_timeout", waited)
        patch.setattr(timer_api, "fire_due", fired)

    def waits(self, start: int, end: int) -> list[float]:
        return [e[1] for e in self.entries[start:end] if e[0] == "wait"]

    def first(self, *entry: Any, after: int = 0) -> int | None:
        """The index of the first entry from ``after`` on that starts with ``entry``."""
        for index in range(after, len(self.entries)):
            if self.entries[index][: len(entry)] == entry:
                return index
        return None

    def last(self, kind: str, before: int) -> int:
        """The index of the last ``kind`` entry before ``before``."""
        return max(i for i in range(before) if self.entries[i][0] == kind)

    def assert_waits_within_interval(
        self, interval: float, start: int, end: int, most: int
    ) -> None:
        """Between ``start`` and ``end`` the loop waited at most ``most`` times, each wait no
        longer than the polling ``interval``."""
        waits = self.waits(start, end)
        assert len(waits) <= most, self.entries[start : end + 1]
        assert all(w <= interval for w in waits), waits
