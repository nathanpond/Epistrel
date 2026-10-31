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

1. takes ``SELECT … FOR UPDATE`` on the story row, with ``lock_timeout`` 5 s (past it, 423
   ``story_busy`` with ``Retry-After``, nothing written; #472), and in the same
   statement sets the revision this commit leaves (``core.readset.COMMIT_REVISION_SETTING``),
   which the read set's change index stamps on what the commit's canon writes record (#136);
2. with ``decide``, revalidates against the base (``epistrel.core.concurrency``), including
   whether a configuration dependency changed (``ConfigRecheck``, FR-CONC-16): ``Regenerate``,
   ``Abandon`` and ``ConfigRecheck`` roll back and raise ``RevalidationRequired``. A structured
   turn (a keyed read set, #136) is also touched when a change since its base matches one of its
   query keys (one query over ``proposition_changes``) or its base is below the story's pruning
   watermark, and its rebase re-runs ``core.readset.REBASE_CHECKS`` under the lock;
3. positions the operation: only ``turn`` operations allocate a turn index (FR-STORE-8,24),
   as variant ``max(existing) + 1`` at it, recording the active variant of the turn before it
   as ``parent_variant`` (the variant tree, #66); a ``turn`` at the head's index (regenerate)
   makes everything the old variant holds ``dormant``, and every turn makes the still-
   ``discarded`` operations at or after its index ``dormant`` (a rewound tail becomes a
   dormant continuation at the first new turn over it, #66);
4. assigns dense per-story ``seq`` values (``max(seq)+1`` under the lock), writes the events,
   and maintains every projection for them (``projections.apply_events``, FR-STORE-4), then
   runs any commit steps the caller added; then the resurrection guard checks the events
   against the erasure ledger (``core.erasure.guard``, FR-ADMIN-9: 409
   ``erased_subject_referenced``, the operation ``abandoned``; a revalidating turn regenerates
   within its budget first) before they are indexed;
5. finalises the operation (``committed``, ``membership_tx`` = the commit's ``tx_time``, the
   story's resulting ``timeline_revision``), refreshes a turn's structured scene and what the
   operations it made dormant project (``projections.structured.after_turn``, #137), runs the
   ``TURN_HOOKS`` (#147's hot-set deadline), bumps
   ``stories.timeline_revision`` by one when the
   operation ``bumps_revision`` (FR-CONC-1), records a control action on the story, and checks
   the active variants are one chain (FR-STORE-28, ``ChainBroken``): at the chain's head, by
   index lookups, unless the commit is a control action or ``stories.chain_unchecked`` says the
   active turns changed some other way, when every active turn is checked (#281);
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
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace, TracebackType
from typing import Any, Literal, Self

from sqlalchemy import (
    Integer,
    Select,
    Text,
    and_,
    cast,
    delete,
    func,
    insert,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from epistrel.core import (
    clock,
    config_dependencies,
    idempotency,
    pause,
    projections,
    readset,
    summary_port,
    turn_gate,
)
from epistrel.core.commit import boundary as _boundary  # noqa: F401 - registers #154
from epistrel.core.concurrency import (
    ERASED_SUBJECT,
    Abandon,
    ConfigRecheck,
    Decide,
    KeyedReadSetSource,
    ReadSet,
    ReadSetSource,
    Rebase,
    RebaseCheckFailed,
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
from epistrel.core.erasure import guard as erasure_guard
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import event_record
from epistrel.core.errors import (
    ChainBroken,
    ErasedSubjectReferenced,
    InvalidRequest,
    PrincipalRequired,
    StoryArchived,
    StoryBusy,
    StoryNotFound,
)
from epistrel.core.events.registry import EventPayload, lookup, validate
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, follow
from epistrel.core.ratings.stamp import guardrail_stamp
from epistrel.core.settings_schema import StorySettings
from epistrel.core.tables import events, operations, stories, turn_queue
from epistrel.observability import context as telemetry
from epistrel.observability import stages, tracing
from epistrel.testing import crash

logger = logging.getLogger(__name__)

Origin = Literal["generated", "ingested"]
"""Who produced an operation's records: the Engine, or a consumer's ``ingest`` (#58)."""

CommitStep = Callable[[AsyncConnection], Awaitable[None]]
"""Extra work run inside the commit transaction, under the story lock, after the events."""

TurnHook = Callable[[AsyncConnection, str, int, tuple[str, ...]], Awaitable[None]]
"""``hook(conn, story_id, turn, made_dormant)``: run inside a committed ``turn`` operation's
transaction, after it is finalised (``turn`` is the story's head now), with the operations it
made ``dormant`` (a regenerated variant, a shelved tail). #147's hot set registers its deadline
check here."""

TURN_HOOKS: list[TurnHook] = []


def register_turn_hook(hook: TurnHook) -> TurnHook:
    """Run ``hook`` in every turn commit (once per hook)."""
    if hook not in TURN_HOOKS:
        TURN_HOOKS.append(hook)
    return hook


Database = AsyncEngine | AsyncConnection

LOCK_TIMEOUT = "5s"
"""How long ``commit()`` waits for the story row lock before failing. Read when each commit
transaction starts (``set_lock_timeout``), so a test can make it small."""

LOCK_RETRY_AFTER_S = 5
"""The ``Retry-After`` (seconds) of a story-lock timeout (#472): the lock timeout itself. The
holder has already kept the lock that long, so a retry sooner would most likely wait again."""

LOCK_NOT_AVAILABLE = "55P03"
"""PostgreSQL's ``lock_not_available``: a ``lock_timeout`` expired."""


async def set_lock_timeout(conn: AsyncConnection) -> None:
    """Bound how long the open transaction waits for a lock (``LOCK_TIMEOUT``)."""
    await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))


def lock_timed_out(error: BaseException) -> bool:
    """Whether ``error`` is the server's ``lock_not_available`` on a live connection."""
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    return isinstance(error, DBAPIError) and sqlstate == LOCK_NOT_AVAILABLE


def story_lock_busy(story_id: str) -> StoryBusy:
    """423 ``story_busy`` (``reason: lock_timeout``) with ``Retry-After``: another transaction
    held the story's lock past ``LOCK_TIMEOUT``, and nothing was written (#472). The database
    answered, so this is not ``database_unavailable``."""
    return StoryBusy(
        f"story {story_id} is busy: another commit held its lock; retry later",
        {"story_id": story_id, "reason": "lock_timeout"},
        retry_after=LOCK_RETRY_AFTER_S,
    )


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


def _failure_status(error: BaseException) -> OperationStatus:
    """A commit that raised ends ``failed``, except one the resurrection guard refused (#127):
    nothing went wrong, the work's context is stale, so it ends ``abandoned``."""
    if isinstance(error, ErasedSubjectReferenced):
        return OperationStatus.ABANDONED
    return OperationStatus.FAILED


CHAIN_OPERATION_SETTING = "epistrel.chain_operation"
"""Transaction-local: the operation whose commit holds the story lock. Migration 0045's chain
triggers leave that operation's own finalising update to its head check (#281)."""

ACTIVE_TURN_SQL = (
    "operations.kind = 'turn' AND operations.status = 'committed' "
    "AND operations.membership = 'active'"
)
"""``ACTIVE_TURN_PREDICATE`` as literal SQL, so the planner can use the partial unique index
``uq_operations_active_turn`` (bound parameters would hide the predicate from a generic plan)."""


def _active_turns(story_id: str | None) -> ColumnElement[bool]:
    return and_(operations.c.story_id == story_id, text(ACTIVE_TURN_SQL))


async def chain_violation(conn: AsyncConnection, story_id: str) -> dict[str, Any] | None:
    """The full FR-STORE-28 chain check over every active turn of the story (TIME (f)): ``None``
    when the active turn variants are turns 1..k, one variant each, every parent link points to
    the active variant of the turn before it, and the next turn index is k+1; otherwise the
    ``ChainBroken`` details. Its cost grows with the story, so a commit runs it only when its
    head check cannot decide (``Operation._check_chain``, #281)."""
    active = (
        await conn.execute(
            select(
                func.count(),
                func.count(func.distinct(operations.c.narrative_turn)),
                func.coalesce(func.min(operations.c.narrative_turn), 1),
                func.coalesce(func.max(operations.c.narrative_turn), 0),
            ).where(
                operations.c.story_id == story_id,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.ACTIVE.value,
            )
        )
    ).one()
    count, distinct, lowest, highest = (int(v) for v in active)
    next_turn = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
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
            operations.c.story_id == story_id,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
            operations.c.membership == Membership.ACTIVE.value,
            operations.c.narrative_turn > 1,
            operations.c.parent_variant.is_not(None),
            parent.c.turn_variant.is_distinct_from(operations.c.parent_variant),
        )
    )
    if count == distinct == highest and lowest == 1 and next_turn == highest + 1 and not orphans:
        return None
    return {
        "story_id": story_id,
        "active_turns": count,
        "highest_turn": highest,
        "next_turn_index": next_turn,
        "orphaned_turns": int(orphans or 0),
    }


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
    step: int | None = None
    """The beat's declared causal step (#168, FR-STORE-31; ``None``: outside declared steps)."""


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
        erasure: bool = False,
        slot: bool = False,
    ) -> None:
        if principal is None or not principal.strip():
            raise PrincipalRequired("an operation needs a requesting principal")
        if story_id is None and kind in (OperationKind.TURN, OperationKind.GENESIS):
            raise InvalidRequest(f"a {kind} operation needs a story", {"field": "story_id"})
        if turn is not None and (story_id is None or turn < 1):
            raise InvalidRequest(
                "turn must be a positive turn index of the operation's story", {"field": "turn"}
            )
        if slot and (kind is not OperationKind.INTER_TURN or story_id is None or turn is not None):
            raise InvalidRequest(
                "an inter-turn slot is an inter_turn operation of a story, after its last turn",
                {"field": "slot"},
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
        self.erasure = erasure
        """An erasure's operation (#124): it runs on an archived or paused story too, since an
        erasure must reach every story the subject appears in (FR-ADMIN-2)."""
        self.slot = slot
        """An inter-turn slot (#193, ``core.world_clock.slots``): positioned at commit, under the
        story lock, after the last completed turn ``N`` (its active variant) in the next slot
        ``slot_k`` no operation at ``N`` has taken (a slot number, like a variant, is never
        reused)."""
        self.slot_k: int | None = None
        self.turn_times: Any = None
        """#194: a structured turn's in-turn timing (``core.world_clock.timing.TurnTiming``):
        while it commits, the story's time basis gives every position of the turn its step's
        start (``core.world_clock.turn_scope``), its events' stamps included."""
        self._stages: list[dict[str, Any]] = []
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
        self.own_predicates: set[str] = set()
        """The predicates this operation auto-registered itself (``core.own_predicates``): its
        configuration captures leave them out, so it never re-checks against its own change."""
        self.story_settings = StorySettings()
        self.attempts: list[dict[str, Any]] = []
        self.timeline_revision: int | None = None
        """The story's revision after this operation committed."""
        self._staged: list[StagedEvent] = []
        self._commit_steps: list[CommitStep] = []
        self._followed: list[str] = []
        """Operations this commit made ``dormant`` (a regenerate, a rewound tail), for the
        structured projections (#137)."""

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
                        read_set=self._read_set_document(),
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

    async def begin_in(self, conn: AsyncConnection) -> None:
        """Begin inside ``conn``'s open transaction, which the caller owns (#119's genesis,
        which creates the story row in that transaction, as #67's fork does): the base, the
        story settings and the configuration the operation runs under are read there, and the
        ``in_flight`` row is inserted there. ``write_in(conn)`` then does the commit work in the
        same transaction, and ``settle`` records the outcome once it has ended. A rollback
        removes the row with everything else, so no ``failed`` row is left behind."""
        if self.status is not None:
            raise RuntimeError(f"operation {self.operation_id} has already begun")
        if self.queued:
            raise RuntimeError("a queued operation is started from the queue")
        if self.story_id is not None:
            found = (
                await conn.execute(
                    select(
                        stories.c.timeline_revision,
                        stories.c.settings,
                        stories.c.status,
                    ).where(stories.c.id == self.story_id)
                )
            ).one_or_none()
            if found is None:
                raise StoryNotFound(f"story {self.story_id} does not exist")
            await self._refuse_blocked(conn, found.status)
            self._refuse_archived(found.status)
            self.base_revision = int(found.timeline_revision)
            self.story_settings = StorySettings.from_row(found.settings)
            self.config_revisions = await config_dependencies.capture(
                conn, self.story_id, self.config_captured, own_predicates=self.own_predicates
            )
        await conn.execute(
            insert(operations).values(
                operation_id=self.operation_id,
                story_id=self.story_id,
                kind=self.kind.value,
                principal=self.principal,
                base_revision=self.base_revision,
                read_set=self._read_set_document(),
                config_revisions=self.config_revisions,
                idempotency_key=self.idempotency_key,
                viewpoint=self.viewpoint,
                origin=self.origin,
                source_window=self.source_window,
            )
        )
        self.status = OperationStatus.IN_FLIGHT
        telemetry.operation_opened(self.operation_id, self.story_id)

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
                read_set=self._read_set_document(),
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
                        read_set=self._read_set_document(),
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
            await self._refuse_blocked(conn, found.status)
            self._refuse_archived(found.status)
            if self.bumps_revision and not self.erasure:
                # A paused story (#118) refuses new truth-changing work; work that began before
                # the pause still commits (no check at commit). Erasure is never refused.
                await pause.refuse(conn, self.story_id, found.status)
            self.base_revision = int(found.timeline_revision)
            self.story_settings = StorySettings.from_row(found.settings)
            self.config_revisions = await config_dependencies.capture(
                conn, self.story_id, self.config_captured, own_predicates=self.own_predicates
            )
            if self.read_set_source is not None:
                keyed = self._keyed()
                if keyed is not None:
                    keyed.start_attempt()  # each attempt reads afresh (#136)
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
            current = await config_dependencies.capture(
                conn, self.story_id, self.config_captured, own_predicates=self.own_predicates
            )
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

    def use_read_set(self, source: ReadSetSource) -> None:
        """Compute the operation's read set with ``source`` (``run_with_revalidation``). A
        structured turn's (a ``KeyedReadSetSource``, #136) also depends on the story's
        predicate registry and the interpretive verification settings (FR-CONC-16, M4)."""
        self.read_set_source = source
        if isinstance(source, KeyedReadSetSource) and self.kind is OperationKind.TURN:
            self.config_captured = config_dependencies.captured_for(
                self.kind, self.origin, structured=True
            )
            self.config_dependencies = config_dependencies.dependencies_for(
                self.kind, self.origin, structured=True
            )

    def _keyed(self) -> KeyedReadSetSource | None:
        source = self.read_set_source
        return source if isinstance(source, KeyedReadSetSource) else None

    def _read_set_document(self) -> Any:
        """``operations.read_set``: the Lite hashes, or a structured turn's ``{lite, keys,
        retrieved}`` (#136)."""
        keyed = self._keyed()
        return self.read_set if keyed is None else keyed.document(self.read_set)

    async def rebase_recheck(self) -> bool:
        """A structured turn's ``REBASE_CHECKS`` (#140's verify, #144's permission checks) on
        its own connection, after an interpretive configuration change (FR-CONC-16)."""
        self._require_in_flight("re-check")
        async with self._connection() as conn, conn.begin():
            return await readset.run_rebase_checks(conn, self)

    def append(
        self,
        type: str,
        payload: EventPayload | Mapping[str, Any],
        *,
        provenance: Provenance | None = None,
        authority: Authority | None = None,
        character_id: str | None = None,
        beat: int | None = None,
        step: int | None = None,
    ) -> EventPayload:
        """Validate and stage one event; nothing is written until ``commit()``.

        ``beat`` is the event's beat within its turn (FR-RESP-9: the player input is beat 1);
        ``step`` its declared causal step within the render (#168, FR-STORE-31; 0 or ``None``:
        outside declared steps, stored 0).
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
                step=step or 0,
            )
        )
        return model

    def stage(self, name: str, status: str = "done") -> None:
        """Record one stage of the operation's work (``stage_log``, #124: an erasure's
        ``redacted``, ``ledger_written``, …), written with the commit. Identifiers, outcomes
        and the time (``at``, ``clock.now()``; #128's stage view) only."""
        self._require_in_flight("record a stage of")
        self._stages.append({"stage": name, "status": status, "at": clock.now().isoformat()})

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
        started = time.perf_counter()
        try:
            with tracing.span("commit", self._span_attributes()):
                async with self._connection() as conn, conn.begin():
                    await set_lock_timeout(conn)
                    await self._write(conn, decide)
        except RevalidationRequired:
            self._reset_position()
            await self._record_attempts()
            raise
        except BaseException as exc:
            self._reset_position()
            await self._finish(_failure_status(exc), exc)
            if self.story_id is not None and lock_timed_out(exc):
                raise story_lock_busy(self.story_id) from exc
            raise
        self.status = OperationStatus.COMMITTED
        if self.kind is OperationKind.TURN:
            # The turn's atomic commit (NFR-PERF-3; #76), as P-03 times it.
            stages.observe("commit", time.perf_counter() - started)
        self._wake()
        posted = time.perf_counter()
        await self._after_commit()
        if self.kind is OperationKind.TURN:
            # After the transaction: the summary port's scheduling (#72), reported apart.
            stages.observe("post_commit", time.perf_counter() - posted)

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
            await self._finish(_failure_status(error), error)

    def _reset_position(self) -> None:
        self.narrative_turn = self.turn_variant = self.parent_variant = self.tx_time = None
        self.slot_k = None
        self.timeline_revision = None
        self.seqs = ()

    # -- the commit transaction --------------------------------------------------------------

    async def _write(self, conn: AsyncConnection, decide: Decide | None) -> None:
        if self.turn_times is None or self.story_id is None:
            await self._write_body(conn, decide)
            return
        from epistrel.core import world_clock  # here: the clock reads States, which use this

        with world_clock.turn_scope(self.story_id, self):
            await self._write_body(conn, decide)

    async def _write_body(self, conn: AsyncConnection, decide: Decide | None) -> None:
        kind = self.kind.value
        self._followed = []
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
                        stories.c.changes_pruned_through_revision,
                        # The chain triggers leave this operation's own finalising update to
                        # the head check (#281).
                        func.set_config(CHAIN_OPERATION_SETTING, self.operation_id, True),
                        # The revision this commit leaves, which the change index stamps
                        # (#136): no extra statement.
                        func.set_config(
                            readset.COMMIT_REVISION_SETTING,
                            cast(
                                stories.c.timeline_revision + (1 if self.bumps_revision else 0),
                                Text,
                            ),
                            True,
                        ),
                    )
                    .where(stories.c.id == self.story_id)
                    .with_for_update()
                )
            ).one_or_none()
            if locked is None:
                raise StoryNotFound(f"story {self.story_id} does not exist")
            await self._refuse_blocked(conn, locked.status)
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
        story_minutes = await self._story_minutes(conn)
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
                        "step": staged.step,
                        "slot_k": self.slot_k,
                        "valid_time_minutes": story_minutes(staged),
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
            try:
                await step(conn)
            except RebaseCheckFailed as failed:
                self._rebase_failed(decide, current_revision, failed)
        await self._check_erased(conn, decide, current_revision)
        if self._staged:
            # The subject-reference index (FR-ADMIN-6; #123), after the commit steps so a
            # character a step adds is one of the story's subjects.
            await index_records(
                conn,
                [
                    event_record(
                        SimpleNamespace(
                            id=event_id,
                            story_id=self.story_id,
                            character_id=staged.character_id,
                            payload=staged.payload.model_dump(mode="json"),
                        )
                    )
                    for event_id, staged in zip(ids, self._staged, strict=True)
                ],
            )
        await self._finalise(conn, advance, current_revision)
        if (
            self.story_id is not None
            and self.kind is OperationKind.TURN
            and self.narrative_turn is not None
        ):
            # The turn's scene, and what the operations it made dormant project (#137).
            await projections.structured.after_turn(
                conn, self.story_id, self.narrative_turn, self._followed
            )
            for hook in list(TURN_HOOKS):
                await hook(conn, self.story_id, self.narrative_turn, tuple(self._followed))
        if self.story_id is not None and self.kind is OperationKind.TURN:
            # The guardrail set the turn variant passed (FR-RATE-7, #118), in force now.
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id == self.operation_id)
                .values(checked_guardrails=await guardrail_stamp(conn, self.story_id))
            )
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
        keyed = self._keyed()
        if keyed is not None and not control and current != base and not touched:
            # FR-CONC-12 (#136): a change since the base matching a query key touches the read
            # set; a base the change index no longer covers counts as touched.
            if base < int(locked.changes_pruned_through_revision):
                touched = ("changes_pruned",)
            else:
                touched = tuple(await keyed.touched(conn, self.story_id, base))
        config_changed: tuple[str, ...] = ()
        if not control and not touched and self.config_dependencies:
            current_config = await config_dependencies.capture(
                conn, self.story_id, self.config_dependencies, own_predicates=self.own_predicates
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
        if keyed is not None and isinstance(outcome, Rebase):
            # A structured turn's rebase re-runs its deterministic checks against current state
            # (#140's verify, #144's permission checks) and re-stamps its read set.
            if await readset.run_rebase_checks(conn, self):
                keyed.rebased_to = current
            else:
                outcome = Regenerate(reason="rebase_failed")
        record = attempt_record(state, outcome)
        if keyed is not None:
            record.update(keyed.counts())
        self.attempts.append(record)
        if isinstance(outcome, Regenerate | Abandon | ConfigRecheck):
            raise RevalidationRequired(outcome, state)

    def _rebase_failed(
        self, decide: Decide | None, current_revision: int, failed: RebaseCheckFailed
    ) -> None:
        """A commit step's deterministic check failed (#187, ``RebaseCheckFailed``): a
        revalidating turn regenerates (``rebase_failed``, within its budget); anything else
        fails with it."""
        if decide is None or self.kind is not OperationKind.TURN or not self.attempts:
            raise failed
        state = RevalidationState(
            attempt=len(self.attempts),
            base_revision=self.base_revision or 0,
            current_revision=current_revision,
            control_since_base=False,
            control_operation_id=None,
            touched=(),
        )
        outcome = Regenerate(reason="rebase_failed")
        self.attempts[-1] = attempt_record(state, outcome)
        raise RevalidationRequired(outcome, state) from failed

    async def _check_erased(
        self, conn: AsyncConnection, decide: Decide | None, current_revision: int
    ) -> None:
        """The resurrection guard (FR-ADMIN-9; #127, ``core.erasure.guard``): refuse events that
        name or reference a subject erased from the story since the context they were built
        from. It runs after #47's decision and the commit steps, on the final text. A
        revalidating turn regenerates instead (``ERASED_SUBJECT``, within its budget); an
        erasure's own operations are exempt."""
        if self.story_id is None or self.erasure or not self._staged:
            return
        try:
            await erasure_guard.check_content(
                conn,
                self.story_id,
                [
                    erasure_guard.staged_content(
                        staged.payload.model_dump(mode="json"),
                        staged.character_id,
                        staged.provenance,
                    )
                    for staged in self._staged
                ],
                base_revision=self.base_revision,
            )
        except ErasedSubjectReferenced as refusal:
            if decide is None or self.kind is not OperationKind.TURN or not self.attempts:
                raise
            state = RevalidationState(
                attempt=len(self.attempts),
                base_revision=self.base_revision or 0,
                current_revision=current_revision,
                control_since_base=False,
                control_operation_id=None,
                touched=(),
            )
            outcome = Regenerate(reason=ERASED_SUBJECT)
            self.attempts[-1] = attempt_record(state, outcome)
            raise RevalidationRequired(outcome, state) from refusal

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
        FR-STORE-12/28) in the same transaction, and their derived records with them (#130)."""
        retired = await conn.execute(
            update(operations)
            .where(
                operations.c.story_id == self.story_id,
                operations.c.narrative_turn >= self.narrative_turn,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.ACTIVE.value,
            )
            .values(membership=Membership.DORMANT.value, membership_tx=func.transaction_timestamp())
            .returning(operations.c.operation_id)
        )
        await self._derived_follow(conn, retired.scalars().all())

    async def _shelve_discarded(self, conn: AsyncConnection) -> None:
        """A rewound tail becomes a dormant continuation at the first turn written over it
        (#66): the still-``discarded`` operations at or after this turn's index go ``dormant``.
        The transcript projection makes the same change from the turn's events; the derived
        records of those operations follow them (#130)."""
        shelved = await conn.execute(
            update(operations)
            .where(
                operations.c.story_id == self.story_id,
                operations.c.narrative_turn >= self.narrative_turn,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.DISCARDED.value,
            )
            .values(membership=Membership.DORMANT.value, membership_tx=func.transaction_timestamp())
            .returning(operations.c.operation_id)
        )
        await self._derived_follow(conn, shelved.scalars().all())

    async def _derived_follow(self, conn: AsyncConnection, operation_ids: Sequence[str]) -> None:
        """The derived records of operations this commit made ``dormant`` follow them (#130):
        these changes run no ``core.timeline`` membership hook."""
        await follow(conn, operation_ids, Membership.DORMANT, story_id=self.story_id)
        self._followed.extend(operation_ids)

    async def _check_chain(self, conn: AsyncConnection) -> None:
        """The active turn variants must be one chain: turns 1..k, one variant each, and the
        story's next turn index k+1 (FR-STORE-28, TIME (f)). Raises ``ChainBroken``.

        The head check (#281) decides it without reading every active turn: given a chain that
        was valid before this commit, the only way this commit can have changed it is at the head.
        The partial unique index ``uq_operations_active_turn`` keeps one active variant per turn,
        so the head check reads the lowest and the highest active turn and the story's next turn
        index. For a turn it also reads the active variant of the turn before it, which must be
        this turn's ``parent_variant``. Each of these is one index lookup. The full check
        (``chain_violation``) runs instead for a control action, and for a story whose
        ``chain_unchecked`` flag says its active turn set changed some other way, through a
        commit step, a fork's copy or SQL run outside an operation; a passing full check clears
        the flag. When the head check fails, the full check runs, so the refusal and its details
        are always the full check's."""
        assert self.story_id is not None  # noqa: S101 - only a story's commit checks its chain
        probe = (await conn.execute(self._head_probe())).one()
        if not self.control_action and not probe.chain_unchecked and self._head_holds(probe):
            return
        violation = await chain_violation(conn, self.story_id)
        if violation is not None:
            raise ChainBroken("the active turn variants would not form a single chain", violation)
        if probe.chain_unchecked:
            await conn.execute(
                update(stories).where(stories.c.id == self.story_id).values(chain_unchecked=False)
            )

    def _head_probe(self) -> Select[Any]:
        """One statement, index lookups only: the story's next turn index and change marker,
        its lowest and highest active turn, and (for a turn after turn 1) the active variant of
        the turn before this one."""
        turn = self.narrative_turn if self.kind is OperationKind.TURN else None
        parent = (
            select(operations.c.turn_variant)
            .where(_active_turns(self.story_id), operations.c.narrative_turn == turn - 1)
            .scalar_subquery()
            if turn is not None and turn > 1
            else literal(None, Integer)
        )
        return select(
            stories.c.next_turn_index,
            stories.c.chain_unchecked,
            select(func.min(operations.c.narrative_turn))
            .where(_active_turns(self.story_id))
            .scalar_subquery()
            .label("lowest"),
            select(func.max(operations.c.narrative_turn))
            .where(_active_turns(self.story_id))
            .scalar_subquery()
            .label("highest"),
            parent.label("parent"),
        ).where(stories.c.id == self.story_id)

    def _head_holds(self, probe: Any) -> bool:
        """The full check's conditions, at the head of a chain that was valid before."""
        lowest = 1 if probe.lowest is None else int(probe.lowest)
        highest = 0 if probe.highest is None else int(probe.highest)
        if lowest != 1 or probe.next_turn_index != highest + 1:
            return False
        if self.kind is not OperationKind.TURN or self.narrative_turn is None:
            return True
        if self.narrative_turn <= 1:
            return True
        return probe.parent is not None and (
            self.parent_variant is None or probe.parent == self.parent_variant
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
                step=staged.step or 0,
                slot_k=self.slot_k,
            )
            for event_id, seq, staged in zip(ids, self.seqs, self._staged, strict=True)
        ]

    async def _position(
        self, conn: AsyncConnection, next_turn_index: int
    ) -> tuple[int | None, int | None, bool]:
        """(narrative_turn, turn_variant, advances the turn index) for this operation."""
        turn = self.requested_turn
        if self.slot:
            return await self._slot_position(conn, next_turn_index - 1)
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

    async def _story_minutes(self, conn: AsyncConnection) -> Callable[[StagedEvent], int | None]:
        """Each staged event's story-time (``events.valid_time_minutes``, #193): the story's
        ``core.world_clock`` basis at the event's position; ``None`` for a position-less
        operation's events and a deployment's."""
        if self.story_id is None or self.narrative_turn is None or not self._staged:
            return lambda _: None
        from epistrel.core import world_clock  # here: the clock reads States, which use this

        basis = await world_clock.time_basis(conn, self.story_id)
        turn, variant, slot = self.narrative_turn, self.turn_variant or 0, self.slot_k or 0

        def at(staged: StagedEvent) -> int:
            return basis.at(Position(turn, variant, slot, staged.beat or 0, staged.step or 0))

        return at

    async def _slot_position(
        self, conn: AsyncConnection, turn: int
    ) -> tuple[int | None, int | None, bool]:
        """An inter-turn slot's position (``self.slot``): after ``turn`` (0 before the first
        turn), on its active variant, in the next slot; sets ``slot_k``."""
        variant = 0
        if turn >= 1:
            active = await conn.scalar(
                select(func.max(operations.c.turn_variant)).where(
                    operations.c.story_id == self.story_id,
                    operations.c.narrative_turn == turn,
                    operations.c.kind == OperationKind.TURN.value,
                    operations.c.status == OperationStatus.COMMITTED.value,
                    operations.c.membership == Membership.ACTIVE.value,
                )
            )
            variant = int(active or 0)
        last = await conn.scalar(
            select(func.coalesce(func.max(operations.c.slot_k), 0)).where(
                operations.c.story_id == self.story_id,
                operations.c.narrative_turn == turn,
                operations.c.slot_k.is_not(None),
                operations.c.status == OperationStatus.COMMITTED.value,
            )
        )
        self.slot_k = int(last or 0) + 1
        return turn, variant, False

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
                slot_k=self.slot_k,
                parent_variant=self.parent_variant,
                membership_tx=now,
                committed_at=now,
                attempts=self.attempts,
                timeline_revision=self.timeline_revision,
                **({"stage_log": list(self._stages)} if self._stages else {}),
                **({"read_set": self._read_set_document()} if self._keyed() is not None else {}),
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

    async def _refuse_blocked(self, conn: AsyncConnection, status: str) -> None:
        """A story the erasure reconciler blocked (#125) takes no work at all, background jobs
        included (503 ``erasure_pending``), until its pending erasures are reapplied; an erasure
        runs, and so do the reapply's own operations. Checked at ``begin()`` and again under the
        story lock at commit."""
        if not self.erasure and self.story_id is not None:
            await erasure_ledger.refuse_blocked(conn, self.story_id, status)

    def _refuse_archived(self, status: str) -> None:
        """An archived story is read-only: work that changes story truth (bumps the revision)
        is refused with ``story_archived``; archive, unarchive, maintenance and derived work
        still run (#50), and so does an erasure (#124). Checked at ``begin()``, before the
        operations row is written, and again under the story lock at commit."""
        if self.bumps_revision and not self.erasure and status == StoryStatus.ARCHIVED.value:
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
                    .values(
                        attempts=self.attempts,
                        **(
                            {"read_set": self._read_set_document()}
                            if self._keyed() is not None
                            else {}
                        ),
                    )
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
                # A queued turn that ended keeps the slot until its drainer records the error
                # and drops its queue row (``fail_item``/``close_item``, one transaction):
                # a slot free while the row is there means its drainer died, and another
                # process's drainer would end the turn with ``turn_interrupted`` first (#436).
                ended_in_queue = self.queued and status is not OperationStatus.QUEUED
                if self.holds_turn_slot and self.story_id is not None and not ended_in_queue:
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
