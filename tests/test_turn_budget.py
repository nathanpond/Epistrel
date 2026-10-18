"""``TurnBudget`` planning, reservation and clamping (NFR-PERF-6; #69)."""

from __future__ import annotations

import pytest

from epistrel.core.errors import BudgetBelowSafetyMinimum, InvalidRequest
from epistrel.orchestrator.budget import TurnBudget, lite_default_calls, plan_lite_budget


def _plan(**fields: object) -> TurnBudget:
    values: dict[str, object] = {
        "story_max_calls": None,
        "deployment_max_calls": None,
        "turn_deadline": 45.0,
        "guardrail_input": 1,
        "guardrail_output": 1,
    }
    values.update(fields)
    return plan_lite_budget(**values)  # type: ignore[arg-type]


def test_the_cap_is_request_then_story_then_deployment_then_formula() -> None:
    assert lite_default_calls(0) == 9
    assert _plan().max_calls == 11
    assert _plan(deployment_max_calls=6).max_calls == 6
    assert _plan(story_max_calls=5, deployment_max_calls=6).max_calls == 5
    assert _plan(story_max_calls=5, override_max_calls=4).max_calls == 4
    planned = _plan(override_deadline_s=10.0)
    assert planned.deadline_s == 10.0
    assert planned.held == 3
    assert planned.report() == {
        "max_calls": 11,
        "reserved": {"guardrail_input": 1, "guardrail_output": 1},
        "used": 0,
    }


def test_a_budget_below_the_minimum_or_an_override_above_the_cap_is_refused() -> None:
    with pytest.raises(BudgetBelowSafetyMinimum) as refused:
        _plan(story_max_calls=2)
    assert refused.value.details == {
        "minimum_required": 3,
        "max_calls": 2,
        "reserved": {"guardrail_input": 1, "guardrail_output": 1},
    }
    assert _plan(story_max_calls=3).max_calls == 3
    with pytest.raises(InvalidRequest):
        _plan(story_max_calls=5, override_max_calls=6)
    with pytest.raises(InvalidRequest):
        _plan(override_deadline_s=46.0)


def test_retries_never_spend_a_reserved_slot() -> None:
    budget = _plan(story_max_calls=4)
    assert budget.held == 3
    budget.debit()  # the first input check claims its slot
    assert budget.held == 2
    assert budget.can_afford_unreserved(1)  # 4 - 1 used - 2 held = 1 spare
    budget.debit(claim=False)  # a structured retry spends the spare call
    assert not budget.can_afford_unreserved(1)
    assert budget.can_afford(2)  # the reserved generation and output check still fit
    budget.release_to(1)
    assert budget.held == 1
    budget.hold(2)
    assert budget.held == 3
    budget.refund()
    assert budget.used == 1


def test_the_deadline_clamps_each_call() -> None:
    assert TurnBudget().clamp(60.0) == (60.0, "timeout")
    timeout, reason = TurnBudget(deadline_s=1.0).clamp(60.0)
    assert reason == "deadline"
    assert 0 < timeout <= 1.0
    assert TurnBudget(deadline_s=100.0).clamp(5.0) == (5.0, "timeout")


async def test_embedding_calls_are_counted_apart_from_the_cap() -> None:
    """#141: a retrieval's embedding calls (the query, a rehydration batch) are counted in
    ``TurnBudget.embedding_calls`` through ``counted_embedder``, failed calls too, and never in
    ``used`` (the model-call cap)."""
    from epistrel.orchestrator.budget import counted_embedder

    budget = TurnBudget(max_calls=3)

    async def embed(text: str) -> list[float]:
        if text == "boom":
            raise RuntimeError("down")
        return [1.0, 0.0]

    counted = counted_embedder(embed, budget)
    assert await counted("hello") == [1.0, 0.0]
    with pytest.raises(RuntimeError):
        await counted("boom")
    assert (budget.embedding_calls, budget.used) == (2, 0)
    assert "embedding_calls" not in budget.report()
