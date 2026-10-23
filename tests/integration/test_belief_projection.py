"""I-01 (M5): the belief projection updates in the same transaction as the belief it projects
(docs/04 §4; FR-STORE-3; #159).

``belief_state`` is kept by the belief write's own hook, in the writing operation's transaction,
and re-derived after every membership flip; a rebuild, erasure's ``rebuilt`` stage and a fork
fold it from the beliefs with the same function, so the two paths agree row for row.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import projections
from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import partitions, transitions
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.tables.beliefs import belief_state
from tests import canon_support as c
from tests import partition_support as ps
from tests.fork_support import fork
from tests.memory_support import use_structured
from tests.projection_support import truncate, untruncate

pytestmark = pytest.mark.integration


class Boom(RuntimeError):
    pass


async def _states(conn: AsyncConnection, story_id: str) -> list[Any]:
    return list(
        await conn.execute(
            select(belief_state.c.holder_character_id, belief_state.c.current_level).where(
                belief_state.c.story_id == story_id
            )
        )
    )


async def _agree(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """The stored projection, after checking it equals the fold of the story's beliefs."""
    stored = await ps.state_rows(engine, story_id)
    assert stored == await ps.folded_rows(engine, story_id)
    return stored


@pytest.mark.part("M5", "FR-STORE-3")
async def test_i_01__belief_projection(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ann comes to suspect (a discovery from a canon clue), then to know (in place, #160), that
    Bobby is in the garden: each write moves
    ``belief_state`` in its own transaction (seen inside it before commit, by nobody else
    until then), the old level's row closed (``held_to``) and the projection holding the new
    level since the new position. An induced failure after the write rolls the belief and its
    projection row back together. A truncation drops the projection row of the beliefs it
    discards, an untruncation brings it back, ``unknown`` holds no row, and the rebuild and a
    fork fold exactly the incrementally kept rows. Complement: a projection kept without the
    hook (removed for one write) disagrees with the fold."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann = w.characters["Ann"]
    garden = c.at(w.people["Bobby"], w.places["garden"], 0, None)
    seen: dict[str, Any] = {}
    clue = await c.commit(engine, story, c.at(w.people["Bobby"], w.places["garden"], 0, None))
    settings = await ps.partitioned(engine, story)

    async def discover(
        conn: AsyncConnection, op: Operation, proposition: Any, level: BeliefConfidenceLevel
    ) -> partitions.Belief:
        assert op.narrative_turn is not None
        with ps.delivered():
            found = await transitions.transition(
                conn,
                op,
                holder=ann,
                kind=transitions.Kind.DISCOVERY,
                proposition=proposition,
                antecedent=transitions.Antecedent.record("facts", clue.id),
                position=Position(op.narrative_turn, op.turn_variant or 0),
                confidence=transitions.Confidence(level),
                settings=settings,
            )
        return found.belief

    async def suspect(conn: AsyncConnection, op: Operation) -> partitions.Belief:
        found = await discover(conn, op, garden, BeliefConfidenceLevel.SUSPECTED)
        seen["inside"] = await _states(conn, story)
        async with engine.connect() as other:
            seen["outside"] = await _states(other, story)
        return found

    _, first = await c.run(engine, story, suspect)
    assert seen["inside"] == [(ann, "suspected")]
    assert seen["outside"] == []
    (row,) = await _agree(engine, story)
    assert (row["belief_id"], row["current_level"]) == (first.id, "suspected")

    # #160: holding it more strongly is a change in place, with the old level in its history.
    second = await ps.believe(engine, story, ann, garden, level=BeliefConfidenceLevel.KNOWN)
    assert (second.id, second.proposition_id, second.closed_id) == (
        first.id,
        first.proposition_id,
        None,
    )
    (row,) = await _agree(engine, story)
    assert (row["belief_id"], row["current_level"], row["since_turn"]) == (
        second.id,
        "known",
        first.held_from.turn,
    )
    async with engine.connect() as conn:
        steps = await transitions.transitions_of(conn, story, belief_ids=[first.id])
        assert [(t.kind.value, t.confidence_level.value) for t in steps] == [
            ("discovery", "suspected"),
            ("author", "known"),
        ]
        assert steps[1].prior is not None
        assert steps[1].prior["level"] == "suspected"
        (then,) = await partitions.partition(
            conn, story, ann, through=Position.end_of(first.held_from.turn)
        )
        assert then.id == first.id

    # An induced failure after the write: the belief and its projection row roll back together.
    async def fail_after(conn: AsyncConnection, op: Operation) -> None:
        await discover(
            conn,
            op,
            c.at(w.people["Ann"], w.places["school"], 0, None),
            BeliefConfidenceLevel.BELIEVED,
        )
        raise Boom("after the belief write")

    with pytest.raises(Boom):
        await c.run(engine, story, fail_after)
    assert len(await ps.belief_rows(engine, story)) == 1
    assert len(await _agree(engine, story)) == 1

    # Truncating the turn of a belief drops its projection row; untruncating restores it.
    school = await ps.believe(
        engine, story, ann, c.at(w.people["Ann"], w.places["school"], 0, None)
    )
    assert len(await _agree(engine, story)) == 2
    assert school.narrative_turn is not None
    assert await truncate(engine, story, school.narrative_turn - 1) is not None
    assert [r["belief_id"] for r in await _agree(engine, story)] == [second.id]
    assert await untruncate(engine, story) is not None
    assert {r["belief_id"] for r in await _agree(engine, story)} == {second.id, school.id}

    # ``unknown`` (a suspended belief, revised down in place) holds no row.
    await ps.believe(
        engine,
        story,
        ann,
        c.at(w.people["Ann"], w.places["school"], 0, None),
        level=BeliefConfidenceLevel.UNKNOWN,
        cause=transitions.Kind.REVISION,
    )
    assert [r["belief_id"] for r in await _agree(engine, story)] == [second.id]

    # The rebuild folds exactly what the incremental path kept; a fork refolds its copy.
    kept = await ps.state_rows(engine, story)
    await projections.rebuild(engine, story)
    assert await ps.state_rows(engine, story) == kept
    forked = await fork(engine, story)
    copied = await ps.state_rows(engine, forked.story_id)
    assert copied == await ps.folded_rows(engine, forked.story_id)
    assert [r["current_level"] for r in copied] == ["known"]

    # Complement: without the hook the stored projection falls behind the fold.
    monkeypatch.setattr(
        partitions,
        "BELIEF_HOOKS",
        [h for h in partitions.BELIEF_HOOKS if h is not partitions._project],
    )
    await ps.believe(engine, story, w.characters["Bobby"], garden)
    assert await ps.state_rows(engine, story) != await ps.folded_rows(engine, story)
