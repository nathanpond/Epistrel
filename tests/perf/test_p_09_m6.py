"""P-09: budget and degradation (docs/04), its M6 parts (NFR-PERF-6,7).

Every P-09 (M6) function lives in this file (the traceability gate counts a test ID per
milestone in one file): #199's time-estimation clause (``__m6_estimation``); #214 adds the
staged-outcome clause.

- ``test_p_09__m6_estimation`` (#199): time estimation is optional work in NFR-PERF-7's order
  after entailment (``budget.OPTIONAL_PRIORITY``: spent before entailment, given up after it),
  pre-allocated once a render narrates an undeclared skip so an earlier entailment call cannot
  starve it. With a budget at the single-primary minimum (render, extraction, the composed
  message's detection) no estimate fits: the undeclared skip ("by the time the stew is ready
  the lamps are lit") is removed by deterministic span removal, with no ``time_estimation``
  call, no model repair and no guessed duration (no ``estimate`` advance), and the response
  lists ``unestimated_skip_removed`` (cause ``budget``) last among its NFR-PERF-7 steps, in
  the ladder's order. An answer under the confidence floor (0.2 < 0.3) is no estimate either:
  the span is removed the same way (cause ``time_estimation``). Complement: with the story's
  default budget and a confident answer the estimate is made (one call) and its advance
  recorded. The call is already counted in NFR-PERF-6's default
  (``LITE_BASE_CALLS`` is unchanged: 4 + 5 for one primary, no guardrail calls).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.settings_schema import LITE_BASE_CALLS
from epistrel.orchestrator.budget import (
    DIRECTOR_TIEBREAK,
    ENTAILMENT,
    OPTIONAL_PRIORITY,
    PURPOSE_KINDS,
    TIME_ESTIMATION,
    TurnBudget,
    lite_default_calls,
)
from epistrel.orchestrator.structured import planner
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

COOKING = "Jenni starts cooking supper."
SKIP = "She cooks on, and by the time the stew is ready the lamps are lit."
STEW = '{"minutes": 45, "reason": "A stew takes about forty-five minutes.", "confidence": 0.7}'
UNSURE = '{"minutes": 45, "reason": "Hard to say.", "confidence": 0.2}'
"""Under ``MIN_CONFIDENCE`` (0.3): no estimate."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _accounted(turn: dict[str, Any]) -> None:
    """P-09's M2 invariant: counted == recorded, within the cap."""
    assert turn["budget"]["used"] == len(turn["model_calls"])
    if turn["budget"]["max_calls"] is not None:
        assert turn["budget"]["used"] <= turn["budget"]["max_calls"]


async def _estimates(engine: AsyncEngine, story: str) -> list[int]:
    found = await rows(
        engine,
        "SELECT c.delta FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
        "WHERE c.story_id = :s AND d.name = 'world_clock' AND c.source = 'estimate'",
        s=story,
    )
    return [int(r.delta) for r in found]


@pytest.mark.part("M6", "NFR-PERF-6,7")
async def test_p_09__m6_estimation(
    stub_settings: Settings,
    engine: AsyncEngine,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    # The kind's place: after the director's tie-break, before entailment (spent before it,
    # given up after it), and the purpose charged to it.
    rank = OPTIONAL_PRIORITY.index
    assert rank(DIRECTOR_TIEBREAK) + 1 == rank(TIME_ESTIMATION) == rank(ENTAILMENT) - 1
    assert PURPOSE_KINDS["time_estimation"] == TIME_ESTIMATION
    # Pre-allocated, the estimate is not starved by an entailment call: the one call left is
    # refused to entailment and granted to the estimate.
    budget = TurnBudget(max_calls=1)
    budget.pending[TIME_ESTIMATION] = 1
    assert not budget.affordable("entailment", first=True)
    assert budget.try_optional(TIME_ESTIMATION, 1)
    # Its ladder step sits after entailment's safe disposition (NFR-PERF-7 (4) → (5)).
    assert planner.ladder_steps(
        [planner.UNESTIMATED_SKIP_REMOVED, planner.ENTAILMENT_SAFE_DISPOSITION, "skip_polish"]
    ) == ["skip_polish", planner.ENTAILMENT_SAFE_DISPOSITION, planner.UNESTIMATED_SKIP_REMOVED]
    # NFR-PERF-6's default is unchanged: the estimate is within the per-primary share.
    assert LITE_BASE_CALLS == lite_default_calls(0) == 4 + 5

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for cap, answered in ((3, STEW), (None, UNSURE), (None, STEW)):
            story, ids = await structured_story(client, "Cal", "Jenni")
            await ck.set_level(engine, story, "on")
            stub.reset()
            ms.renders_for(stub, "Jenni", es.render(beat("do", COOKING), beat("do", SKIP)))
            stub.script("time_estimation", [answered])
            budget_ = {} if cap is None else {"budget": {"max_calls": cap}}
            answer = await respond(client, story, body(ids["Cal"], "Hm?", **budget_))
            assert answer.status_code == 200, answer.text
            turn = answer.json()
            _accounted(turn)
            report = await turn_report(engine, story)
            if cap is not None or answered == UNSURE:
                # No estimate fits (or none usable came back): the span is removed
                # deterministically, nothing guessed, no model repair.
                assert turn["call_counts"].get("time_estimation", 0) == (cap is None)
                assert turn["call_counts"].get("regeneration", 0) == 0
                assert planner.UNESTIMATED_SKIP_REMOVED in turn["degradation"]
                details = {d["step"]: d["cause"] for d in turn["degradation_details"]}
                assert details[planner.UNESTIMATED_SKIP_REMOVED] == (
                    "budget" if cap is not None else "time_estimation"
                )
                # Listed in NFR-PERF-7's order: what went before it, then the removal.
                listed = [s for s in turn["degradation"] if s in planner.RANK]
                assert listed == planner.ladder_steps(listed), turn["degradation"]
                assert listed[-1] == planner.UNESTIMATED_SKIP_REMOVED, turn["degradation"]
                assert SKIP not in turn["message"]["text"]
                assert COOKING in turn["message"]["text"]
                assert await _estimates(engine, story) == []
                assert report["clock"]["skips"]["unestimated"] == 1
                assert [r["tag"] for r in report["repairs"]] == ["unestimated_skip"]
            else:
                # Complement: with headroom the estimate is made and recorded.
                assert turn["call_counts"]["time_estimation"] == 1
                assert planner.UNESTIMATED_SKIP_REMOVED not in turn["degradation"]
                assert SKIP in turn["message"]["text"]
                assert await _estimates(engine, story) == [45]
                assert report["budget"]["used_by_kind"][TIME_ESTIMATION] == 1
