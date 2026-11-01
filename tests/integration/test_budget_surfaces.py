"""#456: the structured budget's surfaces (NFR-PERF-6,7, FR-PROF-7, FR-OBS-1).

- ``model_calls[].stage``: every call a turn's budget counted names the stage it was charged to
  (``render``, ``narrator``, ``extraction``, ``composed``, ``guardrail``, ...), and the stages
  count exactly what ``turn_report.budget.used_by_kind`` counts; the stored result an operation
  read returns carries them too, and so does the library's result.
- The ``below_safety_minimum`` warning on profile writes (``POST /engine-profiles``, its copy,
  ``PUT /engine-profiles/{id}``) and in the impact reports of a profile edit, a story's
  overrides and the impact preview, computed as ``POST /engine-profiles/{id}/validate`` does.
  Complement: a cap at or above the minimum warns of nothing.
- ``epistrel_turn_degradation_total{step}`` counts each step a committed turn reports, and
  ``epistrel_budget_rejected_total`` each call refused ``budget_below_safety_minimum`` (REST
  and library alike), with label values from the step names only (invariant 3).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import httpx2 as httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import ProfileCreate, RequestContext, RespondRequest
from epistrel.config import Settings
from epistrel.core.errors import BudgetBelowSafetyMinimum
from epistrel.core.seeds.profiles import structured_m4_settings
from epistrel.observability.metrics import OTHER_STEP, step_label
from epistrel.testing.profiles import structured_m4
from tests.auth_support import auth_settings
from tests.event_support import scalar
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, override
from tests.structured_turn_support import beat, render, structured_story, turn_report

BELOW = "below_safety_minimum"
LABEL = re.compile(r"^[a-z_]+$")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _settings(model_calls: int | None) -> dict[str, Any]:
    document = structured_m4_settings()
    document["budgets"]["model_calls"] = model_calls
    return document


def _below(warnings: list[dict[str, Any]], model_calls: int) -> dict[str, Any]:
    (warning,) = warnings
    assert warning["code"] == BELOW
    assert warning["setting"] == "budgets.model_calls"
    assert warning["model_calls"] == model_calls
    assert warning["minimum_required"] >= 3  # render, extraction, composed detection (+ G)
    assert warning["reservation"]["extraction"] == 1
    assert warning["reservation"]["composed"] == 1
    return warning


async def test_model_calls_name_their_stage(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A structured turn the narrator opens (its input addresses no character): the narrator's
    render is ``narrator``, the character's ``render``, the extraction calls ``extraction`` or
    ``composed``; every counted call has a stage and the stages count ``used_by_kind``. The
    operation read's stored result and the library's result carry the same."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")
        stub.script(
            "generation",
            [
                render(beat("narration", "The kitchen door swings open.")),
                render(beat("do", "Ada looks up.")),
            ],
        )
        answer = await respond(client, story, body(ids["Wren"], None, do="waits by the window"))
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        calls = turn["model_calls"]
        counted = [c for c in calls if c["purpose"] != "embedding"]
        assert counted, calls
        assert all("stage" in c for c in counted), calls
        renders = [c["stage"] for c in counted if c["purpose"] == "generation"]
        assert renders[:2] == ["narrator", "render"], renders
        assert {c["stage"] for c in counted if c["purpose"] == "extraction_backstop"} <= {
            "extraction",
            "composed",
        }
        report = await turn_report(engine, story)
        used = report["budget"]["used_by_kind"]
        assert used["narrator"] == 1, used
        assert used["render"] == 1, used
        assert Counter(c["stage"] for c in counted) == Counter(used)
        assert [c.get("stage") for c in report["model_calls"]] == [c.get("stage") for c in calls]

        # The operation read: the stored response carries the stages.
        operation_id = await scalar(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND status = 'committed' ORDER BY narrative_turn DESC LIMIT 1",
            s=story,
        )
        operation = await client.get(
            f"/operations/{operation_id}",
            params={"viewpoint": turn["output_viewpoint"]},
            headers=WRITE,
        )
        assert operation.status_code == 200, operation.text
        stored = operation.json()["result"]["body"]["model_calls"]
        assert [c.get("stage") for c in stored] == [c.get("stage") for c in calls]

        # The library: the same stages on the result's ``ModelCall`` entries.
        stub.script("generation", [render(beat("say", "Hello, Wren."))])
        result = await lib.respond(
            RequestContext(principal="p-author"),
            story,
            RespondRequest.model_validate(body(ids["Wren"], "Hello, Ada.")),
        )
        stages = {c.purpose: c.stage for c in result.model_calls if c.purpose != "embedding"}
        assert stages["generation"] == "render", result.model_calls


async def test_profile_writes_and_impact_reports_warn_below_the_minimum(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``below_safety_minimum`` on a create, a copy, an edit (in its impact report and on the
    result's profile write), an overrides patch and the impact preview, as the validate route
    answers it. Complement: no warning at or above the minimum, or with no cap."""
    async with respond_client(stub_settings, engine) as (client, lib):
        created = await client.post(
            "/engine-profiles",
            json={"name": "Tight structured", "settings": _settings(2)},
            headers=WRITE,
        )
        assert created.status_code == 201, created.text
        profile = created.json()
        warning = _below(profile["warnings"], 2)
        validated = await client.post(f"/engine-profiles/{profile['id']}/validate", headers=WRITE)
        assert validated.json()["warnings"] == [warning]  # the same computation

        copied = await client.post(
            f"/engine-profiles/{profile['id']}/copy", json={"name": "Tight copy"}, headers=WRITE
        )
        assert copied.status_code == 201, copied.text
        assert copied.json()["warnings"] == [warning]

        quiet = await client.post(
            "/engine-profiles",
            json={"name": "Roomy structured", "settings": _settings(None)},
            headers=WRITE,
        )
        assert quiet.status_code == 201, quiet.text
        assert quiet.json()["warnings"] == []

        # An edit down to 2: the report warns (informational: it does not hold the edit).
        edited = await client.put(
            f"/engine-profiles/{quiet.json()['id']}",
            json={"name": "Roomy structured", "settings": _settings(2)},
            headers=WRITE,
        )
        assert edited.status_code == 200, edited.text
        edit = edited.json()
        assert edit["applied"] is True, edit
        assert edit["impact"]["budget_warnings"] == [warning]
        assert BELOW not in edit["impact"]["warnings"]
        back = await client.put(
            f"/engine-profiles/{quiet.json()['id']}",
            json={"name": "Roomy structured", "settings": _settings(warning["minimum_required"])},
            headers=WRITE,
        )
        assert back.json()["impact"]["budget_warnings"] == []

        # A story's overrides and the impact preview: the story's configuration after.
        story, _ = await structured_story(client, "Wren", "Ada")
        preview = await client.post(
            f"/stories/{story}/impact-preview",
            json={"overrides": {"budgets": {"model_calls": 2}}},
            headers=WRITE,
        )
        assert preview.status_code == 200, preview.text
        _below(preview.json()["budget_warnings"], 2)
        patched = await override(client, story, {"budgets": {"model_calls": 2}})
        _below(patched["impact"]["budget_warnings"], 2)
        roomy = await client.post(
            f"/stories/{story}/impact-preview",
            json={"overrides": {"budgets": {"model_calls": 40}}},
            headers=WRITE,
        )
        assert roomy.json()["budget_warnings"] == []
        assigned = await client.put(
            f"/stories/{story}/engine-profile",
            json={"profile_id": structured_m4(), "ignore_warnings": True},
            headers=WRITE,
        )
        assert assigned.status_code == 200, assigned.text
        _below(assigned.json()["impact"]["budget_warnings"], 2)  # the override still holds

        # The library answers what REST does.
        write = await lib.create_profile(
            RequestContext(principal="p-author"),
            ProfileCreate(name="Tight library", settings=_settings(2)),
        )
        assert [w.model_dump(mode="json") for w in write.warnings] == [warning]


async def _degraded(client: httpx.AsyncClient, story: str, actor: str) -> dict[str, Any]:
    answer = await respond(client, story, body(actor, "Hello."))
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


async def test_degradation_and_budget_rejection_counters(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A turn at exactly the structured minimum degrades (``skip_polish``, ...): each step it
    reports adds one to ``epistrel_turn_degradation_total{step}``. A cap below the minimum is
    refused 422 ``budget_below_safety_minimum`` and adds one to
    ``epistrel_budget_rejected_total``, over REST and the library alike. ``/metrics`` lists both,
    with step names as their only label values."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")
        await override(client, story, {"budgets": {"model_calls": 3}})
        stub.script("generation", [render(beat("say", "Hello, Wren."), beat("do", "Ada waves."))])
        before = {
            step: _sample("epistrel_turn_degradation_total", step=step)
            for step in ("skip_polish", "salience_skipped", "narrator_skipped")
        }
        turn = await _degraded(client, story, ids["Wren"])
        steps = turn["degradation"]
        assert "skip_polish" in steps, steps
        for step in steps:
            expected = before.get(step)
            if expected is not None:
                assert _sample("epistrel_turn_degradation_total", step=step) == expected + 1, step
        # Complement: a step the turn did not take did not move.
        for step, value in before.items():
            if step not in steps:
                assert _sample("epistrel_turn_degradation_total", step=step) == value, step

        rejected = _sample("epistrel_budget_rejected_total")
        await override(client, story, {"budgets": {"model_calls": 2}})
        refused = await respond(client, story, body(ids["Wren"], "Hello."))
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "budget_below_safety_minimum"
        assert _sample("epistrel_budget_rejected_total") == rejected + 1
        with pytest.raises(BudgetBelowSafetyMinimum):
            await lib.respond(
                RequestContext(principal="p-author"),
                story,
                RespondRequest.model_validate(body(ids["Wren"], "Hello.")),
            )
        assert _sample("epistrel_budget_rejected_total") == rejected + 2
        # Complement: an ordinary refusal of another kind does not count.
        unknown = await respond(client, story, body("00000000000000000000000000", "Hello."))
        assert unknown.status_code >= 400
        assert _sample("epistrel_budget_rejected_total") == rejected + 2

        text = (await client.get("/metrics")).text
        assert "epistrel_budget_rejected_total" in text
        labels = re.findall(r'^epistrel_turn_degradation_total\{step="([^"]*)"\}', text, re.M)
        assert "skip_polish" in labels
        assert all(LABEL.match(label) for label in labels), labels


def test_step_labels_are_identifiers() -> None:
    """A degradation step outside the identifier grammar is counted as ``other``, never as
    itself (invariant 3: no label value carries content)."""
    assert step_label("skip_polish") == "skip_polish"
    assert step_label("beat_replaced_safe_fallback") == "beat_replaced_safe_fallback"
    for odd in ("Skip polish", "a quiet harbour", "", "x" * 65, "step-1", "step_1"):
        assert step_label(odd) == OTHER_STEP, odd
