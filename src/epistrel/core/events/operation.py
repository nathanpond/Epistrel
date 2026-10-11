"""``Operation``: one unit of work whose events commit atomically (FR-STORE-5, NFR-REL-1).

Two phases. ``begin()`` inserts the ``operations`` row (``in_flight``) without taking a lock,
and ``append()`` validates each payload and buffers it, so model calls between them hold no
lock (FR-CONC-2). ``commit()`` runs one transaction that does all of the following:

1. takes ``SELECT … FOR UPDATE`` on the story row, with ``lock_timeout`` 5 s;
2. positions the operation: only ``turn`` operations allocate a turn index (FR-STORE-8,24);
3. assigns dense per-story ``seq`` values (``max(seq)+1`` under the lock), writes the events,
   and maintains every projection for them (``projections.apply_events``, FR-STORE-4), then
   runs any commit steps the caller added;
4. finalises the operation (``committed``, ``membership_tx`` = the commit's ``tx_time``) and
   bumps ``stories.timeline_revision`` for ``turn`` and ``genesis`` operations (#47 generalises).

Any failure rolls the transaction back, so no event row survives, and the operation is then
marked ``failed`` in a separate transaction. Leaving the ``async with`` block without
``commit()`` marks it ``failed`` when an exception escapes and ``abandoned`` otherwise.

``valid_time`` equals ``tx_time`` (both ``transaction_timestamp()``) until the world clock
arrives in M6. A story-less operation (deployment-scoped control) serialises on a transaction
advisory lock instead of a story row.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from sqlalchemy import and_, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import projections
from epistrel.core.enums import (
    Authority,
    EventCategory,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
)
from epistrel.core.errors import InvalidRequest, PrincipalRequired, StoryNotFound
from epistrel.core.events.registry import EventPayload, lookup, validate
from epistrel.core.ids import new_ulid
from epistrel.core.tables import events, operations, stories

logger = logging.getLogger(__name__)

CommitStep = Callable[[AsyncConnection], Awaitable[None]]
"""Extra work run inside the commit transaction, under the story lock, after the events."""

Database = AsyncEngine | AsyncConnection

LOCK_TIMEOUT = "5s"
"""How long ``commit()`` waits for the story row lock before failing."""

DEPLOYMENT_LOCK_KEY = 0x45_5049_5354  # "EPIST": serialises story-less operations' seq
"""``pg_advisory_xact_lock`` key that stands in for the story row lock when ``story_id`` is
``None``."""

REVISION_BUMPING_KINDS = frozenset({OperationKind.TURN, OperationKind.GENESIS})
"""Kinds whose commit increments ``stories.timeline_revision`` in #45 (#47 generalises)."""


@dataclass(frozen=True, slots=True)
class StagedEvent:
    """A validated event waiting for ``commit()``."""

    type: str
    category: EventCategory
    payload: EventPayload
    provenance: Provenance
    authority: Authority
    character_id: str | None


class Operation:
    """Groups the events of one unit of work and commits them all or none.

    ``async with Operation(db, story_id=..., kind=..., principal=...) as op:`` begins it;
    ``op.append(...)`` stages events; ``await op.commit()`` writes them. ``turn=`` positions a
    ``turn`` operation at an existing turn as a new variant (regenerate), or a non-turn
    operation at an existing turn's active variant; non-turn operations never allocate one.
    """

    def __init__(
        self,
        db: Database,
        *,
        story_id: str | None,
        kind: OperationKind,
        principal: str | None,
        turn: int | None = None,
        provenance: Provenance = Provenance.ENGINE,
        authority: Authority = Authority.AUTHORITATIVE,
    ) -> None:
        if principal is None or not principal.strip():
            raise PrincipalRequired("an operation needs a requesting principal")
        if story_id is None and kind in (OperationKind.TURN, OperationKind.GENESIS):
            raise InvalidRequest(f"a {kind} operation needs a story", {"field": "story_id"})
        if turn is not None and (story_id is None or turn < 1):
            raise InvalidRequest(
                "turn must be a positive turn index of the operation's story", {"field": "turn"}
            )
        self.db = db
        self.operation_id = new_ulid()
        self.story_id = story_id
        self.kind = kind
        self.principal = principal
        self.requested_turn = turn
        self.provenance = provenance
        self.authority = authority
        self.status: OperationStatus | None = None
        self.narrative_turn: int | None = None
        self.turn_variant: int | None = None
        self.tx_time: datetime | None = None
        self.seqs: tuple[int, ...] = ()
        self._staged: list[StagedEvent] = []
        self._commit_steps: list[CommitStep] = []

    # -- lifecycle ---------------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.status is OperationStatus.IN_FLIGHT:
            final = OperationStatus.FAILED if exc_type is not None else OperationStatus.ABANDONED
            await self._finish(final, exc)

    async def begin(self) -> None:
        """Insert the ``in_flight`` operations row. Takes no story lock."""
        if self.status is not None:
            raise RuntimeError(f"operation {self.operation_id} has already begun")
        async with self._connection() as conn, conn.begin():
            await conn.execute(
                insert(operations).values(
                    operation_id=self.operation_id,
                    story_id=self.story_id,
                    kind=self.kind.value,
                    principal=self.principal,
                )
            )
        self.status = OperationStatus.IN_FLIGHT

    def append(
        self,
        type: str,
        payload: EventPayload | Mapping[str, Any],
        *,
        provenance: Provenance | None = None,
        authority: Authority | None = None,
        character_id: str | None = None,
    ) -> EventPayload:
        """Validate and stage one event; nothing is written until ``commit()``.

        Raises ``UnregisteredEventType`` or ``InvalidEventPayload`` before anything is staged.
        The category comes from the registry, never from the caller.
        """
        self._require_in_flight("append to")
        model = validate(type, payload)
        self._staged.append(
            StagedEvent(
                type=type,
                category=lookup(type).category,
                payload=model,
                provenance=provenance or self.provenance,
                authority=authority or self.authority,
                character_id=character_id,
            )
        )
        return model

    def add_commit_step(self, step: CommitStep) -> None:
        """Run ``step(conn)`` inside the commit transaction, after the events and projections
        are written and before the operation is finalised (e.g. a rebuild's refold)."""
        self._require_in_flight("add a commit step to")
        self._commit_steps.append(step)

    @property
    def staged(self) -> tuple[StagedEvent, ...]:
        return tuple(self._staged)

    async def commit(self) -> None:
        """Write every staged event in one transaction, or none of them."""
        self._require_in_flight("commit")
        try:
            async with self._connection() as conn, conn.begin():
                await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                await self._write(conn)
        except BaseException as exc:
            self.narrative_turn = self.turn_variant = self.tx_time = None
            self.seqs = ()
            await self._finish(OperationStatus.FAILED, exc)
            raise
        self.status = OperationStatus.COMMITTED

    async def abandon(self) -> None:
        """Give up without writing anything (status ``abandoned``)."""
        self._require_in_flight("abandon")
        await self._finish(OperationStatus.ABANDONED, None)

    # -- the commit transaction --------------------------------------------------------------

    async def _write(self, conn: AsyncConnection) -> None:
        advance = False
        if self.story_id is not None:
            locked = (
                await conn.execute(
                    select(stories.c.next_turn_index)
                    .where(stories.c.id == self.story_id)
                    .with_for_update()
                )
            ).one_or_none()
            if locked is None:
                raise StoryNotFound(f"story {self.story_id} does not exist")
            self.narrative_turn, self.turn_variant, advance = await self._position(
                conn, locked.next_turn_index
            )
        else:
            await conn.execute(select(func.pg_advisory_xact_lock(DEPLOYMENT_LOCK_KEY)))

        story_match = (
            events.c.story_id.is_(None)
            if self.story_id is None
            else events.c.story_id == self.story_id
        )
        last_seq = await conn.scalar(
            select(func.coalesce(func.max(events.c.seq), 0)).where(story_match)
        )
        first = int(last_seq or 0) + 1
        self.seqs = tuple(range(first, first + len(self._staged)))
        ids = [new_ulid() for _ in self._staged]
        if self._staged:
            await conn.execute(
                insert(events),
                [
                    {
                        "id": event_id,
                        "story_id": self.story_id,
                        "seq": seq,
                        "operation_id": self.operation_id,
                        "category": staged.category.value,
                        "narrative_turn": self.narrative_turn,
                        "turn_variant": self.turn_variant,
                        "character_id": staged.character_id,
                        "principal": self.principal,
                        "type": staged.type,
                        "payload": staged.payload.model_dump(mode="json"),
                        "provenance": staged.provenance.value,
                        "authority": staged.authority.value,
                    }
                    for event_id, seq, staged in zip(ids, self.seqs, self._staged, strict=True)
                ],
            )
        if self.story_id is not None:
            await projections.apply_events(
                conn, self.story_id, self._log_events(self.story_id, ids)
            )
        for step in self._commit_steps:
            await step(conn)
        await self._finalise(conn, advance)

    def _log_events(self, story_id: str, ids: list[str]) -> list[projections.LogEvent]:
        """The staged events as projections see them (this operation is ``active``)."""
        return [
            projections.LogEvent(
                id=event_id,
                story_id=story_id,
                seq=seq,
                operation_id=self.operation_id,
                kind=self.kind,
                membership=Membership.ACTIVE,
                category=staged.category,
                narrative_turn=self.narrative_turn,
                turn_variant=self.turn_variant,
                beat=None,
                character_id=staged.character_id,
                provenance=staged.provenance,
                type=staged.type,
                payload=staged.payload,
            )
            for event_id, seq, staged in zip(ids, self.seqs, self._staged, strict=True)
        ]

    async def _position(
        self, conn: AsyncConnection, next_turn_index: int
    ) -> tuple[int | None, int | None, bool]:
        """(narrative_turn, turn_variant, advances the turn index) for this operation."""
        turn = self.requested_turn
        if turn is None:
            if self.kind is OperationKind.TURN:
                return next_turn_index, 1, True
            return None, None, False
        if turn >= next_turn_index:
            raise InvalidRequest(
                f"turn {turn} has not happened yet (next turn is {next_turn_index})",
                {"field": "turn"},
            )
        committed_turns = and_(
            operations.c.story_id == self.story_id,
            operations.c.narrative_turn == turn,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        if self.kind is OperationKind.TURN:
            last = await conn.scalar(
                select(func.coalesce(func.max(operations.c.turn_variant), 0)).where(committed_turns)
            )
            return turn, int(last or 0) + 1, False
        active = await conn.scalar(
            select(func.max(operations.c.turn_variant)).where(
                committed_turns, operations.c.membership == Membership.ACTIVE.value
            )
        )
        if active is None:
            raise InvalidRequest(f"turn {turn} has no active variant", {"field": "turn"})
        return turn, int(active), False

    async def _finalise(self, conn: AsyncConnection, advance: bool) -> None:
        """Mark the operation committed and move the story's counters, in the same transaction."""
        now = func.transaction_timestamp()
        result = await conn.execute(
            update(operations)
            .where(
                operations.c.operation_id == self.operation_id,
                operations.c.status == OperationStatus.IN_FLIGHT.value,
            )
            .values(
                status=OperationStatus.COMMITTED.value,
                narrative_turn=self.narrative_turn,
                turn_variant=self.turn_variant,
                membership_tx=now,
                committed_at=now,
            )
            .returning(operations.c.membership_tx)
        )
        row = result.one_or_none()
        if row is None:
            raise RuntimeError(f"operation {self.operation_id} is no longer in flight")
        self.tx_time = row.membership_tx
        if self.story_id is None:
            return
        changes: dict[str, Any] = {}
        if advance:
            changes["next_turn_index"] = stories.c.next_turn_index + 1
        if self.kind in REVISION_BUMPING_KINDS:
            changes["timeline_revision"] = stories.c.timeline_revision + 1
        if changes:
            await conn.execute(
                update(stories).where(stories.c.id == self.story_id).values(**changes)
            )

    # -- helpers -----------------------------------------------------------------------------

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection]:
        if isinstance(self.db, AsyncConnection):
            if self.db.in_transaction():
                raise RuntimeError("Operation needs a connection with no open transaction")
            yield self.db
            return
        async with self.db.connect() as conn:
            yield conn

    def _require_in_flight(self, action: str) -> None:
        if self.status is not OperationStatus.IN_FLIGHT:
            state = self.status.value if self.status else "not begun"
            raise RuntimeError(f"cannot {action} operation {self.operation_id}: {state}")

    async def _finish(self, status: OperationStatus, cause: BaseException | None) -> None:
        """Record a terminal non-committed status in its own transaction.

        If that write fails while another exception is propagating, the original stays the
        error and the failure to record the status is attached as a note.
        """
        try:
            async with self._connection() as conn, conn.begin():
                await conn.execute(
                    update(operations)
                    .where(
                        operations.c.operation_id == self.operation_id,
                        operations.c.status == OperationStatus.IN_FLIGHT.value,
                    )
                    .values(status=status.value)
                )
        except Exception as exc:
            if cause is None:
                raise
            logger.warning(
                "could not mark operation %s %s: %s",
                self.operation_id,
                status.value,
                type(exc).__name__,
            )
            cause.add_note(f"operation {self.operation_id} could not be marked {status.value}")
        self.status = status
