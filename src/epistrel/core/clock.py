"""The Engine's wall clock, injectable for tests (#61).

``now()`` is the time every time-window rule reads (the idempotency result window, its expiry
checks and the purge). Tests move it with ``frozen_at``/``set_clock`` instead of sleeping; the
database's ``transaction_timestamp()`` stays the clock of record for event and operation
timestamps (``valid_time == tx_time`` in M2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Clock = system_clock


def now() -> datetime:
    """The current time (timezone-aware, UTC) from the installed clock."""
    return _clock()


def set_clock(clock: Clock | None) -> None:
    """Install ``clock`` (``None`` restores the system clock)."""
    global _clock
    _clock = clock or system_clock


@contextmanager
def frozen_at(moment: datetime) -> Iterator[None]:
    """Hold ``now()`` at ``moment`` for the block, then restore the previous clock."""
    previous = _clock
    set_clock(lambda: moment)
    try:
        yield
    finally:
        set_clock(previous)


__all__ = ["Clock", "frozen_at", "now", "set_clock", "system_clock"]
