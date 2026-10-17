"""The test-only keyed reader of the structured read set tests (#136) and its scripted turn.

Until #150's structured turn lands, these tests drive a stand-in turn:

- its read phase makes its precondition lookups through ``KeyedLookup``, or calls #140's
  ``verify``, which does;
- it records its semantic context as retrieved rows;
- its model is a scripted reply held on a latch inside its call, the way #47's concurrency
  tests hold Lite turns (``tests.concurrency_support``).

Its prompt is captured, so a test can see what the regenerated attempt was given.

This module is a keyed reader: it reads ``facts`` only through ``KeyedLookup``, which
``tests/gates/test_keyed_readers.py`` checks with ``tools.gates check-keyed-readers``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.concurrency import RevalidationResult, run_with_revalidation
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.propositions.model import TypedLiteral
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.settings_schema import OMNISCIENT
from tests.concurrency_support import TURN_PRINCIPAL

Read = Callable[[AsyncConnection, StructuredReadSet], Awaitable[list[list[str]]]]
"""A turn's own read phase: what it found, per check, as the fact ids its prompt holds."""


@dataclass(frozen=True)
class Lookup:
    """One precondition lookup a turn makes: what it asks, at a story-time or over a window."""

    predicate: str
    subjects: str | tuple[str, ...] | None
    object: str | TypedLiteral | None = None
    story_time: int | None = None
    window: tuple[int | None, int | None] | None = None
    viewpoint: str = OMNISCIENT


@dataclass(frozen=True)
class Context:
    """A row the turn reads as generation context only (semantic retrieval): never keyed."""

    table: str
    id: str
    version: Any


@dataclass
class KeyedModel:
    """A scripted model: every call records its prompt (the lookups' results), waits until the
    test releases it, then stages the reply ``reply(found)`` chooses."""

    lookups: Sequence[Lookup]
    found: list[list[list[str]]] = field(default_factory=list)
    """Per attempt, per lookup: the fact ids it answered (the attempt's prompt)."""
    calls: int = 0
    _gate: asyncio.Event | None = None
    _arrived: asyncio.Event = field(default_factory=asyncio.Event)

    def reply(self, found: list[list[str]]) -> str:
        return "The door opens." if not any(found) else "The door is locked."

    async def __call__(self, op: Operation, inputs: Any) -> None:
        self.calls += 1
        self.found.append(inputs)
        gate = asyncio.Event()
        self._gate = gate
        self._arrived.set()
        await gate.wait()
        op.append("player_input", {"text": "I open the door."}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": self.reply(inputs)})

    async def arrived(self) -> None:
        await self._arrived.wait()

    def release(self) -> None:
        assert self._gate is not None
        self._arrived.clear()
        self._gate.set()


@dataclass
class KeyedTurn:
    """A structured turn held inside its model call: start it, act on the story, release it."""

    op: Operation
    model: KeyedModel
    task: asyncio.Task[RevalidationResult]
    read_set: StructuredReadSet

    async def release(self) -> None:
        """Let the current generation finish; return when the turn ended or generates again."""
        self.model.release()
        waiting = asyncio.ensure_future(self.model.arrived())
        await asyncio.wait({self.task, waiting}, return_when=asyncio.FIRST_COMPLETED)
        waiting.cancel()

    @property
    def done(self) -> bool:
        return self.task.done()


async def start_keyed_turn(
    engine: AsyncEngine,
    story_id: str,
    lookups: Sequence[Lookup],
    *,
    context: Sequence[Context] = (),
    budget: int | None = None,
    model: KeyedModel | None = None,
    read: Read | None = None,
) -> KeyedTurn:
    """Begin a structured turn whose read phase makes ``lookups`` through ``KeyedLookup`` (or
    runs ``read``, e.g. #140's ``verify``, on its connection and read set) and records
    ``context`` as retrieved rows; hold it inside its first model call."""
    model = model or KeyedModel(lookups)
    source = StructuredReadSet()
    op = Operation(engine, story_id=story_id, kind=OperationKind.TURN, principal=TURN_PRINCIPAL)

    async def begin(conn: AsyncConnection) -> list[list[str]]:
        lookup = KeyedLookup(conn, source)
        found = [] if read is None else await read(conn, source)
        for asked in lookups:
            facts = await lookup.canon_at(
                story_id,
                predicate=asked.predicate,
                subjects=asked.subjects,
                object=asked.object,
                story_time=asked.story_time,
                window=asked.window,
                viewpoint=asked.viewpoint,
            )
            found.append([f.id for f in facts])
        for row in context:
            lookup.retrieved(row.table, row.id, row.version)
        return found

    task = asyncio.ensure_future(
        run_with_revalidation(op, begin=begin, generate=model, budget=budget, read_set=source)
    )
    arrived = asyncio.ensure_future(model.arrived())
    await asyncio.wait({task, arrived}, return_when=asyncio.FIRST_COMPLETED)
    arrived.cancel()
    if task.done():
        task.result()  # surface a failure to begin
    return KeyedTurn(op=op, model=model, task=task, read_set=source)


async def stored_read_set(engine: AsyncEngine, operation_id: str) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(
            text("SELECT read_set FROM operations WHERE operation_id = :o"), {"o": operation_id}
        )


async def reply_of(engine: AsyncEngine, operation_id: str) -> str:
    async with engine.connect() as conn:
        return str(
            await conn.scalar(
                text(
                    "SELECT payload->>'text' FROM events WHERE operation_id = :o "
                    "AND type = 'message_rendered'"
                ),
                {"o": operation_id},
            )
        )


__all__ = [
    "Context",
    "KeyedModel",
    "KeyedTurn",
    "Lookup",
    "Read",
    "reply_of",
    "start_keyed_turn",
    "stored_read_set",
]
