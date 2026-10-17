"""I-12 (M4): repair primitives (docs/04 §4). Every I-12 (M4) part lives in this file
(traceability counts a test ID per milestone in one file); #151's ``test_i_12__repair`` joins it.

``test_i_12__neighborhood`` (#142, FR-VERIFY-6): a contradicted claim returns the contradicted
fact, and ``fact_neighborhood`` returns its dependency cluster over real canon. The cluster
holds the facts with the same subject and predicate, the facts linked by a declared
implication, the key's closed set, the facts on a shared subject in both directions (to depth
2), the projections and the linked memories. It leaves out superseded, discarded, hidden and
unconnected facts, and it is bounded.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.canon.facts import Visibility
from epistrel.core.enums import OperationKind, Provenance, RecordProvenance
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import memory_record
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.memory import records
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Implication, Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.tables.memories import memories
from epistrel.core.verify.neighborhood import Edge, Neighborhood, Seed, fact_neighborhood
from epistrel.core.verify.rules import Rule, VerdictKind
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.verify.verifier import story_verifier
from tests.canon_support import KNOWS, LOCATED_AT, PRINCIPAL, World, at, commit, supersede, world
from tests.memory_support import use_structured
from tests.projection_support import truncate

pytestmark = pytest.mark.integration

LOCATED_IN = CORE_BY_NAME["located_in"].id
PRESENT_AT = CORE_BY_NAME["present_at"].id
SIBLING_OF = CORE_BY_NAME["sibling_of"].id
FRIEND_OF = CORE_BY_NAME["friend_of"].id


def _rel(
    subject: str, predicate: str, obj: str, lo: int | None = None, hi: int | None = None
) -> Proposition:
    return Proposition(
        subject_id=subject, predicate_id=predicate, object_id=obj, valid_time=Interval.of(lo, hi)
    )


async def _memory(conn: AsyncConnection, op: Operation, story_id: str, **links: Any) -> str:
    """A position-less semantic memory linked by ``proposition_ids`` or ``source_event_ids``."""
    memory_id = new_ulid()
    row = {
        "id": memory_id,
        "story_id": story_id,
        "kind": "semantic",
        "text": "Bobby keeps to the kitchen.",
        "holder_character_id": None,
        "character_id": None,
        "visibility": {"scope": "objective"},
        "provenance": RecordProvenance.SYSTEM_CONSOLIDATED.value,
        "trust_tier": records.trust_tier_of(RecordProvenance.SYSTEM_CONSOLIDATED).value,
        "operation_id": op.operation_id,
        "source_event_ids": list(links.get("source_event_ids", ())),
        "proposition_ids": list(links.get("proposition_ids", ())),
        "narrative_turn": None,
        "turn_variant": None,
    }
    await insert_derived(conn, memories, [row])
    await index_records(conn, [memory_record(SimpleNamespace(**row))])
    return memory_id


async def _cluster(engine: AsyncEngine) -> dict[str, Any]:
    """Bobby in the kitchen (0-100) with two memories about it, and what hangs on it."""
    w: World = await world(engine)
    sid = w.story_id
    await use_structured(engine, sid)
    bobby, ann, cal = w.people["Bobby"], w.people["Ann"], w.people["Cal"]
    kitchen, garden, school = w.places["kitchen"], w.places["garden"], w.places["school"]
    async with engine.begin() as conn:
        house = await store.add_entity(conn, sid, "place", "house")
        dora = await store.add_entity(conn, sid, "person", "Dora")
        attic = await store.add_entity(conn, sid, "place", "attic")
    found: dict[str, Any] = {"world": w, "house": house}
    async with Operation(engine, story_id=sid, kind=OperationKind.TURN, principal=PRINCIPAL) as op:
        op.append("player_input", {"text": "where is Bobby?"}, provenance=Provenance.PLAYER, beat=1)
        op.append("message_rendered", {"text": "In the kitchen."}, beat=2)

        async def step(conn: AsyncConnection) -> None:
            beats = await records.beats_of_operation(conn, op.operation_id)
            event = beats[-1].event_id
            fact = await canon.commit_fact(
                conn,
                sid,
                at(bobby, kitchen, 0, 100),
                provenance=RecordProvenance.RENDER,
                operation=op,
                source_event_ids=[event],
            )
            found["seed"] = fact
            found["by_proposition"] = await _memory(
                conn, op, sid, proposition_ids=[fact.proposition_id]
            )
            found["by_source"] = await _memory(conn, op, sid, source_event_ids=[event])
            found["unlinked"] = await _memory(conn, op, sid)

        op.add_commit_step(step)
        await op.commit()

    found["kitchen_in_house"] = await commit(engine, sid, _rel(kitchen, LOCATED_IN, house))
    found["garden_in_house"] = await commit(engine, sid, _rel(garden, LOCATED_IN, house))
    found["present"] = await commit(engine, sid, _rel(bobby, PRESENT_AT, kitchen, 0, 100))
    found["sibling"] = await commit(engine, sid, _rel(ann, SIBLING_OF, bobby))
    found["knows_cal"] = await commit(engine, sid, _rel(bobby, KNOWS, cal, 0, 100))
    found["ann_in_garden"] = await commit(engine, sid, at(ann, garden, 0, 100))
    old = await commit(engine, sid, at(bobby, garden, 200, 300))
    found["moved"] = await supersede(engine, sid, old.id, at(bobby, school, 200, 300))
    found["superseded"] = old
    found["hidden"] = await commit(
        engine,
        sid,
        _rel(bobby, KNOWS, ann, 0, 100),
        visibility=Visibility(scope="characters", characters=(w.characters["Ann"],)),
    )
    found["dora"] = await commit(engine, sid, at(dora, attic, 0, 100))
    async with engine.begin() as conn:
        reg = await registry.load(conn, sid)
        found["closed"] = await store.declare_closed_set(
            conn, sid, store.ClosedSet(KNOWS, bobby, (cal,)), reg, asserted_by="author"
        )
    # A friendship a truncation discards, with the turn after it.
    async with engine.connect() as conn:
        head = await conn.scalar(
            text("SELECT next_turn_index FROM stories WHERE id = :s"),
            {"s": sid},
        )
    found["discarded"] = await commit(engine, sid, _rel(bobby, FRIEND_OF, ann, 0, 100))
    await commit(engine, sid, at(dora, attic, 100, 200))
    assert await truncate(engine, sid, int(head) - 1) is not None
    return found


async def _neighborhood(
    engine: AsyncEngine, story_id: str, seed: Seed, **kw: Any
) -> tuple[Neighborhood, StructuredReadSet]:
    read_set = StructuredReadSet()
    async with engine.connect() as conn:
        reg = await registry.load(conn, story_id)
        rule = Implication(when=LOCATED_AT, then=PRESENT_AT)
        found = await fact_neighborhood(
            KeyedLookup(conn, read_set),
            story_id,
            seed,
            registry=replace(reg, story_rules=(*reg.story_rules, rule)),
            **kw,
        )
    return found, read_set


def _edges(found: Neighborhood) -> dict[str, tuple[Edge, int]]:
    return {i.id: (i.edge, i.depth) for i in found.items}


@pytest.mark.part("M4", "FR-VERIFY-6")
async def test_i_12__neighborhood(engine: AsyncEngine) -> None:
    """A contradicted claim (Bobby in the garden at 50-60, while canon has him in the kitchen of
    the same house) cites the kitchen fact, and its neighborhood is its dependency cluster:

    - the seed; Bobby's later place (the version that superseded the garden, not the garden);
      ``present_at`` through the declared implication; the closed set of whom Bobby knows;
    - the facts on a shared subject in both directions: the kitchen in the house (depth 1),
      Ann his sibling, whom Bobby knows (depth 1), and through them the garden in the house and
      Ann in the garden (depth 2);
    - the placement and relationship projections, each with its source fact;
    - the memory wrapping the seed's proposition, then the one from its source event.

    Complement: the superseded version, the friendship a truncation discarded, the unrelated
    memory and Dora in the attic (unconnected) are not in it. A fact hidden from Bobby is in
    the omniscient cluster and not in Bobby's. A cap of 3 keeps the seed, the same predicate
    and the implication, and says it truncated. A superseded seed has no neighborhood.
    Everything was read as context: retrieved rows, no query key."""
    f = await _cluster(engine)
    w: World = f["world"]
    sid = w.story_id
    bobby, garden = w.people["Bobby"], w.places["garden"]

    read_set = StructuredReadSet()
    async with engine.connect() as conn:
        v = await story_verifier(conn, read_set, sid, models=None, budget=TurnBudget())
        result = await v.verify(at(bobby, garden, 50, 60))
    assert result is not None
    assert (result.kind, result.rule) == (VerdictKind.CONTRADICTED, Rule.FUNCTIONAL_KEY.value)
    cited = [e.fact_id for e in result.verdict.evidence if e.fact_id is not None]
    assert f["seed"].id in cited
    assert result.needs_repair

    found, keyed = await _neighborhood(engine, sid, Seed.fact(f["seed"].id))
    edges = _edges(found)
    assert edges[f["seed"].id] == (Edge.SEED, 0)
    assert edges[f["moved"].id] == (Edge.PREDICATE, 1)
    assert edges[f["present"].id] == (Edge.IMPLICATION, 1)
    assert edges[f["closed"]] == (Edge.CLOSED_SET, 1)
    assert edges[f["kitchen_in_house"].id] == (Edge.SUBJECT, 1)
    assert edges[f["knows_cal"].id] == (Edge.SUBJECT, 1)
    assert edges[f["sibling"].id][1] == 1
    assert edges[f["garden_in_house"].id] == (Edge.OBJECT, 2)
    assert edges[f["ann_in_garden"].id] == (Edge.SUBJECT, 2)
    assert f["hidden"].id in edges
    projections = [i for i in found.items if i.kind == "projection"]
    assert {i.projection for i in projections} >= {"placement", "relationship"}
    assert all(i.source_fact_id in edges for i in projections if i.source_fact_id)
    assert found.ids("memory") == [f["by_proposition"], f["by_source"]]
    order = [PRIORITY_OF[i.edge] for i in found.items]
    assert order == sorted(order)
    assert not found.truncated

    # The complement: what does not hang on the seed, or is no longer canon, is left out.
    for absent in ("superseded", "discarded", "dora"):
        assert f[absent].id not in edges, absent
    assert f["unlinked"] not in edges
    assert keyed.query_keys == []
    assert keyed.retrieved

    as_bobby, _ = await _neighborhood(
        engine, sid, Seed.fact(f["seed"].id), viewpoint=w.viewpoint("Bobby")
    )
    assert f["hidden"].id not in _edges(as_bobby)
    assert f["knows_cal"].id in _edges(as_bobby)

    capped, _ = await _neighborhood(engine, sid, Seed.fact(f["seed"].id), max_facts=3)
    assert [i.id for i in capped.items] == [f["seed"].id, f["moved"].id, f["present"].id]
    assert capped.truncated

    gone, _ = await _neighborhood(engine, sid, Seed.fact(f["superseded"].id))
    assert gone.items == ()
    assert not gone.truncated

    around, _ = await _neighborhood(engine, sid, Seed.entity(bobby), depth=1)
    near = _edges(around)
    assert f["seed"].id in near
    assert f["sibling"].id in near
    assert f["ann_in_garden"].id not in near  # depth 2 only


PRIORITY_OF = {
    Edge.SEED: 0,
    Edge.PREDICATE: 1,
    Edge.IMPLICATION: 2,
    Edge.CLOSED_SET: 3,
    Edge.SUBJECT: 4,
    Edge.OBJECT: 4,
    Edge.PROJECTION: 5,
    Edge.MEMORY: 6,
}
