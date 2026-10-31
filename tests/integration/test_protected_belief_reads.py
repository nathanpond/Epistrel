"""#522: the protected-content index's belief-only terms (#172) read only the beliefs that can
give one.

P-04's multi-character run spent most of a turn's CPU decoding partitions: every speaker's
protected index read every other character's whole partition (200 beliefs each in the
fixture) to keep the few beliefs whose object is a person the speaker has no way to know. The
index now asks ``partition`` for those objects alone (``objects_``, which also matches a member
of an ``object_set``) and reads nothing when the speaker knows every person the story names.
The terms are the same."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.epistemic import partitions
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.context import load_index
from epistrel.core.verify.protected import load_protected_index
from tests import canon_support as c
from tests import partition_support as ps
from tests import perception_support
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration

MARRIED_TO = CORE_BY_NAME["married_to"].id
DEGREE_AWARDED = CORE_BY_NAME["degree_awarded"].id
ALWAYS = Interval.of(None, None)


async def _story(engine: AsyncEngine) -> c.World:
    w = await c.world(engine)
    await use_structured(engine, w.story_id)
    await perception_support.perceive(engine, w.story_id, "co_location")
    return w


async def _index_reads(engine: AsyncEngine, story_id: str, speaker: str) -> tuple[Any, list[str]]:
    """``speaker``'s protected index and the ``beliefs`` statements building it ran."""
    heard: list[str] = []

    def listen(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if "FROM beliefs" in statement:
            heard.append(" ".join(statement.split()))

    async with engine.connect() as conn:
        index = await load_index(conn, story_id)
        event.listen(engine.sync_engine, "before_cursor_execute", listen)
        try:
            built = await load_protected_index(conn, story_id, speaker, index=index)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", listen)
    return built, heard


async def test_belief_terms_read_only_the_beliefs_naming_an_unknown_person(
    engine: AsyncEngine,
) -> None:
    """Bobby alone believes Cal is married to Dana, a person no canon names, and holds a belief
    of no person besides (Ann is in the garden). Ann's index protects "Dana" with Bobby's
    belief, and each partition it read was asked for the unknown people's beliefs only.
    Complement: once canon names every person (all of them in the kitchen), Ann knows them all
    and her index reads no belief at all, where it read the other two characters' partitions
    whole; Dana is no longer protected."""
    w = await _story(engine)
    story = w.story_id
    async with engine.begin() as conn:
        dana = await store.add_entity(conn, story, "person", "Dana")
    secret = await ps.believe(
        engine,
        story,
        w.characters["Bobby"],
        Proposition(
            subject_id=w.people["Cal"], predicate_id=MARRIED_TO, object_id=dana, valid_time=ALWAYS
        ),
    )
    await ps.believe(
        engine, story, w.characters["Bobby"], c.at(w.people["Ann"], w.places["garden"], None, None)
    )

    built, heard = await _index_reads(engine, story, w.characters["Ann"])
    assert built is not None
    belief_terms = {(t.folded, t.ref_id) for t in built.terms if t.ref_kind == "belief"}
    assert belief_terms == {("dana", secret.id)}
    assert heard, "the other characters' partitions are read while a person is unknown"
    assert all("propositions.object_id IN" in s and "?|" in s for s in heard), heard

    for person in (*w.people.values(), dana):
        await c.commit(engine, story, c.at(person, w.places["kitchen"], 0, None))
    built, heard = await _index_reads(engine, story, w.characters["Ann"])
    assert built is not None
    assert not [t for t in built.terms if t.ref_kind == "belief"]
    assert heard == []


async def test_partition_objects_match_an_object_or_a_member_of_an_object_set(
    engine: AsyncEngine,
) -> None:
    """``partition(objects_=…)`` keeps a belief whose object, or any member of whose object
    set, is one of them, and drops the rest; without it every belief is read."""
    w = await _story(engine)
    story = w.story_id
    async with engine.begin() as conn:
        guild, college, academy = [
            await store.add_entity(conn, story, "organization", name)
            for name in ("Guild", "College", "Academy")
        ]
    holder = w.characters["Ann"]
    joint = await ps.believe(
        engine,
        story,
        holder,
        Proposition(
            subject_id=w.people["Bobby"],
            predicate_id=DEGREE_AWARDED,
            object_set=(guild, college),
            qualifiers={"degree_id": "ba"},
            valid_time=ALWAYS,
        ),
    )
    placed = await ps.believe(
        engine, story, holder, c.at(w.people["Bobby"], w.places["school"], None, None)
    )

    async def ids(objects: list[str] | None) -> set[str]:
        async with engine.connect() as conn:
            return {b.id for b in await partitions.partition(conn, story, holder, objects_=objects)}

    assert await ids([college]) == {joint.id}
    assert await ids([w.places["school"], guild]) == {joint.id, placed.id}
    assert await ids([academy]) == set()
    assert await ids(None) == {joint.id, placed.id}
