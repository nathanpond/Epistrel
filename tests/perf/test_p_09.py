"""P-09: budget and degradation (docs/04), the M2 part (NFR-PERF-6,7, FR-OBS-3; #69).

Every Lite turn runs under ``max_model_calls_per_turn`` and ``turn_deadline``: the budget is
planned (one generation plus the reserved model-assisted guardrail calls G) and refused below
that minimum before any model call; a stub that is slow, hangs or fails never makes a turn
exceed its cap or outlive its deadline (plus the P-09 tolerance); and every response reports
``model_calls`` (one entry per attempt, with tokens), ``call_counts``, ``elapsed_ms``, the budget
it spent and its degradation steps, the calls the budget counted equal the calls recorded, and
the same report is stored on the turn's operation."""

from __future__ import annotations

import time
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.engine import Engine
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.policy_support import AUTHOR, story_item, words
from tests.respond_support import FIRST, PLAYER, body, new_story, respond, respond_client
from tests.story_support import WRITE, count, override

TOLERANCE_S = 5.0
DEADLINE_S = 2.0
"""A turn finishes by its deadline plus this (the planner's P-09 tolerance)."""
ASSISTED = {"type": "model_assisted", "prompt": "Is this text about OWLS?", "on_uncertain": "pass"}
FLAGGED = '{"violation": true, "confidence": 0.9}'


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


BUDGET_DIALS = {
    "max_model_calls_per_turn": ("budgets", "model_calls"),
    "turn_deadline": ("budgets", "deadline_ms"),
    "guardrail_retry_budget": ("repair", "polish", "max_regenerations"),
}
"""M2's story budget settings and the profile dials a story overrides for them since #113."""


async def _settings(client: httpx.AsyncClient, story: str, **values: Any) -> dict[str, Any]:
    """Set the story's budget dials (its overrides of its profile, #113; ``None`` removes one, so
    the profile's value stands) and answer its settings with the budget its next turn plans
    with."""
    overrides: dict[str, Any] = {}
    for name, value in values.items():
        *groups, leaf = BUDGET_DIALS[name]
        node = overrides
        for group in groups:
            node = node.setdefault(group, {})
        node[leaf] = int(value * 1000) if name == "turn_deadline" else value
    if overrides:
        await override(client, story, overrides)
    answer = await client.get(f"/stories/{story}/settings", headers=WRITE)
    assert answer.status_code == 200, answer.text
    result: dict[str, Any] = answer.json()
    return result


async def _policies(lib: Engine, story: str, *items: Any) -> None:
    await lib.policies_set_story(AUTHOR, story, list(items))


async def _turn_reports(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    found = await rows(
        engine,
        "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY committed_at",
        s=story,
    )
    return [r[0] for r in found]


def _accounted(turn: dict[str, Any]) -> None:
    """The report is whole: counted == recorded, within the cap, counts by purpose."""
    calls = turn["model_calls"]
    assert turn["budget"]["used"] == len(calls)
    assert turn["budget"]["used"] <= turn["budget"]["max_calls"]
    counts: dict[str, int] = {}
    for call in calls:
        counts[call["purpose"]] = counts.get(call["purpose"], 0) + 1
        assert call["tokens_in"] >= 0
        assert call["tokens_out"] >= 0
    assert turn["call_counts"] == counts
    assert turn["elapsed_ms"] >= 0


@pytest.mark.part("M2", "NFR-PERF-6,7; FR-OBS-3")
async def test_p_09(stub_settings: Settings, engine: AsyncEngine) -> None:
    """One model-assisted input policy and one model-assisted output policy need three calls,
    so a budget of two (the story's cap, or the request's) is 422
    ``budget_below_safety_minimum`` with ``minimum_required`` 3 before any model call (the
    stub's counter stays 0, nothing is recorded); three runs the turn with exactly three
    counted and recorded calls, and the stored turn report matches the response. One policy
    applying to both input and output also counts two. A request override above the story's
    cap, or past its ``turn_deadline``, is 422 ``validation_error``.

    Complement asserted: with only deterministic policies a cap of 1 runs the turn (one
    generation call, nothing reserved)."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST)
        actor = ids[PLAYER]
        turn_ops = "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'turn'"

        # Deterministic only: one call, a cap of one is enough.
        await _policies(
            lib,
            story,
            story_item(words("zzinput"), applies_to=["text_input"]),
            story_item(words("zzoutput")),
        )
        settings = await _settings(client, story, max_model_calls_per_turn=1)
        assert settings["effective_budget"] == {
            "max_model_calls_per_turn": 1,
            "source": "story",
            "turn_deadline": 45.0,
            "reserved": {"guardrail_input": 0, "guardrail_output": 0},
            "minimum_required": 1,
        }
        answer = await respond(client, story, body(actor, "Hello."))
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        assert turn["call_counts"] == {"generation": 1}
        assert turn["budget"] == {
            "max_calls": 1,
            "reserved": {"guardrail_input": 0, "guardrail_output": 0},
            "used": 1,
        }
        assert turn["degradation"] == []
        assert turn["degradation_details"] == []
        _accounted(turn)

        # One model-assisted input policy plus one model-assisted output policy: G = 2.
        await _policies(
            lib,
            story,
            story_item(ASSISTED, name="owls-in", applies_to=["text_input"]),
            story_item(ASSISTED, name="owls-out", applies_to=["text_output"]),
        )
        settings = await _settings(client, story, max_model_calls_per_turn=2)
        assert settings["effective_budget"]["minimum_required"] == 3
        before = await count(engine, turn_ops, s=story)
        stub.reset()
        refused = await respond(client, story, body(actor, "Hello."))
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "budget_below_safety_minimum"
        assert error["details"]["minimum_required"] == 3
        assert error["details"]["max_calls"] == 2
        assert error["details"]["reserved"] == {"guardrail_input": 1, "guardrail_output": 1}
        assert stub.calls == []
        assert await count(engine, turn_ops, s=story) == before

        # The request's own budget is refused the same way, and may not exceed the story's.
        await _settings(client, story, max_model_calls_per_turn=None)
        default = await _settings(client, story)
        assert default["effective_budget"]["max_model_calls_per_turn"] == 9 + 2
        assert default["effective_budget"]["source"] == "formula"
        refused = await respond(client, story, body(actor, "Hello.", budget={"max_calls": 2}))
        assert refused.json()["error"]["code"] == "budget_below_safety_minimum"
        assert stub.calls == []
        over = await respond(client, story, body(actor, "Hello.", budget={"max_calls": 12}))
        assert over.status_code == 422
        assert over.json()["error"]["code"] == "validation_error"
        late = await respond(client, story, body(actor, "Hello.", budget={"deadline_s": 46}))
        assert late.status_code == 422
        assert late.json()["error"]["code"] == "validation_error"
        assert stub.calls == []

        # Three is enough: exactly three calls, counted == recorded == stored.
        answer = await respond(client, story, body(actor, "Hello.", budget={"max_calls": 3}))
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        assert turn["call_counts"] == {"generation": 1, "guardrail_check": 2}
        assert turn["budget"] == {
            "max_calls": 3,
            "reserved": {"guardrail_input": 1, "guardrail_output": 1},
            "used": 3,
        }
        assert len(stub.calls) == 3
        _accounted(turn)
        stored = (await _turn_reports(engine, story))[-1]
        assert stored["budget"] == turn["budget"]
        assert stored["call_counts"] == turn["call_counts"]
        assert stored["model_calls"] == turn["model_calls"]
        assert stored["elapsed_ms"] == turn["elapsed_ms"]
        assert stored["degradation"] == []

        # One policy on both input and output counts two (a story of its own).
        story, ids = await new_story(client, PLAYER, FIRST)
        actor = ids[PLAYER]
        await _policies(
            lib,
            story,
            story_item(ASSISTED, name="owls-both", applies_to=["text_input", "text_output"]),
        )
        settings = await _settings(client, story, max_model_calls_per_turn=2)
        assert settings["effective_budget"]["reserved"] == {
            "guardrail_input": 1,
            "guardrail_output": 1,
        }
        stub.reset()
        refused = await respond(client, story, body(actor, "Hello."))
        assert refused.json()["error"]["details"]["minimum_required"] == 3
        assert stub.calls == []


@pytest.mark.part("M2", "NFR-PERF-6,7; FR-OBS-3")
async def test_p_09__degradation(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A guardrail regeneration the budget cannot afford takes the safe fallback (NFR-PERF-7
    step 2): with a cap of 3 and one model-assisted output policy that flags every draft, the
    turn makes the generation and its check (2 calls), cannot reserve a regeneration and its
    re-check (2 more), and releases the fallback with ``degradation: ["safe_fallback"]``
    (cause ``call_budget``), never exceeding the cap.

    An empty reply is asked again only when the cap leaves an unreserved call (cap 2), else it is
    503 ``reason=empty`` after one call (cap 1).

    Complement asserted: the same story with a cap of 4 regenerates once (4 calls) before its
    retry budget of 1 is spent, and the cause is ``retry_budget``."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST)
        actor = ids[PLAYER]
        await _policies(lib, story, story_item(ASSISTED, name="owls-out"))
        await _settings(client, story, max_model_calls_per_turn=3, guardrail_retry_budget=1)
        stub.when("OWLS?", FLAGGED, purpose="guardrail_check")

        answer = await respond(client, story, body(actor, "Hello."))
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        assert turn["guardrail"]["fallback_used"] is True
        assert turn["degradation"] == ["safe_fallback"]
        assert turn["degradation_details"] == [{"step": "safe_fallback", "cause": "call_budget"}]
        assert turn["call_counts"] == {"generation": 1, "guardrail_check": 1}
        _accounted(turn)
        stored = (await _turn_reports(engine, story))[-1]
        assert stored["degradation_details"] == turn["degradation_details"]

        await _settings(client, story, max_model_calls_per_turn=4)
        turn = (await respond(client, story, body(actor, "Hello again."))).json()
        assert turn["call_counts"] == {"generation": 2, "guardrail_check": 2}
        assert turn["degradation_details"] == [{"step": "safe_fallback", "cause": "retry_budget"}]
        _accounted(turn)

        # An empty reply is asked again only from the unreserved remainder, else 503 empty.
        story, ids = await new_story(client, PLAYER, FIRST)
        actor = ids[PLAYER]
        await _settings(client, story, max_model_calls_per_turn=1)
        stub.reset()
        stub.fault("generation", "empty")
        failed = await respond(client, story, body(actor, "Hello."))
        assert failed.status_code == 503, failed.text
        assert failed.json()["error"]["details"]["reason"] == "empty"
        assert len(stub.calls) == 1
        await _settings(client, story, max_model_calls_per_turn=2)
        stub.fault("generation", "empty")
        turn = (await respond(client, story, body(actor, "Hello."))).json()
        assert turn["call_counts"] == {"generation": 2}
        _accounted(turn)


@pytest.mark.part("M2", "NFR-PERF-6,7; FR-OBS-3")
async def test_p_09__deadline(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A slow stub: every call takes 0.7 s and the output policy flags every draft, under a
    2 s deadline and a retry budget of 5. The first generation answers; the turn regenerates
    until a call is cut at the deadline (cancelled, recorded as ``timeout``) and the safe
    fallback is released with cause ``deadline``, by the deadline plus the tolerance. A hanging
    first generation is 503 ``model_unavailable`` ``reason=deadline`` whose details carry
    ``model_calls`` and ``elapsed_ms``; a failing one (HTTP 500) carries its failed attempt the
    same way.

    Complement asserted: without the policy the same slow stub answers in one call, inside the
    deadline, with no degradation."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST)
        actor = ids[PLAYER]
        await _settings(client, story, turn_deadline=DEADLINE_S, guardrail_retry_budget=5)
        # 0.7 s calls put the deadline inside the second generation, tolerating 0.6 s of gaps.
        stub.latency_s = 0.7

        turn = (await respond(client, story, body(actor, "Hello."))).json()
        assert turn["call_counts"] == {"generation": 1}
        assert turn["degradation"] == []
        assert turn["elapsed_ms"] < DEADLINE_S * 1000

        await _policies(lib, story, story_item(ASSISTED, name="owls-out"))
        stub.when("OWLS?", FLAGGED, purpose="guardrail_check")
        started = time.monotonic()
        answer = await respond(client, story, body(actor, "Hello."))
        took = time.monotonic() - started
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        assert took < DEADLINE_S + TOLERANCE_S
        assert turn["degradation_details"] == [{"step": "safe_fallback", "cause": "deadline"}]
        outcomes = [c["outcome"] for c in turn["model_calls"]]
        assert turn["model_calls"][0]["purpose"] == "generation"
        assert outcomes[0] == "ok"
        assert outcomes[-1] == "timeout"
        assert all(outcome == "ok" for outcome in outcomes[:-1])
        assert turn["call_counts"]["generation"] >= 2
        _accounted(turn)

        # A hanging first generation: no draft to fall back from, so 503 at the deadline.
        stub.latency_s = 0.0
        story, ids = await new_story(client, PLAYER, FIRST)
        actor = ids[PLAYER]
        stub.reset()
        stub.fault("generation", "hang")
        started = time.monotonic()
        failed = await respond(client, story, body(actor, "Hello?", budget={"deadline_s": 0.5}))
        assert time.monotonic() - started < 0.5 + TOLERANCE_S
        assert failed.status_code == 503, failed.text
        details = failed.json()["error"]["details"]
        assert details["reason"] == "deadline"
        assert [(c["purpose"], c["outcome"]) for c in details["model_calls"]] == [
            ("generation", "timeout")
        ]
        assert details["elapsed_ms"] >= 400

        stub.reset()
        stub.fault("generation", "http_500")
        failed = await respond(client, story, body(actor, "Hello!"))
        assert failed.status_code == 503, failed.text
        details = failed.json()["error"]["details"]
        assert [(c["purpose"], c["outcome"]) for c in details["model_calls"]] == [
            ("generation", "http_error")
        ]
        assert "elapsed_ms" in details
