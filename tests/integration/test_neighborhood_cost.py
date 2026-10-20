"""``fact_neighborhood`` over a larger canon (#470, a regression test of #142's neighborhood).

The neighborhood ranks every neighbor from a lean link and decodes in full only the facts its
cap keeps. ``test_neighborhood_matches_the_full_decode`` proves the answer is unchanged: over a
canon of several hundred facts, for fact and entity seeds, depths 1 to 3, caps from 1 to
uncapped, and the omniscient and a character's viewpoint, it equals (items, order, edges,
depths, decoded facts, ``truncated`` and the read set's retrieved rows) what the previous
algorithm answered, kept below verbatim as ``_reference``. ``test_neighborhood_decodes_only_what
_it_keeps`` is the cost test: the facts decoded are at most the cap, however many neighbors
there are, and the statements do not grow with the facts per entity. Never a wall-clock bound.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.canon.facts import Fact, Visibility, visible_to
from epistrel.core.enums import RecordProvenance
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import memory_record
from epistrel.core.errors import StoryEntityNotFound
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.memory import records
from epistrel.core.memory.records import memories_linked
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Implication, Interval, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import stories
from epistrel.core.tables.memories import memories
from epistrel.core.verify import neighborhood as nb
from epistrel.core.verify.neighborhood import Edge, Neighborhood, NeighborhoodItem, Seed
from epistrel.core.verify.rules import closed_sets_for
from tests.canon_support import KNOWS, LOCATED_AT, World, at, run, world
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration

LOCATED_IN = CORE_BY_NAME["located_in"].id
PRESENT_AT = CORE_BY_NAME["present_at"].id

BOBBY_PLACES = 120
"""Bobby's ``located_at`` facts: one per window, over eight places."""


async def _memory(conn: AsyncConnection, op: Operation, story_id: str, **links: Any) -> str:
    """A position-less semantic memory linked by ``proposition_ids`` or ``source_event_ids``
    (as I-12's)."""
    memory_id = new_ulid()
    row = {
        "id": memory_id,
        "story_id": story_id,
        "kind": "semantic",
        "text": "Bobby keeps moving.",
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


def _rel(subject: str, predicate: str, obj: str, lo: int | None, hi: int | None) -> Proposition:
    return Proposition(
        subject_id=subject, predicate_id=predicate, object_id=obj, valid_time=Interval.of(lo, hi)
    )


async def _canon(engine: AsyncEngine) -> dict[str, Any]:
    """Bobby with ``BOBBY_PLACES`` places, a dozen acquaintances (half of whom know him back),
    twenty ``present_at`` facts (an implication of ``located_at``), a fact hidden from him and a
    superseded one; each acquaintance with ten places of their own (depth 2), the places in a
    house, a closed set and memories on some of his facts. About 430 facts."""
    w: World = await world(engine)
    sid = w.story_id
    await use_structured(engine, sid)
    bobby, ann = w.people["Bobby"], w.people["Ann"]
    async with engine.begin() as conn:
        people = [await store.add_entity(conn, sid, "person", f"P{j}") for j in range(12)]
        places = [await store.add_entity(conn, sid, "place", f"L{k}") for k in range(8)]
        house = await store.add_entity(conn, sid, "place", "house")
    found: dict[str, Any] = {"world": w, "people": people, "places": places}

    async def write(conn: AsyncConnection, op: Operation) -> None:
        async def add(p: Proposition, **kw: Any) -> Fact:
            return await canon.commit_fact(
                conn, sid, p, provenance=RecordProvenance.RENDER, operation=op, **kw
            )

        mine = [
            await add(at(bobby, places[i % 8], 10 * i, 10 * i + 5)) for i in range(BOBBY_PLACES)
        ]
        found["seed"] = mine[37]
        for j, person in enumerate(people):
            await add(_rel(bobby, KNOWS, person, 10 * j, 10 * j + 10))
            if j % 2 == 0:
                await add(_rel(person, KNOWS, bobby, 10 * j, 10 * j + 10))
            for i in range(10):
                await add(at(person, places[(i + j) % 8], 10 * i, 10 * i + 5))
        for k in range(20):
            await add(_rel(bobby, PRESENT_AT, places[k % 8], 10 * k, 10 * k + 3))
        for place in places:
            await add(_rel(place, LOCATED_IN, house, None, None))
        await add(
            _rel(bobby, KNOWS, ann, 0, 100),
            visibility=Visibility(scope="characters", characters=(w.characters["Ann"],)),
        )
        old = await add(_rel(ann, KNOWS, bobby, 500, 600))
        await canon.supersede(conn, sid, old.id, _rel(ann, KNOWS, bobby, 500, 700), operation=op)
        for fact in mine[:30:3]:
            await _memory(conn, op, sid, proposition_ids=[fact.proposition_id])
        await _memory(conn, op, sid, source_event_ids=list(mine[5].source_event_ids))

    op, _ = await run(engine, sid, write)
    found["turn"] = op.narrative_turn
    async with engine.begin() as conn:
        reg = await registry.load(conn, sid)
        await store.declare_closed_set(
            conn, sid, store.ClosedSet(KNOWS, bobby, tuple(people)), reg, asserted_by="author"
        )
    return found


def _rules(reg: Registry) -> Registry:
    rule = Implication(when=LOCATED_AT, then=PRESENT_AT)
    return replace(reg, story_rules=(*reg.story_rules, rule))


async def _reference(
    lookup: KeyedLookup,
    story_id: str,
    seed: Seed,
    *,
    depth: int,
    viewpoint: str,
    max_facts: int,
    registry: Registry,
    through_position: tuple[int, int] | None = None,
) -> Neighborhood:
    """``fact_neighborhood`` as it was before #470 (every neighbor decoded, then capped)."""
    out = nb._Collector()

    async def rows(**filters: Any) -> list[Fact]:
        return await lookup.canon_rows(
            story_id, viewpoint=viewpoint, through_position=through_position, **filters
        )

    seed_predicates: set[str] = set()
    if seed.fact_id is not None:
        found = await rows(ids=[seed.fact_id])
        if not found:
            return Neighborhood(seed, depth)
        (fact,) = found
        out.add(NeighborhoodItem("fact", fact.id, Edge.SEED, 0, fact=fact))
        start = nb._entities_of(fact)
        seed_predicates = {fact.predicate_id}
    else:
        start = [str(seed.entity_id)]
    linked = nb._linked_predicates(registry, seed_predicates)

    def edge_of(fact: Fact, level: int, direction: Edge) -> Edge:
        if level == 1 and fact.predicate_id in seed_predicates and direction is Edge.SUBJECT:
            return Edge.PREDICATE
        if fact.predicate_id in linked:
            return Edge.IMPLICATION
        return direction

    frontier = list(dict.fromkeys(start))
    visited: set[str] = set()
    first_level: list[Fact] = []
    for level in range(1, depth + 1):
        following: list[str] = []
        for entity in frontier:
            if entity in visited:
                continue
            visited.add(entity)
            for fact in await rows(subject=entity):
                out.add(
                    NeighborhoodItem(
                        "fact", fact.id, edge_of(fact, level, Edge.SUBJECT), level, fact=fact
                    )
                )
                following += nb._entities_of(fact)
                if level == 1:
                    first_level.append(fact)
            for fact in await rows(object=entity):
                out.add(
                    NeighborhoodItem(
                        "fact", fact.id, edge_of(fact, level, Edge.OBJECT), level, fact=fact
                    )
                )
                following.append(fact.subject_id)
        frontier = [e for e in dict.fromkeys(following) if e not in visited]
        if not frontier:
            break

    keys = set(start)
    predicates = seed_predicates | linked | {f.predicate_id for f in first_level}
    for predicate_id in sorted(predicates):
        for closed in await closed_sets_for(lookup.conn, story_id, predicate_id):
            if closed.subject_id in keys:
                out.add(
                    NeighborhoodItem("closed_set", closed.id, Edge.CLOSED_SET, 1, closed_set=closed)
                )
    as_of = None if through_position is None else through_position[0]
    if as_of is not None:
        head = await lookup.conn.scalar(
            select(stories.c.next_turn_index).where(stories.c.id == story_id)
        )
        if head is not None and as_of > int(head) - 1 >= 0:
            as_of = None
    for entity in dict.fromkeys(start):
        try:
            view = await lookup.profile(story_id, entity, as_of=as_of, viewpoint=viewpoint)
        except StoryEntityNotFound:
            continue
        for item in nb._projection_items(view, 1):
            out.add(item)

    collected = [i.fact for i in out.items() if i.fact is not None]
    memories = await memories_linked(
        lookup.conn,
        story_id,
        proposition_ids=nb._unique(f.proposition_id for f in collected),
        source_event_ids=nb._unique(e for f in collected for e in f.source_event_ids),
        limit=max_facts,
    )
    for memory in memories:
        if visible_to(memory.visibility, viewpoint):
            out.add(NeighborhoodItem("memory", memory.id, Edge.MEMORY, 1, memory=memory))

    items = out.items()
    kept = tuple(items[:max_facts])
    return Neighborhood(seed, depth, kept, truncated=len(items) > len(kept))


async def _both(
    engine: AsyncEngine, sid: str, seed: Seed, **kw: Any
) -> tuple[tuple[Neighborhood, StructuredReadSet], tuple[Neighborhood, StructuredReadSet]]:
    async with engine.connect() as conn:
        reg = _rules(await registry.load(conn, sid))
        new_set, old_set = StructuredReadSet(), StructuredReadSet()
        new = await nb.fact_neighborhood(KeyedLookup(conn, new_set), sid, seed, registry=reg, **kw)
        old = await _reference(KeyedLookup(conn, old_set), sid, seed, registry=reg, **kw)
    return (new, new_set), (old, old_set)


@contextmanager
def _counted(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """Statements issued and fact versions decoded (``canon._fact_of`` calls) inside."""
    seen = {"statements": 0, "decoded": 0}
    decode = canon._fact_of

    def counting(row: Any, **extra: Any) -> Fact:
        seen["decoded"] += 1
        return decode(row, **extra)

    def statement(*_: Any) -> None:
        seen["statements"] += 1

    monkeypatch.setattr(canon, "_fact_of", counting)
    event.listen(engine.sync_engine, "before_cursor_execute", statement)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", statement)
        monkeypatch.setattr(canon, "_fact_of", decode)


async def test_neighborhood_matches_the_full_decode(engine: AsyncEngine) -> None:
    """Over about 430 facts, the neighborhood equals the previous algorithm's for every seed,
    depth, cap and viewpoint tried: the same items in the same order (edges, depths, ids), the
    same decoded facts, the same ``truncated`` and the same retrieved rows in the read set.
    Complement: the cases cover truncated and untruncated answers, and answers with and
    without memories, so the equality is not vacuous."""
    f = await _canon(engine)
    w: World = f["world"]
    sid = w.story_id
    seeds = [Seed.fact(f["seed"].id), Seed.entity(w.people["Bobby"]), Seed.entity(f["people"][3])]
    viewpoints = [OMNISCIENT, w.viewpoint("Bobby")]
    # At the repair's depth: none, past the head (the turn being produced: the head's
    # projections) and before the big turn (none of its canon); at depths 1 and 3, none.
    turn = f["turn"]
    assert isinstance(turn, int)
    positions = [None, (turn + 1, 1), (max(turn - 1, 0), 0)]
    truncated, with_memory, cases = set(), set(), 0
    for seed in seeds:
        for depth in (1, 2, 3):
            for viewpoint in viewpoints:
                for position in positions if depth == nb.DEFAULT_DEPTH else [None]:
                    for cap in (1, 10, 10_000):
                        (new, new_set), (old, old_set) = await _both(
                            engine,
                            sid,
                            seed,
                            depth=depth,
                            viewpoint=viewpoint,
                            max_facts=cap,
                            through_position=position,
                        )
                        case = (seed, depth, viewpoint, position, cap)
                        assert new.as_payload() == old.as_payload(), case
                        assert new.facts() == old.facts(), case
                        assert [i.fact is not None for i in new.items] == [
                            i.fact is not None for i in old.items
                        ], case
                        assert new_set.retrieved == old_set.retrieved, case
                        assert new_set.query_keys == old_set.query_keys == [], case
                        truncated.add(new.truncated)
                        with_memory.add(bool(new.ids("memory")))
                        cases += 1
    assert cases == 90
    assert truncated == {True, False}
    assert with_memory == {True, False}


async def test_neighborhood_decodes_only_what_it_keeps(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost test: around Bobby (about 400 facts within depth 2), a neighborhood capped at
    10 decodes at most 10 facts (the previous algorithm decoded every neighbor), issues at most
    one statement more than it (the kept facts' single read) and fewer once it skips the
    memories no room is left for. Adding 60 more facts on the same entities leaves its
    statements and its decodes unchanged."""
    f = await _canon(engine)
    w: World = f["world"]
    sid, bobby = w.story_id, w.people["Bobby"]
    seed = Seed.entity(bobby)

    async def measure(new: bool) -> tuple[dict[str, int], Neighborhood]:
        async with engine.connect() as conn:
            reg = _rules(await registry.load(conn, sid))
            lookup = KeyedLookup(conn, StructuredReadSet())
            with _counted(engine, monkeypatch) as seen:
                if new:
                    found = await nb.fact_neighborhood(
                        lookup, sid, seed, max_facts=10, registry=reg
                    )
                else:
                    found = await _reference(
                        lookup,
                        sid,
                        seed,
                        depth=nb.DEFAULT_DEPTH,
                        viewpoint=OMNISCIENT,
                        max_facts=10,
                        registry=reg,
                    )
        return dict(seen), found

    cost, found = await measure(new=True)
    before, previous = await measure(new=False)
    assert found.as_payload() == previous.as_payload()
    assert found.truncated
    assert len(found.items) == 10  # a closed set and 9 facts
    assert before["decoded"] > 300  # every neighbor, before #470
    assert cost["decoded"] <= 10
    # The reference also reads the stories head (one statement) the original read only with a
    # through-position; it skips nothing, so the new read is at most one statement more.
    assert cost["statements"] <= before["statements"]

    async def more(conn: AsyncConnection, op: Operation) -> None:
        for i in range(60):
            p = at(bobby, f["places"][i % 8], 5000 + 10 * i, 5000 + 10 * i + 5)
            await canon.commit_fact(conn, sid, p, provenance=RecordProvenance.RENDER, operation=op)

    await run(engine, sid, more)
    grown, _ = await measure(new=True)
    assert grown == cost
