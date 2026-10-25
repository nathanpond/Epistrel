"""Per-stage turn latency (NFR-PERF-1,2,3,4; #76, #153): ``epistrel_turn_stage_seconds{stage}``.

One histogram, labelled by stage only (never ``story_id``: per-story figures come from the
P-04 driver), observed once per stage per committed turn:

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

Two more stages are a structured turn's alone (``STRUCTURED_STAGES``; P-04's structured variant
gates on them, #153), observed once per call rather than once per turn:

- ``retrieve``: one retrieval (``core.retrieval.pipeline.retrieve``), its own ``timings``
  total, which leaves the query embedding out (NFR-PERF-1's span, as P-01 measures it);
- ``verify``: one claim's deterministic verify path (the verifier's keyed fetch and ``check``,
  no model fallback; NFR-PERF-2's span, as P-02 measures it).

Two more are a structured turn's per-character work (``MULTI_STAGES``; P-04's multi-character
variant reports them as its ``context_ms`` and ``compose_ms`` columns, #182):

- ``context``: one character render's context (#170's ``build_context``: the partition read,
  lazy resolutions and the viewpoint-scoped retrieval included), once per render;
- ``compose``: the turn's reconciliation of concurrent commitments (#177), the audience filter
  and the composition, the polish's model calls left out, once per turn.

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
    "retrieve",
    "verify",
    "context",
    "compose",
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
"""The stages every committed turn observes once."""
STRUCTURED_STAGES: tuple[Stage, ...] = ("retrieve", "verify")
"""The stages only a structured turn observes, once per retrieval and once per claim checked."""
MULTI_STAGES: tuple[Stage, ...] = ("context", "compose")
"""A structured turn's per-character stages: ``context`` once per character render, ``compose``
once per turn (#182)."""
STAGE_BUCKETS = (
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.05,
    0.1,
    0.15,
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
"""1 ms to 60 s, log-spaced (1-2-5 per decade), plus 150 ms and 250 ms: NFR-PERF-1's retrieval
budget, NFR-PERF-2's 50 ms verify budget, NFR-PERF-3's commit budget and the 1 s
queue-front-to-first-call bound are bucket edges, so P-04 reads each gate exactly."""

TURN_STAGE = Histogram(
    "epistrel_turn_stage_seconds",
    "Turn latency by stage: queue_wait, front_to_first_call, assemble, generate, "
    "policy_check, commit, post_commit; a structured turn's retrieve and verify, per call; "
    "and its context, per character render, and compose, per turn.",
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


__all__ = [
    "MULTI_STAGES",
    "STAGES",
    "STAGE_BUCKETS",
    "STRUCTURED_STAGES",
    "TURN_STAGE",
    "Stage",
    "observe",
    "timed",
]
