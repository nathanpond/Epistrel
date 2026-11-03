"""The keyed reads' SQL pre-filters at their boundaries (#593; #520 added them): each is a
superset of the Python filter that still decides, so a belief or fact the decider keeps must
also survive the query. Two boundaries the other tests do not reach:

- a symmetric belief read through its stored *object* (``partitions.partition``'s ``entities_``
  matches subject or object);
- a fact whose valid-time starts at a window's last minute (``_canon_where``'s overlap bound,
  ``valid_from < hi``)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from tests.canon_support import LOCATED_AT, at, commit, world
from tests.partition_support import believe

pytestmark = pytest.mark.integration

FRIEND_OF = CORE_BY_NAME["friend_of"].id


async def test_a_symmetric_belief_is_read_through_its_object(engine: AsyncEngine) -> None:
    """A symmetric belief is stored lower id first; a lookup naming the higher id (the stored
    object) finds it. Complement: a lookup naming a third person does not."""
    w = await world(engine)
    story = w.story_id
    cal, bobby = w.people["Cal"], w.people["Bobby"]
    low, high = sorted((cal, bobby))
    holder = w.characters["Ann"]
    held = await believe(
        engine,
        story,
        holder,
        Proposition(
            subject_id=high, predicate_id=FRIEND_OF, object_id=low, valid_time=Interval.of(0, None)
        ),
    )
    assert (held.proposition.subject_id, held.proposition.object_id) == (low, high)
    async with engine.connect() as conn:
        lookup = KeyedLookup(conn, StructuredReadSet())
        found = await lookup.beliefs(story, holder=holder, predicate=FRIEND_OF, subjects=high)
        other = await lookup.beliefs(
            story, holder=holder, predicate=FRIEND_OF, subjects=w.people["Ann"]
        )
    assert [b.id for b in found] == [held.id]
    assert other == []


async def test_a_fact_starting_at_the_windows_last_minute_overlaps(engine: AsyncEngine) -> None:
    """A window ``[0, 100)`` meets a fact valid from minute 99; one valid from minute 100 does
    not (the complement: the bound is exclusive)."""
    w = await world(engine)
    story = w.story_id
    ann, bobby = w.people["Ann"], w.people["Bobby"]
    await commit(engine, story, at(ann, w.places["kitchen"], 99, None), kind=OperationKind.CONTROL)
    await commit(
        engine, story, at(bobby, w.places["garden"], 100, None), kind=OperationKind.CONTROL
    )
    async with engine.connect() as conn:
        lookup = KeyedLookup(conn, StructuredReadSet())
        last_minute = await lookup.canon_at(
            story, predicate=LOCATED_AT, subjects=ann, window=(0, 100)
        )
        after = await lookup.canon_at(story, predicate=LOCATED_AT, subjects=bobby, window=(0, 100))
    assert [f.proposition.object_id for f in last_minute] == [w.places["kitchen"]]
    assert after == []
