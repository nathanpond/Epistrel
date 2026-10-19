"""A structured turn's eval hooks (FR-OBS-2; #155): ``turn_report.eval_hooks``.

``eval_hooks(...)`` is what the turn report keeps for the eval harness, ids and counts only:

- ``raw_render_event_ids``: the events holding the turn's raw render: each rendered beat's own
  event when it was released as rendered, and, for a beat #151's ladder repaired, the
  ``render_rejected`` event of its original text (attempt 0);
- ``verdicts``: where the verification verdicts are, not a copy: ``{"sections": [...]}`` names
  the report's sections (``verify``, ``verify_follow_up``) whose ``verdicts`` they are, which
  ``GET /operations/{id}?viewpoint=omniscient`` expands (``core.operation_view``);
- ``repair_rungs``: per repaired beat, the rung accepted (4: the safe fallback) and whether a
  correction was accepted;
- ``model_calls_by_purpose``: the turn's call counts by purpose;
- ``latency_ms_by_stage``: the committed attempt's wall time by stage (``StageClock``):
  ``plan`` (the turn's read), ``render`` (the renders and their output gate, the follow-up
  render included), ``normalize`` (declared propositions), ``detection`` (claim detection and
  salience), ``verify`` (the deterministic verify path and the verifier's reads), ``entailment``
  (the entailment calls), ``repair`` (#151's ladder, its re-checks included), ``composition``
  (#152) and ``commit`` (the commit's steps until the report is written). A model call's
  latency counts in its stage's.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

STAGES: tuple[str, ...] = (
    "plan",
    "render",
    "normalize",
    "detection",
    "verify",
    "entailment",
    "repair",
    "composition",
    "commit",
)
VERDICT_SECTIONS: tuple[str, ...] = ("verify", "verify_follow_up")


class StageClock:
    """Wall time by stage (see the module docstring)."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = dict.fromkeys(STAGES, 0.0)

    def reset(self) -> None:
        self.seconds = dict.fromkeys(STAGES, 0.0)

    def add(self, stage: str, seconds: float) -> None:
        if stage not in self.seconds:
            raise ValueError(f"unknown stage {stage!r}")
        self.seconds[stage] += max(0.0, seconds)

    @contextmanager
    def timing(self, stage: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(stage, time.perf_counter() - started)

    def total(self, stages: Sequence[str]) -> float:
        return sum(self.seconds[s] for s in stages)

    def ms(self) -> dict[str, int]:
        return {stage: round(value * 1000) for stage, value in self.seconds.items()}


def eval_hooks(
    report: Mapping[str, Any],
    *,
    raw_render_event_ids: Sequence[str],
    repair_rungs: Sequence[Mapping[str, Any]],
    clock: StageClock,
) -> dict[str, Any]:
    """``turn_report.eval_hooks`` over the report built so far (see the module docstring)."""
    return {
        "raw_render_event_ids": list(raw_render_event_ids),
        "verdicts": {"sections": [s for s in VERDICT_SECTIONS if s in report]},
        "repair_rungs": [dict(r) for r in repair_rungs],
        "model_calls_by_purpose": dict(report.get("call_counts") or {}),
        "latency_ms_by_stage": clock.ms(),
    }


__all__ = ["STAGES", "VERDICT_SECTIONS", "StageClock", "eval_hooks"]
