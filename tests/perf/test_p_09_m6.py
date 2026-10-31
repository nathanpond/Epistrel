"""P-09: budget and degradation (docs/04), its M6 parts (NFR-PERF-6,7).

Every P-09 (M6) function lives in this file (the traceability gate counts a test ID per
milestone in one file): #199's time-estimation clause (``__m6_estimation``) and #214's
staged-outcome clause (``__m6``: the outcome step's reservation O, see its docstring).

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
from epistrel.core.profiles.effective import StoryProfile, effective_configuration
from epistrel.core.profiles.reservation import SafetyFacts, outcome_calls, safety_minimum
from epistrel.core.seeds.profiles import structured_m4_settings
from epistrel.core.settings_schema import LITE_BASE_CALLS
from epistrel.orchestrator.budget import (
    DIRECTOR_ORDER,
    DIRECTOR_TIEBREAK,
    ENTAILMENT,
    EXTRACTION,
    OPTIONAL_PRIORITY,
    OUTCOME,
    POLISH,
    PURPOSE_KINDS,
    TIME_ESTIMATION,
    TurnBudget,
    lite_default_calls,
)
from epistrel.orchestrator.structured import planner
from epistrel.testing.profiles import structured_m4
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import staged_support as sts
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.policy_support import AUTHOR, story_item
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, override
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


# -- #214: the outcome step's reservation (O) --------------------------------------------------

ASSISTED = {"type": "model_assisted", "prompt": "Is this text about OWLS?", "on_uncertain": "pass"}
TALK = ("I keep my own counsel.", "Mira wipes the counter.")
"""A render of two beats with no effect (the polish has something to compose)."""


async def _owls(lib: Any, story: str) -> None:
    """One model-assisted output-only story policy (G = G_out = 1)."""
    await lib.policies_set_story(
        AUTHOR, story, [story_item(ASSISTED, name="owls", applies_to=["text_output"])]
    )


async def _profile_of(engine: AsyncEngine, story: str) -> StoryProfile:
    async with engine.connect() as conn:
        return await effective_configuration(conn, story, validate=False)


async def _effective_budget(client: Any, story: str) -> dict[str, Any]:
    found = await client.get(f"/stories/{story}/settings", headers=WRITE)
    assert found.status_code == 200, found.text
    budget: dict[str, Any] = found.json()["effective_budget"]
    return budget


async def _turns(engine: AsyncEngine, story: str) -> int:
    found = await rows(
        engine,
        "SELECT count(*) AS n FROM operations WHERE story_id = :s AND kind = 'turn'",
        s=story,
    )
    return int(found[0].n)


@pytest.mark.part("M6", "NFR-PERF-6,7; FR-EXT-16")
async def test_p_09__m6(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#214: a story that may stage its turns reserves the outcome step (O).

    Cal and Mira; Cal's gold is owned by a bound ``synchronous`` write-capable provider
    (``staged_support.sync_story``), ``states`` on, and one model-assisted output-only story
    policy. O = 3 (the outcome render, its extraction, the second output check), so the
    one-primary minimum is exactly 7 (render, extraction, composed detection, output check, the
    outcome render, its extraction, the second output check):

    - a cap of 6 is 422 ``budget_below_safety_minimum`` (``minimum_required`` 7, the reservation
      naming O) before any model call, nothing recorded; the story's effective budget states
      the minimum 7, and binding the synchronous provider (``PUT …/binding``, a story-aware
      surface) warns ``below_safety_minimum`` at 6 and not at 7;
    - at 7 a purchase becomes a staged turn whose outcome step is model-rendered: two renders,
      two extractions, two output checks (the intent's beats before intent, the completed
      message's outcome step after), within the cap;
    - at 7 a turn that requests no effect releases O after its render, and the polish uses it
      (complement: no ``skip_polish``);
    - with the cap ``null`` the formula's default is 4 + 5 + 1 + 3 = 13.

    Complement: the same story without a synchronous provider has O = 0: minimum 4, default 10.
    The safety minimum takes ``outcome`` on the story-aware surfaces (the planner, the impact
    preview of the story) and 0 on the profile's own (validation); P primaries plan ``P + B +
    C + G + O`` and default to ``4 + 5·P + G + O``; and O is a named slot nothing but the
    outcome step spends (the director, an escalation, a re-hold, optional work)."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub

        # -- the synchronous story ----------------------------------------------------------
        story, cal, name, fake = await sts.sync_story(client, engine, lib)
        await _owls(lib, story)
        profile = await _profile_of(engine, story)
        settings = profile.settings
        assert outcome_calls(settings, synchronous=True, guardrail_output=1) == 3
        assert outcome_calls(settings, synchronous=False, guardrail_output=1) == 0
        assert safety_minimum(settings, guardrail_input=0, guardrail_output=1, outcome=3) == 7
        # The story-aware facts count O, a profile's own do not (owner decision 13).
        assert SafetyFacts(guardrail_output=1, synchronous=True).minimum(settings) == 7
        assert SafetyFacts(guardrail_output=1).minimum(settings) == 4
        capped = settings.model_copy(
            update={"budgets": settings.budgets.model_copy(update={"model_calls": 6})}
        )
        (warning,) = SafetyFacts(guardrail_output=1, synchronous=True).warnings(capped)
        assert (warning["minimum_required"], warning["reservation"]["outcome"]) == (7, 3)
        assert SafetyFacts(guardrail_output=1).warnings(capped) == []

        await override(client, story, {"budgets": {"model_calls": 6}})
        assert (await _effective_budget(client, story))["minimum_required"] == 7
        turns = await _turns(engine, story)
        stub.reset()
        refused = await sts.buy(client, story, cal)
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "budget_below_safety_minimum"
        assert (error["details"]["minimum_required"], error["details"]["max_calls"]) == (7, 6)
        assert error["details"]["reservation"] == {
            "extraction": 1,
            "composed": 1,
            "guardrail": 1,
            "outcome": 3,
        }
        assert stub.calls == []
        assert await _turns(engine, story) == turns  # nothing recorded

        # Binding the synchronous provider is a story-aware surface: it warns at 6, not at 7.
        put = f"/stories/{story}/providers/{name}/binding"
        bound = await client.put(put, json={"external_ids": {"player": "p-1"}}, headers=WRITE)
        assert bound.status_code == 200, bound.text
        (warned,) = bound.json()["warnings"]
        assert warned["code"] == "below_safety_minimum"
        assert (warned["model_calls"], warned["minimum_required"]) == (6, 7)
        assert warned["reservation"]["outcome"] == 3
        await override(client, story, {"budgets": {"model_calls": 7}})
        bound = await client.put(put, json={"external_ids": {"player": "p-1"}}, headers=WRITE)
        assert bound.json()["warnings"] == []

        # -- at 7: a staged purchase with a model-rendered outcome step ----------------------
        fake.script_writes("gold", "confirmed")
        stub.reset()
        sts.outcome_step(stub, beat("narration", sts.WENT))
        ms.renders_for(stub, "Mira", sts.purchase(cal))
        answer = await sts.buy(client, story, cal)
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        _accounted(turn)
        assert turn["budget"]["max_calls"] == 7
        assert turn["call_counts"] == {
            "generation": 2,
            "extraction_backstop": 2,
            "guardrail_check": 2,
        }
        assert await sts.stage_names(engine, story) == [
            "intent",
            "claim",
            "outcome",
            "completion",
            "response",
        ]
        report = await turn_report(engine, story)
        assert report["staged"]["outcome_step"]["deterministic"] is False
        assert report["budget"]["reservation"]["outcome"] == 3
        assert sts.WENT in turn["message"]["text"]
        await ps.drop_outbox_jobs(engine, story)

        # -- at 7: no effect, so O is released after the render and the polish uses it -------
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", TALK[0]), beat("do", TALK[1])))
        answer = await respond(client, story, body(cal, "Anything new?"))
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        _accounted(turn)
        assert planner.SKIP_POLISH not in turn["degradation"], turn["degradation"]
        report = await turn_report(engine, story)
        assert report["composition"]["mode"] != "deterministic"
        assert report["budget"]["released"] >= 3  # O, back to the pool
        assert "staged" not in report

        # -- the formula's default: 4 + 5 + G + O = 13 ---------------------------------------
        await override(client, story, {"budgets": {"model_calls": None}})
        assert (await _effective_budget(client, story))["max_model_calls_per_turn"] == 13
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", TALK[0]), beat("do", TALK[1])))
        answer = await respond(client, story, body(cal, "And now?"))
        assert answer.status_code == 200, answer.text
        assert answer.json()["budget"]["max_calls"] == 13

        # The impact preview of this story is story-aware: O counts; validation is not.
        preview = await client.post(
            f"/stories/{story}/impact-preview",
            json={"overrides": {"budgets": {"model_calls": 4}}},
            headers=WRITE,
        )
        assert preview.status_code == 200, preview.text
        (impact_warning,) = preview.json()["budget_warnings"]
        reservation = impact_warning["reservation"]
        assert reservation["outcome"] == 2 + reservation["guardrail_output"]
        assert impact_warning["minimum_required"] == 1 + sum(
            reservation[k] for k in ("extraction", "composed", "guardrail", "outcome")
        )
        document = structured_m4_settings()
        document["budgets"]["model_calls"] = 2
        validated = await client.post(
            f"/engine-profiles/{structured_m4()}/validate",
            json={"settings": document},
            headers=WRITE,
        )
        assert validated.status_code == 200, validated.text
        (profile_warning,) = validated.json()["warnings"]
        assert profile_warning["reservation"]["outcome"] == 0  # a profile knows no providers
        await ss.set_level(engine, story, None)

        # -- complement: no synchronous provider, O = 0 --------------------------------------
        plain, ids = await structured_story(client, "Cal", "Mira")
        await ss.set_level(engine, plain, "on")
        await _owls(lib, plain)
        await override(client, plain, {"budgets": {"model_calls": 4}})
        assert (await _effective_budget(client, plain))["minimum_required"] == 4
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", TALK[0]), beat("do", TALK[1])))
        answer = await respond(client, plain, body(ids["Cal"], "Hello."))
        assert answer.status_code == 200, answer.text
        _accounted(answer.json())
        assert "outcome" not in (await turn_report(engine, plain))["budget"]["reservation"]
        await override(client, plain, {"budgets": {"model_calls": None}})
        assert (await _effective_budget(client, plain))["max_model_calls_per_turn"] == 10
        await ss.set_level(engine, plain, None)

    # -- P primaries, and O as a named slot -----------------------------------------------------
    for primaries in (1, 2, 3):
        plan = planner.plan_turn(
            None,
            profile,
            guardrail_input=0,
            guardrail_output=1,
            primaries=primaries,
            outcome=3,
        )
        b, c, g = primaries, 1, primaries  # one extraction and one output check per render
        assert plan.minimum == primaries + b + c + g + 3
        assert planner.default_calls(plan) == 4 + 5 * primaries + g + 3
    plan = planner.plan_turn(None, profile, guardrail_input=0, guardrail_output=1, outcome=3)
    assert (plan.minimum, planner.default_calls(plan)) == (7, 13)
    budget = planner.plan_budget(plan, story_max_calls=7, turn_deadline=60.0)
    assert budget.named[OUTCOME] == 3
    budget.debit(purpose="generation")  # the render
    budget.debit(purpose="extraction_backstop")  # its extraction
    budget.debit(purpose="guardrail_check")  # its output check
    # Nothing but the outcome step spends O: no optional work, no re-hold, no director.
    assert not budget.can_afford_unreserved(1)
    assert not budget.try_optional(POLISH, 1)
    assert not budget.affordable("generation", first=True)  # a re-render, an escalation
    budget.release_to(10)
    assert budget.held == 0  # nothing to re-hold: O is not held
    assert not budget.try_optional(DIRECTOR_ORDER, 1)
    assert budget.named[OUTCOME] == 3
    # Staged: the outcome step draws it (render and check held, its extraction owner-only).
    staged_budget = planner.plan_budget(plan, story_max_calls=7, turn_deadline=60.0)
    assert planner.draw_outcome(staged_budget, plan) == 3
    assert OUTCOME not in staged_budget.named
    assert staged_budget.named[EXTRACTION] == 2  # the render's B and the outcome step's
    # Not staged: released after the last render, it funds the polish.
    budget.denied.clear()
    assert planner.release_outcome(budget) == 3
    assert budget.try_optional(POLISH, 2)
