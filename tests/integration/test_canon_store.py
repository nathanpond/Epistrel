"""Bitemporal canon against PostgreSQL (#133): the ``facts`` table's registrations beyond the
milestone parts (U-02 in ``tests/test_u_02.py``, BITEMP in ``tests/property/test_bitemp.py``,
TIME (M4) in ``tests/property/test_time_m4.py``, S-FLASHBACK-TRUNCATE in
``tests/scenario/test_s_flashback_truncate.py``): erasure, the resurrection guard, two writes
in one operation, approximate bounds and the Lite path."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.db import make_engine
from epistrel.core.enums import RecordProvenance
from epistrel.core.errors import ErasedSubjectReferenced
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Bound, Interval, Proposition
from tests import erasure_support as e
from tests.auth_support import auth_settings
from tests.canon_support import KNOWS, LOCATED_AT, at, at_time, commit, fact_rows, run, world
from tests.conformance.conftest import migrated_database
from tests.event_support import play_turn, rows
from tests.respond_support import new_story as new_rest_story
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


async def _entity(engine: AsyncEngine, story: str, name: str, **fields: Any) -> str:
    async with engine.begin() as conn:
        return await store.add_entity(conn, story, fields.pop("kind", "person"), name, **fields)


async def test_erasure_deletes_facts_about_the_subject(pg_url: str, engine: AsyncEngine) -> None:
    """Erasing a character deletes every fact version whose subject or object is its entity
    and every version only it could see, each with a marker; a version it shared with another
    character loses its name. A later write naming it is refused. Complement: Ty's own fact,
    seen by Ty alone, stays as it was."""
    settings = e.erasure_settings(pg_url)
    async with respond_client(settings, engine) as (client, _):
        story, cast = await new_rest_story(client, "Wren", "Marisol Quenby", "Ty")
        marisol = await _entity(
            engine, story, "Marisol Quenby", character_id=cast["Marisol Quenby"]
        )
        ty = await _entity(engine, story, "Ty", character_id=cast["Ty"])
        well = await _entity(engine, story, "the well", kind="place")
        about = await commit(engine, story, at(marisol, well, 0, 10))
        knows = await commit(
            engine,
            story,
            Proposition(
                subject_id=ty,
                predicate_id=KNOWS,
                object_id=marisol,
                valid_time=Interval.of(0, None),
            ),
        )
        shared = await commit(
            engine,
            story,
            at(ty, well, 0, 10),
            visibility=canon.Visibility(
                scope="characters", characters=(cast["Marisol Quenby"], cast["Ty"])
            ),
        )
        only_hers = await commit(
            engine,
            story,
            at(ty, well, 20, 30),
            visibility=canon.Visibility(scope="characters", characters=(cast["Marisol Quenby"],)),
        )
        own = await commit(
            engine,
            story,
            at(ty, well, 40, 50),
            visibility=canon.Visibility(scope="characters", characters=(cast["Ty"],)),
        )
        done = await e.erase(client, "character", cast["Marisol Quenby"], story)
        assert done["residue"] == 0
    left = await fact_rows(engine, story)
    assert set(left) == {shared.id, own.id}
    assert left[shared.id].visibility == {"scope": "characters", "characters": [cast["Ty"]]}
    assert left[own.id].visibility == {"scope": "characters", "characters": [cast["Ty"]]}
    markers = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'erasure_marker' "
        "AND payload->>'record_table' = 'facts'",
        s=story,
    )
    assert {m.payload["record_id"] for m in markers} == {about.id, knows.id, only_hers.id}
    with pytest.raises(ErasedSubjectReferenced):
        await commit(
            engine,
            story,
            at(ty, well, 60, 70),
            visibility=canon.Visibility(
                scope="characters", characters=(cast["Marisol Quenby"], cast["Ty"])
            ),
        )


async def test_two_writes_in_one_operation_keep_transaction_time_ordered(
    engine: AsyncEngine,
) -> None:
    """A second write in one operation that closes the first still gets a non-empty
    transaction-time interval: its ``tx_from`` is a microsecond past the first's."""
    w = await world(engine)
    bobby = w.people["Bobby"]

    async def twice(conn: Any, op: Any) -> tuple[canon.Fact, canon.Fact]:
        one = await canon.commit_fact(
            conn,
            w.story_id,
            at(bobby, w.places["kitchen"], 0, 10),
            provenance=RecordProvenance.RENDER,
            operation=op,
        )
        two = await canon.commit_fact(
            conn,
            w.story_id,
            at(bobby, w.places["garden"], 0, 10),
            provenance=RecordProvenance.RENDER,
            operation=op,
        )
        return one, two

    _, (one, two) = await run(engine, w.story_id, twice)
    found = await fact_rows(engine, w.story_id)
    assert found[one.id].tx_to == found[two.id].tx_from
    assert found[one.id].tx_from < found[one.id].tx_to
    assert [f.id for f in await at_time(engine, w.story_id, 5)] == [two.id]


async def test_approximate_bounds_read_under_every_or_any_reading(engine: AsyncEngine) -> None:
    """A fact whose start is approximate ("some time in [0, 100)") covers T = 50 only under the
    ``any`` reading; at T = 150 under both."""
    w = await world(engine)
    vague = Proposition(
        subject_id=w.people["Ann"],
        predicate_id=LOCATED_AT,
        object_id=w.places["school"],
        valid_time=Interval(start=Bound(inner=100, outer=0), end=Bound.at(200)),
    )
    fact = await commit(engine, w.story_id, vague)
    row = (await fact_rows(engine, w.story_id))[fact.id]
    assert (row.valid_from, row.valid_from_inner, row.valid_to, row.valid_to_inner) == (
        0,
        100,
        200,
        200,
    )
    assert await at_time(engine, w.story_id, 50) == []
    assert [f.id for f in await at_time(engine, w.story_id, 50, reading="any")] == [fact.id]
    assert [f.id for f in await at_time(engine, w.story_id, 150)] == [fact.id]
    assert fact.valid_time == vague.valid_time


async def test_the_lite_path_reads_no_facts(engine: AsyncEngine) -> None:
    """A Lite turn (no structured write) issues no statement on ``facts``; truncating it flips
    no fact (the generic flip touches the table, and finds nothing)."""
    story = (await world(engine)).story_id
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        await play_turn(engine, story, "a Lite turn")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert not [s for s in statements if "facts" in s]


KNOWN = ("Ivo", "Dree", "Holt", "Mabe", "Pell", "Sorrel")


async def _neighbor_order(pg_url: str) -> tuple[list[str], list[str]]:
    """On a fresh database: Cal comes to know six people in one operation, every fact valid
    from story-time 0. Answers ``canon_links``'s and ``canon``'s orders, by the known name."""
    with migrated_database(pg_url, f"epistrel_canon_{secrets.token_hex(4)}") as url:
        db = make_engine(auth_settings(url))
        try:
            w = await world(db)
            known = {await _entity(db, w.story_id, name): name for name in KNOWN}

            async def write(conn: Any, op: Any) -> None:
                for person in known:
                    proposition = Proposition(
                        subject_id=w.people["Cal"],
                        predicate_id=KNOWS,
                        object_id=person,
                        valid_time=Interval.of(0, None),
                    )
                    await canon.commit_fact(
                        conn,
                        w.story_id,
                        proposition,
                        operation=op,
                        provenance=RecordProvenance.RENDER,
                    )

            await run(db, w.story_id, write)
            async with db.connect() as conn:
                links = await canon.canon_links(conn, w.story_id, subject=w.people["Cal"])
                found = await canon.canon(conn, w.story_id, subject=w.people["Cal"])
            return (
                [known[str(link.object_id)] for link in links],
                [known[str(f.proposition.object_id)] for f in found],
            )
        finally:
            await db.dispose()


@pytest.mark.usefixtures("frozen_ids")
async def test_canon_ties_order_the_same_on_two_fresh_databases(pg_url: str) -> None:
    """#477 (NFR-DETERM-1): facts tied on ``valid_from`` are ordered by id, the tie-break of
    ``canon_links`` (a neighborhood's ranking, #470) and ``canon``. Written in one millisecond
    (their timestamp frozen), they come back in the order they were written on each of two
    fresh databases, because ``new_ulid`` is monotonic. With random ids, the order changed from
    one database to the next."""
    first = await _neighbor_order(pg_url)
    second = await _neighbor_order(pg_url)
    assert first == second == (list(KNOWN), list(KNOWN))
