"""S-ABSENT-FACT, M4 part: empty lookups are dependencies (FR-CONC-11,12; docs/04 §7; #136).

A structured turn checks for locks on the door, finds none, and narrates opening it; while it is
held inside its model call (a latch, never a sleep), a concurrent ``commit_fact`` adds a lock.
At commit the turn's empty "locks on this door" key matches the new fact in the change index,
so the turn regenerates, and the regenerated attempt is given the lock and narrates it.
Complements: an unrelated ``commit_fact`` (which also supersedes the fact the turn read as
semantic context) lands during generation and the turn rebases and commits without
regenerating; a lock only another character can see does not touch the speaker's key; and the
semantic context is a retrieved row, never a key. The negative run
(``test_s_absent_fact__without_intersection``) is the same lock scenario on a build with the
intersection off (``epistrel.testing.faults``): the turn rebases and narrates opening a locked
door, which is the failure this story removes.

The #140 variant (``test_s_absent_fact__verify``): the lock is checked by ``verify``'s
precondition rule rather than by a bare lookup. The story runs on ``Structured (M4)``, and the
turn's read phase asks ``verify`` whether the door has the padlock. The answer is ``novel``
(none), and its keyed fetch reads every lock on the door (object ``*``). A lock committed during
generation touches that key, the turn regenerates, and the regenerated attempt's verdict is
``supported``, so it narrates the lock. Complement: an unrelated commit rebases, and the rebase
re-runs the verdict (``REBASE_CHECKS``), which is unchanged, so the turn commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon.facts import Visibility
from epistrel.core.enums import OperationKind
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.readset import EMPTY_RESULT, StructuredReadSet
from epistrel.core.verify import rules
from epistrel.testing.faults import readset_intersection_disabled
from tests.canon_support import LOCATED_AT, World, at, auto_predicate, commit, world
from tests.memory_support import use_structured
from tests.readset_support import (
    Context,
    KeyedTurn,
    Lookup,
    reply_of,
    start_keyed_turn,
    stored_read_set,
)

pytestmark = pytest.mark.integration

NOW = 1_000
"""The story-time the turn opens the door at."""
OPENS = "The door opens."
LOCKED = "The door is locked."


@dataclass(frozen=True)
class Door:
    world: World
    door: str
    padlock: str
    has_lock: str
    context_fact: str
    context: Context

    @property
    def story_id(self) -> str:
        return self.world.story_id

    def lookup(self, viewpoint: str = "omniscient") -> Lookup:
        """The precondition lookup: locks on this door, now (any lock: object ``*``)."""
        return Lookup(
            predicate=self.has_lock, subjects=self.door, story_time=NOW, viewpoint=viewpoint
        )

    def lock(self) -> Proposition:
        return Proposition(
            subject_id=self.door,
            predicate_id=self.has_lock,
            object_id=self.padlock,
            valid_time=Interval.of(0, None),
        )


async def _door(engine: AsyncEngine) -> Door:
    w = await world(engine)
    async with engine.begin() as conn:
        door = await store.add_entity(conn, w.story_id, "item", "the cellar door")
        padlock = await store.add_entity(conn, w.story_id, "item", "a padlock")
    has_lock = await auto_predicate(engine, w.story_id, "has lock")
    # What the turn reads as generation context: Ann is in the kitchen.
    context = await commit(
        engine,
        w.story_id,
        at(w.people["Ann"], w.places["kitchen"], 0, None),
        kind=OperationKind.CONTROL,
    )
    return Door(
        w, door, padlock, has_lock, context.id, Context("facts", context.id, context.tx_from)
    )


async def _start(engine: AsyncEngine, d: Door, viewpoint: str = "omniscient") -> KeyedTurn:
    held = await start_keyed_turn(engine, d.story_id, [d.lookup(viewpoint)], context=[d.context])
    keys = held.read_set.query_keys
    assert [k.result_hash for k in keys] == [EMPTY_RESULT], keys  # nothing there: still a key
    return held


async def _finish(engine: AsyncEngine, held: KeyedTurn) -> str:
    while not held.done:
        await held.release()
    result = await held.task
    return await reply_of(engine, result.operation.operation_id)


@pytest.mark.part("M4", "FR-CONC-11,12")
async def test_s_absent_fact(engine: AsyncEngine) -> None:
    """An empty precondition key matches a lock added during generation: the turn regenerates
    and the regenerated beats respect the lock."""
    d = await _door(engine)
    held = await _start(engine, d)
    lock = await commit(engine, d.story_id, d.lock(), kind=OperationKind.CONTROL)

    await held.release()
    first = held.op.attempts[0]
    assert first["outcome"] == "regenerate", first
    assert first["reason"] == "read_set_touched"
    assert first["touched"] == ["query_key:0"]
    assert not held.done
    assert held.model.calls == 2
    assert held.model.found[1] == [[lock.id]]  # the regenerated prompt holds the lock
    assert await _finish(engine, held) == LOCKED
    assert [a["outcome"] for a in held.op.attempts] == ["regenerate", "commit"]

    stored = await stored_read_set(engine, held.op.operation_id)
    (key,) = stored["keys"]
    assert key["result_hash"] != EMPTY_RESULT  # the committed attempt's key saw the lock
    assert key["subjects"] == [d.door]
    assert key["object_id"] is None
    assert stored["retrieved"] == [
        {"table": "facts", "id": d.context_fact, "version": d.context.version.isoformat()}
    ]


@pytest.mark.part("M4", "FR-CONC-11,12")
async def test_s_absent_fact__unrelated(engine: AsyncEngine) -> None:
    """Complement: an unrelated ``commit_fact`` during generation — Ann moves to the garden,
    which also supersedes the fact the turn read as context — rebases: no regeneration, and the
    door opens. Semantic context is a retrieved row, never a key."""
    d = await _door(engine)
    held = await _start(engine, d)
    keys = held.read_set.query_keys
    assert {k.predicate_id for k in keys} == {d.has_lock}
    assert LOCATED_AT not in {k.predicate_id for k in keys}
    assert [r.id for r in held.read_set.retrieved.values()] == [d.context_fact]
    moved = await commit(
        engine,
        d.story_id,
        at(d.world.people["Ann"], d.world.places["garden"], 0, None),
        kind=OperationKind.CONTROL,
    )
    assert d.context_fact in moved.superseded_ids

    assert await _finish(engine, held) == OPENS
    assert [a["outcome"] for a in held.op.attempts] == ["rebase"]
    assert held.model.calls == 1


@pytest.mark.part("M4", "FR-CONC-11,12")
async def test_s_absent_fact__latent(engine: AsyncEngine) -> None:
    """Complement: the key carries its viewpoint. A lock only Bobby can see does not touch the
    key of a turn read for Cal: it rebases and the door opens for him. A second such lock does
    touch an omniscient turn's key: it regenerates."""
    d = await _door(engine)
    bobby_only = Visibility(scope="characters", characters=(d.world.characters["Bobby"],))
    cal = await _start(engine, d, viewpoint=d.world.viewpoint("Cal"))
    await commit(engine, d.story_id, d.lock(), kind=OperationKind.CONTROL, visibility=bobby_only)
    assert await _finish(engine, cal) == OPENS
    assert [a["outcome"] for a in cal.op.attempts] == ["rebase"]

    omniscient = await start_keyed_turn(engine, d.story_id, [d.lookup()], context=[d.context])
    async with engine.begin() as conn:
        bolt = await store.add_entity(conn, d.story_id, "item", "a bolt")
    await commit(
        engine,
        d.story_id,
        d.lock().model_copy(update={"object_id": bolt}),
        kind=OperationKind.CONTROL,
        visibility=bobby_only,
    )
    assert await _finish(engine, omniscient) == LOCKED
    assert [a["outcome"] for a in omniscient.op.attempts] == ["regenerate", "commit"]


@pytest.mark.part("M4", "FR-CONC-11,12")
async def test_s_absent_fact__without_intersection(engine: AsyncEngine) -> None:
    """The negative run: with the key-against-change-index intersection off, the same lock
    lands unnoticed — the turn rebases and narrates opening a locked door. The scenario above
    fails on such a build; this run shows that it is the intersection that makes it pass."""
    d = await _door(engine)
    with readset_intersection_disabled():
        held = await _start(engine, d)
        await commit(engine, d.story_id, d.lock(), kind=OperationKind.CONTROL)
        assert await _finish(engine, held) == OPENS
    assert [a["outcome"] for a in held.op.attempts] == ["rebase"]


def _verify_read(d: Door) -> Any:
    """The turn's read phase: ``verify`` the precondition "the door has the padlock"; its
    prompt holds the evidence (nothing while the verdict is ``novel``)."""

    async def read(conn: AsyncConnection, source: StructuredReadSet) -> list[list[str]]:
        verdict = await rules.verify(conn, source, d.story_id, d.lock())
        assert verdict is not None  # Structured (M4) verifies
        return [[e.fact_id for e in verdict.evidence if e.fact_id is not None]]

    return read


@pytest.mark.part("M4", "FR-CONC-11,12")
async def test_s_absent_fact__verify(engine: AsyncEngine) -> None:
    """The #140 variant: the lock checked by ``verify``'s precondition rule. Its empty keyed
    fetch is a dependency: a lock added during generation regenerates the turn, whose
    regenerated verdict is ``supported`` and narrates the lock. Complement: an unrelated commit
    rebases, the rebase re-runs the verdict unchanged, and the door opens."""
    d = await _door(engine)
    await use_structured(engine, d.story_id)
    held = await start_keyed_turn(engine, d.story_id, [], read=_verify_read(d))
    keys = held.read_set.query_keys
    (lock_key,) = [k for k in keys if k.predicate_id == d.has_lock]
    assert lock_key.result_hash == EMPTY_RESULT
    assert lock_key.object_id is None  # any lock on the door
    assert [c.kind for c in rules.recorded(held.read_set)] == [rules.VerdictKind.NOVEL]
    lock = await commit(engine, d.story_id, d.lock(), kind=OperationKind.CONTROL)
    await held.release()
    first = held.op.attempts[0]
    assert (first["outcome"], first["reason"]) == ("regenerate", "read_set_touched")
    assert held.model.found[1] == [[lock.id]]
    assert await _finish(engine, held) == LOCKED
    assert [c.kind for c in rules.recorded(held.read_set)] == [rules.VerdictKind.SUPPORTED]

    calm = await _door(engine)
    await use_structured(engine, calm.story_id)
    quiet = await start_keyed_turn(engine, calm.story_id, [], read=_verify_read(calm))
    checks = len(rules.recorded(quiet.read_set))
    await commit(
        engine,
        calm.story_id,
        at(calm.world.people["Ann"], calm.world.places["garden"], 0, None),
        kind=OperationKind.CONTROL,
    )
    assert await _finish(engine, quiet) == OPENS
    assert [a["outcome"] for a in quiet.op.attempts] == ["rebase"]
    assert quiet.model.calls == 1
    assert checks == 1
