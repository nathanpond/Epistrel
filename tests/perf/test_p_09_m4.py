"""P-09: budget and degradation (docs/04), the M4 part (NFR-PERF-6,7, FR-VERIFY-13,14; #156).

Structured turns are planned before any model call: one primary render plus the safety
reservation S = B + C + G (one extraction per planned render, the composed message's
detection, the model-assisted guardrail calls). A budget below that minimum is refused 422
``budget_below_safety_minimum`` with nothing recorded; at exactly the minimum the turn runs its
reserved calls and nothing optional; and as the budget shrinks the optional work goes in
NFR-PERF-7's order (polish, extra repairs, entailment), with a render that cannot get its
reserved detection before the deadline replaced by the safe fallback. The calls the budget
counted always equal the calls recorded, and every committed beat has a detection record."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.seeds.profiles import structured_m4_settings
from epistrel.engine import Engine
from epistrel.orchestrator.structured import planner
from epistrel.testing.profiles import structured_m4
from tests.auth_support import auth_settings
from tests.escalation_support import CAMP_LINE, camp_story, entailment, regenerated
from tests.event_support import rows
from tests.policy_support import AUTHOR, story_item
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, count, override
from tests.structured_turn_support import (
    age_fact,
    beat,
    claim,
    render,
    structured_story,
    turn_report,
)

ASSISTED = {"type": "model_assisted", "prompt": "Is this text about OWLS?", "on_uncertain": "pass"}
FLAGGED = '{"violation": true, "confidence": 0.9}'
LADDER = (
    planner.SKIP_POLISH,
    planner.REPAIR_DETERMINISTIC,
    planner.SAFE_FALLBACK,
    planner.ENTAILMENT_SAFE_DISPOSITION,
    planner.BEAT_REPLACED,
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _cap(client: httpx.AsyncClient, story: str, calls: int | None) -> None:
    await override(client, story, {"budgets": {"model_calls": calls}})


async def _policies(lib: Engine, story: str, *items: Any) -> None:
    await lib.policies_set_story(AUTHOR, story, list(items))


def _accounted(turn: dict[str, Any]) -> None:
    """P-09's M2 invariant on a structured turn: counted == recorded, within the cap."""
    calls = turn["model_calls"]
    assert turn["budget"]["used"] == len(calls)
    assert turn["budget"]["used"] <= turn["budget"]["max_calls"]
    counts: dict[str, int] = {}
    for call in calls:
        counts[call["purpose"]] = counts.get(call["purpose"], 0) + 1
    assert turn["call_counts"] == counts


async def _detected(engine: AsyncEngine, story: str) -> None:
    """Every committed beat of the story's latest turn has a detection record."""
    report = await turn_report(engine, story)
    recorded = {b["beat"] for b in report["detection"]["beats"]}
    found = await rows(
        engine,
        "SELECT e.beat FROM events e JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE o.story_id = :s AND o.kind = 'turn' AND e.type LIKE 'beat_%' "
        "AND o.committed_at = (SELECT max(committed_at) FROM operations "
        "WHERE story_id = :s AND kind = 'turn')",
        s=story,
    )
    beats = {int(r.beat) for r in found}
    assert beats, "the turn committed beats"
    assert beats <= recorded, (beats, recorded)


def _ladder(turn: dict[str, Any]) -> list[str]:
    return [s for s in turn["degradation"] if s in LADDER]


def _calls_counted(purpose: str) -> float:
    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != "epistrel_model_calls":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels.get("purpose") == purpose:
                total += sample.value
    return total


def _camp_render() -> str:
    """The camp say (``uncertain``, so entailment decides it: ``contradicted``, repaired by a
    regeneration) and a second beat, so the polish has something to compose."""
    return render(
        beat(
            "say",
            CAMP_LINE,
            intent="sincere",
            propositions=[claim("I", "attended_program", "Florida")],
        ),
        beat("do", "He grins at Cal."),
    )


@pytest.mark.part("M4", "NFR-PERF-6,7; FR-VERIFY-13,14")
async def test_p_09__structured(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety minimum, the reservations and the degradation order of structured turns.

    - One model-assisted policy on both input and output makes the one-primary minimum
      exactly 5 (render, extraction, composed-message detection, input check, output check):
      a cap of 4 (the story's, or a request's ``budget.max_calls``) is 422
      ``budget_below_safety_minimum`` with ``minimum_required`` 5 and the reservation, before
      any model call and with nothing recorded; 5 runs, with its reserved calls made and
      nothing optional (polish and salience skipped, the composed message's reservation
      released by the deterministic assembly). Profile validation warns about such a cap.
    - At exactly the minimum a contradicted say gets no model repair (the deterministic rungs,
      then the safe fallback) and the turn reports it; complement: a budget above the minimum
      does attempt the model repair.
    - The camp turn (salience, entailment, a model repair and a polish) under shrinking caps
      gives its optional work up in NFR-PERF-7's order: ``skip_polish``, then
      ``repair_deterministic`` (the safe fallback), then ``entailment_safe_disposition``;
      salience goes last (``salience_skipped``: every beat is texture, the ledger stays
      empty). A slow stub whose render leaves no time for the reserved extraction replaces the
      render by the safe fallback (``beat_replaced_safe_fallback``).
    - An output-policy violation whose repair cannot reserve its re-render and re-check takes
      the stored safe fallback.

    Every turn: counted == recorded, the cap never exceeded, every committed beat detected,
    ``profile``, ``quality_status`` and ``degradation`` on the response, and ``model_calls``
    by purpose in #54's counters."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        turn_ops = "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'turn'"

        # -- 5 runs, 4 is refused: one model-assisted policy on input and output -----------
        story, ids = await structured_story(client, "Wren", "Ada")
        wren = ids["Wren"]
        await _policies(
            lib, story, story_item(ASSISTED, name="owls", applies_to=["text_input", "text_output"])
        )
        await _cap(client, story, 4)
        settings = (await client.get(f"/stories/{story}/settings", headers=WRITE)).json()
        assert settings["effective_budget"]["minimum_required"] == 5
        stub.reset()
        refused = await respond(client, story, body(wren, "Hello."))
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "budget_below_safety_minimum"
        assert error["details"]["minimum_required"] == 5
        assert error["details"]["max_calls"] == 4
        assert error["details"]["reservation"] == {"extraction": 1, "composed": 1, "guardrail": 2}
        assert stub.calls == []
        assert await count(engine, turn_ops, s=story) == 0
        await _cap(client, story, 5)
        refused = await respond(client, story, body(wren, "Hello.", budget={"max_calls": 4}))
        assert refused.json()["error"]["code"] == "budget_below_safety_minimum"
        assert stub.calls == []
        assert await count(engine, turn_ops, s=story) == 0

        extraction_before = _calls_counted("extraction_backstop")
        stub.script("generation", [render(beat("say", "Hello, Wren."), beat("do", "Ada waves."))])
        answer = await respond(client, story, body(wren, "Hello."))
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        assert turn["call_counts"] == {
            "generation": 1,
            "extraction_backstop": 1,
            "guardrail_check": 2,
        }
        assert turn["budget"]["max_calls"] == 5
        _accounted(turn)
        assert _ladder(turn) == [planner.SKIP_POLISH]
        assert planner.SALIENCE_SKIPPED in turn["degradation"]
        assert not [c for c in stub.calls if c.purpose == "regeneration"]
        assert turn["profile"] == {"id": structured_m4(), "version": 1}
        assert turn["quality_status"] is not None
        report = await turn_report(engine, story)
        assert report["budget"]["minimum"] == 5
        assert report["budget"]["released"] == 1  # C, the deterministic assembly
        assert report["budget"]["used_by_kind"] == {"extraction": 1, "guardrail": 2, "render": 1}
        assert report["composition"]["mode"] == "deterministic"
        assert report["commit"]["salience"]["entries"] == 0
        assert _calls_counted("extraction_backstop") == extraction_before + 1
        await _detected(engine, story)

        # Profile validation warns about a cap below the structured minimum.
        document = structured_m4_settings()
        document["budgets"]["model_calls"] = 2
        validated = await client.post(
            f"/engine-profiles/{structured_m4()}/validate",
            json={"settings": document},
            headers=WRITE,
        )
        assert validated.status_code == 200, validated.text
        (warning,) = validated.json()["warnings"]
        assert warning["code"] == "below_safety_minimum"
        assert warning["model_calls"] == 2
        assert warning["minimum_required"] >= 3
        assert warning["reservation"]["extraction"] == 1
        assert warning["reservation"]["composed"] == 1
        document["budgets"]["model_calls"] = None
        quiet = await client.post(
            f"/engine-profiles/{structured_m4()}/validate",
            json={"settings": document},
            headers=WRITE,
        )
        assert quiet.json()["warnings"] == []

        # -- exactly the minimum: no model repair; complement: above it, the model repair ---
        for cap, repaired in ((3, False), (None, True)):
            story, ids = await structured_story(client, "Wren", "Ada")
            await age_fact(engine, story, ids["Ada"], 64)
            await _cap(client, story, cap)
            stub.reset()
            stub.script(
                "generation",
                [
                    render(
                        beat(
                            "say",
                            "I am seventy years old.",
                            intent="sincere",
                            propositions=[claim("I", "age", "70")],
                        )
                    )
                ],
            )
            stub.script("regeneration", [regenerated("I'm sixty-four.")])
            turn = (await respond(client, story, body(ids["Wren"], "How old are you?"))).json()
            _accounted(turn)
            regenerations = [c for c in stub.calls if c.purpose == "regeneration"]
            assert bool(regenerations) is repaired, turn["call_counts"]
            if repaired:
                assert planner.REPAIR_DETERMINISTIC not in turn["degradation"]
            else:
                assert turn["call_counts"] == {"generation": 1, "extraction_backstop": 1}
                assert _ladder(turn) == [planner.REPAIR_DETERMINISTIC, planner.SAFE_FALLBACK]
                details = {d["step"]: d["cause"] for d in turn["degradation_details"]}
                assert details[planner.REPAIR_DETERMINISTIC] == "budget"
            await _detected(engine, story)

        # -- the degradation order: polish, then repairs, then entailment ------------------
        expected = {
            None: [],
            7: [planner.SKIP_POLISH],
            6: [planner.SKIP_POLISH, planner.REPAIR_DETERMINISTIC, planner.SAFE_FALLBACK],
            4: [
                planner.SKIP_POLISH,
                planner.REPAIR_DETERMINISTIC,
                planner.SAFE_FALLBACK,
                planner.ENTAILMENT_SAFE_DISPOSITION,
            ],
            3: [
                planner.SKIP_POLISH,
                planner.REPAIR_DETERMINISTIC,
                planner.SAFE_FALLBACK,
                planner.ENTAILMENT_SAFE_DISPOSITION,
            ],
        }
        for cap, ladder in expected.items():
            edu = await camp_story(client, engine)
            await _cap(client, edu.story_id, cap)
            stub.reset()
            stub.script("generation", [_camp_render()])
            stub.script("entailment", [entailment("contradicted")])
            stub.script("regeneration", [regenerated()])
            answer = await respond(client, edu.story_id, body(edu.cal, "Where did you learn?"))
            assert answer.status_code == 200, answer.text
            turn = answer.json()
            _accounted(turn)
            assert _ladder(turn) == ladder, (cap, turn["degradation"])
            # Salience is the last optional work given up: only at the minimum.
            assert (planner.SALIENCE_SKIPPED in turn["degradation"]) is (cap == 3)
            counts = turn["call_counts"]
            assert counts.get("regeneration", 0) == (1 if cap in (None, 7) else 0)
            assert counts.get("entailment", 0) == (0 if cap in (3, 4) else 1)
            assert counts.get("generation") == (2 if cap is None else 1)  # the polish
            report = await turn_report(engine, edu.story_id)
            if cap == 3:
                assert report["commit"]["salience"]["entries"] == 0
                assert counts == {"generation": 1, "extraction_backstop": 1}
            await _detected(engine, edu.story_id)

        # -- a slow stub: the render leaves no time for its reserved extraction ------------
        monkeypatch.setattr(planner, "DEFAULT_LATENCY_ESTIMATE_S", 9.5)
        story, ids = await structured_story(client, "Wren", "Ada")
        stub.reset()
        stub.latency_s = 1.0
        stub.script(
            "generation", [render(beat("say", "The vault code is 1234."), beat("do", "Ada nods."))]
        )
        try:
            answer = await respond(
                client, story, body(ids["Wren"], "Hello.", budget={"deadline_s": 10})
            )
        finally:
            stub.latency_s = 0.0
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        _accounted(turn)
        assert _ladder(turn) == [planner.BEAT_REPLACED]
        assert turn["call_counts"] == {"generation": 1}  # nothing after the render could start
        assert "1234" not in turn["message"]["text"]
        stored = await rows(
            engine, "SELECT payload FROM events WHERE story_id = :s AND type LIKE 'beat_%'", s=story
        )
        assert stored
        assert all("1234" not in str(r.payload) for r in stored)
        await _detected(engine, story)

        # -- an output-policy repair that cannot reserve its re-render and re-check --------
        story, ids = await structured_story(client, "Wren", "Ada")
        await _policies(lib, story, story_item(ASSISTED, name="owls-out"))
        await _cap(client, story, 4)  # render + B + C + one output check: the minimum
        stub.reset()
        stub.when("OWLS?", FLAGGED, purpose="guardrail_check")
        turn = (await respond(client, story, body(ids["Wren"], "Hello."))).json()
        _accounted(turn)
        assert turn["guardrail"]["fallback_used"] is True
        details = {d["step"]: d["cause"] for d in turn["degradation_details"]}
        assert details[planner.SAFE_FALLBACK] == "call_budget"
        assert turn["call_counts"]["generation"] == 1
        await _detected(engine, story)
        # Complement: two more calls pay for one regeneration and its re-check.
        await _cap(client, story, 6)
        turn = (await respond(client, story, body(ids["Wren"], "Hello again."))).json()
        _accounted(turn)
        assert turn["call_counts"]["generation"] == 2
