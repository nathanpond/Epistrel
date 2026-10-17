"""U-02: coordinate logic (FR-BITEMP-1,2,3,4,5; docs/04 §3), against PostgreSQL (#133).

Narrative position, valid-time and transaction-time are queried independently; supersede closes
transaction-time and never deletes; a retcon is true across its span while the record keeps
what it said before; truncation flips membership only (valid-time and transaction-time stay)
and is reversible; reads filter by visibility.

Complement: no application path deletes a fact (a scan of ``src/``), and the table's trigger
refuses a DELETE, an UPDATE of anything but the closing and membership columns, a second
closure and a TRUNCATE outside erasure and story deletion.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.canon.errors import (
    FactInactive,
    FactNotFound,
    FactSuperseded,
    InvalidVisibility,
    KeyMismatch,
    UnknownPredicate,
)
from epistrel.core.enums import OperationKind
from epistrel.core.errors import ViewpointUnresolved
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import Interval, Proposition
from tests.canon_support import (
    DEAD,
    KNOWS,
    LOCATED_AT,
    as_recorded,
    at,
    at_time,
    commit,
    committed_at,
    fact_rows,
    supersede,
    world,
)
from tests.event_support import play_turn, record, sqlstate
from tests.projection_support import truncate, untruncate

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[1] / "src"

APPLICATION_DELETE = re.compile(r"delete\(\s*facts\b|DELETE\s+FROM\s+\"?facts\b", re.IGNORECASE)
DELETE_PATHS = frozenset({"epistrel/core/erasure/erase.py"})
"""The one module that deletes facts: erasure, under ``epistrel.erasure`` (story deletion
empties every story table through ``STORY_TABLES``, never by naming ``facts``)."""


def _ids(found: list[canon.Fact]) -> list[str]:
    return [f.id for f in found]


def _places(found: list[canon.Fact]) -> list[tuple[str | None, int | None, int | None]]:
    return [(f.proposition.object_id, f.valid_from, f.valid_to) for f in found]


@pytest.mark.part("M4", "FR-BITEMP-1,2,3,4,5")
async def test_u_02(engine: AsyncEngine) -> None:
    w = await world(engine)
    story = w.story_id
    bobby, kitchen, garden, school = (
        w.people["Bobby"],
        w.places["kitchen"],
        w.places["garden"],
        w.places["school"],
    )

    # FR-BITEMP-1: valid-time and transaction-time on every version; position from the
    # operation, never from seq (background work between turns moves seq, not position).
    await play_turn(engine, story, "turn 1")
    await record(engine, story, OperationKind.CONTROL, "pinned_memories_set", {"items": []})
    first = await commit(engine, story, at(bobby, kitchen, 0, 1000))
    rows = await fact_rows(engine, story)
    row = rows[first.id]
    assert (row.valid_from, row.valid_to, row.tx_to) == (0, 1000, None)
    assert row.tx_from == await committed_at(engine, row.operation_id)
    assert (row.narrative_turn, row.turn_variant) == (2, 1)
    seqs = await _event_seqs(engine, row.operation_id)
    assert seqs
    assert all(s != row.narrative_turn for s in seqs)
    assert (str(row.provenance), str(row.authority)) == ("render", "engine")
    committed = await _events(engine, row.operation_id, "fact_committed")
    assert [e.payload["fact_id"] for e in committed] == [first.id]
    assert [str(e.category) for e in committed] == ["derived"]
    assert row.source_event_ids == [committed[0].id]
    assert row.committed_at_position == {"turn": 2, "variant": 1, "beat": None}

    # FR-BITEMP-2: supersede closes the old version in transaction-time and keeps it.
    second = await supersede(engine, story, first.id, at(bobby, garden, 0, 1000))
    rows = await fact_rows(engine, story)
    old, new = rows[first.id], rows[second.id]
    assert old.tx_to == new.tx_from
    assert (old.superseded_by, new.supersedes, new.lineage_id) == (
        second.id,
        [first.id],
        first.id,
    )
    assert str(old.membership) == "active"
    assert (old.valid_from, old.valid_to) == (0, 1000)
    assert second.superseded_ids == (first.id,)
    assert [e.payload["superseded_id"] for e in await _events(engine, new.operation_id)] == [
        first.id
    ]

    # FR-BITEMP-3: the two questions are answered independently.
    assert _places(await at_time(engine, story, 500)) == [(garden, 0, 1000)]
    assert _places(await as_recorded(engine, story, old.tx_from)) == [(kitchen, 0, 1000)]
    assert _places(await as_recorded(engine, story, new.tx_from)) == [(garden, 0, 1000)]
    assert await at_time(engine, story, 2000) == []
    with pytest.raises(FactSuperseded):
        await supersede(engine, story, first.id, at(bobby, school, 0, 1000))

    # FR-BITEMP-4: a retcon is true across its whole span; the record before it says otherwise,
    # and the retcon's record-time, author and authority are kept.
    retcon = await supersede(
        engine, story, second.id, at(bobby, school, -5000, 5000), authority="author"
    )
    rows = await fact_rows(engine, story)
    for t in (-4000, 0, 500, 4999):
        assert _places(await at_time(engine, story, t)) == [(school, -5000, 5000)]
    assert await at_time(engine, story, 5000) == []
    assert _places(await as_recorded(engine, story, rows[second.id].tx_from)) == [(garden, 0, 1000)]
    assert str(rows[retcon.id].authority) == "author"
    assert rows[retcon.id].tx_from == await committed_at(engine, rows[retcon.id].operation_id)
    assert await _principal(engine, rows[retcon.id].operation_id) == "p-test"
    assert len(rows) == 3  # nothing deleted

    # A functional key never overlaps: a new place over part of the span trims the current
    # version into what lies before and after it.
    inside = await commit(engine, story, at(bobby, kitchen, 100, 200))
    assert _places(await at_time(engine, story, 150)) == [(kitchen, 100, 200)]
    assert _places(await at_time(engine, story, 50)) == [(school, -5000, 100)]
    assert _places(await at_time(engine, story, 300)) == [(school, 200, 5000)]
    assert len(inside.remainder_ids) == 2
    assert inside.superseded_ids == (retcon.id,)
    rows = await fact_rows(engine, story)
    before, after = (rows[r] for r in inside.remainder_ids)
    assert before.lineage_id == first.id
    assert after.lineage_id == after.id
    assert (rows[retcon.id].valid_from, rows[retcon.id].valid_to) == (-5000, 5000)

    # FR-BITEMP-5: truncation flips membership only; both time coordinates stay; untruncation
    # brings the version back, and the version it closed is current again in between.
    head = await _head(engine, story)
    rows_before = await fact_rows(engine, story)
    await truncate(engine, story, head - 1)
    rows_after = await fact_rows(engine, story)
    for fact_id, before_row in rows_before.items():
        assert _coordinates(rows_after[fact_id]) == _coordinates(before_row)
    discarded = {k for k, r in rows_after.items() if str(r.membership) == "discarded"}
    assert discarded == {inside.id, *inside.remainder_ids}
    assert _places(await at_time(engine, story, 150)) == [(school, -5000, 5000)]
    await untruncate(engine, story)
    assert _places(await at_time(engine, story, 150)) == [(kitchen, 100, 200)]

    # The flashback: a fact valid decades earlier, written at a later turn, leaves canon with
    # its turn and comes back with it; membership decides, not valid-time.
    arm = await commit(engine, story, at(bobby, garden, -10_000_000, -9_999_000))
    assert arm.id in _ids(await at_time(engine, story, -9_999_500))
    head = await _head(engine, story)
    await truncate(engine, story, head - 1)
    assert arm.id not in _ids(await at_time(engine, story, -9_999_500))
    assert (await fact_rows(engine, story))[arm.id].valid_from == -10_000_000
    await untruncate(engine, story)
    assert arm.id in _ids(await at_time(engine, story, -9_999_500))

    # through_position: only versions written at or before a narrative position; a
    # position-less (control) version always qualifies.
    admin = await commit(
        engine, story, at(w.people["Ann"], kitchen, 0, None), kind=OperationKind.CONTROL
    )
    assert (await fact_rows(engine, story))[admin.id].narrative_turn is None
    through_two = _ids(await at_time(engine, story, 150, through_position=(3, 0)))
    assert admin.id in through_two
    assert inside.id not in through_two

    # A permanent predicate's fact is canon at every story-time.
    dead = await commit(
        engine,
        story,
        Proposition(subject_id=w.people["Cal"], predicate_id=DEAD, valid_time=Interval.of(0, 1)),
    )
    assert dead.id in _ids(await at_time(engine, story, 10**9))

    # A negation over an open affirmed version supersedes it (polarity conflict).
    knows = Proposition(
        subject_id=w.people["Ann"],
        predicate_id=KNOWS,
        object_id=bobby,
        valid_time=Interval.of(0, None),
    )
    knew = await commit(engine, story, knows)
    # An identical multi proposition already current over the span is a no-op.
    again = await commit(engine, story, knows)
    assert again.id == knew.id
    denied = await commit(
        engine, story, knows.negation().model_copy(update={"valid_time": Interval.of(50, None)})
    )
    assert denied.superseded_ids == (knew.id,)
    ann_bobby = await at_time(engine, story, 10, subject=w.people["Ann"], predicate=KNOWS)
    assert [f.proposition.polarity.value for f in ann_bobby] == ["affirmed"]
    assert [
        f.proposition.polarity.value
        for f in await at_time(engine, story, 60, subject=w.people["Ann"], predicate=KNOWS)
    ] == ["negated"]

    await _visibility(engine, w)
    await _refusals(engine, w, first.id)
    await _hooks(engine, w)
    await _append_only(engine, story, first.id)


async def _visibility(engine: AsyncEngine, w: Any) -> None:
    """``objective``/``public`` reach every viewpoint; a ``characters`` scope only those it
    names and ``omniscient``; ``engine`` only ``omniscient``; readers see the shared scopes."""
    story = w.story_id
    ann = w.people["Ann"]
    school = w.places["school"]
    scopes = {
        "objective": canon.Visibility(),
        "public": canon.Visibility(scope="public"),
        "engine": canon.Visibility(scope="engine"),
        "secret": canon.Visibility(scope="characters", characters=(w.characters["Bobby"],)),
    }
    written = {}
    for i, (name, visibility) in enumerate(scopes.items()):
        written[name] = (
            await commit(
                engine,
                story,
                at(ann, school, 10_000 + 10 * i, 10_005 + 10 * i),
                visibility=visibility,
            )
        ).id

    async def seen(viewpoint: str) -> set[str]:
        found = set()
        for i in range(len(scopes)):
            found |= set(_ids(await at_time(engine, story, 10_001 + 10 * i, viewpoint=viewpoint)))
        return {name for name, fact_id in written.items() if fact_id in found}

    assert await seen("omniscient") == set(scopes)
    assert await seen(w.viewpoint("Bobby")) == {"objective", "public", "secret"}
    assert await seen(w.viewpoint("Ann")) == {"objective", "public"}
    assert await seen("narrator") == {"objective", "public"}
    async with engine.connect() as conn:
        hidden_ids = {f.id for f in await canon.hidden_from(conn, story, w.viewpoint("Ann"))}
        assert hidden_ids >= {written["engine"], written["secret"]}
        assert written["objective"] not in hidden_ids
        with pytest.raises(ViewpointUnresolved):
            await canon.canon_at(conn, story, story_time=0, viewpoint=f"character:{new_ulid()}")
    with pytest.raises(ValueError, match="at least one character"):
        canon.Visibility(scope="characters", characters=())
    with pytest.raises(InvalidVisibility):
        await commit(
            engine,
            story,
            at(ann, school, 20_000, 20_001),
            visibility=canon.Visibility(scope="characters", characters=(new_ulid(),)),
        )


async def _refusals(engine: AsyncEngine, w: Any, closed: str) -> None:
    story = w.story_id
    bobby, ann = w.people["Bobby"], w.people["Ann"]
    kitchen = w.places["kitchen"]
    with pytest.raises(FactNotFound):
        await supersede(engine, story, new_ulid(), at(bobby, kitchen, 0, 1))
    with pytest.raises(FactSuperseded):
        await supersede(engine, story, closed, at(bobby, kitchen, 0, 1))
    with pytest.raises(UnknownPredicate):
        await commit(engine, story, at(bobby, kitchen, 0, 1, predicate=new_ulid()))
    current = await at_time(engine, story, 150, subject=bobby, predicate=LOCATED_AT)
    with pytest.raises(KeyMismatch):
        await supersede(engine, story, current[0].id, at(ann, kitchen, 0, 1))
    # A version whose operation left the timeline cannot be superseded.
    victim = await commit(engine, story, at(ann, kitchen, 30_000, 30_001))
    head = await _head(engine, story)
    await truncate(engine, story, head - 1)
    with pytest.raises(FactInactive):
        await supersede(engine, story, victim.id, at(ann, kitchen, 30_000, 30_002))
    await untruncate(engine, story)


async def _hooks(engine: AsyncEngine, w: Any) -> None:
    """FACT_HOOKS run in the write's transaction; a hook's exception aborts the commit."""
    story = w.story_id
    seen: list[tuple[str, int]] = []

    async def hook(conn: AsyncConnection, change: canon.FactChange) -> None:
        count = await conn.scalar(
            text("SELECT count(*) FROM facts WHERE id = :i"), {"i": change.fact.id}
        )
        seen.append((change.fact.id, int(count or 0)))

    async def refuse(conn: AsyncConnection, change: canon.FactChange) -> None:
        raise RuntimeError("refused")

    canon.register_fact_hook(hook)
    try:
        fact = await commit(engine, story, at(w.people["Cal"], w.places["garden"], 0, 5))
    finally:
        canon.remove_fact_hook(hook)
    assert seen == [(fact.id, 1)]
    before = await fact_rows(engine, story)
    canon.register_fact_hook(refuse)
    try:
        with pytest.raises(RuntimeError):
            await commit(engine, story, at(w.people["Cal"], w.places["school"], 40_000, 40_001))
    finally:
        canon.remove_fact_hook(refuse)
    assert await fact_rows(engine, story) == before


async def _append_only(engine: AsyncEngine, story: str, fact_id: str) -> None:
    """The complement: no application DELETE exists, and the trigger refuses one."""
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in sorted(SRC.rglob("*.py"))
        if APPLICATION_DELETE.search(p.read_text(encoding="utf-8"))
    ]
    assert set(offenders) == DELETE_PATHS
    for statement in (
        "DELETE FROM facts WHERE id = :i",
        "UPDATE facts SET valid_from = valid_from - 1 WHERE id = :i",
        "UPDATE facts SET tx_to = tx_to + interval '1 second' WHERE id = :i",
        'UPDATE facts SET visibility = \'{"scope": "public"}\' WHERE id = :i',
        "TRUNCATE facts",
    ):
        with pytest.raises(DBAPIError) as refused:
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"i": fact_id})
        assert sqlstate(refused.value) == "EPI01", statement
    assert fact_id in await fact_rows(engine, story)


def _coordinates(row: Any) -> tuple[Any, ...]:
    return (
        row.valid_from,
        row.valid_to,
        row.valid_from_inner,
        row.valid_to_inner,
        row.tx_from,
        row.tx_to,
        row.narrative_turn,
        row.turn_variant,
        row.superseded_by,
    )


async def _head(engine: AsyncEngine, story: str) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT next_turn_index FROM stories WHERE id = :s"), {"s": story}
        )
    return int(found) - 1


async def _event_seqs(engine: AsyncEngine, operation_id: str) -> list[int]:
    async with engine.connect() as conn:
        return [
            int(s)
            for s in await conn.scalars(
                text("SELECT seq FROM events WHERE operation_id = :o"), {"o": operation_id}
            )
        ]


async def _events(engine: AsyncEngine, operation_id: str, type_: str | None = None) -> list[Any]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT id, category, payload, type FROM events WHERE operation_id = :o "
                "AND category = 'derived' ORDER BY seq"
            ),
            {"o": operation_id},
        )
        return [r for r in found if type_ is None or r.type == type_]


async def _principal(engine: AsyncEngine, operation_id: str) -> str:
    async with engine.connect() as conn:
        return str(
            await conn.scalar(
                text("SELECT principal FROM operations WHERE operation_id = :o"),
                {"o": operation_id},
            )
        )
