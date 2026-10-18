"""S-DERIVED-VIEWS, M4 part (docs/04 §7; #143): a derived record is judged by its provenance in
the corrected-history view (FR-STORE-26). The as-originally-recorded view's clause is M7's."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.memory import records
from epistrel.core.memory.consolidation import Window
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import consolidation_support as cs
from tests.auth_support import auth_settings
from tests.fork_support import fork

CHECKPOINT = 8
"""docs/04's turn 800, scaled down: the clause is about positions, not length."""


async def _view(engine: AsyncEngine, story: str, through: int) -> set[str]:
    async with engine.connect() as conn:
        return {m.text for m in await records.memories_through(conn, story, through)}


@pytest.mark.part("M4", "FR-STORE-26")
async def test_s_derived_views__m4(
    engine: AsyncEngine, pg_url: str, drained_embedding_jobs: None
) -> None:
    """Ten turns are played; then (after turn 10) a reflection is computed from turns 1-8 and
    another from turns 5-9. The first is in the corrected history through 8 (it is a cache over
    pre-8 history, though computed later) and in a fork at 8; the second, whose window includes
    turn 9, is excluded from the history through 8 and from the fork, and included through 9.
    Complement: both are in the head's view."""
    models = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    models.bind(engine)
    w = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    await cs.bulk_turns(engine, story, 10, actor=bobby, name="Bobby", does=True)
    models.stub.script(
        "consolidation",
        [
            cs.answer(reflections=[("Bobby kept busy early on.", "observation", [0, 7])]),
            cs.answer(reflections=[("Bobby kept busy later.", "observation", [0, 4])]),
        ],
    )
    early = await consolidate_window(engine, story, Window(1, CHECKPOINT), models=models)
    late = await consolidate_window(
        engine, story, Window(5, CHECKPOINT + 1), explicit=True, models=models
    )
    assert (str(early.outcome), str(late.outcome)) == ("committed", "committed")
    through = await _view(engine, story, CHECKPOINT)
    assert "Bobby kept busy early on." in through
    assert "Bobby kept busy later." not in through
    assert "Bobby kept busy later." in await _view(engine, story, CHECKPOINT + 1)
    head = await _view(engine, story, 10)
    assert {"Bobby kept busy early on.", "Bobby kept busy later."} <= head

    forked = await fork(engine, story, at_turn=CHECKPOINT)
    copied = {r.text for r in await cs.outputs(engine, forked.story_id)}
    assert copied == {"Bobby kept busy early on."}
    assert "Bobby kept busy early on." in await _view(engine, forked.story_id, CHECKPOINT)
