"""Consolidation's scheduling, its gate and its operator route against PostgreSQL (#143;
FR-MEM-3,6,9,10). The scenario tests (``tests/scenario/test_s_consolidation_*``,
``test_s_rewind``, ``test_s_derived_views``, ``test_s_undo_swap``) hold docs/04's clauses; this
file holds what they stand on: both triggers (the owner's decision on #143), the catch-up and
level-2 windows, the overflow entry point, the inert path with the module off, the REST route
and ``lite_summary``'s new default model row."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.context import RequestContext
from epistrel.core.errors import ModuleOff
from epistrel.core.memory import consolidation as core
from epistrel.core.memory.consolidation import ConsolidateRequest, Window
from epistrel.orchestrator.jobs.handlers import consolidation
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.config import ConfigRow, KeyBox, describe
from epistrel.orchestrator.models.registry import CONSOLIDATION, DELIVERED, LITE_SUMMARY, RESERVED
from epistrel.testing.fixtures import scale
from tests import consolidation_support as cs
from tests.auth_support import auth_settings, bearer
from tests.event_support import execute_in_tx, new_story, scalar
from tests.memory_support import turn
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-operator"}


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


def _window(job: Any) -> tuple[int, int, int, bool]:
    p = job.payload
    return (p["from_turn"], p["to_turn"], p["level"], p["explicit"])


async def test_both_triggers_enqueue_the_oldest_uncovered_window(
    engine: AsyncEngine, models: ModelAccessLayer, drained_embedding_jobs: None
) -> None:
    """FR-MEM-6, the owner's decision on #143: the commit that makes ``window_memories`` (3)
    episodic memories uncovered enqueues a window over them (turns 1-3 of one memory each); a
    story whose memories stay under it is consolidated when the head is ``cadence_turns`` (4)
    turns past the last covered turn instead. Each window starts after the last one consolidated
    and the job that commits it enqueues the next one already due (none here). Complement: one
    commit short of either trigger enqueues nothing."""
    counted = await cs.story(engine, cs.dials(window_memories=3, cadence_turns=1_000))
    bobby = counted.characters["Bobby"]
    for n in range(2):
        await turn(engine, counted.story_id, actor=bobby, say=f"Count {n}.", reply=None)
    assert await cs.jobs(engine, counted.story_id) == []
    await turn(engine, counted.story_id, actor=bobby, say="Count 2.", reply=None)
    (job,) = await cs.jobs(engine, counted.story_id)
    assert _window(job) == (1, 3, 1, False)
    assert await cs.run_jobs(engine, models, counted.story_id) == ["committed"]
    (covered,) = await cs.windows(engine, counted.story_id)
    assert (covered.narrative_turn, covered.source_window["to_turn"]) == (3, 3)
    await turn(engine, counted.story_id, actor=bobby, say="Count 3.", reply=None)
    assert await cs.jobs(engine, counted.story_id, "queued") == []
    for n in (4, 5):
        await turn(engine, counted.story_id, actor=bobby, say=f"Count {n}.", reply=None)
    (second,) = await cs.jobs(engine, counted.story_id, "queued")
    assert _window(second) == (4, 6, 1, False)

    timed = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=4))
    ann = timed.characters["Ann"]
    for n in range(3):
        await turn(engine, timed.story_id, actor=ann, say=f"Time {n}.", reply=None)
    assert await cs.jobs(engine, timed.story_id) == []
    await turn(engine, timed.story_id, actor=ann, say="Time 3.", reply=None)
    (job,) = await cs.jobs(engine, timed.story_id)
    assert _window(job) == (1, 4, 1, False)


async def test_level_two_and_the_overflow_entry(
    engine: AsyncEngine, models: ModelAccessLayer, drained_embedding_jobs: None
) -> None:
    """Every fifth level-1 window enqueues a level-2 window over the five (FR-MEM-3: two
    levels); its reflections are made from the level-1 outputs, which they cover. #147's
    overflow entry enqueues whatever is uncovered, due ahead of the queue (FR-MEM-8)."""
    w = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    await cs.bulk_turns(engine, story, 10, actor=bobby, name="Bobby")
    for n in range(5):
        models.stub.script(
            "consolidation", [cs.answer(facts=[(f"Fact of window {n}.", "observation", [0])])]
        )
        run = await consolidate_window(engine, story, Window(2 * n + 1, 2 * n + 2), models=models)
        assert str(run.outcome) == "committed"
    (folded,) = await cs.jobs(engine, story, "queued")
    assert _window(folded) == (1, 10, 2, False)
    models.stub.script(
        "consolidation",
        [cs.answer(facts=[("A standing habit.", "observation", [0, 1, 2, 3, 4])])],
    )
    assert await cs.run_jobs(engine, models, story) == ["committed"]
    top = [r for r in await cs.outputs(engine, story) if r.level == 2]
    assert [(r.kind, r.text, r.narrative_turn) for r in top] == [
        ("reflection", "Bobby said a standing habit.", 10)
    ]
    first = [r for r in await cs.outputs(engine, story) if r.level == 1]
    assert all(top[0].id in r.covered_by for r in first)

    await cs.bulk_turns(engine, story, 2, actor=bobby, name="Bobby")
    async with engine.begin() as conn:
        job_id = await consolidation.schedule_overflow(conn, story)
    assert job_id is not None
    (job,) = await cs.jobs(engine, story, "queued")
    assert _window(job) == (11, 12, 1, False)
    due = await scalar(
        engine, "SELECT due_at < now() - interval '30 minutes' FROM jobs WHERE id = :j", j=job_id
    )
    assert due is True
    await execute_in_tx(engine, ("DELETE FROM jobs WHERE id = :j", {"j": job_id}))


async def test_consolidation_off_is_inert(
    pg_url: str, engine: AsyncEngine, models: ModelAccessLayer, drained_embedding_jobs: None
) -> None:
    """#129: with ``memory.consolidation.enabled`` off nothing is enqueued however many
    memories a story writes, a job over its turns makes no model call and writes nothing, and
    ``Engine.consolidate`` is 409 ``module_off``. Complement: the same story with the module on
    enqueues at the same commit."""
    off = await cs.story(engine, {"memory": {"consolidation": {"enabled": False}}})
    bobby = off.characters["Bobby"]
    for n in range(4):
        await turn(engine, off.story_id, actor=bobby, say=f"Off {n}.", reply=None)
    async with engine.begin() as conn:
        assert await consolidation.schedule_in(conn, off.story_id, force=True) is None
    assert await cs.jobs(engine, off.story_id) == []
    before = cs.calls(models)
    run = await consolidate_window(engine, off.story_id, Window(1, 4), models=models)
    assert run.result == {"calls": 0, "outputs": 0, "noop": "module_off"}
    assert cs.calls(models) == before
    assert await cs.outputs(engine, off.story_id) == []
    assert await cs.windows(engine, off.story_id) == []
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (_, lib):
        with pytest.raises(ModuleOff):
            await lib.consolidate(
                RequestContext(principal="p-operator"),
                off.story_id,
                ConsolidateRequest(from_turn=1, to_turn=4),
            )

    on = await cs.story(engine, cs.dials(window_memories=4))
    for n in range(4):
        await turn(engine, on.story_id, actor=on.characters["Bobby"], say=f"On {n}.", reply=None)
    assert len(await cs.jobs(engine, on.story_id)) == 1


async def test_consolidate_route(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """``POST /admin/stories/{id}/consolidate`` (FR-MEM-3,10): 202 ``{job_id,
    timeline_revision}`` with an explicit job over the window; the same request while it waits
    is that job; an ``Idempotency-Key`` retry replays it; 422 ``scope_mismatch`` for two
    characters' private memories; 409 ``module_off`` on a Lite story; 404 for an unknown story;
    422 for ``to_turn`` before ``from_turn``. Nothing is queued by a refusal."""
    settings = auth_settings(pg_url, model_adapter="stub")
    w = await cs.story(engine)
    story = w.story_id
    for n in range(3):
        await turn(engine, story, actor=w.characters["Bobby"], say=f"Route {n}.", reply=None)
    await execute_in_tx(
        engine,
        (
            "DELETE FROM jobs WHERE story_id = :s AND kind = 'consolidation'",
            {"s": story},
        ),
    )
    async with respond_client(settings, engine) as (client, _):
        path = f"/admin/stories/{story}/consolidate"
        first = await client.post(path, json={"from_turn": 1, "to_turn": 3}, headers=WRITE)
        assert first.status_code == 202, first.text
        body = first.json()
        assert set(body) == {"job_id", "timeline_revision", "idempotency_key"}
        again = await client.post(path, json={"from_turn": 1, "to_turn": 3}, headers=WRITE)
        assert again.status_code == 202
        assert again.json()["job_id"] == body["job_id"]
        keyed = {**WRITE, "Idempotency-Key": "consolidate-once"}
        made = await client.post(path, json={"from_turn": 2, "to_turn": 3}, headers=keyed)
        replay = await client.post(path, json={"from_turn": 2, "to_turn": 3}, headers=keyed)
        assert made.status_code == replay.status_code == 202
        assert replay.json()["job_id"] == made.json()["job_id"]
        (job, other) = await cs.jobs(engine, story, "queued")
        assert _window(job) == (1, 3, 1, True)
        assert _window(other) == (2, 3, 1, True)

        ann, bobby = w.characters["Ann"], w.characters["Bobby"]
        await cs.private_memory(engine, story, ann, [ann], "Ann hid the key.")
        await cs.private_memory(engine, story, bobby, [bobby], "Bobby saw the key.")
        mixed = await client.post(
            path,
            json={"from_turn": 4, "to_turn": 5, "scope": {"characters": [ann, bobby]}},
            headers=WRITE,
        )
        assert mixed.status_code == 422, mixed.text
        assert mixed.json()["error"]["code"] == "scope_mismatch"
        assert len(await cs.jobs(engine, story, "queued")) == 2
        unknown = await client.post(
            "/admin/stories/00000000000000000000000000/consolidate",
            json={"from_turn": 1, "to_turn": 1},
            headers=WRITE,
        )
        assert unknown.status_code == 404, unknown.text
        backwards = await client.post(path, json={"from_turn": 3, "to_turn": 1}, headers=WRITE)
        assert backwards.status_code == 422
        lite = await new_story(engine)
        off = await client.post(
            f"/admin/stories/{lite}/consolidate",
            json={"from_turn": 1, "to_turn": 1},
            headers=WRITE,
        )
        assert off.status_code == 409, off.text
        assert off.json()["error"]["code"] == "module_off"
    await execute_in_tx(
        engine, ("DELETE FROM jobs WHERE story_id = :s AND kind = 'consolidation'", {"s": story})
    )


def test_lite_summary_defaults_to_consolidation() -> None:
    """#51's note, applied by #143: ``consolidation`` is delivered (background, story-scoped,
    parse-validate-retry floor) and declared before ``lite_summary``, whose default row it is;
    it is no longer reserved."""
    assert DELIVERED["consolidation"] is CONSOLIDATION
    assert "consolidation" not in RESERVED
    assert CONSOLIDATION.hot_path is False
    assert CONSOLIDATION.story_scoped
    assert CONSOLIDATION.capability_floor.structured_output.value == "parse_validate_retry"
    assert LITE_SUMMARY.default_model_key == "consolidation"
    order = list(DELIVERED)
    assert order.index("consolidation") < order.index("lite_summary")


def test_lite_summary_resolves_through_the_consolidation_row() -> None:
    """With a ``consolidation`` row and no ``lite_summary`` row, the rolling summary uses the
    consolidation model, ``inherited`` (resolution: ``lite_summary`` → ``consolidation`` →
    ``default``); with neither, ``default``'s. Complement: a ``lite_summary`` row of its own
    wins."""
    stamp = datetime(2026, 10, 17, tzinfo=UTC)

    def row(purpose: str, model: str) -> ConfigRow:
        return ConfigRow(purpose, "http://models.invalid/v1", model, None, {}, stamp)

    rows = {"default": row("default", "base")}
    assert describe("lite_summary", rows, KeyBox(None)).model == "base"
    rows["consolidation"] = row("consolidation", "compressor")
    entry = describe("lite_summary", rows, KeyBox(None))
    assert (entry.model, entry.sources["model"]) == ("compressor", "inherited")
    rows["lite_summary"] = row("lite_summary", "own")
    entry = describe("lite_summary", rows, KeyBox(None))
    assert (entry.model, entry.sources["model"]) == ("own", "own")


async def test_a_window_decodes_only_its_own_facts(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-04 structured (#452): a consolidation window's fact inputs are read for the window's turns
    in the query, not filtered from the whole canon: collecting turns 3-6 of a story whose
    facts are spread over every turn decodes exactly the facts written in those turns."""
    fixture = await scale.build_scale_fixture(
        engine, scale.ScaleSpec.scaled(60, characters=3, dimension=8)
    )
    story_id = fixture.story.story_id
    window = Window(3, 6)
    in_window = int(
        await scalar(
            engine,
            "SELECT count(*) FROM facts WHERE story_id = :s AND narrative_turn BETWEEN 3 AND 6",
            s=story_id,
        )
    )
    total = int(await scalar(engine, "SELECT count(*) FROM facts WHERE story_id = :s", s=story_id))
    assert 0 < in_window < total
    decoded = 0
    fact_of = canon._fact_of

    def counted(row: Any, **extra: Any) -> Any:
        nonlocal decoded
        decoded += 1
        return fact_of(row, **extra)

    monkeypatch.setattr(canon, "_fact_of", counted)
    async with engine.connect() as conn:
        inputs, _ = await core.collect(conn, story_id, window, core.Scope(), 1)
    facts = [i for i in inputs if i.kind == "fact"]
    assert decoded == in_window
    assert len(facts) == in_window
    assert all(3 <= i.narrative_turn <= 6 for i in facts)
