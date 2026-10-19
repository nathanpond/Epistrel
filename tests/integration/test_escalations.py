"""Escalation records, eval hooks, yield and shadow tightening (FR-OBS-2,4, NFR-PERF-5,
FR-VERIFY-8; #155), over S-TENNIS variant 4's camp claim without its rule: the deterministic
check answers ``uncertain(world_knowledge)`` at confidence 0.5, so the model residual decides it.

- every entailment call leaves an ``escalations`` row in the turn's commit with its gate reason,
  shape, confidences, tentative and model verdicts, flip and consequence (a ``contradicted``
  flip whose beat the ladder corrected is ``repair_kept``; a ``supported`` flip ``none``; an
  unparseable answer has no model verdict and no flip); the turn report's ``eval_hooks`` is
  read under ``omniscient`` only;
- a shadowed proposal leaves the live gate as it is and records what the tightened gate would
  have decided; promotion is refused below the rule (409 with the numbers) and allowed above
  it, a ``config_changes`` row is written and #142's gate then skips the shape's call;
- ``live`` needs ``ignore_warnings``; the yield report and the retention sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.observability.metrics import shape_label
from epistrel.orchestrator.jobs.handlers.retention import sweep_retention
from tests.auth_support import auth_settings
from tests.escalation_support import (
    ELSEWHERE,
    TEST_SHAPE,
    any_operation,
    camp_render,
    camp_story,
    clear_gates,
    count_rows,
    entailment,
    escalation_rows,
    play_camp,
    promote_gate,
    put_gate,
    regenerated,
    remove_gate,
    turn_operation,
    window_rows,
    yield_report,
)
from tests.respond_support import respond_client
from tests.story_support import READ, create, override, story_body

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _counted(shape: str, flipped: str) -> float:
    value = REGISTRY.get_sample_value(
        "epistrel_verify_escalations_total",
        {"gate_reason": "world_knowledge", "shape": shape_label(shape), "flipped": flipped},
    )
    return value or 0.0


async def test_an_escalation_is_recorded_with_its_flip_and_consequence(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A ``contradicted`` answer: flipped, the ladder's regeneration kept, so ``repair_kept``;
    the eval hooks name the original render's ``render_rejected`` event and the rung, and only
    an omniscient read sees them. Complement: a ``supported`` answer is a flip with consequence
    ``none``; an unparseable one has no model verdict and no flip."""
    await clear_gates(engine)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        edu = await camp_story(client, engine)
        stub.script("generation", [camp_render()])
        stub.script("entailment", [entailment("contradicted")])
        stub.script("regeneration", [regenerated()])
        played = await play_camp(client, edu, "omniscient")
        assert played["message"]["text"].endswith(f'"{ELSEWHERE}"')
        (row,) = [r for r in await escalation_rows(engine, edu.story_id) if r.pass_ == "render"]
        before = _counted(row.predicate_shape, "true") - 1
        assert (row.gate_reason, row.tentative_verdict, row.model_verdict) == (
            "world_knowledge",
            "uncertain",
            "contradicted",
        )
        assert (row.called, row.skipped_by_gate, row.flipped) == (True, False, True)
        assert (row.final_kind, row.consequence) == ("contradicted", "repair_kept")
        assert (row.deterministic_confidence, row.model_confidence, row.threshold) == (
            0.5,
            0.9,
            1.0,
        )
        assert row.predicate_shape.count("|") == 3
        assert row.claim_id.startswith(row.operation_id)
        assert (row.shadow_threshold, row.shadow_decision, row.shadow_agreed) == (None,) * 3
        assert _counted(row.predicate_shape, "true") == before + 1

        operation = await turn_operation(engine, edu.story_id)
        seen = await client.get(
            f"/operations/{operation}", params={"viewpoint": "omniscient"}, headers=READ
        )
        assert seen.status_code == 200, seen.text
        hooks = seen.json()["eval_hooks"]
        assert hooks["repair_rungs"] == [{"beat": 2, "rung": 3, "accepted": True}]
        assert [v["kind"] for v in hooks["verdicts"]] == ["contradicted"]
        assert hooks["model_calls_by_purpose"]["entailment"] == 1
        assert set(hooks["latency_ms_by_stage"]) == {
            "plan",
            "render",
            "normalize",
            "detection",
            "verify",
            "entailment",
            "repair",
            "composition",
            "commit",
        }
        (raw,) = hooks["raw_render_event_ids"]
        kind = await count_rows(
            engine,
            "SELECT count(*) FROM events WHERE id = :e AND type = 'render_rejected'",
            e=raw,
        )
        assert kind == 1
        plain = await client.get(f"/operations/{operation}", headers=READ)
        assert plain.json()["eval_hooks"] is None

        # Complement: supported is a flip with no consequence; unparseable is no verdict.
        for scripted, expected, cap in (
            ([entailment("supported")], ("supported", True, "supported", "none"), None),
            # The parse retries spend what the regeneration needed: the safe fallback. A cap of
            # 7 leaves no room for it once render, extraction, salience and three entailment
            # attempts are made with C reserved (#156: the player's input is detected without
            # an extraction call, so the default cap would have one call to spare).
            (["not json"] * 3, (None, None, "uncertain", "repair_discarded"), 7),
        ):
            edu = await camp_story(client, engine)
            if cap is not None:
                await override(client, edu.story_id, {"budgets": {"model_calls": cap}})
            stub.reset()
            stub.script("generation", [camp_render()])
            stub.script("entailment", scripted)
            stub.script("regeneration", [regenerated()])
            await play_camp(client, edu)
            (row,) = [r for r in await escalation_rows(engine, edu.story_id) if r.pass_ == "render"]
            assert (row.model_verdict, row.flipped, row.final_kind, row.consequence) == expected
            # FR-VERIFY-8: no valid model verdict never ends supported.
            assert row.model_verdict is not None or row.final_kind != "supported"


async def test_a_shadowed_tightening_records_and_promotes(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """In shadow the live gate still escalates and the row records that the tightened gate
    (0.25, below the claim's 0.5) would have skipped, and that skipping disagreed (the model's
    ``contradicted`` was taken). Promotion is 409 ``gate_not_promotable`` below 200 cases and
    0.98 agreement, then allowed: a ``config_changes`` row, and the next turn's claim of the
    shape is skipped by the gate (no entailment call, ``skipped_by_gate``)."""
    await clear_gates(engine)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        edu = await camp_story(client, engine)
        stub.script("generation", [camp_render()])
        stub.script("entailment", [entailment("contradicted")])
        stub.script("regeneration", [regenerated()])
        await play_camp(client, edu)
        (first,) = await escalation_rows(engine, edu.story_id)
        shape = first.predicate_shape
        try:
            put = await put_gate(client, shape, 0.25)
            assert put.status_code == 200, put.text
            assert (put.json()["mode"], put.json()["live_threshold"]) == ("shadow", None)

            edu = await camp_story(client, engine)
            stub.reset()
            stub.script("generation", [camp_render()])
            stub.script("entailment", [entailment("contradicted")])
            stub.script("regeneration", [regenerated()])
            await play_camp(client, edu)
            (row,) = [r for r in await escalation_rows(engine, edu.story_id) if r.pass_ == "render"]
            assert row.called is True  # the live gate is unchanged
            assert [c.purpose for c in stub.calls].count("entailment") == 1
            assert (row.shadow_threshold, row.shadow_decision, row.shadow_agreed) == (
                0.25,
                False,
                False,
            )

            refused = await promote_gate(client, shape)
            assert refused.status_code == 409, refused.text
            error = refused.json()["error"]
            assert error["code"] == "gate_not_promotable"
            assert error["details"] == {
                "shape": shape,
                "cases": 1,
                "required_cases": 200,
                "agreement": 0.0,
                "required_agreement": 0.98,
            }
            operation = await any_operation(engine, edu.story_id)
            await window_rows(engine, edu.story_id, operation, shape, cases=250, agreed=240)
            refused = await promote_gate(client, shape)
            assert refused.status_code == 409  # 240 / 251 agreed is below 0.98
            await window_rows(engine, edu.story_id, operation, shape, cases=300, agreed=300)
            promoted = await promote_gate(client, shape)
            assert promoted.status_code == 200, promoted.text
            assert (promoted.json()["mode"], promoted.json()["live_threshold"]) == ("live", 0.25)
            changes = await count_rows(
                engine,
                "SELECT count(*) FROM config_changes WHERE setting = :s AND class = 'interpretive'",
                s=f"verify_gate_threshold:{shape}",
            )
            assert changes == 1

            edu = await camp_story(client, engine)
            stub.reset()
            stub.script("generation", [camp_render()])
            stub.script("regeneration", [regenerated()])
            await play_camp(client, edu)
            (row,) = [r for r in await escalation_rows(engine, edu.story_id) if r.pass_ == "render"]
            assert [c.purpose for c in stub.calls].count("entailment") == 0
            assert (row.skipped_by_gate, row.called, row.model_verdict, row.flipped) == (
                True,
                False,
                None,
                None,
            )
            assert row.final_kind == "uncertain"  # never a silent supported

            removed = await remove_gate(client, shape)
            assert removed.status_code == 200, removed.text
            assert removed.json()["live_threshold"] is None
            gone = await remove_gate(client, shape)
            assert gone.status_code == 404
        finally:
            await clear_gates(engine, [shape])


async def test_live_needs_confirmation_and_the_yield_report(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A ``live`` write without ``ignore_warnings`` writes nothing (``applied: false``,
    ``unvalidated_tightening``); with it the threshold is in force at once. The yield report
    clusters the window's rows; the retention sweep drops rows past
    ``escalation_retention_days``. Complement: rows outside the window are not counted."""
    await clear_gates(engine)
    async with respond_client(stub_settings, engine) as (client, _):
        try:
            held = await put_gate(client, TEST_SHAPE, 0.3, "live")
            assert held.status_code == 200, held.text
            assert (held.json()["applied"], held.json()["warnings"]) == (
                False,
                ["unvalidated_tightening"],
            )
            assert held.json()["mode"] is None
            applied = await put_gate(client, TEST_SHAPE, 0.3, "live", ignore_warnings=True)
            assert (applied.json()["applied"], applied.json()["live_threshold"]) == (True, 0.3)

            story = (await create(client, story_body("Wren")))["story_id"]
            operation = await any_operation(engine, story)
            await window_rows(engine, story, operation, TEST_SHAPE, cases=4, agreed=3)
            report = await yield_report(client)
            (cluster,) = [c for c in report["by_shape"] if c["shape"] == TEST_SHAPE]
            assert (cluster["escalations"], cluster["valid"], cluster["flips"]) == (4, 4, 1)
            assert cluster["flip_rate"] == 0.25
            assert cluster["weighted_flip_rate"] == 0.0
            assert (report["reference_baseline"], report["is_gate"]) == (0.2, False)
            old = datetime(2020, 1, 1, tzinfo=UTC)
            empty = await yield_report(
                client, **{"from": old.isoformat(), "to": (old + timedelta(days=1)).isoformat()}
            )
            assert empty["by_shape"] == []

            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE escalations SET at = :t WHERE story_id = :s"),
                    {"t": datetime.now(UTC) - timedelta(days=91), "s": story},
                )
            swept = await sweep_retention(engine)
            assert swept["escalations"] >= 4
            assert (
                await count_rows(
                    engine, "SELECT count(*) FROM escalations WHERE story_id = :s", s=story
                )
                == 0
            )
        finally:
            await clear_gates(engine, [TEST_SHAPE])
