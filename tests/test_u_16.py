"""U-16 (M4): ledger obligations are per proposition (FR-COMMIT-13; docs/04 §3; #146). One file
per test ID and milestone; U-16's M5 part (FR-COMMIT-6,13 with partitions: the knocker-side
commitment of a modeled character acting with intent) joins it."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.commit import lifecycle
from epistrel.core.commit.threads import open_threads
from epistrel.core.enums import DischargeMode, OpenSlot
from epistrel.core.events import Operation
from epistrel.core.propositions.registry import load
from epistrel.core.retrieval.pipeline import SourceRequest
from epistrel.orchestrator.models.registry import SalienceSketch
from tests import commit_support as c

pytestmark = pytest.mark.integration

WHO_MIGHT_IT_BE = SalienceSketch(subject="somebody", relation="knocked_at", object="the door")
"""The prose dwelling on who it might be: the knocker made salient in its own right."""


@pytest.mark.part("M4", "FR-COMMIT-13")
async def test_u_16(engine: AsyncEngine) -> None:
    """Turn 1 narrates a salient knock ("someone knocked at the door": one entry, on that
    proposition) and, as background, that the knock was for something (an open thunk with no
    entry). At the close no dependence has settled the knock, so the minimal commit discharges
    its entry: "a knock occurred" (``knocked_at_occurred`` about the door, no object, no
    anonymous knocker), with ``ledger_minimal_commit`` in the report. Who knocked and why stay
    open thunks with no obligation, and the turn closes with them open (no blocker). In turn 2
    the prose dwells on who it might be: the knocker becomes an obligation of its own (a new
    entry on the same thunk, now a blocker), while "why" still carries none. Complement: making
    the same reference salient again while its entry is undischarged adds no second entry, and
    the ``## Open threads`` context lists the salient knocker only."""
    k = await c.knock_story(engine)
    narration = k.beat(2, "narration", "Someone knocks at the door.")

    # "Why" is a background thunk: introduced without salience.
    async def background(conn: AsyncConnection, op: Operation) -> dict[str, object]:
        knock = await c.salient(conn, op, k, narration, c.KNOCK)
        why_sketch = c.knock_sketch(k).model_copy(
            update={
                "subject_text": "the door",
                "subject_id": k.door,
                "relation_text": "knocked_for",
                "object_text": "something",
                "predicate_id": k.knocked_for,
                "object_id": None,
                "open_slot": OpenSlot.OBJECT,
            }
        )
        why = await lifecycle.introduce(conn, op, why_sketch, valid_time=0, beat=2)
        held = await lifecycle.blockers(conn, k.story_id)
        closed = await lifecycle.close_turn(conn, op)
        return {"knock": knock, "why": why, "held": held, "closed": closed}

    found = await c.in_turn(engine, k.story_id, background)
    knock_thunk = found["knock"].recorded[0].thunk_id
    why = found["why"]
    assert [e.thunk_id for e in found["held"]] == [knock_thunk]  # only the salient knock blocks
    closed = found["closed"]
    assert closed.minimal_commits == 1
    assert closed.degradation == [lifecycle.MINIMAL_COMMIT]
    assert await c.blockers(engine, k.story_id) == []  # the turn closed with both thunks open
    assert (await c.thunk(engine, k.story_id, knock_thunk)).state.value == "open"
    assert (await c.thunk(engine, k.story_id, why.id)).state.value == "open"
    (entry,) = await c.rows(engine, "promissory_ledger", k.story_id)
    assert (entry.thunk_id, str(entry.discharged_mode)) == (
        knock_thunk,
        DischargeMode.MINIMAL.value,
    )

    async with engine.connect() as conn:
        registry = await load(conn, k.story_id)
    occurred = registry.named("knocked_at_occurred")
    assert occurred is not None
    assert occurred.object_type == "none"
    facts = await c.rows(engine, "facts", k.story_id)
    assert [(f.subject_id, f.predicate_id) for f in facts] == [(k.door, occurred.id)]
    assert not any(f.predicate_id == k.knocked_at for f in facts)  # no anonymous knocker

    async def second(conn: AsyncConnection, op: Operation) -> dict[str, object]:
        dwell = await c.salient(
            conn, op, k, k.beat(2, "narration", "Who might it be?"), WHO_MIGHT_IT_BE
        )
        again = await c.salient(conn, op, k, narration, c.KNOCK)
        held = await lifecycle.blockers(conn, k.story_id)
        threads = await open_threads(
            conn, SourceRequest(k.story_id, (op.narrative_turn or 0, 0), "omniscient", ())
        )
        return {"dwell": dwell, "again": again, "held": held, "threads": threads}

    later = await c.in_turn(engine, k.story_id, second)
    assert later["dwell"].recorded[0].outcome == "entry"
    assert later["dwell"].recorded[0].thunk_id == knock_thunk
    assert later["again"].recorded[0].outcome == "owed"  # complement: no second entry
    assert [e.thunk_id for e in later["held"]] == [knock_thunk]
    entries = await c.rows(engine, "promissory_ledger", k.story_id)
    assert [e.thunk_id for e in entries] == [knock_thunk, knock_thunk]
    assert not any(e.thunk_id == why.id for e in entries)  # "why" still owes nothing
    (thread,) = later["threads"]
    assert thread.kind == "ledger"
    assert thread.item_id == knock_thunk
    assert thread.text == "- someone knocked_at door (subject unknown; raised turn 1)"
