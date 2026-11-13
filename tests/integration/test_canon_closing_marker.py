"""#471 (ADR-189): canon reads find a version's closers by primary key through ``closed_by``.

- ``test_canon_context_plan_has_no_supersedes_probe``: on a few hundred facts about one
  subject, the plan of ``KeyedLookup.canon_context``'s statement has no scan of
  ``ix_facts_supersedes`` (the index is gone) and no ``supersedes`` filter.
- ``test_rebuild_closers_sets_an_imported_story_s_markers``: rows written without
  ``closed_by`` (a bundle import's, whose lines never carry it) get it rebuilt from
  ``supersedes``; rows that have it are left alone.

The equivalence of the marker with the old anti-join is BITEMP's (``tests/property/
test_bitemp.py``, ``tests/closing_marker_support.py``).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.enums import RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.tables.facts import facts
from tests.canon_support import at, commit, run, supersede, world

pytestmark = pytest.mark.integration


FACTS_ON_ONE_SUBJECT = 300


async def test_canon_context_plan_has_no_supersedes_probe(engine: AsyncEngine) -> None:
    """A subject with a few hundred versions, most of them closed: ``canon_context``'s statement
    reads its closers by primary key, never a ``supersedes`` index loop."""
    w = await world(engine)
    story = w.story_id
    bobby = w.people["Bobby"]
    kitchen, garden = w.places["kitchen"], w.places["garden"]

    async def many(conn: AsyncConnection, op: Operation) -> None:
        for n in range(FACTS_ON_ONE_SUBJECT // 2):
            first = await canon.commit_fact(
                conn,
                story,
                at(bobby, kitchen, n * 10, n * 10 + 10),
                provenance=RecordProvenance.RENDER,
                operation=op,
            )
            await canon.supersede(
                conn, story, first.id, at(bobby, garden, n * 10, n * 10 + 10), operation=op
            )

    await run(engine, story, many)
    async with engine.connect() as conn:
        written = await conn.scalar(
            select(func.count()).select_from(facts).where(facts.c.story_id == story)
        )
    assert written is not None
    assert written >= FACTS_ON_ONE_SUBJECT
    captured: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, params: Any, *_: Any) -> None:
        if "FROM facts AS f JOIN propositions" in statement:
            captured.append((statement, params))

    async with engine.connect() as conn:
        sync = engine.sync_engine
        event.listen(sync, "before_cursor_execute", capture)
        try:
            found = await KeyedLookup(conn, StructuredReadSet()).canon_context(
                story, subjects=[bobby], story_time=15
            )
        finally:
            event.remove(sync, "before_cursor_execute", capture)
        assert [f.proposition.object_id for f in found] == [garden]
        ((statement, params),) = captured
        raw = await conn.exec_driver_sql(f"EXPLAIN (ANALYZE, VERBOSE) {statement}", params)
        plan = "\n".join(row[0] for row in raw)
    assert "ix_facts_supersedes" not in plan, plan
    assert "supersedes @>" not in plan, plan
    assert "closed_by" in plan, plan


async def test_rebuild_closers_sets_an_imported_story_s_markers(engine: AsyncEngine) -> None:
    """A closed version and its closer copied in without ``closed_by`` (as a bundle import
    writes them) read open until ``rebuild_closers`` sets the marker from ``supersedes``; it
    leaves the rows that have one alone."""
    w = await world(engine)
    story = w.story_id
    first = await commit(engine, story, at(w.people["Bobby"], w.places["kitchen"], 0, None))
    closer = await supersede(
        engine, story, first.id, at(w.people["Bobby"], w.places["garden"], 0, None)
    )
    copied, copied_closer = new_ulid(), new_ulid()
    names = [c.name for c in facts.c if c.name not in ("id", "supersedes", "closed_by")]
    columns = ", ".join(f'"{n}"' for n in names)
    async with engine.begin() as conn:
        for new_id, old_id, supersedes in (
            (copied, first.id, []),
            (copied_closer, closer.id, [copied]),
        ):
            await conn.execute(
                text(
                    f"INSERT INTO facts (id, supersedes, {columns}) "  # noqa: S608 - metadata names
                    f"SELECT :n, CAST(:sup AS char(26)[]), {columns} FROM facts WHERE id = :o"
                ),
                {"n": new_id, "o": old_id, "sup": supersedes},
            )
    async with engine.connect() as conn:
        assert copied in {f.id for f in await canon.canon(conn, story)}
    async with engine.begin() as conn:
        assert await canon.rebuild_closers(conn, story) == 1
    async with engine.connect() as conn:
        current = {f.id for f in await canon.canon(conn, story)}
        stored = {
            str(r.id): [str(c) for c in r.closed_by]
            for r in await conn.execute(
                select(facts.c.id, facts.c.closed_by).where(facts.c.story_id == story)
            )
        }
    assert copied not in current
    assert first.id not in current
    assert stored[copied] == [copied_closer]
    assert stored[first.id] == [closer.id]
