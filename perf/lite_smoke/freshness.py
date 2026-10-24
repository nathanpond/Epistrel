"""How long a committed P-04 report counts, and when to warn that it is running out (#283).

P-04's reference workload is run by hand (``python -m perf.lite_smoke``), so the gate
(``tests/perf/test_p_04.py::test_p_04``) accepts its committed evidence for ``FRESH`` (30 days)
from the run's start. The scheduled ``p04-expiry.yml`` workflow runs this module every day and
opens a reminder issue once the newest full-length report is ``WARN`` (14 days) or less from
expiring, so the rerun happens before the PR gate turns red.

    uv run python -m perf.lite_smoke.freshness [--now 2026-10-27T00:00:00Z]

prints ``key=value`` lines (``report``, ``started``, ``expires``, ``due``, ``expired``) for
``$GITHUB_OUTPUT``; it always exits 0, because deciding what to do is the workflow's job.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from epistrel.testing.perf import TURNS
from perf.lite_smoke import (
    FULL_MINUTES,
    REPO_ROOT,
    RESULTS_DIR,
    STRUCTURED_EVENTS,
    STRUCTURED_MEMORIES,
    report,
)

FRESH = timedelta(days=30)
"""A full-length report counts for the gate for 30 days from the run's start."""
WARN = timedelta(days=14)
"""The reminder is due once a report is 14 days or less from expiring."""


def full_turns(scenario: str) -> int:
    """The seeded turns of a full-size story: 10,000 for Lite; for the structured and the
    multi-character variants, the scale spec of 10,000 memories and 20,000 events (5,000 turns,
    the rest canon's events)."""
    if scenario in ("structured", "multi"):
        from epistrel.testing.fixtures import scale

        return scale.ScaleSpec.scaled(STRUCTURED_MEMORIES, events=STRUCTURED_EVENTS).turns
    return TURNS


def is_full(found: report.Header) -> bool:
    """A full-length run: at least 30 minutes over the full-size stories of its scenario."""
    return found.minutes >= FULL_MINUTES and found.turns == full_turns(found.scenario)


def newest_full(
    results_dir: Path = RESULTS_DIR, scenario: str = "lite"
) -> tuple[Path, report.Header] | None:
    """The newest full-length report of ``scenario`` in ``results_dir`` (by its start time),
    or ``None``.

    A file that is not a P-04 report is skipped here; ``test_p_04`` is what refuses it."""
    found: list[tuple[Path, report.Header]] = []
    for path in sorted(results_dir.glob(f"*-{scenario}.md")):
        try:
            parsed = report.header(path.read_text())
        except ValueError:
            continue
        if parsed.scenario == scenario and is_full(parsed):
            found.append((path, parsed))
    return max(found, key=lambda item: item[1].started, default=None)


@dataclass(frozen=True, slots=True)
class Status:
    """Where the newest full-length report stands at ``now``."""

    report: Path | None
    started: datetime | None
    now: datetime

    @property
    def expires(self) -> datetime | None:
        return None if self.started is None else self.started + FRESH

    @property
    def expired(self) -> bool:
        """The gate no longer accepts it (or there is none)."""
        return self.expires is None or self.now > self.expires

    @property
    def due(self) -> bool:
        """The reminder is due: expiring within ``WARN``, expired, or missing."""
        return self.expires is None or self.now >= self.expires - WARN


def status(now: datetime, results_dir: Path = RESULTS_DIR, scenario: str = "lite") -> Status:
    newest = newest_full(results_dir, scenario)
    if newest is None:
        return Status(report=None, started=None, now=now)
    return Status(report=newest[0], started=newest[1].started, now=now)


def outputs(found: Status) -> list[str]:
    """``key=value`` lines for ``$GITHUB_OUTPUT`` (the report's path relative to the repo)."""
    path = found.report
    if path is not None and path.resolve().is_relative_to(REPO_ROOT):
        path = path.resolve().relative_to(REPO_ROOT)
    name = "" if path is None else path.as_posix()
    started = "" if found.started is None else found.started.strftime("%Y-%m-%d %H:%M UTC")
    expires = "" if found.expires is None else found.expires.strftime("%Y-%m-%d")
    return [
        f"report={name}",
        f"started={started}",
        f"expires={expires}",
        f"due={str(found.due).lower()}",
        f"expired={str(found.expired).lower()}",
    ]


def _now(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m perf.lite_smoke.freshness")
    parser.add_argument("--now", type=_now, default=None, help="ISO time to judge at (UTC)")
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)
    now: datetime = args.now or datetime.now(UTC)
    for line in outputs(status(now, args.results)):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
