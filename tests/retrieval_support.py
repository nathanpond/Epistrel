"""Helpers for the retrieval tests (#141), over the bulk memory writer #153's scale fixture
extends (``epistrel.testing.fixtures.scale``: ``memory_row``, ``insert_memories``).

The tests run on a database of their own (``embedding_support.embedding_db``): the vector arm
needs a pinned dimension, and pinning retypes ``memories.embedding`` (a schema change the shared
session database must never see). ``pin(db)`` pins it at ``DIMENSION`` under ``VERSION``.

``build_story`` makes the story the occasion tests ask about: #133's world (Cal the player,
Bobby, Ann; Bobby and Ann siblings in canon, Cal and Bobby in the kitchen) plus Dan and
Quillon Vexby (named people who are no characters), on the ``Structured (M4)`` profile (its
``episodic`` module on), a few real turns, and then ``fillers`` episodic memories written
straight into ``memories`` at narrative turns spread over ``turns`` (a bulk insert through
``core.membership.insert_derived``, referencing one real turn's operation: building thousands
of turns through the pipeline would take minutes). The head is moved to ``turns`` so the
story's clock reads "now" there (M4's 10 story-minutes a turn: 5,000 turns are about 35 days,
so "a month ago" falls inside the story). No operation can commit after that (the chain
would not hold): every canon write comes first. ``memory(...)`` adds one memory with chosen
occasion, participants, importance, tier and vector.

Rows written here are not indexed for erasure and enqueue no job: the database is dropped with
the module.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import embeddings
from epistrel.core.enums import MemoryKind, RetrievalTier
from epistrel.core.membership import insert_derived
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.tables import operations, stories
from epistrel.core.tables.memories import memories
from epistrel.testing.fixtures import scale
from tests import canon_support as c
from tests.memory_support import use_structured

DIMENSION = 4
VERSION = "test:retrieval:4"
TURNS = 5_000
"""The long story's length (S-THOSEGUYS's 5,000-turn fixture)."""
MONTH_AGO = TURNS - 30 * 144
"""The turn 30 days before the head's next turn (144 turns a day)."""

FILLER_TYPES = ("dinner", "party", "walk", "work", None)
FILLER_LINES = (
    "{a} says: The weather turned again.",
    "{a} does: stacks the plates by the sink.",
    "{a} says: We should fix the fence before winter.",
    "The wind rattles the shutters.",
    "{a} does: hums an old song.",
)


@dataclass
class Story:
    """A built story: its id, the entity of each person, the operation the bulk rows cite and
    the head turn."""

    story_id: str
    people: dict[str, str]
    places: dict[str, str]
    operation_id: str
    head: int
    ids: dict[str, str] = field(default_factory=dict)
    """Named memories ``memory`` wrote."""


async def pin(db: AsyncEngine) -> None:
    """Pin the scratch deployment at ``VERSION`` / ``DIMENSION`` (retypes the column and builds
    the HNSW index)."""
    async with db.begin() as conn:
        await embeddings.pin_first(conn, version=VERSION, dimension=DIMENSION)


async def build_story(
    db: AsyncEngine,
    *,
    fillers: int = 0,
    turns: int = TURNS,
    seed: int = 141,
    before_head: Callable[[AsyncEngine, Story], Awaitable[None]] | None = None,
) -> Story:
    """The story (see the module docstring), with ``fillers`` filler memories. ``before_head``
    runs while the story's turns still form a chain (before its head moves to ``turns``): a
    test's own operations go there."""
    w = await c.world(db)
    story = w.story_id
    await use_structured(db, story)
    people = dict(w.people)
    async with db.begin() as conn:
        people["Dan"] = await store.add_entity(conn, story, "person", "Dan")
        people["Quillon"] = await store.add_entity(conn, story, "person", "Quillon Vexby")
    sibling = Proposition(
        subject_id=people["Bobby"],
        predicate_id=CORE_BY_NAME["sibling_of"].id,
        object_id=people["Ann"],
        valid_time=Interval.of(None, None),
    )
    await c.commit(db, story, sibling)
    for who in ("Cal", "Bobby"):  # in the kitchen: "you" is Bobby, the one other present
        await c.commit(db, story, c.at(people[who], w.places["kitchen"], 0, None))
    op, _ = await c.run(db, story, _nothing, said="Hello.")
    built = Story(story, people, dict(w.places), op.operation_id, turns)
    if fillers:
        rnd = random.Random(seed)  # noqa: S311 - a reproducible fixture
        names = ("Bobby", "Ann", "Cal", "Dan")
        rows = []
        for n in range(fillers):
            actor = rnd.choice(names)
            other = rnd.choice([x for x in names if x != actor])
            turn = 1 + (n * turns) // fillers
            rows.append(
                _row(
                    built,
                    rnd.choice(FILLER_LINES).format(a=actor),
                    turn=turn,
                    beat=1,
                    event_type=rnd.choice(FILLER_TYPES),
                    participants=(people[actor], people[other]),
                    importance=round(rnd.random(), 3),
                )
            )
        await scale.insert_memories(db, rows)
    if before_head is not None:
        await before_head(db, built)
    async with db.begin() as conn:
        await conn.execute(
            update(stories).where(stories.c.id == story).values(next_turn_index=turns + 1)
        )
    return built


async def _nothing(conn: Any, op: Any) -> None:
    del conn, op


def _row(
    story: Story,
    line: str,
    *,
    turn: int | None,
    beat: int | None = 1,
    kind: MemoryKind = MemoryKind.EPISODIC,
    event_type: str | None = None,
    participants: Sequence[str] = (),
    importance: float | None = None,
    character: str | None = None,
) -> dict[str, Any]:
    return scale.memory_row(
        story_id=story.story_id,
        operation_id=story.operation_id,
        line=line,
        turn=turn,
        beat=beat,
        kind=kind,
        event_type=event_type,
        participants=participants,
        importance=importance,
        character=character,
    )


async def memory(
    db: AsyncEngine,
    story: Story,
    name: str,
    line: str,
    *,
    turn: int | None,
    beat: int | None = 1,
    kind: MemoryKind = MemoryKind.EPISODIC,
    event_type: str | None = None,
    participants: Sequence[str] = (),
    importance: float | None = None,
    tier: RetrievalTier = RetrievalTier.HOT,
    vector: Sequence[float] | None = None,
    memory_id: str | None = None,
) -> str:
    """One memory (``name`` keys it in ``story.ids``): participants are person names;
    ``memory_id`` fixes its id (a fresh ULID otherwise)."""
    row = _row(
        story,
        line,
        turn=turn,
        beat=beat,
        kind=kind,
        event_type=event_type,
        participants=[story.people[p] for p in participants],
        importance=importance,
    )
    if memory_id is not None:
        row["id"] = memory_id
    async with db.begin() as conn:
        await insert_derived(conn, memories, [row])
        values: dict[str, Any] = {}
        if vector is not None:
            values.update(embedding=list(vector), embedding_model_version=VERSION)
        if tier is not RetrievalTier.HOT:
            values["retrieval_tier"] = tier.value
        if values:
            await conn.execute(update(memories).where(memories.c.id == row["id"]).values(values))
    story.ids[name] = str(row["id"])
    return str(row["id"])


async def set_vectors(db: AsyncEngine, story_id: str, vector: Sequence[float]) -> None:
    """Give every hot memory of the story without a vector ``vector`` (in the pinned version),
    and the story the pinned version (retired memories have none, FR-MEM-8)."""
    async with db.begin() as conn:
        await conn.execute(
            update(memories)
            .where(
                memories.c.story_id == story_id,
                memories.c.embedding.is_(None),
                memories.c.retrieval_tier == RetrievalTier.HOT.value,
            )
            .values(embedding=list(vector), embedding_model_version=VERSION)
        )
        await conn.execute(
            update(stories).where(stories.c.id == story_id).values(embedding_model_version=VERSION)
        )


async def access_and_tiers(db: AsyncEngine, ids: Sequence[str]) -> dict[str, tuple[Any, str]]:
    async with db.connect() as conn:
        found = await conn.execute(
            select(memories.c.id, memories.c.last_accessed_at, memories.c.retrieval_tier).where(
                memories.c.id.in_(list(ids))
            )
        )
        return {str(r.id): (r.last_accessed_at, str(r.retrieval_tier)) for r in found}


async def operation_count(db: AsyncEngine, story_id: str) -> int:
    async with db.connect() as conn:
        return len(
            list(
                await conn.scalars(
                    select(operations.c.operation_id).where(operations.c.story_id == story_id)
                )
            )
        )


@dataclass
class Seen:
    """The statements a block ran: their SQL and parameters, and the rows each answered."""

    statements: list[tuple[str, Any]] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    """The rows each statement answered (its cursor's ``rowcount``), in ``statements``' order."""

    def __len__(self) -> int:
        return len(self.statements)

    def _reads(self, table: str) -> list[int]:
        needle = (f"from {table} ", f"from {table}\n", f'from "{table}"', f"join {table} ")
        return [
            i
            for i, (sql, _) in enumerate(self.statements)
            if sql.lstrip().lower().startswith(("select", "with"))
            and any(n in sql.lower() for n in needle)
        ]

    def reading(self, table: str) -> list[tuple[str, Any]]:
        """The SELECTs that read ``table`` (``FROM``/``JOIN``)."""
        return [self.statements[i] for i in self._reads(table)]

    def rows_reading(self, table: str) -> int:
        """The rows the SELECTs that read ``table`` answered, together (#451's cost tests)."""
        return sum(self.rows[i] for i in self._reads(table) if i < len(self.rows))


@contextmanager
def statements(db: AsyncEngine) -> Iterator[Seen]:
    """Every statement ``db`` runs while the block runs."""
    seen = Seen()

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        del conn, cursor, args
        seen.statements.append((statement, parameters))

    def answered(conn: Any, cursor: Any, *args: Any) -> None:
        del conn, args
        # A statement that failed has no row count: pad it with 0 to keep the order aligned.
        seen.rows.extend([0] * (len(seen.statements) - 1 - len(seen.rows)))
        seen.rows.append(max(int(getattr(cursor, "rowcount", 0) or 0), 0))

    event.listen(db.sync_engine, "before_cursor_execute", record)
    event.listen(db.sync_engine, "after_cursor_execute", answered)
    try:
        yield seen
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", record)
        event.remove(db.sync_engine, "after_cursor_execute", answered)


async def plan_of(db: AsyncEngine, sql: str, params: Any) -> str:
    """``EXPLAIN`` of ``sql`` with ``params``, sequential scans and sorts priced out: a plan that
    still scans ``memories`` sequentially has no index that serves the query, and a nearest-
    neighbour query then reads the HNSW index in distance order. (On a small table the planner
    may otherwise prefer a b-tree and a sort; the pricing shows which index can serve.)"""
    async with db.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        await conn.execute(text("SET enable_sort = off"))
        await conn.execute(text("SET hnsw.iterative_scan = relaxed_order"))
        raw = await conn.get_raw_connection()
        cursor = raw.driver_connection.cursor()  # type: ignore[union-attr]
        await cursor.execute("EXPLAIN " + sql, params)
        rows = await cursor.fetchall()
        await conn.rollback()
    return "\n".join(str(r[0]) for r in rows)


__all__ = [
    "DIMENSION",
    "MONTH_AGO",
    "TURNS",
    "VERSION",
    "Seen",
    "Story",
    "access_and_tiers",
    "build_story",
    "memory",
    "operation_count",
    "pin",
    "plan_of",
    "set_vectors",
    "statements",
]
