"""``Operation``: one unit of work whose events commit atomically (FR-STORE-5, NFR-REL-1).

Two phases. ``begin()`` inserts the ``operations`` row (``in_flight``) without taking a lock,
in one REPEATABLE READ read phase that also captures the story's ``timeline_revision`` as
``base_revision`` (and, given a read-set source, the read set, #47) and the configuration it
runs under (``config_revisions``, FR-CONC-14: the story and deployment configuration
revisions, the story rating's version and the interpretive settings its kind depends on,
``core.config_dependencies``, #48/#64).
``append()`` validates each payload and buffers it, so model calls between them hold no lock
(FR-CONC-2). ``commit()`` runs
one transaction that does all of the following:

1. takes ``SELECT … FOR UPDATE`` on the story row, with ``lock_timeout`` 5 s;
2. with ``decide``, revalidates against the base (``epistrel.core.concurrency``), including
   whether a configuration dependency changed (``ConfigRecheck``, FR-CONC-16): ``Regenerate``,
   ``Abandon`` and ``ConfigRecheck`` roll back and raise ``RevalidationRequired``;
3. positions the operation: only ``turn`` operations allocate a turn index (FR-STORE-8,24),
   as variant ``max(existing) + 1`` at it, recording the active variant of the turn before it
   as ``parent_variant`` (the variant tree, #66); a ``turn`` at the head's index (regenerate)
   makes everything the old variant holds ``dormant``, and every turn makes the still-
   ``discarded`` operations at or after its index ``dormant`` (a rewound tail becomes a
   dormant continuation at the first new turn over it, #66);
4. assigns dense per-story ``seq`` values (``max(seq)+1`` under the lock), writes the events,
   and maintains every projection for them (``projections.apply_events``, FR-STORE-4), then
   runs any commit steps the caller added;
5. finalises the operation (``committed``, ``membership_tx`` = the commit's ``tx_time``, the
   story's resulting ``timeline_revision``), bumps ``stories.timeline_revision`` by one when the
   operation ``bumps_revision`` (FR-CONC-1), records a control action on the story, and checks
   the active variants are one chain (FR-STORE-28, ``ChainBroken``);
6. under an idempotent call (#61), binds the operation to the call's ``Idempotency-Key``: a late
   original whose key a retry took over is refused, so a key never maps to two commits.

Any failure rolls the transaction back, so no event row survives, and the operation is then
marked ``failed`` in a separate transaction. Leaving the ``async with`` block without
``commit()`` marks it ``failed`` when an exception escapes and ``abandoned`` otherwise.

``valid_time`` equals ``tx_time`` (both ``transaction_timestamp()``) until the world clock
arrives in M6. A story-less operation (deployment-scoped control) serialises on a transaction
advisory lock instead of a story row.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Literal, Self

from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased

from epistrel.core import (
    config_dependencies,
    idempotency,
    projections,
    summary_port,
    turn_gate,
)
from epistrel.core.concurrency import (
    Abandon,
    ConfigRecheck,
    Decide,
    ReadSet,
    ReadSetSource,
    Regenerate,
    RevalidationRequired,
    RevalidationState,
    attempt_record,
    touched_members,
)
from epistrel.core.enums import (
    Authority,
    EventCategory,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
    StoryStatus,
)
from epistrel.core.errors import (
    ChainBroken,
    InvalidRequest,
    PrincipalRequired,
    StoryArchived,
    StoryNotFound,
)
from epistrel.core.events.registry import EventPayload, lookup, validate
from epistrel.core.ids import new_ulid
from epistrel.core.settings_schema import StorySettings
from epistrel.core.tables import events, operations, stories, turn_queue
from epistrel.observability import context as telemetry
from epistrel.observability import tracing
from epistrel.testing import crash

logger = logging.getLogger(__name__)

Origin = Literal["generated", "ingested"]
"""Who produced an operation's records: the Engine, or a consumer's ``ingest`` (#58)."""

CommitStep = Callable[[AsyncConnection], Awaitable[None]]
"""Extra work run inside the commit transaction, under the story lock, after the events."""

Database = AsyncEngine | AsyncConnection

LOCK_TIMEOUT = "5s"
"""How long ``commit()`` waits for the story row lock before failing."""

DEPLOYMENT_LOCK_KEY = 0x45_5049_5354  # "EPIST": serialises story-less operations' seq
"""``pg_advisory_xact_lock`` key that stands in for the story row lock when ``story_id`` is
``None``."""

DEFAULT_BUMPING_KINDS = frozenset({OperationKind.TURN, OperationKind.GENESIS})
"""Kinds that bump ``timeline_revision`` when the caller does not say (``bumps_revision=None``).

Every other truth change (truncate, untruncate, swap, a pinned-memory or custom-instruction
edit, ingest, …) declares ``bumps_revision=True`` explicitly; settings and archive do not."""

NEVER_BUMPING_KINDS = frozenset({OperationKind.CONSOLIDATION, OperationKind.MAINTENANCE})
"""Derived and maintenance work never bumps the revision (FR-CONC-1), so background work never
invalidates in-flight turns. Audit-only records are written by such operations."""


class Withdrawn(Exception):
    """Raised inside a batch commit when its work is no longer wanted although nothing went
    wrong (e.g. a concurrent duplicate already recorded it): the operations end ``abandoned``."""


@dataclass(frozen=True, slots=True)
class StagedEvent:
    """A validated event waiting for ``commit()``."""

    type: str
    category: EventCategory
    payload: EventPayload
    provenance: Provenance
    authority: Authority
    character_id: str | None
    beat: int | None = None


class Operation:
    """Groups the events of one unit of work and commits them all or none.

    ``async with Operation(db, story_id=..., kind=..., principal=...) as op:`` begins it;
    ``op.append(...)`` stages events; ``await op.commit()`` writes them. ``turn=`` positions a
    ``turn`` operation at the head turn as a new variant (regenerate; any other turn is 422),
    or a non-turn operation at an existing turn's active variant; non-turn operations never
    allocate one.

    ``bumps_revision`` declares whether the commit changes story truth (``None``: only ``turn``
    and ``genesis`` do, plus any control action). ``control_action`` marks a timeline edit
    (truncate, untruncate, swap, regenerate — a ``turn`` with ``turn=`` is one by default):
    its commit records itself as the story's latest control action, so revalidating operations
    begun before it abandon (FR-CONC-4d). Control actions commit against current state.
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
        bumps_revision: bool | None = None,
        control_action: bool | None = None,
        idempotency_key: str | None = None,
        viewpoint: str | None = None,
        origin: Origin = "generated",
        operation_id: str | None = None,
        queued: bool = False,
        source_window: dict[str, Any] | None = None,
    ) -> None:
        if principal is None or not principal.strip():
            raise PrincipalRequired("an operation needs a requesting principal")
        if story_id is None and kind in (OperationKind.TURN, OperationKind.GENESIS):
            raise InvalidRequest(f"a {kind} operation needs a story", {"field": "story_id"})
        if turn is not None and (story_id is None or turn < 1):
            raise InvalidRequest(
                "turn must be a positive turn index of the operation's story", {"field": "turn"}
            )
        if control_action is None:
            control_action = kind is OperationKind.TURN and turn is not None
        if bumps_revision is None:
            bumps_revision = kind in DEFAULT_BUMPING_KINDS or control_action
        if bumps_revision and kind in NEVER_BUMPING_KINDS:
            raise InvalidRequest(
                f"a {kind} operation never changes story truth", {"field": "bumps_revision"}
            )
        if control_action and not bumps_revision:
            raise InvalidRequest(
                "a control action changes the timeline, so it bumps the revision",
                {"field": "bumps_revision"},
            )
        if (bumps_revision or control_action) and story_id is None:
            raise InvalidRequest("only a story has a timeline revision", {"field": "story_id"})
        if queued and operation_id is None:
            raise InvalidRequest("a queued operation is started by its id", {"field": "turn"})
        self.db = db
        self.operation_id = operation_id or new_ulid()
        self.queued = queued
        """Starts the ``queued`` row a story's turn queue holds (#63) instead of inserting one;
        its commit removes the turn from the queue in the same transaction."""
        self.holds_turn_slot = False
        """Set once the operation took the story's turn slot (#63): its commit or failure gives
        the slot back in the same transaction."""
        self.bumps_revision = bumps_revision
        self.control_action = control_action
        self.story_id = story_id
        self.kind = kind
        self.principal = principal
        self.idempotency_key = idempotency_key
        """The caller's idempotency key, stored on the row (#57); #61 adds replay."""
        self.viewpoint = viewpoint
        """The resolved output viewpoint the operation's result is for (FR-RESP-12)."""
        self.origin: Origin = origin
        """``ingested`` when a consumer recorded the operation's events (#58)."""
        self.requested_turn = turn
        self.source_window = source_window
        """A derivation's declared source window (FR-STORE-27; #72's summary segments:
        ``{from_turn, to_turn}``), written with the row at ``begin()``."""
        self.provenance = provenance
        self.authority = authority
        self.status: OperationStatus | None = None
        self.narrative_turn: int | None = None
        self.turn_variant: int | None = None
        self.parent_variant: int | None = None
        """The active variant of the turn before a ``turn`` operation's (#66), set at commit."""
        self.tx_time: datetime | None = None
        self.seqs: tuple[int, ...] = ()
        self.base_revision: int | None = None
        self.read_set: ReadSet | None = None
        self.read_set_source: ReadSetSource | None = None
        self.config_revisions: dict[str, Any] | None = None
        """The configuration the operation runs under (``core.config_dependencies``, #64)."""
        self.config_captured = config_dependencies.captured_for(kind, origin)
        self.config_dependencies = config_dependencies.dependencies_for(kind, origin)
        """The members compared at commit: an interpretive change to one is re-checked."""
        self.story_settings = StorySettings()
        self.attempts: list[dict[str, Any]] = []
        self.timeline_revision: int | None = None
        """The story's revision after this operation committed."""
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
            final = (
                OperationStatus.FAILED
                if exc_type is not None and not isinstance(exc, RevalidationRequired)
                else OperationStatus.ABANDONED
            )
            await self._finish(final, exc)

    @property
    def in_flight(self) -> bool:
        return self.status is OperationStatus.IN_FLIGHT

    async def begin(self, read: Callable[[AsyncConnection], Awaitable[Any]] | None = None) -> Any:
        """Insert the ``in_flight`` operations row and capture the base. Takes no story lock.

        One REPEATABLE READ transaction reads ``base_revision``, the story settings, the read
        set (when ``read_set_source`` is set), the story rating's current version and whatever
        ``read(conn)`` reads, all from the
        same snapshot (FR-CONC-2); ``read``'s result is returned. The row is inserted after it.
        """
        if self.status is not None:
            raise RuntimeError(f"operation {self.operation_id} has already begun")
        async with self._connection() as conn:
            async with conn.begin():
                result = await self._read_phase(conn, read)
            # The insert runs after the snapshot, read committed: its foreign-key check locks
            # the story row, which a REPEATABLE READ transaction cannot do once a concurrent
            # commit has updated it.
            async with conn.begin():
                if self.queued:
                    await self._start_queued(conn)
                    self.status = OperationStatus.IN_FLIGHT
                    telemetry.operation_opened(self.operation_id, self.story_id)
                    return result
                await conn.execute(
                    insert(operations).values(
                        operation_id=self.operation_id,
                        story_id=self.story_id,
                        kind=self.kind.value,
                        principal=self.principal,
                        base_revision=self.base_revision,
                        read_set=self.read_set,
                        config_revisions=self.config_revisions,
                        idempotency_key=self.idempotency_key,
                        viewpoint=self.viewpoint,
                        origin=self.origin,
                        source_window=self.source_window,
                    )
                )
        self.status = OperationStatus.IN_FLIGHT
        telemetry.operation_opened(self.operation_id, self.story_id)
        return result

    def _span_attributes(self) -> dict[str, str]:
        """The commit span's identifiers (#74): never a payload."""
        found = {"epistrel.operation_id": self.operation_id, "epistrel.kind": self.kind.value}
        if self.story_id is not None:
            found["epistrel.story_id"] = self.story_id
        return found

    async def _start_queued(self, conn: AsyncConnection) -> None:
        """``queued`` → ``in_flight`` with the base just read (the row was inserted at enqueue
        with the request's key, viewpoint and origin)."""
        started = await conn.scalar(
            update(operations)
            .where(
                operations.c.operation_id == self.operation_id,
                operations.c.status == OperationStatus.QUEUED.value,
            )
            .values(
                status=OperationStatus.IN_FLIGHT.value,
                base_revision=self.base_revision,
                read_set=self.read_set,
                config_revisions=self.config_revisions,
            )
            .returning(operations.c.operation_id)
        )
        if started is None:
            raise RuntimeError(f"operation {self.operation_id} is not queued")

    async def restart(self, read: Callable[[AsyncConnection], Awaitable[Any]] | None = None) -> Any:
        """Start a regeneration attempt: the same operation with a new base (FR-CONC-4c).

        Drops the staged events and commit steps, then re-runs the read phase and records the
        new ``base_revision`` and read set on the operation row.
        """
        self._require_in_flight("restart")
        self._staged.clear()
        self._commit_steps.clear()
        async with self._connection() as conn:
            async with conn.begin():
                result = await self._read_phase(conn, read)
            async with conn.begin():
                await conn.execute(
                    update(operations)
                    .where(operations.c.operation_id == self.operation_id)
                    .values(
                        base_revision=self.base_revision,
                        read_set=self.read_set,
                        config_revisions=self.config_revisions,
                    )
                )
        return result

    async def _read_phase(
        self, conn: AsyncConnection, read: Callable[[AsyncConnection], Awaitable[Any]] | None
    ) -> Any:
        if self.story_id is None:
            return await read(conn) if read is not None else None
        await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        found = (
            await conn.execute(
                select(
                    stories.c.timeline_revision,
                    stories.c.settings,
                    stories.c.status,
                ).where(stories.c.id == self.story_id)
            )
        ).one_or_none()
        if found is not None:
            self._refuse_archived(found.status)
            self.base_revision = int(found.timeline_revision)
            self.story_settings = StorySettings.from_row(found.settings)
            self.config_revisions = await config_dependencies.capture(
                conn, self.story_id, self.config_captured
            )
            if self.read_set_source is not None:
                self.read_set = await self.read_set_source.hashes(conn, self.story_id)
        return await read(conn) if read is not None else None

    async def recapture_config(self) -> tuple[str, ...]:
        """Re-capture the configuration the operation runs under (read committed, its own
        transaction) before a re-check under it (FR-CONC-16); keeps keys the caller added
        (``generation_settings``) and records the result on the row. Returns the dependency
        members that changed."""
        self._require_in_flight("re-capture the configuration of")
        assert self.story_id is not None  # noqa: S101 - only story operations depend on config
        async with self._connection() as conn, conn.begin():
            current = await config_dependencies.capture(conn, self.story_id, self.config_captured)
            moved = config_dependencies.changed(
                self.config_revisions, current, self.config_dependencies
            )
            self.config_revisions = {**(self.config_revisions or {}), **(current or {})}
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id == self.operation_id)
                .values(config_revisions=self.config_revisions)
            )
        return moved

    def append(
        self,
        type: str,
        payload: EventPayload | Mapping[str, Any],
        *,
        provenance: Provenance | None = None,
        authority: Authority | None = None,
        character_id: str | None = None,
        beat: int | None = None,
    ) -> EventPayload:
        """Validate and stage one event; nothing is written until ``commit()``.

        ``beat`` is the event's beat within its turn (FR-RESP-9: the player input is beat 1).
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
                beat=beat,
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

    async def commit(self, decide: Decide | None = None) -> None:
        """Write every staged event in one transaction, or none of them.

        With ``decide``, the FR-CONC-4 decision runs under the same story lock first. Without
        it the operation commits against current state (control actions, background work).
        ``Regenerate`` and ``Abandon`` raise ``RevalidationRequired`` with nothing written and
        the operation still ``in_flight`` (the attempt is recorded in its own transaction).
        """
        self._require_in_flight("commit")
        if decide is not None and (self.story_id is None or self.base_revision is None):
            raise InvalidRequest("only a story operation with a base revision revalidates")
        try:
            with tracing.span("commit", self._span_attributes()):
                async with self._connection() as conn, conn.begin():
                    await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                    await self._write(conn, decide)
        except RevalidationRequired:
            self._reset_position()
            await self._record_attempts()
            raise
        except BaseException as exc:
            self._reset_position()
            await self._finish(OperationStatus.FAILED, exc)
            raise
        self.status = OperationStatus.COMMITTED
        self._wake()
        await self._after_commit()

    async def _after_commit(self) -> None:
        """A committed turn may leave the Lite summary behind its window: ask the summary port
        (#72) to schedule the story's job (logged, never raised)."""
        if (
            self.kind is OperationKind.TURN
            and self.story_id is not None
            and isinstance(self.db, AsyncEngine)
        ):
            await summary_port.notify(self.db, self.story_id)

    async def abandon(self) -> None:
        """Give up without writing anything (status ``abandoned``); ``attempts`` is kept."""
        self._require_in_flight("abandon")
        await self._record_attempts()
        await self._finish(OperationStatus.ABANDONED, None)

    async def fail(self, cause: BaseException | None = None) -> None:
        """Give up after an error (status ``failed``)."""
        self._require_in_flight("fail")
        await self._finish(OperationStatus.FAILED, cause)

    # -- batch hooks (``OperationBatch``) -------------------------------------------------

    async def write_in(self, conn: AsyncConnection, decide: Decide | None = None) -> None:
        """Do this operation's commit work inside ``conn``'s open transaction, which the caller
        (``OperationBatch``) owns: it commits or rolls back, then calls ``settle``."""
        self._require_in_flight("commit")
        if decide is not None and (self.story_id is None or self.base_revision is None):
            raise InvalidRequest("only a story operation with a base revision revalidates")
        with tracing.span("commit", self._span_attributes()):
            await self._write(conn, decide)

    async def settle(self, error: BaseException | None) -> None:
        """Record the outcome of a ``write_in`` whose transaction has ended: ``committed``
        without ``error``; else ``abandoned`` (a revalidation or ``Withdrawn``) or ``failed``."""
        if error is None:
            self.status = OperationStatus.COMMITTED
            self._wake()
            await self._after_commit()
            return
        self._reset_position()
        if isinstance(error, RevalidationRequired):
            await self._record_attempts()
            await self._finish(OperationStatus.ABANDONED, error)
        elif isinstance(error, Withdrawn):
            await self._finish(OperationStatus.ABANDONED, error)
        else:
            await self._finish(OperationStatus.FAILED, error)

    def _reset_position(self) -> None:
        self.narrative_turn = self.turn_variant = self.parent_variant = self.tx_time = None
        self.timeline_revision = None
        self.seqs = ()

    # -- the commit transaction --------------------------------------------------------------

    async def _write(self, conn: AsyncConnection, decide: Decide | None) -> None:
        kind = self.kind.value
        crash.hit("before_lock", kind)
        advance = False
        current_revision = 0
        if self.story_id is not None:
            locked = (
                await conn.execute(
                    select(
                        stories.c.next_turn_index,
                        stories.c.timeline_revision,
                        stories.c.last_control_revision,
                        stories.c.last_control_operation_id,
                        stories.c.status,
                    )
                    .where(stories.c.id == self.story_id)
                    .with_for_update()
                )
            ).one_or_none()
            if locked is None:
                raise StoryNotFound(f"story {self.story_id} does not exist")
            self._refuse_archived(locked.status)
            current_revision = int(locked.timeline_revision)
            if decide is not None:
                await self._revalidate(conn, locked, decide)
            self.narrative_turn, self.turn_variant, advance = await self._position(
                conn, locked.next_turn_index
            )
            if self.kind is OperationKind.TURN:
                self.parent_variant = await self._parent_variant(conn)
                if self.requested_turn is not None:
                    await self._retire_variant(conn)
                await self._shelve_discarded(conn)
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
                        "beat": staged.beat,
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
        crash.hit("after_events", kind)
        if self.story_id is not None:
            await projections.apply_events(
                conn, self.story_id, self._log_events(self.story_id, ids), kind=kind
            )
        for step in self._commit_steps:
            await step(conn)
        await self._finalise(conn, advance, current_revision)
        if self.story_id is not None and (
            self.kind is OperationKind.TURN or self.control_action or self._commit_steps
        ):
            await self._check_chain(conn)
        call = idempotency.current_call()
        if call is not None:
            # The key's operation commits with it (a late original is refused here, #61).
            await call.bind(conn, self)
        crash.hit("before_commit", kind)

    async def _revalidate(self, conn: AsyncConnection, locked: Any, decide: Decide) -> None:
        """Run ``decide`` under the lock; raise ``RevalidationRequired`` unless it commits."""
        assert self.story_id is not None  # noqa: S101 - checked by commit()
        assert self.base_revision is not None  # noqa: S101
        base = self.base_revision
        current = int(locked.timeline_revision)
        control = int(locked.last_control_revision) > base
        touched: tuple[str, ...] = ()
        if not control and current != base and self.read_set is not None:
            assert self.read_set_source is not None  # noqa: S101 - set together
            now = await self.read_set_source.hashes(conn, self.story_id)
            touched = touched_members(self.read_set, now)
        config_changed: tuple[str, ...] = ()
        if not control and not touched and self.config_dependencies:
            current_config = await config_dependencies.capture(
                conn, self.story_id, self.config_dependencies
            )
            config_changed = config_dependencies.changed(
                self.config_revisions, current_config, self.config_dependencies
            )
        state = RevalidationState(
            attempt=len(self.attempts) + 1,
            base_revision=base,
            current_revision=current,
            control_since_base=control,
            control_operation_id=locked.last_control_operation_id if control else None,
            touched=touched,
            config_changed=config_changed,
        )
        outcome = decide(state)
        self.attempts.append(attempt_record(state, outcome))
        if isinstance(outcome, Regenerate | Abandon | ConfigRecheck):
            raise RevalidationRequired(outcome, state)

    async def _parent_variant(self, conn: AsyncConnection) -> int | None:
        """The active variant of the turn before this one (``None`` for turn 1)."""
        assert self.narrative_turn is not None  # noqa: S101 - a turn is positioned first
        if self.narrative_turn <= 1:
            return None
        found = await conn.scalar(
            select(operations.c.turn_variant).where(
                operations.c.story_id == self.story_id,
                operations.c.narrative_turn == self.narrative_turn - 1,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.ACTIVE.value,
            )
        )
        return int(found) if found is not None else None

    async def _retire_variant(self, conn: AsyncConnection) -> None:
        """A regenerated turn replaces the active variant at its index: everything positioned
        at it (the old turn and the work hanging off it) goes ``dormant`` (kept, swappable,
        FR-STORE-12/28) in the same transaction."""
        await conn.execute(
            update(operations)
            .where(
                operations.c.story_id == self.story_id,
                operations.c.narrative_turn >= self.narrative_turn,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.ACTIVE.value,
            )
            .values(membership=Membership.DORMANT.value, membership_tx=func.transaction_timestamp())
        )

    async def _shelve_discarded(self, conn: AsyncConnection) -> None:
        """A rewound tail becomes a dormant continuation at the first turn written over it
        (#66): the still-``discarded`` operations at or after this turn's index go ``dormant``.
        The transcript projection makes the same change from the turn's events."""
        await conn.execute(
            update(operations)
            .where(
                operations.c.story_id == self.story_id,
                operations.c.narrative_turn >= self.narrative_turn,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.DISCARDED.value,
            )
            .values(membership=Membership.DORMANT.value, membership_tx=func.transaction_timestamp())
        )

    async def _check_chain(self, conn: AsyncConnection) -> None:
        """The active turn variants must be one chain: turns 1..k, one variant each, and the
        story's next turn index k+1 (FR-STORE-28, TIME (f)). Raises ``ChainBroken``."""
        active = (
            await conn.execute(
                select(
                    func.count(),
                    func.count(func.distinct(operations.c.narrative_turn)),
                    func.coalesce(func.min(operations.c.narrative_turn), 1),
                    func.coalesce(func.max(operations.c.narrative_turn), 0),
                ).where(
                    operations.c.story_id == self.story_id,
                    operations.c.kind == OperationKind.TURN.value,
                    operations.c.status == OperationStatus.COMMITTED.value,
                    operations.c.membership == Membership.ACTIVE.value,
                )
            )
        ).one()
        count, distinct, lowest, highest = (int(v) for v in active)
        next_turn = await conn.scalar(
            select(stories.c.next_turn_index).where(stories.c.id == self.story_id)
        )
        parent = aliased(operations)
        orphans = await conn.scalar(
            select(func.count())
            .select_from(operations)
            .outerjoin(
                parent,
                and_(
                    parent.c.story_id == operations.c.story_id,
                    parent.c.narrative_turn == operations.c.narrative_turn - 1,
                    parent.c.kind == OperationKind.TURN.value,
                    parent.c.status == OperationStatus.COMMITTED.value,
                    parent.c.membership == Membership.ACTIVE.value,
                ),
            )
            .where(
                operations.c.story_id == self.story_id,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.ACTIVE.value,
                operations.c.narrative_turn > 1,
                operations.c.parent_variant.is_not(None),
                parent.c.turn_variant.is_distinct_from(operations.c.parent_variant),
            )
        )
        if not (
            count == distinct == highest
            and lowest == 1
            and next_turn == highest + 1
            and not orphans
        ):
            raise ChainBroken(
                "the active turn variants would not form a single chain",
                {
                    "story_id": self.story_id,
                    "active_turns": count,
                    "highest_turn": highest,
                    "next_turn_index": next_turn,
                    "orphaned_turns": int(orphans or 0),
                },
            )

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
                beat=staged.beat,
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
                return next_turn_index, await self._next_variant(conn, next_turn_index), True
            return None, None, False
        if turn >= next_turn_index:
            raise InvalidRequest(
                f"turn {turn} has not happened yet (next turn is {next_turn_index})",
                {"field": "turn"},
            )
        if self.kind is OperationKind.TURN and turn != next_turn_index - 1:
            raise InvalidRequest(
                f"only the latest turn ({next_turn_index - 1}) can be regenerated",
                {"field": "turn", "head": next_turn_index - 1},
            )
        committed_turns = and_(
            operations.c.story_id == self.story_id,
            operations.c.narrative_turn == turn,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        if self.kind is OperationKind.TURN:
            return turn, await self._next_variant(conn, turn), False
        active = await conn.scalar(
            select(func.max(operations.c.turn_variant)).where(
                committed_turns, operations.c.membership == Membership.ACTIVE.value
            )
        )
        if active is None:
            raise InvalidRequest(f"turn {turn} has no active variant", {"field": "turn"})
        return turn, int(active), False

    async def _next_variant(self, conn: AsyncConnection, turn: int) -> int:
        """``max(existing variants at turn) + 1`` (#66): a turn written over a rewound tail,
        like a regenerated one, never reuses a variant number."""
        last = await conn.scalar(
            select(func.coalesce(func.max(operations.c.turn_variant), 0)).where(
                operations.c.story_id == self.story_id,
                operations.c.narrative_turn == turn,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == OperationStatus.COMMITTED.value,
            )
        )
        return int(last or 0) + 1

    async def _finalise(self, conn: AsyncConnection, advance: bool, current_revision: int) -> None:
        """Mark the operation committed and move the story's counters, in the same transaction."""
        now = func.transaction_timestamp()
        if self.story_id is not None:
            self.timeline_revision = current_revision + (1 if self.bumps_revision else 0)
            if self.attempts:
                self.attempts[-1]["committed_revision"] = self.timeline_revision
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
                parent_variant=self.parent_variant,
                membership_tx=now,
                committed_at=now,
                attempts=self.attempts,
                timeline_revision=self.timeline_revision,
            )
            .returning(operations.c.membership_tx)
        )
        row = result.one_or_none()
        if row is None:
            raise RuntimeError(f"operation {self.operation_id} is no longer in flight")
        self.tx_time = row.membership_tx
        if self.story_id is None:
            return
        if self.holds_turn_slot:
            await turn_gate.release(conn, self.story_id, self.operation_id)
        if self.queued:
            await conn.execute(
                delete(turn_queue).where(turn_queue.c.operation_id == self.operation_id)
            )
        changes: dict[str, Any] = {}
        if advance:
            changes["next_turn_index"] = stories.c.next_turn_index + 1
        if self.bumps_revision:
            changes["timeline_revision"] = stories.c.timeline_revision + 1
        if self.control_action:
            changes["last_control_revision"] = self.timeline_revision
            changes["last_control_operation_id"] = self.operation_id
        if changes:
            await conn.execute(
                update(stories).where(stories.c.id == self.story_id).values(**changes)
            )

    # -- helpers -----------------------------------------------------------------------------

    def _refuse_archived(self, status: str) -> None:
        """An archived story is read-only: work that changes story truth (bumps the revision)
        is refused with ``story_archived``; archive, unarchive, maintenance and derived work
        still run (#50). Checked at ``begin()``, before the operations row is written, and again
        under the story lock at commit."""
        if self.bumps_revision and status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {self.story_id} is archived; unarchive it to change it",
                {"story_id": self.story_id},
            )

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

    async def _record_attempts(self) -> None:
        """Persist ``attempts`` after a rolled-back commit (its own short transaction)."""
        try:
            async with self._connection() as conn, conn.begin():
                await conn.execute(
                    update(operations)
                    .where(operations.c.operation_id == self.operation_id)
                    .values(attempts=self.attempts)
                )
        except Exception as exc:
            logger.warning(
                "could not record attempts of operation %s: %s",
                self.operation_id,
                type(exc).__name__,
            )

    async def _finish(self, status: OperationStatus, cause: BaseException | None) -> None:
        """Record a terminal non-committed status in its own transaction.

        If that write fails while another exception is propagating, the original stays the
        error and the failure to record the status is attached as a note.
        """
        if self.queued and isinstance(cause, asyncio.CancelledError):
            # A drainer stopped (shutdown) mid-turn: the turn goes back to the queue.
            status = OperationStatus.QUEUED
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
                if self.holds_turn_slot and self.story_id is not None:
                    await turn_gate.release(conn, self.story_id, self.operation_id)
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
        self._wake()

    def _wake(self) -> None:
        """Wake this process's waiters and drainer for the story (#63)."""
        if self.story_id is not None and (self.holds_turn_slot or self.queued):
            turn_gate.wakeups.wake(self.story_id)
