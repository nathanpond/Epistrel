"""S-REWIND, M2 part: delete message 801 of a 1,000-message story and undo it (docs/04 §7;
#65). One turn per message: player-only turns written through ``Operation``. The M4 part
(#143, ``test_s_rewind__reflections``): a reflection across the cut is invalid and rebuilt from
what survived."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.memory import records
from epistrel.core.memory.consolidation import Window
from epistrel.core.views import corrected_history
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import consolidation_support as cs
from tests.auth_support import auth_settings
from tests.concurrency_support import assert_single_chain, chain, regenerate
from tests.event_support import new_story, rows, scalar
from tests.projection_support import snapshot, truncate

MESSAGES = 1_000
CHECKPOINT = 800
PRINCIPAL = "p-player"


async def _message(engine: AsyncEngine, story: str, n: int) -> None:
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        op.append("player_input", {"text": f"message {n}"}, provenance=Provenance.PLAYER)
        await op.commit()


async def _table(engine: AsyncEngine, story: str, table: str, key: str) -> dict[str, Any]:
    found = await rows(engine, f"SELECT * FROM {table} WHERE story_id = :s", s=story)  # noqa: S608
    return {r._mapping[key]: dict(r._mapping) for r in found}


@pytest.mark.part("M2", "FR-STORE-10..12,18")
async def test_s_rewind(engine: AsyncEngine) -> None:
    """Truncating to 800 marks every record positioned after 800 discarded — rows persist,
    valid-time unchanged — and the story continues from 800; untruncating restores 801-1000
    exactly (a row-level diff: only ``membership_tx`` moves); regenerating the latest message
    then keeps the old variant. Complement: records through 800 are never touched."""
    story = await new_story(engine)
    for n in range(1, MESSAGES + 1):
        await _message(engine, story, n)
    events_before = await _table(engine, story, "events", "id")
    ops_before = await _table(engine, story, "operations", "operation_id")
    view_before = [e.id for e in await corrected_history(engine, story)]
    projections_before = await snapshot(engine, story)

    cut = await timeline.truncate(
        engine,
        story,
        timeline.TruncateRequest(to_turn=CHECKPOINT),
        principal=PRINCIPAL,
    )
    assert cut.discarded_operations == MESSAGES - CHECKPOINT
    assert cut.next_turn_index == CHECKPOINT + 1
    stamp = await scalar(
        engine, "SELECT committed_at FROM operations WHERE operation_id = :o", o=cut.operation_id
    )
    events_after = await _table(engine, story, "events", "id")
    assert {k: v for k, v in events_after.items() if v["type"] != "truncate"} == events_before
    ops_after = await _table(engine, story, "operations", "operation_id")
    for op_id, old in ops_before.items():
        row = ops_after[op_id]
        if (old["narrative_turn"] or 0) > CHECKPOINT:
            assert (row["membership"], row["membership_tx"]) == ("discarded", stamp)
            assert {**row, "membership": "active", "membership_tx": old["membership_tx"]} == old
        else:
            assert row == old
    view = await corrected_history(engine, story)
    assert max(e.narrative_turn or 0 for e in view) == CHECKPOINT
    assert [e.id for e in view] == [
        e.id for e in await corrected_history(engine, story, CHECKPOINT)
    ]
    await assert_single_chain(engine, story)

    restored = await timeline.untruncate(engine, story, principal=PRINCIPAL)
    assert restored.restored_operations == MESSAGES - CHECKPOINT
    assert restored.next_turn_index == MESSAGES + 1
    restamp = await scalar(
        engine,
        "SELECT committed_at FROM operations WHERE operation_id = :o",
        o=restored.operation_id,
    )
    ops_restored = await _table(engine, story, "operations", "operation_id")
    for op_id, old in ops_before.items():
        row = ops_restored[op_id]
        assert {**row, "membership_tx": old["membership_tx"]} == old
        later = (old["narrative_turn"] or 0) > CHECKPOINT
        assert row["membership_tx"] == (restamp if later else old["membership_tx"])
    assert [e.id for e in await corrected_history(engine, story)] == view_before
    assert await snapshot(engine, story) == projections_before
    events_restored = await _table(engine, story, "events", "id")
    assert {k: v for k, v in events_restored.items() if k in events_before} == events_before

    await regenerate(engine, story)
    assert (await chain(engine, story))[-1] == (MESSAGES, 2)
    dormant = await scalar(
        engine,
        "SELECT membership FROM operations WHERE story_id = :s AND narrative_turn = :t "
        "AND turn_variant = 1",
        s=story,
        t=MESSAGES,
    )
    assert dormant == "dormant"
    assert [e.id for e in await corrected_history(engine, story, MESSAGES - 1)] == [
        e.id for e in await corrected_history(engine, story) if (e.narrative_turn or 0) < MESSAGES
    ]


@pytest.mark.part("M4", "FR-STORE-13,26")
async def test_s_rewind__reflections(
    engine: AsyncEngine, pg_url: str, drained_embedding_jobs: None
) -> None:
    """A 1,000-turn structured story (one episodic memory a turn) with turns 1-299 consolidated
    and a reflection distilled across 300-900 (positioned at 900). Truncating to 800 discards
    the reflection (it is not in the view through 800, and its row is kept) and enqueues
    consolidation over the surviving 300-800 window, which rebuilds a reflection from the
    surviving episodes only: positioned at 800, every source at or before 800, in the corrected
    view through 800 (a reflection created later from pre-800 sources). Complement: the
    untouched 1-299 window and the episodes through 800 stay active."""
    models = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    models.bind(engine)
    w = await cs.story(engine, cs.dials(window_memories=10_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    await cs.bulk_turns(engine, story, MESSAGES, actor=bobby, name="Bobby", does=True)
    early = await consolidate_window(engine, story, Window(1, 299), models=models)
    assert str(early.outcome) == "committed"
    models.stub.script(
        "consolidation",
        [cs.answer(reflections=[("Bobby works every day.", "observation", [0, 1])])],
    )
    wide = await consolidate_window(engine, story, Window(300, 900), models=models)
    assert str(wide.outcome) == "committed"
    (original,) = await cs.outputs(engine, story)
    assert (original.narrative_turn, original.kind) == (900, "reflection")

    await truncate(engine, story, CHECKPOINT)
    (gone,) = await cs.outputs(engine, story)
    assert (gone.id, gone.membership_text) == (original.id, "discarded")
    async with engine.connect() as conn:
        assert original.id not in {m.id for m in await records.memories_through(conn, story, 800)}
    (job,) = await cs.jobs(engine, story, "queued")
    assert (job.payload["from_turn"], job.payload["to_turn"]) == (300, CHECKPOINT)

    models.stub.script(
        "consolidation",
        [cs.answer(reflections=[("Bobby works every day, still.", "observation", [0, 1])])],
    )
    assert await cs.run_jobs(engine, models, story) == ["committed"]
    rebuilt = [r for r in await cs.outputs(engine, story) if r.membership_text == "active"]
    assert [(r.text, r.narrative_turn) for r in rebuilt] == [
        ("Bobby works every day, still.", CHECKPOINT)
    ]
    sources = await rows(
        engine,
        "SELECT e.narrative_turn FROM events e WHERE e.id = ANY(:ids)",
        ids=list(rebuilt[0].source_event_ids),
    )
    assert len(sources) == CHECKPOINT - 299
    assert max(s.narrative_turn for s in sources) == CHECKPOINT
    async with engine.connect() as conn:
        view = {m.id for m in await records.memories_through(conn, story, CHECKPOINT)}
    assert rebuilt[0].id in view
    windows = [r for r in await cs.windows(engine, story) if r.membership == "active"]
    assert [(r.source_window["from_turn"], r.source_window["to_turn"]) for r in windows] == [
        (1, 299),
        (300, CHECKPOINT),
    ]
