"""#220: the typed dependency edges and their one search (``core.edges``).

- ``test_dependency_edges``: lineage (a superseded fact's dependents are found through the new
  version's id), ``depth`` (``depth = 2`` finds a belief drawn from a belief, ``depth = 1`` does
  not), the cap (``truncated``), the clamp (``depth_clamped``), a transition's ``derives`` edge
  (a ``record`` antecedent in canon targets the fact, with its acquisition kind), and amendment
  edges (FR-REWRITE-11: part B relying on part A's fact has both a ``fact`` and an
  ``amendment_part`` target, and a search for part A returns B).
- ``test_dependency_edges__membership``: an edge follows its operation's membership (truncate
  excludes it, untruncate restores it identically), a fork copies edges naming only the fork's
  own ids, and a search never returns another story's edge (complement: a second story with
  the same proposition shape).
- ``test_lite_writes_no_edges`` (complement): a Lite turn leaves the table empty.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import edges
from epistrel.core.enums import (
    BeliefTransitionKind,
    DependencyEdgeType,
    DependencyTarget,
    OperationKind,
)
from epistrel.core.epistemic import transitions
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from tests import canon_support as c
from tests import fork_support as forks
from tests import partition_support as parts
from tests import projection_support as projections
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, new_story, played, respond_client

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _search(engine: AsyncEngine, story_id: str, **kwargs: Any) -> edges.DependentResult:
    targets = kwargs.pop("targets")
    async with engine.connect() as conn:
        return await edges.dependents(conn, story_id, targets, **kwargs)


async def _mention(engine: AsyncEngine, story_id: str, fact_id: str, n: int = 1) -> Operation:
    """A turn whose ``n`` events presuppose ``fact_id``."""

    async def write(conn: AsyncConnection, op: Operation) -> None:
        event = await conn.scalar(
            text("SELECT id FROM events WHERE operation_id = :o ORDER BY seq LIMIT 1"),
            {"o": op.operation_id},
        )
        found = await edges.fact_edges(
            conn,
            story_id,
            DependencyEdgeType.PRESUPPOSES,
            "events",
            str(event) if event else new_ulid(),
            [fact_id],
            position=Position(op.narrative_turn or 0, 0, 0, 2, 0),
        )
        extra = [
            edges.EdgeSpec(
                DependencyEdgeType.PRESUPPOSES,
                "events",
                new_ulid(),
                DependencyTarget.FACT,
                fact_id,
                target_subject_id=found[0].target_subject_id,
                target_predicate_id=found[0].target_predicate_id,
                position=Position(op.narrative_turn or 0, 0, 0, 3 + i, 0),
            )
            for i in range(n - 1)
        ]
        await edges.record_edges(conn, op, [*found, *extra])

    op, _ = await c.run(engine, story_id, write)
    return op


async def test_dependency_edges(engine: AsyncEngine) -> None:
    w = await c.world(engine)
    bobby, ann, cal = w.people["Bobby"], w.people["Ann"], w.people["Cal"]
    first = await c.commit(engine, w.story_id, c.at(bobby, w.places["school"], None, None))
    await _mention(engine, w.story_id, first.id)

    # Lineage: superseded, the old version's dependents are found through the new one.
    later = await c.supersede(
        engine, w.story_id, first.id, c.at(bobby, w.places["garden"], None, None)
    )
    found = await _search(engine, w.story_id, targets=edges.Targets(fact_ids=(later.id,)))
    assert [(d.edge_type, d.target) for d in found.items] == [
        (DependencyEdgeType.PRESUPPOSES, ("fact", first.id))
    ]
    assert not found.truncated

    # A transition's derives edge: Ann's belief from the fact (a record antecedent in canon).
    ann_belief = (
        await parts.transit(
            engine,
            w.story_id,
            w.characters["Ann"],
            first.proposition,
            cause=transitions.Kind.DISCOVERY,
            antecedent=transitions.Antecedent.record("facts", first.id),
        )
    ).belief
    (edge,) = await rows(
        engine,
        "SELECT * FROM dependency_edges WHERE dependent_id = :b",
        b=ann_belief.id,
    )
    assert (edge.edge_type, edge.target_kind, edge.target_id) == ("derives", "fact", first.id)
    assert edge.acquisition_kind == BeliefTransitionKind.DISCOVERY.value
    # Ann infers that she will find Bobby at school from her belief (a belief antecedent; its
    # own proposition, another key, is its target): a depth-2 search follows the chain.
    inferred = (
        await parts.transit(
            engine,
            w.story_id,
            w.characters["Ann"],
            c.at(w.people["Ann"], w.places["school"], None, None),
            cause=transitions.Kind.INFERENCE,
            antecedent=transitions.Antecedent.record("beliefs", ann_belief.id),
        )
    ).belief
    del cal, ann
    deep = await _search(
        engine,
        w.story_id,
        targets=edges.Targets(fact_ids=(first.id,)),
        edge_types=[DependencyEdgeType.DERIVES],
        depth=2,
    )
    by_belief = {d.dependent[1]: d for d in deep.items}
    assert by_belief[ann_belief.id].depth == 1
    assert by_belief[inferred.id].depth == 2
    assert by_belief[inferred.id].acquisition_kind == "inference"
    shallow = await _search(
        engine,
        w.story_id,
        targets=edges.Targets(fact_ids=(first.id,)),
        edge_types=[DependencyEdgeType.DERIVES],
        depth=1,
    )
    assert inferred.id not in {d.dependent[1] for d in shallow.items}

    # The cap: truncated, never silently dropped; a deeper request is clamped.
    capped = await _search(
        engine,
        w.story_id,
        targets=edges.Targets(fact_ids=(first.id,)),
        max_results=1,
        depth=9,
        max_depth=3,
    )
    assert (len(capped.items), capped.truncated, capped.depth_clamped) == (1, True, True)

    # Amendment edges: part B relies on part A's fact.
    part_a, part_b = new_ulid(), new_ulid()

    async def amend(conn: AsyncConnection, op: Operation) -> None:
        await edges.amendment_edges(conn, op, part_a, [first.id])
        await edges.amendment_edges(conn, op, part_b, [later.id], written_by={later.id: part_a})

    await c.run(engine, w.story_id, amend, kind=OperationKind.CONTROL)
    written = await rows(
        engine,
        "SELECT target_kind::text AS kind, target_id, position_turn FROM dependency_edges "
        "WHERE dependent_id = :b ORDER BY target_kind",
        b=part_b,
    )
    assert {(r.kind, r.target_id) for r in written} == {
        ("fact", later.id),
        ("amendment_part", part_a),
    }
    assert all(r.position_turn is None for r in written)
    relying = await _search(engine, w.story_id, targets=edges.Targets(amendment_part_ids=(part_a,)))
    assert [d.dependent for d in relying.items] == [("amendment_parts", part_b)]

    with pytest.raises(ValueError, match="omniscient"):
        await _search(
            engine,
            w.story_id,
            targets=edges.Targets(fact_ids=(first.id,)),
            viewpoint=w.viewpoint("Bobby"),
        )


async def test_dependency_edges__membership(engine: AsyncEngine) -> None:
    w = await c.world(engine)
    bobby = w.people["Bobby"]
    fact = await c.commit(engine, w.story_id, c.at(bobby, w.places["school"], None, None))
    await c.run(engine, w.story_id, lambda conn, op: _nothing())  # a turn after the fact
    depending = await _mention(engine, w.story_id, fact.id)
    targets = edges.Targets(fact_ids=(fact.id,))
    before = await _search(engine, w.story_id, targets=targets)
    assert len(before.items) == 1

    turn = int(depending.narrative_turn or 0)
    await c.run(engine, w.story_id, lambda conn, op: _nothing())  # the head moves on
    assert await projections.truncate(engine, w.story_id, turn - 1) is not None
    assert (await _search(engine, w.story_id, targets=targets)).items == []
    await projections.untruncate(engine, w.story_id)
    assert (await _search(engine, w.story_id, targets=targets)).items == before.items

    # A fork copies the edge, naming only the fork's own ids.
    forked = await forks.fork(engine, w.story_id)
    copied = await rows(
        engine, "SELECT * FROM dependency_edges WHERE story_id = :s", s=forked.story_id
    )
    assert copied
    own = {
        str(r.id)
        for r in await rows(
            engine,
            "SELECT id FROM facts WHERE story_id = :s UNION SELECT id FROM events "
            "WHERE story_id = :s",
            s=forked.story_id,
        )
    }
    for edge in copied:
        assert edge.target_id in own, edge
        assert edge.dependent_id in own, edge

    # Story isolation: another story, the same proposition shape, never answers.
    other = await c.world(engine)
    twin = await c.commit(
        engine, other.story_id, c.at(other.people["Bobby"], other.places["school"], None, None)
    )
    await _mention(engine, other.story_id, twin.id)
    mine = await _search(
        engine,
        w.story_id,
        targets=edges.Targets(fact_ids=(fact.id,), keys=((bobby, fact.predicate_id),)),
    )
    theirs_ids = {e.id for e in await _edges(engine, other.story_id)}
    assert theirs_ids
    assert all(d.edge_id not in theirs_ids for d in mine.items)
    theirs = await _search(engine, w.story_id, targets=edges.Targets(fact_ids=(twin.id,)))
    assert theirs.items == []


async def _edges(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(engine, "SELECT id FROM dependency_edges WHERE story_id = :s", s=story_id)


async def _nothing() -> None:
    return None


async def test_lite_writes_no_edges(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, _epistrel):
        story, ids = await new_story(client)
        await played(client, story, body(next(iter(ids.values())), "Hello?"))
    assert await _edges(engine, story) == []
