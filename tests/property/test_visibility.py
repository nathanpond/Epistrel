"""VISIBILITY: scope soundness (docs/04 §6; FR-EPI-5; #159).

No retrieval returns a memory or a fact whose visibility excludes the requester. Over random
stories (a cast of up to six characters, up to 200 records, each with a random scope) and random
requesters (``character:<id>``, ``omniscient`` and a ``narrator`` reader), the filter every
retrieval arm composes (``core.retrieval.viewpoint``: ``scope_for`` and ``admits``, with the
``partitions`` module on) and canon's (``core.visibility.visible_to``) return exactly the
records an independent statement of the rule admits: never one it excludes, and every one it
admits. Complement: an ``omniscient`` read returns everything. It runs in memory (300 examples);
one Postgres smoke runs the same requesters through the SQL fragment of the lexical arm
(``scope_sql``) and the Core clause of the occasion and by-id reads (``visible_clause``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import MemoryKind
from epistrel.core.events import Operation
from epistrel.core.membership import insert_derived
from epistrel.core.retrieval.lexical import LexicalFilters, TsvectorLexicalSearch
from epistrel.core.retrieval.viewpoint import Viewpoint, admits, scope_for
from epistrel.core.tables.memories import memories
from epistrel.core.visibility import Visibility, visible_clause, visible_to
from epistrel.testing.fixtures import scale
from tests import canon_support as c
from tests.memory_support import use_structured

CAST = tuple(f"01JV00000000000000000000C{i}" for i in range(6))
NARRATOR = "narrator"
OMNISCIENT = "omniscient"


@dataclass(frozen=True)
class Item:
    """A record: its id and its visibility document."""

    id: str
    visibility: dict[str, Any]


def may_read(requester: str, visibility: dict[str, Any]) -> bool:
    """The rule, stated on its own (docs/03 FR-EPI-5, docs/02 §4.4): ``omniscient`` reads
    everything; ``objective`` and ``public`` reach every requester; ``engine`` only
    ``omniscient``; ``characters`` only the characters it lists."""
    if requester == OMNISCIENT:
        return True
    scope = visibility["scope"]
    if scope in ("objective", "public"):
        return True
    if scope != "characters" or not requester.startswith("character:"):
        return False
    return requester.split(":", 1)[1] in visibility["characters"]


@st.composite
def stories(draw: st.DrawFn) -> tuple[tuple[str, ...], list[Item]]:
    cast = tuple(draw(st.lists(st.sampled_from(CAST), min_size=1, max_size=6, unique=True)))
    scope = st.sampled_from(("objective", "public", "engine", "characters"))
    items = []
    for n in range(draw(st.integers(0, 200))):
        kind = draw(scope)
        document: dict[str, Any] = {"scope": kind}
        if kind == "characters":
            document["characters"] = draw(
                st.lists(st.sampled_from(cast), min_size=1, max_size=len(cast), unique=True)
            )
        items.append(Item(f"r{n}", document))
    return cast, items


def requesters(cast: tuple[str, ...]) -> list[str]:
    return [OMNISCIENT, NARRATOR, *(f"character:{x}" for x in cast)]


@pytest.mark.part("M5", "FR-EPI-5")
@settings(max_examples=300, deadline=None)
@given(stories())
def test_visibility(story: tuple[tuple[str, ...], list[Item]]) -> None:
    """For every requester, the retrieval filter and canon's return exactly what the rule admits
    (sound: nothing it excludes; complete: everything it admits); ``omniscient`` returns every
    record (the complement: the filter is not vacuous only because nothing is excluded)."""
    cast, items = story
    for requester in requesters(cast):
        scope = scope_for(Viewpoint.parse(requester), partitions=True)
        retrieved = {i.id for i in items if admits(scope, i.visibility)}
        canon = {i.id for i in items if visible_to(Visibility(**i.visibility), requester)}
        wanted = {i.id for i in items if may_read(requester, i.visibility)}
        assert retrieved == wanted == canon, requester
        if requester == OMNISCIENT:
            assert retrieved == {i.id for i in items}
    # A belief is visible to its holder alone, under every requester.
    for holder in cast:
        private = Visibility(scope="characters", characters=(holder,)).document()
        assert [r for r in requesters(cast) if may_read(r, private)] == [
            OMNISCIENT,
            f"character:{holder}",
        ]


@pytest.mark.integration
@pytest.mark.part("M5", "FR-EPI-5")
async def test_visibility__postgres(engine: AsyncEngine) -> None:
    """The smoke: memories of every scope in a real story, read through the lexical arm's SQL
    (``scope_sql``) and the Core clause of the occasion and by-id reads (``visible_clause``) as
    each requester, return exactly what the rule admits."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    cast = tuple(w.characters.values())
    documents: list[dict[str, Any]] = [
        {"scope": "objective"},
        {"scope": "public"},
        {"scope": "engine"},
        *({"scope": "characters", "characters": [x]} for x in cast),
        {"scope": "characters", "characters": sorted(cast[:2])},
    ]
    stored: dict[str, dict[str, Any]] = {}

    async def remember(conn: AsyncConnection, op: Operation) -> None:
        for n, document in enumerate(documents):
            row = scale.memory_row(
                story_id=story,
                operation_id=op.operation_id,
                line=f"The lantern number {n} glows.",
                turn=op.narrative_turn,
                kind=MemoryKind.EPISODIC,
            )
            row["visibility"] = document
            await insert_derived(conn, memories, [row])
            stored[str(row["id"])] = document

    await c.run(engine, story, remember)
    lexical = TsvectorLexicalSearch()
    async with engine.connect() as conn:
        for requester in [OMNISCIENT, NARRATOR, *(f"character:{x}" for x in cast)]:
            scope = scope_for(Viewpoint.parse(requester), partitions=True)
            wanted = {i for i, d in stored.items() if may_read(requester, d)}
            hits = await lexical.search(conn, story, "lantern", 50, LexicalFilters(scope=scope))
            assert {i for i, _ in hits} == wanted, requester
            where = [memories.c.story_id == story]
            clause = None if scope is None else visible_clause(memories, scope)
            if clause is not None:
                where.append(clause)
            core = {str(i) for i in await conn.scalars(select(memories.c.id).where(*where))}
            assert core == wanted, requester
