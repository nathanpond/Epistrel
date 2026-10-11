"""Per-stage turn latency (NFR-PERF-3,4; #76): ``epistrel_turn_stage_seconds{stage}``.

One histogram, labelled by stage only (never ``story_id``: per-story figures come from the
P-04 driver), observed once per stage per committed Lite turn:

- ``queue_wait``: a queued turn's wait from its enqueue to reaching the front of its story's
  queue (the drainer starting it); unqueued turns observe nothing here;
- ``front_to_first_call``: from the turn reaching the front (a queued turn starting, or an
  unqueued request's arrival) to its first model call starting;
- ``assemble``: the read phase and prompt assembly;
- ``generate``: the ``generation`` calls' own time;
- ``policy_check``: the input and output gates' time, generation excluded;
- ``commit``: a committed ``turn`` operation's commit transaction in ``Operation.commit()``
  (NFR-PERF-3's atomic turn commit);
- ``post_commit``: what ``Operation.commit()`` does for a turn after its transaction committed
  (the summary port's scheduling, #72), still before the turn answers.

The buckets are log-spaced from 1 ms to 60 s, with the gates' bounds as edges. Labels are the
stage names only (invariant 3). This module imports nothing from the Engine, so
``epistrel.core`` may observe ``commit``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from prometheus_client import Histogram

Stage = Literal[
    "queue_wait",
    "front_to_first_call",
    "assemble",
    "generate",
    "policy_check",
    "commit",
    "post_commit",
]
STAGES: tuple[Stage, ...] = (
    "queue_wait",
    "front_to_first_call",
    "assemble",
    "generate",
    "policy_check",
    "commit",
    "post_commit",
)
STAGE_BUCKETS = (
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.05,
    0.1,
    0.2,
    0.25,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    60.0,
)
"""1 ms to 60 s, log-spaced (1-2-5 per decade), plus 250 ms: NFR-PERF-3's commit budget and
the 1 s queue-front-to-first-call bound are bucket edges, so P-04 reads both gates exactly."""

TURN_STAGE = Histogram(
    "epistrel_turn_stage_seconds",
    "Lite turn latency by stage: queue_wait, front_to_first_call, assemble, generate, "
    "policy_check, commit, post_commit.",
    ("stage",),
    buckets=STAGE_BUCKETS,
)


def observe(stage: Stage, seconds: float) -> None:
    """Record ``seconds`` (clamped at 0) for ``stage``."""
    TURN_STAGE.labels(stage).observe(max(0.0, seconds))


@contextmanager
def timed(stage: Stage) -> Iterator[None]:
    """Observe the block's wall-clock time for ``stage`` (also when it raises)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        observe(stage, time.perf_counter() - started)


__all__ = ["STAGES", "STAGE_BUCKETS", "TURN_STAGE", "Stage", "observe", "timed"]
