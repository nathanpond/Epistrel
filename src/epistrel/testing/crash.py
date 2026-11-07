"""Named crash points inside the commit path (#75; NFR-REL-1,2).

``hit(point, kind)`` is a permanent call at each named point of ``Operation``'s commit, the fork,
the Engine's keyed calls and the queue drainer. It does nothing unless the process was started
with ``EPISTREL_TEST_FAULTS=1`` and ``EPISTREL_TEST_CRASH_AT=<point>:<nth>[:<kind>]``: then the
``nth`` hit of ``point`` (counted per operation kind when ``kind`` is given, else over every
kind) ends the process at once with ``os._exit(137)``, the exit status of a ``SIGKILL``: no
``finally`` block, rollback or shutdown hook runs, exactly as when the plug is pulled. A point
the process never reaches ``nth`` times is a run without a crash.

The points:

- ``before_lock``: before the commit transaction takes the story lock (and before the drainer
  takes a queued turn's slot);
- ``after_events``: the events are written (flushed) but no projection has been applied;
- ``during_projection``: between two projections' applies;
- ``before_commit``: everything is written, the transaction is not committed;
- ``after_commit_before_response``: committed, the caller has not been answered.

A staged turn's stage boundaries (#213; FR-EXT-17, ``orchestrator.structured.staged``), each
hit once per staged turn by whichever process runs that stage:

- ``staged.before_intent_commit``: the turn planned staged, its intent not committed;
- ``staged.after_intent``: the intent committed, nothing claimed;
- ``staged.after_claim``: the claim committed (``awaiting_outcome``), nothing sent;
- ``staged.after_send``: the provider answered a send (``jobs.handlers.outbox.send``), the
  answer not recorded;
- ``staged.after_outcome``: the outcome transaction committed (``outcome_recorded``), the
  completion not;
- ``staged.after_completion``: the completion committed, the response not stored.

A story import's job (#250; ``core.bundles``), each hit by the worker running it:

- ``bundles.import.mid_records``: one table of the bundle inserted (hit after each table), its
  transaction not committed;
- ``bundles.import.after_insert``: the bundle's rows committed into the importing story, no
  gate run yet (the next attempt purges them and starts again from the staged file).

An image job (#256; ``orchestrator.images.jobs``), each hit by the worker running it:

- ``images.after_stage``: an attempt's outputs are written to private staging, none checked;
- ``images.promote.before_commit``: the passing outputs are copied to their content-addressed
  keys and the promote transaction has written its rows, not committed (the blobs are then
  orphans the media sweep removes after its hour).

This module imports nothing but the standard library, so ``epistrel.core`` can call it.
``epistrel.testing.faults`` (the child-process harness) re-exports ``crash_at`` and ``hit``.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from dataclasses import dataclass

COMMIT_POINTS = (
    "before_lock",
    "after_events",
    "during_projection",
    "before_commit",
    "after_commit_before_response",
)
"""The commit path's points (#75)."""
STAGED_POINTS = (
    "staged.before_intent_commit",
    "staged.after_intent",
    "staged.after_claim",
    "staged.after_send",
    "staged.after_outcome",
    "staged.after_completion",
)
"""A staged turn's stage boundaries (#213)."""
IMPORT_POINTS = ("bundles.import.mid_records", "bundles.import.after_insert")
"""A story import's job (#250)."""
IMAGE_POINTS = ("images.after_stage", "images.promote.before_commit")
"""An image job (#256)."""
POINTS = (*COMMIT_POINTS, *STAGED_POINTS, *IMPORT_POINTS, *IMAGE_POINTS)
ENABLE_VAR = "EPISTREL_TEST_FAULTS"
CRASH_VAR = "EPISTREL_TEST_CRASH_AT"
EXIT_STATUS = 137
"""What a shell reports for a process killed by ``SIGKILL`` (128 + 9)."""
CRASH_MARKER = "EPISTREL-FAULT crash"
"""Printed (to stdout, flushed) just before the exit, so a harness can tell a crash point fired
(``faults.EngineProcess`` queues every ``EPISTREL-FAULT`` line it reads)."""


@dataclass(frozen=True, slots=True)
class CrashPlan:
    point: str
    nth: int
    kind: str | None = None

    def env(self) -> dict[str, str]:
        spec = f"{self.point}:{self.nth}" + (f":{self.kind}" if self.kind else "")
        return {ENABLE_VAR: "1", CRASH_VAR: spec}


def crash_at(point: str, nth: int = 1, kind: str | None = None) -> CrashPlan:
    """The crash the child process should die of: its ``env()`` is what to start it with."""
    if point not in POINTS:
        raise ValueError(f"unknown crash point {point!r}; one of {', '.join(POINTS)}")
    if nth < 1:
        raise ValueError("nth counts from 1")
    return CrashPlan(point, nth, kind)


def parse(spec: str) -> CrashPlan:
    """``<point>:<nth>[:<kind>]`` as ``crash_at`` checks it."""
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"{CRASH_VAR} is <point>:<nth>[:<kind>], not {spec!r}")
    return crash_at(parts[0], int(parts[1]), parts[2] if len(parts) == 3 else None)


_UNREAD = object()
_plan: object = _UNREAD
_hits: Counter[tuple[str, str | None]] = Counter()


def armed() -> CrashPlan | None:
    """The configured crash, read from the environment once; ``None`` unless enabled."""
    global _plan
    if _plan is _UNREAD:
        spec = os.environ.get(CRASH_VAR)
        _plan = parse(spec) if os.environ.get(ENABLE_VAR) == "1" and spec else None
    return _plan if isinstance(_plan, CrashPlan) else None


def reset() -> None:
    """Forget the configuration and the counts (tests that change the environment)."""
    global _plan
    _plan = _UNREAD
    _hits.clear()


def hit(point: str, kind: str | None = None) -> None:
    """A crash point; see the module docstring. A no-op unless a crash is configured."""
    plan = armed()
    if plan is None or plan.point != point:
        return
    if plan.kind is not None and plan.kind != kind:
        return
    counted = (point, plan.kind)
    _hits[counted] += 1
    if _hits[counted] == plan.nth:
        print(f"{CRASH_MARKER} {point} {kind or '-'}", file=sys.stdout, flush=True)
        os._exit(EXIT_STATUS)


__all__ = [
    "COMMIT_POINTS",
    "CRASH_MARKER",
    "CRASH_VAR",
    "ENABLE_VAR",
    "EXIT_STATUS",
    "IMAGE_POINTS",
    "IMPORT_POINTS",
    "POINTS",
    "STAGED_POINTS",
    "CrashPlan",
    "armed",
    "crash_at",
    "hit",
    "parse",
    "reset",
]
