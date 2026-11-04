"""Progress lines for a long eval run (#729): where the run is, how much is left, and when it
should end, measured on this session's own cases.

A line goes to stderr when a suite starts and at most every ``every_s`` seconds as cases end::

    full · e06 · seed 2/3 · suite 45/600 · preset 1,234/10,200 (812 from the journal) ·
    3 errors · 4.1 cases/min · ETA 1d 2h

The rate counts only the cases this session played (journalled ones cost nothing), over the
wall time since the first of them started, so it already reflects ``--concurrency``. The ETA is
the preset's cases not yet done at that rate; ``ETA —`` until a case has been played.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TextIO

from epistrel.evals.runner import Progress

EM_DASH = "—"


def duration(seconds: float) -> str:
    """``45s``, ``12m``, ``3h 05m``, ``2d 4h``."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}s"
    minutes, s = divmod(s, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


class Printer(Progress):
    """``Progress`` as stderr lines (module docstring) for one preset of ``planned`` cases."""

    def __init__(
        self,
        preset: str,
        planned: int,
        *,
        every_s: float = 30.0,
        out: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.preset = preset
        self.planned = planned
        self.every_s = every_s
        self.out = out or sys.stderr
        self.clock = clock
        self.done = 0
        self.reused = 0
        self.errors = 0
        self.played = 0
        self.first_played_at: float | None = None
        self.last_line_at: float | None = None
        self.suite = ""
        self.suite_planned = 0
        self.suite_done = 0
        self.seeds = 0
        self.seeds_seen: set[int] = set()

    def suite_started(self, suite: str, cases: int, seeds: int) -> None:
        self.suite = suite
        self.suite_planned = cases * seeds
        self.suite_done = 0
        self.seeds = seeds
        self.seeds_seen = set()
        self._line(force=True)

    def case_done(
        self, suite: str, seed: int, latency_ms: int, *, reused: bool, error: bool
    ) -> None:
        now = self.clock()
        self.done += 1
        self.suite_done += 1
        self.seeds_seen.add(seed)
        if reused:
            self.reused += 1
        else:
            self.played += 1
            if self.first_played_at is None:
                self.first_played_at = now - latency_ms / 1000
        if error:
            self.errors += 1
        self._line(force=self.suite_done == self.suite_planned)

    def rate_per_s(self) -> float | None:
        if self.first_played_at is None or not self.played:
            return None
        elapsed = self.clock() - self.first_played_at
        return self.played / elapsed if elapsed > 0 else None

    def eta_s(self) -> float | None:
        rate = self.rate_per_s()
        if rate is None:
            return None
        return max(0, self.planned - self.done) / rate

    def line(self) -> str:
        rate = self.rate_per_s()
        eta = self.eta_s()
        parts = [
            self.preset,
            self.suite or EM_DASH,
            f"seed {len(self.seeds_seen) or 1}/{self.seeds or 1}",
            f"suite {self.suite_done:,}/{self.suite_planned:,}",
            f"preset {self.done:,}/{self.planned:,}"
            + (f" ({self.reused:,} from the journal)" if self.reused else ""),
            f"{self.errors:,} errors",
            f"{rate * 60:.1f} cases/min" if rate is not None else f"{EM_DASH} cases/min",
            f"ETA {duration(eta) if eta is not None else EM_DASH}",
        ]
        return " · ".join(parts)

    def _line(self, *, force: bool = False) -> None:
        now = self.clock()
        if not force and self.last_line_at is not None and now - self.last_line_at < self.every_s:
            return
        self.last_line_at = now
        print(self.line(), file=self.out, flush=True)


__all__ = ["Printer", "duration"]
