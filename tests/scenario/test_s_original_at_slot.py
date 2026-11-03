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


async def _set_at_slot(engine: AsyncEngine, story: str, value: int) -> str:
    """An inter-turn slot after the head turn that sets the story's ``lamp`` State; the
    change's id."""
    from epistrel.core import states
    from tests import state_support as ss

    found: dict[str, Any] = {}
    async with Operation(
        engine, story_id=story, kind=OperationKind.INTER_TURN, principal="p-test", slot=True
    ) as op:
        op.append("message_rendered", {"text": f"the lamp is set to {value}"})

        async def step(conn: Any) -> None:
            applied = await states.apply_change(conn, op, ss.set_to("lamp", value))
            assert isinstance(applied, states.Applied)
            found["id"] = applied.change_id

        op.add_commit_step(step)
        await op.commit()
    return str(found["id"])


@pytest.mark.part("M7", "FR-STORE-23,26")
async def test_s_original_at_slot__derived_sections(
    engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#222's derived sections. Fourteen turns each write a memory; slot ``14⁺1`` sets the
    ``lamp`` State to 3 and ``14⁺2`` to 5. Later an amendment corrects turn 12's memory text and
    the ``14⁺1`` set (to 4), both ``in_place``. As recorded, ``lamp`` is its initial 0 at
    ``14``, 3 at ``14⁺1`` (not the later 4) and 5 at ``14⁺2``; turn 12's memory reads its
    original text at every checkpoint; turns 13 and 14's memories, written after turn 14
    committed, are absent at ``14`` and present at ``14⁺1``. Complement: the current values
    carry the corrections."""
    from epistrel.core.states import StateDefinition
    from tests import consolidation_support as cs
    from tests import state_support as ss

    w = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    await ss.set_level(engine, story, "on")
    lamp = StateDefinition.model_validate(
        {"name": "lamp", "type": "number", "scope": "story", "initial_value": 0}
    )
    await ss.define(engine, story, lamp)
    # Two batches: the first's twelve memories are written before turn 13 commits, the
    # second's two after turn 14 (a bulk batch writes its memories after its last turn).
    turns = await cs.bulk_turns(engine, story, 12, actor=bobby, name="Bobby", does=True)
    turns += await cs.bulk_turns(engine, story, 2, actor=bobby, name="Bobby", does=True)
    first = await _set_at_slot(engine, story, 3)
    await _set_at_slot(engine, story, 5)
    twelve = next(t for t in turns if t.number == 12)
    original = next(
        str(r["text"])
        for r in await am.table_rows(engine, "memories", story)
        if str(r["id"]) == twelve.memory_id
    )
    await am.amend(
        engine,
        story,
        [
            RecordPartDraft("memories", twelve.memory_id, "replace", {"text": "corrected"}),
            RecordPartDraft("state_changes", first, "replace", {"value": 4}),
        ],
    )

    def lamp_of(view: Any) -> Any:
        (found,) = [s for s in view.states if s["state"] == "lamp"]
        return found["value"]

    def memory_texts(view: Any) -> dict[int | None, str]:
        return {m["narrative_turn"]: m["text"] for m in view.memories}

    at_14 = await as_originally_recorded(engine, story, Checkpoint(14))
    at_1 = await as_originally_recorded(engine, story, Checkpoint(14, 1))
    at_2 = await as_originally_recorded(engine, story, Checkpoint(14, 2))
    assert [lamp_of(v) for v in (at_14, at_1, at_2)] == [0, 3, 5]
    for view in (at_14, at_1, at_2):
        assert memory_texts(view)[12] == original  # the later in-place correction undone
    # A memory created after the boundary is in no view of it (FR-STORE-26).
    assert 13 not in memory_texts(at_14)
    assert {13, 14} <= set(memory_texts(at_1))

    assert await ss.value(engine, story, "lamp") == 5
    texts = await am.table_rows(engine, "memories", story)
    assert "corrected" in {str(r["text"]) for r in texts}
