"""S-CONSOLIDATION-REPLAY, M4 (docs/04 §7; #143): replay uses the stored consolidation result;
a rerun is a new consolidation with new provenance, made only when an input changed or on
request (FR-MEM-10)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import projections
from epistrel.core.memory.consolidation import Window
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import consolidation_support as cs
from tests.auth_support import auth_settings
from tests.fork_support import fork
from tests.profile_support import store_overrides
from tests.projection_support import truncate, untruncate

QUIET = cs.dials(window_memories=1_000, cadence_turns=1_000)
"""No automatic window: the test asks for each one."""
OTHER = {"models": {"purposes": {"consolidation": {"model": "another-consolidator"}}}}
"""A deliberately different consolidation model, routed by the story's profile override."""
PROVENANCE = ("text", "kind", "modality", "model", "model_version", "prompt_hash", "inputs_hash")


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


def _provenance(rows: list[Any]) -> list[tuple[Any, ...]]:
    return [tuple(getattr(r, f) for f in PROVENANCE) for r in rows]


@pytest.mark.part("M4", "FR-MEM-10")
async def test_s_consolidation_replay(
    engine: AsyncEngine, models: ModelAccessLayer, drained_embedding_jobs: None
) -> None:
    """Six turns of Bobby are consolidated once (the stub, model ``stub``). Then a different
    consolidation model is routed for the story. A fork at turn 6 copies the stored outputs
    (same text, same model, prompt hash and replay key) and its own consolidation of the same
    window replays them; in the source, a projection rebuild, a truncation to 3 with its undo
    and the jobs they enqueue, and a non-explicit consolidation of the window all reuse the
    stored outputs: **no model call** anywhere. Complement: an explicit request reruns with the
    other model (new provenance, new operation) and supersedes the first outputs; and after an
    input's text changes (redacted, as an erasure redacts it) a non-explicit consolidation
    reruns too, with a new replay key."""
    w = await cs.story(engine, QUIET)
    story, bobby = w.story_id, w.characters["Bobby"]
    await cs.bulk_turns(engine, story, 6, actor=bobby, name="Bobby", line=lambda n: f"rain {n}")
    models.stub.script(
        "consolidation",
        [
            cs.answer(
                facts=[("Bobby talks about the rain.", "attributed_claim", [0, 1, 2])],
                reflections=[("The rain worries Bobby.", "observation", [3, 4, 5])],
            )
        ],
    )
    first = await consolidate_window(engine, story, Window(1, 6), models=models)
    assert str(first.outcome) == "committed"
    stored = await cs.outputs(engine, story)
    assert len(stored) == 2
    assert {r.model for r in stored} == {"stub"}
    await store_overrides(engine, story, {**QUIET, **OTHER})
    calls = cs.calls(models)

    # Fork: the copies are the stored results, and the fork's consolidation replays them.
    forked = await fork(engine, story, at_turn=6)
    copies = await cs.outputs(engine, forked.story_id)
    assert _provenance(copies) == _provenance(stored)
    assert {c.id for c in copies}.isdisjoint({s.id for s in stored})
    assert await cs.run_jobs(engine, models, forked.story_id) == []
    replayed = await consolidate_window(engine, forked.story_id, Window(1, 6), models=models)
    assert replayed.result["noop"] == "replayed"

    # Reconstruction in the source: nothing reruns the model.
    await projections.rebuild(engine, story, principal="p-operator")
    await truncate(engine, story, 3)
    assert [r.membership_text for r in await cs.outputs(engine, story)] == ["discarded"] * 2
    await untruncate(engine, story)
    await cs.run_jobs(engine, models, story)
    again = await consolidate_window(engine, story, Window(1, 6), models=models)
    assert again.result["noop"] == "replayed"
    assert cs.calls(models) == calls
    after = await cs.outputs(engine, story)
    assert _provenance(after) == _provenance(stored)
    assert {r.membership_text for r in after} == {"active"}

    # Complement: an explicit request reruns, with the other model and new provenance.
    models.stub.script(
        "consolidation", [cs.answer(facts=[("Bobby repeats himself.", "observation", [0])])]
    )
    rerun = await consolidate_window(engine, story, Window(1, 6), explicit=True, models=models)
    assert str(rerun.outcome) == "committed"
    assert rerun.result["superseded"] == 2
    assert cs.calls(models) == calls + 1
    active = await cs.outputs(engine, story, membership="active")
    assert [(r.text, r.model) for r in active] == [
        ("Bobby repeats himself.", "another-consolidator")
    ]
    assert {r.operation_id for r in active}.isdisjoint({s.operation_id for s in stored})
    assert {r.id for r in await cs.outputs(engine, story, membership="superseded")} == {
        s.id for s in stored
    }

    # Complement: an input that changed (redacted as an erasure redacts) reruns unasked.
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL epistrel.erasure = 'on'"))
        await conn.execute(
            text(
                "UPDATE memories SET text = 'Bobby says: [erased]' WHERE story_id = :s "
                "AND kind = 'episodic' AND narrative_turn = 2"
            ),
            {"s": story},
        )
    models.stub.script(
        "consolidation", [cs.answer(facts=[("Bobby talks about rain.", "observation", [0])])]
    )
    changed = await consolidate_window(engine, story, Window(1, 6), models=models)
    assert str(changed.outcome) == "committed"
    assert cs.calls(models) == calls + 2
    (latest,) = await cs.outputs(engine, story, membership="active")
    assert latest.inputs_hash not in {active[0].inputs_hash}
    assert latest.text == "Bobby talks about rain."
