"""Helpers for the canon tests (#133): a story with a cast and entities, and fact writes run
inside an operation's commit, the way the structured pipeline makes them."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.core.tables import operations

LOCATED_AT = CORE_BY_NAME["located_at"].id
"""A functional fluent predicate: one place per person at a time."""
DEAD = CORE_BY_NAME["dead"].id
"""A permanent predicate: true at every story-time once canon."""
KNOWS = CORE_BY_NAME["knows"].id
"""A ``multi`` fluent predicate."""

PRINCIPAL = "p-test"

Write = Callable[[AsyncConnection, Operation], Awaitable[Any]]


@dataclass(frozen=True)
class World:
    """A story with Bobby, Ann and Cal (the player), their entities, and three places."""

    story_id: str
    characters: dict[str, str]
    people: dict[str, str]
    places: dict[str, str]

    def viewpoint(self, name: str) -> str:
        return f"character:{self.characters[name]}"


async def world(engine: AsyncEngine) -> World:
    spec = StorySpec(
        name="canon",
        characters=[
            CharacterSpec(name="Cal", definition="The player.", true_age=30, is_player=True),
            CharacterSpec(name="Bobby", definition="A boy.", true_age=12),
            CharacterSpec(name="Ann", definition="His aunt.", true_age=40),
        ],
    )
    created = await create_story_from(engine, spec, principal=PRINCIPAL)
    story = created.story_id
    cast = {c.name: c.id for c in created.characters}
    async with engine.begin() as conn:
        people = {
            name: await store.add_entity(conn, story, "person", name, character_id=cast[name])
            for name in ("Bobby", "Ann", "Cal")
        }
        places = {
            name: await store.add_entity(conn, story, "place", name)
            for name in ("kitchen", "garden", "school")
        }
    return World(story, cast, people, places)


def at(
    subject: str,
    place: str,
    lo: int | None,
    hi: int | None,
    *,
    negated: bool = False,
    predicate: str = LOCATED_AT,
) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=place,
        valid_time=Interval.of(lo, hi),
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
    )


async def run(
    engine: AsyncEngine,
    story_id: str,
    write: Write,
    *,
    kind: OperationKind = OperationKind.TURN,
    said: str = "go on",
    turn: int | None = None,
) -> tuple[Operation, Any]:
    """Run ``write(conn, op)`` as a commit step of a new operation: a turn (with its input and
    reply) by default, else a position-less ``control`` operation. Answers the operation and
    what ``write`` answered."""
    found: dict[str, Any] = {}
    async with Operation(
        engine,
        story_id=story_id,
        kind=kind,
        principal=PRINCIPAL,
        turn=turn,
        bumps_revision=True if kind is not OperationKind.TURN else None,
    ) as op:
        if kind is OperationKind.TURN:
            op.append("player_input", {"text": said}, provenance=Provenance.PLAYER)
            op.append("message_rendered", {"text": f"reply to {said}"})

        async def step(conn: AsyncConnection) -> None:
            found["value"] = await write(conn, op)

        op.add_commit_step(step)
        await op.commit()
    return op, found.get("value")


async def commit(
    engine: AsyncEngine,
    story_id: str,
    proposition: Proposition,
    *,
    kind: OperationKind = OperationKind.TURN,
    turn: int | None = None,
    **options: Any,
) -> canon.Fact:
    options.setdefault("provenance", "render")

    async def write(conn: AsyncConnection, op: Operation) -> canon.Fact:
        return await canon.commit_fact(conn, story_id, proposition, operation=op, **options)

    _, fact = await run(engine, story_id, write, kind=kind, turn=turn)
    assert isinstance(fact, canon.Fact)
    return fact


async def supersede(
    engine: AsyncEngine,
    story_id: str,
    fact_id: str,
    proposition: Proposition,
    *,
    kind: OperationKind = OperationKind.TURN,
    **options: Any,
) -> canon.Fact:
    async def write(conn: AsyncConnection, op: Operation) -> canon.Fact:
        return await canon.supersede(conn, story_id, fact_id, proposition, operation=op, **options)

    _, fact = await run(engine, story_id, write, kind=kind)
    assert isinstance(fact, canon.Fact)
    return fact


async def at_time(engine: AsyncEngine, story_id: str, story_time: int, **kw: Any) -> list[Any]:
    async with engine.connect() as conn:
        return await canon.canon_at(conn, story_id, story_time=story_time, **kw)


async def as_recorded(engine: AsyncEngine, story_id: str, record_time: Any, **kw: Any) -> list[Any]:
    async with engine.connect() as conn:
        return await canon.canon_as_recorded(conn, story_id, record_time=record_time, **kw)


async def fact_rows(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT * FROM facts WHERE story_id = :s ORDER BY tx_from, id"), {"s": story_id}
        )
        return {str(r.id): r for r in found}


async def committed_at(engine: AsyncEngine, operation_id: str) -> Any:
    """The operation's commit time (its ``membership_tx``, the transaction's time)."""
    async with engine.connect() as conn:
        return await conn.scalar(
            select(operations.c.membership_tx).where(operations.c.operation_id == operation_id)
        )


async def auto_predicate(engine: AsyncEngine, story_id: str, relation: str) -> str:
    async with engine.begin() as conn:
        return (await registry.auto_register(conn, story_id, relation)).id


__all__ = [
    "DEAD",
    "KNOWS",
    "LOCATED_AT",
    "PRINCIPAL",
    "World",
    "as_recorded",
    "at",
    "at_time",
    "auto_predicate",
    "commit",
    "committed_at",
    "fact_rows",
    "run",
    "supersede",
    "world",
]
