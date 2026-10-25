"""``TurnBudget`` planning, reservation and clamping (NFR-PERF-6; #69), and a structured turn's
named reservations and optional work (#156)."""

from __future__ import annotations

import pytest

from epistrel.core.errors import BudgetBelowSafetyMinimum, InvalidRequest
from epistrel.core.profiles.reservation import reservation, safety_minimum
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.orchestrator.budget import (
    COMPOSED,
    EXTRACTION,
    TurnBudget,
    lite_default_calls,
    plan_lite_budget,
)


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


# -- #156: a structured turn's named reservations, optional work and plan ---------------------


def _structured(**fields: object) -> TurnBudget:
    from epistrel.core.seeds.profiles import structured_m4_settings
    from epistrel.orchestrator.structured import planner

    settings = ProfileSettings.model_validate(structured_m4_settings())
    plan = planner.TurnPlan(
        primaries=1,
        beats_planned=6,
        reservation=reservation(settings, guardrail_input=0, guardrail_output=1),
        optional=planner.optional_work(settings),
    )
    values: dict[str, object] = {"story_max_calls": None, "turn_deadline": 45.0}
    values.update(fields)
    return planner.plan_budget(plan, **values)  # type: ignore[arg-type]


def test_a_named_reservation_is_spent_only_by_its_owner() -> None:
    """B and C (owner-only) are never spent by another call: a first ``entailment`` or
    ``salience_detection`` attempt, or a guardrail check past the held pool, sees them as
    spent; ``extraction_backstop`` spends B, and C only while ``charging(COMPOSED)``. A refund
    gives the slot back; a released slot funds optional work."""
    budget = _structured(story_max_calls=5)  # minimum 4: render, B, C, one output check
    assert (budget.held, budget.named) == (2, {EXTRACTION: 1, COMPOSED: 1})
    budget.debit(purpose="generation")
    budget.debit(purpose="guardrail_check")
    assert budget.held == 0
    budget.settle_optional("salience")  # no salience to protect here
    assert budget.affordable("entailment", first=True)  # the one free call
    budget.debit(purpose="entailment")
    assert not budget.affordable("entailment", first=True)
    assert not budget.affordable("salience_detection", first=True)
    assert not budget.can_afford(1)  # only the owners' slots are left
    assert budget.affordable("extraction_backstop", first=True)
    budget.debit(purpose="extraction_backstop")
    assert budget.named == {EXTRACTION: 0, COMPOSED: 1}
    assert not budget.affordable("extraction_backstop", first=True)  # C is not B's
    with budget.charging(COMPOSED):
        assert budget.affordable("extraction_backstop", first=True)
        budget.debit(purpose="extraction_backstop")
        budget.refund("extraction_backstop")  # nothing was sent: the slot comes back
    assert budget.named[COMPOSED] == 1
    assert budget.release_named(COMPOSED) == 1
    assert budget.released == 1
    assert budget.can_afford_unreserved(1)


def test_optional_work_goes_in_priority_order() -> None:
    """Salience's pre-allocation keeps an earlier entailment call from starving it, and once a
    kind is refused no less important kind runs (polish before repairs before entailment)."""
    budget = _structured(story_max_calls=6)
    budget.debit(purpose="generation")
    budget.debit(purpose="guardrail_check")
    # 6 - 2 used - 2 named = 2 free: salience keeps one of them.
    assert budget.pending == {"salience": 1}
    assert budget.affordable("entailment", first=True)
    budget.debit(purpose="entailment")
    assert not budget.affordable("entailment", first=True)  # the last free call is salience's
    assert budget.affordable("salience_detection", first=True)
    budget.debit(purpose="salience_detection")
    budget.settle_optional("salience")
    budget.release_named(EXTRACTION)
    assert budget.can_afford_unreserved(1)  # B's released slot
    # Entailment was refused, so the repair and the polish are too, whatever is left.
    assert not budget.try_optional("repair", 1)
    assert not budget.try_optional("polish", 1)
    assert budget.held == 0


def test_the_plan_and_its_minimum() -> None:
    """``1 + B + C + G``: one model-assisted policy on input and output makes 5; Lite, or a
    structured story without extraction, reserves neither B nor C; a budget below the minimum
    is refused with the reservation; #69's formula is the default cap."""
    from epistrel.core.seeds.profiles import structured_m4_settings
    from epistrel.orchestrator.structured import planner

    settings = ProfileSettings.model_validate(structured_m4_settings())
    assert safety_minimum(settings, guardrail_input=1, guardrail_output=1) == 5
    lite = ProfileSettings()
    assert safety_minimum(lite, guardrail_input=1, guardrail_output=1) == 3
    declared = settings.model_copy(
        update={"verification": settings.verification.model_copy(update={"level": "declared"})}
    )
    assert safety_minimum(declared, guardrail_input=1, guardrail_output=1) == 3
    no_polish = settings.model_copy(
        update={
            "repair": settings.repair.model_copy(
                update={"polish": settings.repair.polish.model_copy(update={"enabled": False})}
            )
        }
    )
    assert safety_minimum(no_polish, guardrail_input=0, guardrail_output=0) == 2
    assert _structured().max_calls == 9 + 1
    with pytest.raises(BudgetBelowSafetyMinimum) as refused:
        _structured(story_max_calls=3)
    assert refused.value.details["minimum_required"] == 4
    assert refused.value.details["reservation"] == {"extraction": 1, "composed": 1, "guardrail": 1}
    assert planner.optional_work(settings) == (
        "salience",
        "entailment",
        "follow_up",
        "repair",
        "polish",
    )
    assert planner.ladder_steps(
        ["entailment_safe_disposition", "salience_skipped", "skip_polish", "safe_fallback"]
    ) == ["skip_polish", "safe_fallback", "entailment_safe_disposition"]


def test_optional_work_never_spends_a_later_renders_held_slot() -> None:
    """#182 (Rule 1, found by P-08's three-character rows): a sequential turn's later renders
    and their output checks are still held while an earlier render's salience pass runs. With
    only held and named slots left, the salience call is refused (``salience_skipped``) instead
    of spending a slot the last render needs, which failed that render (``budget_exhausted``).

    Complement asserted: with one free call beyond the reservation, the salience call fits."""
    from epistrel.core.seeds.profiles import structured_m4_settings
    from epistrel.orchestrator.structured import planner

    settings = ProfileSettings.model_validate(structured_m4_settings())

    def three_primaries(cap: int) -> TurnBudget:
        plan = planner.TurnPlan(
            primaries=3,
            beats_planned=6,
            reservation=reservation(settings, guardrail_input=1, guardrail_output=3, renders=3),
            optional=planner.optional_work(settings),
        )
        assert plan.minimum == 11  # 3 renders, 3 B, C, G = 1 input + 3 output checks
        budget = planner.plan_budget(plan, story_max_calls=cap, turn_deadline=45.0)
        # The input check, the first render, its output check and its extraction.
        for purpose in ("guardrail_check", "generation", "guardrail_check", "extraction_backstop"):
            budget.debit(purpose=purpose)
        return budget

    tight = three_primaries(11)
    assert tight.held == 4  # two renders and their output checks
    assert not tight.affordable("salience_detection", first=True)
    assert tight.affordable("generation", first=True)  # the second render keeps its slot
    assert three_primaries(12).affordable("salience_detection", first=True)
