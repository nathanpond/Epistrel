"""S-ORIGINAL-AT-SLOT (FR-STORE-23,31): a checkpoint with inter-turn slots read as originally
recorded (#222)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.amendments.store import RecordPartDraft
from epistrel.core.enums import OperationKind
from epistrel.core.errors import CheckpointNotFound
from epistrel.core.events import Operation
from epistrel.core.views import Checkpoint, as_originally_recorded, corrected_history
from tests import amendment_support as am
from tests import canon_support as c

pytestmark = pytest.mark.integration


async def _slot(engine: AsyncEngine, story: str, text: str) -> str:
    """An inter-turn slot after the head turn, recording ``text``; its operation id."""
    async with Operation(
        engine, story_id=story, kind=OperationKind.INTER_TURN, principal="p-test", slot=True
    ) as op:
        op.append("message_rendered", {"text": text})
        await op.commit()
    return op.operation_id


def _texts(events: list[Any]) -> set[str]:
    return {str(getattr(e.payload, "text", "")) for e in events}


@pytest.mark.part("M7", "FR-STORE-23,31")
async def test_s_original_at_slot(engine: AsyncEngine) -> None:
    """After turn 14, slots ``14⁺1`` and ``14⁺2`` commit; later an amendment corrects turn 12's
    reply. As recorded: ``14`` has neither slot, ``14⁺1`` the first only, ``14⁺2`` both, and
    none has the correction (turn 12's original reply). Complement: the corrected view has the
    correction and both slots; a slot that never committed is 404."""
    w = await c.world(engine)
    replies = await am.play(engine, w.story_id, 14)
    await _slot(engine, w.story_id, "slot one")
    await _slot(engine, w.story_id, "slot two")
    await am.amend(
        engine,
        w.story_id,
        [RecordPartDraft("events", replies[12], "replace", {"text": "corrected twelve"})],
    )
    at_14 = await as_originally_recorded(engine, w.story_id, Checkpoint(14))
    at_1 = await as_originally_recorded(engine, w.story_id, Checkpoint(14, 1))
    at_2 = await as_originally_recorded(engine, w.story_id, Checkpoint(14, 2))
    assert at_14.boundary_tx < at_1.boundary_tx < at_2.boundary_tx
    assert not {"slot one", "slot two"} & _texts(at_14.events)
    assert "slot one" in _texts(at_1.events)
    assert "slot two" not in _texts(at_1.events)
    assert {"slot one", "slot two"} <= _texts(at_2.events)
    for view in (at_14, at_1, at_2):
        assert "reply 12" in _texts(view.events)
        assert "corrected twelve" not in _texts(view.events)
    corrected = _texts(await corrected_history(engine, w.story_id))
    assert "corrected twelve" in corrected
    assert "reply 12" not in corrected
    with pytest.raises(CheckpointNotFound):
        await as_originally_recorded(engine, w.story_id, Checkpoint(14, 3))


@pytest.mark.part("M7", "FR-STORE-26")
async def test_s_original_at_slot__derived(
    engine: AsyncEngine, pg_url: str, drained_embedding_jobs: None
) -> None:
    """After turn 14 and its slots ``14⁺1`` and ``14⁺2``, a reflection is computed from turns
    1-14. Every as-originally-recorded view (``14``, ``14⁺1``, ``14⁺2``) excludes it (no event
    of the consolidation), while the corrected history through 14 includes it (FR-STORE-26: a
    derived record from sources at or before the checkpoint)."""
    from epistrel.core.memory import records
    from epistrel.core.memory.consolidation import Window
    from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
    from epistrel.orchestrator.models.access import ModelAccessLayer
    from tests import consolidation_support as cs
    from tests.auth_support import auth_settings
    from tests.event_support import rows

    models = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    models.bind(engine)
    w = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    await cs.bulk_turns(engine, story, 14, actor=bobby, name="Bobby", does=True)
    await _slot(engine, story, "slot one")
    await _slot(engine, story, "slot two")
    models.stub.script(
        "consolidation",
        [cs.answer(reflections=[("Bobby kept at it all day.", "observation", [0, 1])])],
    )
    await consolidate_window(engine, story, Window(1, 14), models=models)
    consolidations = {
        str(r.operation_id)
        for r in await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'consolidation'",
            s=story,
        )
    }
    assert consolidations
    for checkpoint in (Checkpoint(14), Checkpoint(14, 1), Checkpoint(14, 2)):
        view = await as_originally_recorded(engine, story, checkpoint)
        assert not consolidations & {e.operation_id for e in view.events}, checkpoint
    async with engine.connect() as conn:
        through = {m.text for m in await records.memories_through(conn, story, 14)}
    assert "Bobby kept at it all day." in through
