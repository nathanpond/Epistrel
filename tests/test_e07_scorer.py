"""E-07's ``nfr_qual_5.valid_after_retry`` counts only the render's structured-call validity
(the owner's decision on #465): ``invalid_output`` attempts retried to a valid answer. What
happened after a valid render (a repair, a safe fallback) plays no part."""

from __future__ import annotations

from epistrel.evals.suites.e07_structured_output import render_calls, render_valid
from epistrel.orchestrator.models.metrics import ModelCall, Outcome


def _call(purpose: str, attempt: int, outcome: Outcome = "ok") -> ModelCall:
    return ModelCall(
        purpose=purpose,
        model="m",
        attempt=attempt,
        outcome=outcome,
        latency_ms=1,
        tokens_in=1,
        tokens_out=1,
    )


def test_a_render_retried_to_a_valid_answer_is_valid() -> None:
    calls = [
        _call("guardrail_input", 1),
        _call("generation", 1, "invalid_output"),
        _call("generation", 2),
        _call("extraction_backstop", 1),
        _call("generation", 1, "invalid_output"),  # a later call (a repair re-render)
        _call("generation", 2, "invalid_output"),
    ]
    assert [c.attempt for c in render_calls(calls)] == [1, 2]
    assert render_valid(calls)


def test_a_render_that_never_validated_is_not() -> None:
    calls = [_call("generation", 1, "invalid_output"), _call("generation", 2, "timeout")]
    assert not render_valid(calls)
    assert not render_valid([_call("extraction_backstop", 1)])  # no render at all


def test_a_safe_fallback_after_a_valid_render_does_not_count() -> None:
    """The live run's typical miss under the old scorer: the render validated first time, the
    repair ladder ended in the safe fallback (its later calls failed)."""
    calls = [
        _call("generation", 1),
        _call("entailment", 1),
        _call("generation", 1, "invalid_output"),
        _call("generation", 2, "invalid_output"),
    ]
    assert render_valid(calls)
