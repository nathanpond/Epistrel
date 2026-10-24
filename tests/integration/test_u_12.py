"""U-12: belief revision history (docs/04 §3; FR-BELIEF-10; #160).

A correction preserves the prior belief bitemporally: the character can be shown to have been
wrong, then corrected. Forgetting lowers confidence and accessibility without contradicting the
belief, and is visible to consolidation (``decayed_beliefs``). Both follow the timeline: a
truncated correction reopens the prior, and a truncated forgetting reverts in place.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.enums import BeliefHoldState as Hold
from epistrel.core.epistemic import partitions, transitions
from epistrel.core.epistemic.errors import AntecedentInvalid
from epistrel.core.epistemic.transitions import Antecedent, Confidence, Kind
from epistrel.core.membership import Position
from tests import canon_support as c
from tests import partition_support as ps
from tests.memory_support import use_structured
from tests.projection_support import truncate, untruncate

pytestmark = pytest.mark.integration

Level = BeliefConfidenceLevel


async def _belief(engine: AsyncEngine, story: str, belief_id: str) -> partitions.Belief:
    async with engine.connect() as conn:
        found = await partitions.read_belief(conn, story, belief_id)
    assert found is not None
    return found


@pytest.mark.part("M5", "FR-BELIEF-10")
async def test_u_12__revision_history(engine: AsyncEngine) -> None:
    """Ann is told Bobby is in the garden (an author-seeded lie, turn 2). At turn 3 she learns
    from canon that he is in the kitchen: a correction closes the garden belief at the new
    belief's position, in held-time and in transaction-time, and the history reads belief, then
    correction. Truncating the correction's turn reopens the garden belief; untruncating closes
    it again. Forgetting then lowers the kitchen belief one band and its accessibility, marks it
    decayed (consolidation's input) without touching what she believes; a truncated forgetting
    reverts in place, and a later revision holds it again at full accessibility."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.people["Bobby"]
    kitchen = c.at(bobby, w.places["kitchen"], 0, None)
    fact = await c.commit(engine, story, kitchen)

    lie = await ps.believe(engine, story, ann, c.at(bobby, w.places["garden"], 0, None))
    assert lie.narrative_turn is not None
    turn = lie.narrative_turn + 1
    corrected = await ps.transit(
        engine,
        story,
        ann,
        kitchen,
        cause=Kind.CORRECTION,
        antecedent=Antecedent.record("facts", fact.id),
        position=Position(turn, 1),
    )
    new = corrected.belief
    assert corrected.created
    assert corrected.closed == (lie.id,)
    assert corrected.transition.kind is Kind.CORRECTION
    assert corrected.transition.prior_belief_ids == (lie.id,)

    prior = await _belief(engine, story, lie.id)
    assert prior.held_to == new.held_from
    assert prior.tx_to is not None
    assert prior.tx_to > prior.tx_from
    assert (prior.state, prior.corrected_by) == (Hold.CORRECTED, new.id)
    assert new.lineage_id == prior.lineage_id == lie.id

    async with engine.connect() as conn:
        chain = await transitions.history(conn, story, new.id)
        assert [(e.belief.id, [t.kind for t in e.transitions]) for e in chain] == [
            (lie.id, [Kind.AUTHOR]),
            (new.id, [Kind.CORRECTION]),
        ]
        # Wrong then (at the end of the lie's turn), corrected now.
        (then,) = await partitions.partition(
            conn, story, ann, through=Position.end_of(lie.narrative_turn)
        )
        (now,) = await partitions.partition(conn, story, ann)
        assert (then.id, now.id) == (lie.id, new.id)
    assert [r["belief_id"] for r in await ps.state_rows(engine, story)] == [new.id]

    # Truncating the correction reopens the prior; untruncating closes it again.
    assert await truncate(engine, story, lie.narrative_turn) is not None
    reopened = await _belief(engine, story, lie.id)
    assert (reopened.held_to, reopened.tx_to, reopened.state, reopened.corrected_by) == (
        None,
        None,
        Hold.HELD,
        None,
    )
    assert [r["belief_id"] for r in await ps.state_rows(engine, story)] == [lie.id]
    assert await untruncate(engine, story) is not None
    closed = await _belief(engine, story, lie.id)
    assert (closed.held_to, closed.corrected_by) == (new.held_from, new.id)
    assert [r["belief_id"] for r in await ps.state_rows(engine, story)] == [new.id]
    assert await ps.state_rows(engine, story) == await ps.folded_rows(engine, story)

    # Forgetting: one band down, accessibility down, decayed, never a contradiction.
    later = turn + 1
    forgot = await ps.transit(
        engine,
        story,
        ann,
        kitchen,
        cause=Kind.FORGETTING,
        antecedent=Antecedent.record("beliefs", new.id),
        position=Position(later, 1),
    )
    faded = forgot.belief
    assert (faded.id, faded.polarity, faded.held_to) == (new.id, new.polarity, None)
    assert (faded.confidence_level, faded.accessibility, faded.state) == (
        Level.SUSPECTED,
        0.7,
        Hold.DECAYED,
    )
    assert forgot.transition.prior == {
        "level": "believed",
        "scalar": None,
        "accessibility": 1.0,
        "state": "held",
    }
    async with engine.connect() as conn:
        assert [b.id for b in await transitions.decayed_beliefs(conn, story, ann)] == [new.id]
        assert await transitions.decayed_beliefs(conn, story, ann, (0, turn - 1)) == []
    (row,) = await ps.state_rows(engine, story)
    assert (row["belief_id"], row["current_level"]) == (new.id, "suspected")

    # A truncated forgetting reverts in place.
    assert await truncate(engine, story, turn) is not None
    restored = await _belief(engine, story, new.id)
    assert (restored.confidence_level, restored.accessibility, restored.state) == (
        Level.BELIEVED,
        1.0,
        Hold.HELD,
    )
    assert await untruncate(engine, story) is not None
    assert (await _belief(engine, story, new.id)).state is Hold.DECAYED

    # A later revision on a decayed belief holds it again at full accessibility.
    revised = await ps.transit(
        engine,
        story,
        ann,
        kitchen,
        cause=Kind.REVISION,
        antecedent=Antecedent.record("facts", fact.id),
        confidence=Confidence(scalar=0.95),
    )
    assert (revised.belief.id, revised.belief.state, revised.belief.accessibility) == (
        new.id,
        Hold.HELD,
        1.0,
    )
    assert (revised.belief.confidence_level, revised.belief.confidence_scalar) == (
        Level.KNOWN,
        0.95,
    )


@pytest.mark.part("M5", "FR-BELIEF-10")
async def test_u_12__forgetting_a_scalar(engine: AsyncEngine) -> None:
    """With a scalar the forgetting halves it and the level is its projection; repeated
    forgetting reaches ``unknown``, and the belief leaves ``belief_state`` while it stays
    recorded (a suspended belief, never a contradiction). A correction needs a contradicted
    prior; a forgetting cites the belief itself."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    bobby = w.characters["Bobby"]
    garden = c.at(w.people["Ann"], w.places["garden"], 0, None)
    held = await ps.believe(engine, story, bobby, garden, scalar=0.8)
    assert (held.confidence_level, held.confidence_scalar) == (Level.BELIEVED, 0.8)

    levels = []
    for _ in range(3):
        step = await ps.transit(
            engine,
            story,
            bobby,
            garden,
            cause=Kind.FORGETTING,
            antecedent=Antecedent.record("beliefs", held.id),
        )
        levels.append((step.belief.confidence_scalar, step.belief.confidence_level))
    assert levels == [(0.4, Level.SUSPECTED), (0.2, Level.SUSPECTED), (0.1, Level.UNKNOWN)]
    assert await ps.state_rows(engine, story) == []
    assert (await _belief(engine, story, held.id)).held_to is None

    knows = c.at(w.people["Ann"], w.people["Cal"], None, None, predicate=c.KNOWS)
    other = await ps.believe(engine, story, bobby, knows)
    with pytest.raises(AntecedentInvalid, match="forgetting_cites_its_belief"):
        await ps.transit(
            engine,
            story,
            bobby,
            garden,
            cause=Kind.FORGETTING,
            antecedent=Antecedent.record("beliefs", other.id),
        )


@pytest.mark.part("M5", "FR-BELIEF-10")
async def test_u_12__consolidation_forgets(engine: AsyncEngine) -> None:
    """Consolidating a character scope over a window decays that character's beliefs from the
    window by one forgetting transition each (``forget_covered``, #143's handler calls it), and
    ``decayed_beliefs`` lists them; a belief from outside the window, another holder's, and a
    story without partitions are untouched."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    early = await ps.believe(engine, story, ann, c.at(w.people["Cal"], w.places["garden"], 0, None))
    his = await ps.believe(engine, story, bobby, c.at(w.people["Cal"], w.places["garden"], 0, None))
    late = await ps.believe(
        engine, story, ann, c.at(w.people["Bobby"], w.places["school"], 0, None)
    )
    assert early.narrative_turn is not None
    assert his.narrative_turn is not None
    assert late.narrative_turn is not None
    window = (early.narrative_turn, his.narrative_turn)
    end = late.narrative_turn
    settings = await ps.partitioned(engine, story)

    async def consolidate(conn: Any, op: Any) -> int:
        return await transitions.forget_covered(
            conn,
            op,
            holders=[ann],
            from_turn=window[0],
            to_turn=window[1],
            settings=settings,
        )

    _, written = await c.run(engine, story, consolidate)
    assert written == 1
    async with engine.connect() as conn:
        decayed = await transitions.decayed_beliefs(conn, story, ann)
        assert [b.id for b in decayed] == [early.id]
        assert decayed[0].confidence_level is Level.SUSPECTED
        assert await transitions.decayed_beliefs(conn, story, bobby) == []
    assert (await _belief(engine, story, late.id)).state is Hold.HELD

    async def without(conn: Any, op: Any) -> int:
        return await transitions.forget_covered(conn, op, holders=[ann], from_turn=0, to_turn=end)

    _, none = await c.run(engine, story, without)
    assert none == 0
