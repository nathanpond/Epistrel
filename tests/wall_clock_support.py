"""The wall clock a test moves (``core.clock.set_clock``): the job tests' leases and timers,
X-07's outages and S-RESERVATION-EXPIRY's two-hour reservation expiry (#218 lifted it here
from ``tests/integration/test_jobs.py`` and ``tests/chaos/test_x_07.py``)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from epistrel.core import clock


class MovableClock:
    """The real time plus an offset the test moves (``clock.set_clock``)."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@contextmanager
def moving() -> Iterator[MovableClock]:
    """A ``MovableClock`` installed for the block, the real clock restored after it."""
    movable = MovableClock()
    clock.set_clock(movable)
    try:
        yield movable
    finally:
        clock.set_clock(None)


__all__ = ["MovableClock", "moving"]
