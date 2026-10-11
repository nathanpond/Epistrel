"""The model-call budget a turn carries (minimal form; #69 adds reserved guardrail calls,
planning from story settings, and the turn report).

``TurnBudget(max_calls, deadline_s)`` counts every model call the Model Access Layer makes for
the turn and bounds them by a wall-clock deadline on the monotonic clock. ``None`` means
unbounded. A call the budget cannot afford is not made; each call's timeout is clamped to the
time left (``ModelAccessLayer``), so no call outlives the turn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TurnBudget:
    """Calls remaining and the deadline for one turn (or one test operation)."""

    max_calls: int | None = None
    deadline_s: float | None = None
    started: float = field(default_factory=time.monotonic)
    used: int = 0

    def __post_init__(self) -> None:
        if self.max_calls is not None and self.max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise ValueError("deadline_s must be > 0")

    @property
    def remaining_calls(self) -> int | None:
        return None if self.max_calls is None else max(0, self.max_calls - self.used)

    def can_afford(self, calls: int = 1) -> bool:
        remaining = self.remaining_calls
        return remaining is None or remaining >= calls

    def debit(self) -> None:
        """Count one call; the caller checked ``can_afford`` first."""
        self.used += 1

    def remaining_s(self) -> float | None:
        """Seconds until the deadline (negative once passed); ``None`` when unbounded."""
        if self.deadline_s is None:
            return None
        return self.started + self.deadline_s - time.monotonic()

    def expired(self) -> bool:
        remaining = self.remaining_s()
        return remaining is not None and remaining <= 0


__all__ = ["TurnBudget"]
