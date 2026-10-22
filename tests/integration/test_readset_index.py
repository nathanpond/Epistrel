"""The structured read set's change index and keys (#136): what each canon write records, the
revision it is stamped with, how a key matches a change (objects, literals, sets, symmetric and
inverse predicates, ``same_as``, holders, windows), the pruning watermark, the key cap, the
operation view, and the Lite turn left as it was."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.canon import facts as canon
from epistrel.core.concurrency import READ_SET_MEMBERS
from epistrel.core.config_dependencies import LITE_DEPENDENCIES
from epistrel.core.enums import OperationKind, RecordProvenance
from epistrel.core.errors import RevisionConflict
from epistrel.core.events import Operation
from epistrel.core.operation_view import read_operation
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, SAME_AS
from epistrel.core.readset import (
    CANON,
    EMPTY_RESULT,
    KeyedLookup,
    QueryKey,
    StructuredReadSet,
    touched_keys,
)
from epistrel.orchestrator.jobs.handlers import retention
from tests.canon_support import LOCATED_AT, PRINCIPAL, World, at, auto_predicate, commit, world
from tests.concurrency_support import revision, start_turn
from tests.readset_support import Lookup, start_keyed_turn, stored_read_set

pytestmark = pytest.mark.integration

HOLDS = CORE_BY_NAME["holds"].id
HELD_BY = CORE_BY_NAME["held_by"].id
FRIEND_OF = CORE_BY_NAME["friend_of"].id
DEGREE_AWARDED = CORE_BY_NAME["degree_awarded"].id


async def _changes(engine: AsyncEngine, story_id: str) -> list[Any]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT timeline_revision, proposition_id, subject_id, object_id, change, "
                "lower(valid_window) AS lo, upper(valid_window) AS hi, operation_id "
                "FROM proposition_changes WHERE story_id = :s ORDER BY timeline_revision, change"
            ),
            {"s": story_id},
        )
        return list(found)


async def _touched(engine: AsyncEngine, story_id: str, base: int, *keys: QueryKey) -> list[str]:
    async with engine.connect() as conn:
        return await touched_keys(conn, story_id, base, list(keys))


def _key(predicate: str, subjects: tuple[str, ...] | None, **extra: Any) -> QueryKey:
    return QueryKey(subjects=subjects, predicate_id=predicate, **extra)


async def test_each_canon_write_records_its_changes(engine: AsyncEngine) -> None:
    """An add is one ``add`` row; a supersede trimming a current version writes the new version
    (``supersede``), the closed one (``close``) and the remainders (``add``), stamped with the
    revision the writing operation left; derived work that does not bump is stamped with the
    current revision."""
    w = await world(engine)
    story, ann = w.story_id, w.people["Ann"]
    first = await commit(engine, story, at(ann, w.places["kitchen"], 0, 100))
    rev = await revision(engine, story)
    assert [(r.timeline_revision, r.change, r.lo, r.hi) for r in await _changes(engine, story)] == [
        (rev, "add", 0, 100)
    ]
    second = await commit(engine, story, at(ann, w.places["garden"], 40, 60))
    assert first.id in second.superseded_ids
    rows = [r for r in await _changes(engine, story) if r.timeline_revision == rev + 1]
    assert sorted((r.change, r.lo, r.hi) for r in rows) == [
        ("add", 0, 40),
        ("add", 60, 100),
        ("close", 0, 100),
        ("supersede", 40, 60),
    ]
    assert {r.operation_id for r in rows} == {rows[0].operation_id}

    async with Operation(
        engine, story_id=story, kind=OperationKind.CONSOLIDATION, principal=PRINCIPAL
    ) as op:

        async def derive(conn: AsyncConnection) -> None:
            await canon.commit_fact(
                conn,
                story,
                at(ann, w.places["school"], 200, 300),
                provenance=RecordProvenance.SYSTEM_CONSOLIDATED,
                operation=op,
            )

        op.add_commit_step(derive)
        await op.commit()
    assert await revision(engine, story) == rev + 1  # derived work never bumps
    derived = [r for r in await _changes(engine, story) if r.operation_id == op.operation_id]
    assert [r.timeline_revision for r in derived] == [rev + 1]


async def _two_items(engine: AsyncEngine, w: World) -> tuple[str, str]:
    async with engine.begin() as conn:
        door = await store.add_entity(conn, w.story_id, "item", "the door")
        gate = await store.add_entity(conn, w.story_id, "item", "the gate")
    return door, gate


async def test_key_matching(engine: AsyncEngine) -> None:
    """Objects, wildcards, windows, holders, set objects, literals, ``same_as`` and symmetric
    predicates, each against one change committed since the base."""
    w = await world(engine)
    story = w.story_id
    door, gate = await _two_items(engine, w)
    near = await auto_predicate(engine, story, "is near")
    base = await revision(engine, story)
    await commit(
        engine,
        story,
        Proposition(
            subject_id=door, predicate_id=near, object_id=gate, valid_time=Interval.of(10, 20)
        ),
        kind=OperationKind.CONTROL,
    )
    assert await _touched(engine, story, base, _key(near, (door,))) == ["query_key:0"]
    assert await _touched(engine, story, base, _key(near, None)) == ["query_key:0"]
    assert await _touched(engine, story, base, _key(near, (door,), object_id=gate)) != []
    assert await _touched(engine, story, base, _key(near, (door,), object_id=door)) == []
    assert await _touched(engine, story, base, _key(near, (gate,))) == []  # another subject
    assert await _touched(engine, story, base + 1, _key(near, (door,))) == []  # not since base
    assert await _touched(engine, story, base, _key(near, (door,), valid_window=(20, 30))) == []
    assert await _touched(engine, story, base, _key(near, (door,), valid_window=(19, 30))) != []
    utterance = _key(near, (door,), holder=f"utterance:{w.characters['Ann']}")
    assert await _touched(engine, story, base, utterance) == []  # a canon change only
    # Several keys: the touched ones by index.
    assert await _touched(
        engine, story, base, _key(near, (gate,)), _key(near, (door,)), _key(near, None)
    ) == ["query_key:1", "query_key:2"]

    # ``same_as`` (symmetric): a new identity touches keys on either side, of any predicate.
    base = await revision(engine, story)
    alias = await _alias(engine, story, "the old door")
    await commit(
        engine,
        story,
        Proposition(
            subject_id=door, predicate_id=SAME_AS, object_id=alias, valid_time=Interval.of(0, None)
        ),
        kind=OperationKind.CONTROL,
    )
    assert await _touched(engine, story, base, _key(near, (alias,))) == ["query_key:0"]
    assert await _touched(engine, story, base, _key(near, (door,))) == ["query_key:0"]
    assert await _touched(engine, story, base, _key(near, (gate,))) == []

    # A symmetric key matches the stored (canonical) direction either way round.
    base = await revision(engine, story)
    ann, bobby = w.people["Ann"], w.people["Bobby"]
    await commit(
        engine,
        story,
        Proposition(
            subject_id=ann, predicate_id=FRIEND_OF, object_id=bobby, valid_time=Interval.of(0, None)
        ),
        kind=OperationKind.CONTROL,
    )
    low, high = sorted((ann, bobby))
    flipped = _key(FRIEND_OF, (high,), object_id=low, symmetric=True)
    assert await _touched(engine, story, base, flipped) != []
    assert await _touched(engine, story, base, _key(FRIEND_OF, (high,), object_id=low)) == []


async def _alias(engine: AsyncEngine, story_id: str, name: str) -> str:
    async with engine.begin() as conn:
        return await store.add_entity(conn, story_id, "item", name)


async def test_set_and_literal_objects(engine: AsyncEngine) -> None:
    """A set object matches a key naming one of its members; a literal matches by value
    (``40`` is ``40.0``)."""
    w = await world(engine)
    story = w.story_id
    door, _ = await _two_items(engine, w)
    async with engine.begin() as conn:
        oxford = await store.add_entity(conn, story, "organization", "Oxford")
        yale = await store.add_entity(conn, story, "organization", "Yale")
    weighs = await auto_predicate(engine, story, "weighs")
    base = await revision(engine, story)
    await commit(
        engine,
        story,
        Proposition(
            subject_id=w.people["Ann"],
            predicate_id=DEGREE_AWARDED,
            object_set=(oxford, yale),
            valid_time=Interval.of(0, None),
        ),
        kind=OperationKind.CONTROL,
    )
    await commit(
        engine,
        story,
        Proposition.model_validate(
            {
                "subject_id": door,
                "predicate_id": weighs,
                "literal": {"kind": "int", "value": 40},
                "valid_time": {"kind": "interval", "start": None, "end": None},
            }
        ),
        kind=OperationKind.CONTROL,
    )
    ann = (w.people["Ann"],)
    assert await _touched(engine, story, base, _key(DEGREE_AWARDED, ann, object_id=yale)) != []
    assert await _touched(engine, story, base, _key(DEGREE_AWARDED, ann, object_id=door)) == []
    forty = {"kind": "float", "value": 40.0}
    assert await _touched(engine, story, base, _key(weighs, (door,), literal=forty)) != []
    other = {"kind": "int", "value": 41}
    assert await _touched(engine, story, base, _key(weighs, (door,), literal=other)) == []


async def test_inverse_lookup_records_the_canonical_key(engine: AsyncEngine) -> None:
    """``held_by(padlock, *)`` is read as ``holds(*, padlock)``: the key is canonical, so a new
    ``holds`` fact touches it, and the lookup finds the stored fact."""
    w = await world(engine)
    story = w.story_id
    padlock, _ = await _two_items(engine, w)
    held = await commit(
        engine,
        story,
        Proposition(
            subject_id=w.people["Ann"],
            predicate_id=HOLDS,
            object_id=padlock,
            valid_time=Interval.of(0, None),
        ),
        kind=OperationKind.CONTROL,
    )
    turn = await start_keyed_turn(
        engine, story, [Lookup(predicate=HELD_BY, subjects=padlock, story_time=5)]
    )
    (key,) = turn.read_set.query_keys
    assert (key.predicate_id, key.subjects, key.object_id) == (HOLDS, None, padlock)
    assert turn.model.found[0] == [[held.id]]
    await commit(
        engine,
        story,
        Proposition(
            subject_id=w.people["Cal"],
            predicate_id=HOLDS,
            object_id=padlock,
            valid_time=Interval.of(0, None),
        ),
        kind=OperationKind.CONTROL,
    )
    await turn.release()
    assert turn.op.attempts[0]["outcome"] == "regenerate"
    await turn.release()
    await turn.task


async def test_a_base_below_the_pruning_watermark_conflicts(engine: AsyncEngine) -> None:
    """The retention sweep prunes the change index past ``readset_change_retention_days`` and
    moves the story's watermark in the same transaction; a turn whose base is below it cannot be
    checked, so it counts as touched (regenerates), and the next attempt, based at the
    watermark, commits."""
    w = await world(engine)
    story = w.story_id
    door, _ = await _two_items(engine, w)
    near = await auto_predicate(engine, story, "is near")
    turn = await start_keyed_turn(engine, story, [Lookup(predicate=near, subjects=door)])
    base = turn.op.base_revision
    await commit(engine, story, at(w.people["Ann"], w.places["garden"], 0, None))
    swept = await retention.sweep_retention(engine, clock.now() + timedelta(days=8))
    assert swept["proposition_changes"] >= 1
    assert await _changes(engine, story) == []
    async with engine.connect() as conn:
        watermark = await conn.scalar(
            text("SELECT changes_pruned_through_revision FROM stories WHERE id = :s"),
            {"s": story},
        )
    assert base is not None
    assert watermark == base + 1
    await turn.release()
    first = turn.op.attempts[0]
    # The unrelated change (Ann's move) would have rebased, but its record is gone.
    assert (first["outcome"], first["touched"]) == ("regenerate", ["window"]), first
    await turn.release()
    assert (await turn.task).attempts[-1]["outcome"] == "commit"


async def test_a_pruned_base_without_other_touches(engine: AsyncEngine) -> None:
    """Pruned, with nothing else touched: the watermark alone regenerates the turn."""
    w = await world(engine)
    story = w.story_id
    door, _ = await _two_items(engine, w)
    near = await auto_predicate(engine, story, "is near")
    turn = await start_keyed_turn(engine, story, [Lookup(predicate=near, subjects=door)])
    await commit(
        engine, story, at(w.people["Ann"], w.places["garden"], 0, None), kind=OperationKind.CONTROL
    )
    await retention.sweep_retention(engine, clock.now() + timedelta(days=8))
    await turn.release()
    first = turn.op.attempts[0]
    assert (first["outcome"], first["touched"]) == ("regenerate", ["changes_pruned"]), first
    await turn.release()
    assert (await turn.task).attempts[-1]["outcome"] == "commit"


async def test_the_key_cap_conflicts(engine: AsyncEngine) -> None:
    """More keys than ``readset_max_keys``: 409 ``revision_conflict``, never a dropped key; a
    repeated lookup is one entry."""
    w = await world(engine)
    source = StructuredReadSet(max_keys=2)
    async with engine.connect() as conn:
        lookup = KeyedLookup(conn, source)
        for _ in range(3):
            await lookup.canon_at(w.story_id, predicate=LOCATED_AT, subjects=w.people["Ann"])
        await lookup.canon_at(w.story_id, predicate=LOCATED_AT, subjects=w.people["Cal"])
        with pytest.raises(RevisionConflict) as refused:
            await lookup.canon_at(w.story_id, predicate=LOCATED_AT, subjects=w.people["Bobby"])
    assert refused.value.details["reason"] == "read_set_overflow"
    assert len(source.query_keys) == 2
    assert all(k.result_hash == EMPTY_RESULT for k in source.query_keys)


async def test_the_operation_view_shows_the_read_set(engine: AsyncEngine) -> None:
    """``GET /operations/{id}``'s view: counts for every read, the keys for an omniscient one."""
    w = await world(engine)
    door, _ = await _two_items(engine, w)
    near = await auto_predicate(engine, w.story_id, "is near")
    turn = await start_keyed_turn(
        engine, w.story_id, [Lookup(predicate=near, subjects=door, story_time=3)]
    )
    await turn.release()
    op = (await turn.task).operation
    plain = await read_operation(engine, op.operation_id)
    assert plain.read_set is not None
    assert (plain.read_set.keys_count, plain.read_set.retrieved_count) == (1, 0)
    assert plain.read_set.keys is None
    seen = await read_operation(engine, op.operation_id, viewpoint="omniscient")
    assert seen.read_set is not None
    assert seen.read_set.keys == [
        {
            "subject": [door],
            "predicate": near,
            "object": "*",
            "holder": CANON,
            "window": [3, 4],
            "result_hash": EMPTY_RESULT,
        }
    ]
    assert op.attempts[-1]["keys_count"] == 1


async def test_a_lite_turn_is_unchanged(engine: AsyncEngine) -> None:
    """A Lite turn keeps #47's flat read set and M2's dependency set, and records no keys."""
    w = await world(engine)
    held = await start_turn(engine, w.story_id, "hello")
    held.model.release()
    result = await held.task
    stored = await stored_read_set(engine, result.operation.operation_id)
    assert set(stored) == set(READ_SET_MEMBERS)
    recorded = result.operation.config_revisions or {}
    assert set(recorded) == {"story", "deployment", "rating_version", *LITE_DEPENDENCIES}
    assert all("keys_count" not in a for a in result.attempts)
    view = await read_operation(engine, result.operation.operation_id, viewpoint="omniscient")
    assert view.read_set is not None
    assert (view.read_set.keys_count, view.read_set.keys) == (0, [])
