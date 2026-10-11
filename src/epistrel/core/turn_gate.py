"""Same-story turn concurrency: the turn slot, its lease, and the per-story queue (FR-CONC-5;
#63).

Every story has one **turn slot**: ``stories.in_flight_turn`` names the turn holding it and
``stories.in_flight_lease_until`` its lease. A turn takes the slot in its own short transaction
before its operation begins (``acquire``) and gives it back inside the transaction that commits
or fails it (``Operation`` calls ``release``). No database lock is held across model calls
(FR-CONC-2): the slot is a durable marker, and a lease that ran out (a crash) is reclaimed by
the next request, which marks the dead turn ``failed`` (or, for a queued turn, puts it back in
the queue; after ``MAX_RUNS`` interrupted runs it fails). The lease is ``turn_deadline`` +
``LEASE_SLACK_S``, renewed by the running turn (``renewing``), so a live turn never loses it.

``turn_concurrency`` (a ``stories`` column, prospective) decides what a second turn gets:

- ``reject`` (the default): 423 ``story_busy`` ``{reason: in_flight, operation_id,
  queue_depth}`` with ``Retry-After`` (the lease's seconds left), with nothing written;
- ``queue``: a ``turn_queue`` row and an ``operations`` row with status ``queued``
  (``enqueue``), drained in order by the Orchestrator (``orchestrator.queue_drainer``), each
  starting only after the previous one committed or failed. More than ``turn_queue_max``
  waiting turns is 423 ``story_busy`` ``{reason: queue_full}``. A newcomer never overtakes a
  queued turn, under either setting (``queue`` → ``reject`` drains the backlog first).

Different stories never contend: every row this module touches is the story's own.

A request waiting on a queued turn learns its outcome from the operation (``outcome``): a
commit leaves the result under the request's ``Idempotency-Key`` (#61), and a failure leaves
``operations.error_code``/``error``. In-process waiters and drainers are woken at once
(``wakeups``), others poll (``POLL_S``); every release also sends ``NOTIFY epistrel_turns``
for #70's worker loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, insert, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind, OperationStatus
from epistrel.core.errors import EpistrelError, StoryBusy, StoryNotFound
from epistrel.core.idempotency import QUEUE_OWNER_PREFIX
from epistrel.core.settings_schema import (
    DEFAULT_TURN_QUEUE_MAX,
    StorySettings,
    TurnBudgetDefaults,
    TurnConcurrency,
    TurnQueueDefaults,
)
from epistrel.core.tables import deployment_state, idempotency_keys, operations, stories, turn_queue
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

logger = logging.getLogger(__name__)

LEASE_SLACK_S = 10.0
"""The turn slot's lease is the story's ``turn_deadline`` plus this."""
lease_override_s: float | None = None
"""Fault tests set a short lease here (``epistrel.testing.faults``); ``None`` in production."""
MAX_RUNS = 3
"""How many times a crash may interrupt a queued turn before it fails."""
POLL_S = 0.1
"""How often a waiter or drainer re-reads the database when nothing woke it (other processes)."""
NOTIFY_CHANNEL = "epistrel_turns"
RESPOND = "respond"
INGEST = "ingest"


def queue_owner(operation_id: str) -> str:
    """The owner token a queued turn's ``Idempotency-Key`` is handed to."""
    return f"{QUEUE_OWNER_PREFIX}{operation_id}"


def lease_seconds(turn_deadline: float) -> float:
    return lease_override_s if lease_override_s is not None else turn_deadline + LEASE_SLACK_S


# -- in-process wake-ups ---------------------------------------------------------------------


class Wakeups:
    """Futures waiting for a story's slot or queue to move, woken by this process's commits."""

    def __init__(self) -> None:
        self._waiting: dict[str, set[asyncio.Future[None]]] = {}

    async def wait(self, story_id: str, within: float) -> None:
        """Return when ``wake(story_id)`` is called or ``within`` seconds pass."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(story_id, set()).add(future)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(future, within)
        finally:
            waiting = self._waiting.get(story_id)
            if waiting is not None:
                waiting.discard(future)
                if not waiting:
                    self._waiting.pop(story_id, None)

    def wake(self, story_id: str) -> None:
        for future in self._waiting.pop(story_id, set()):
            if not future.done():
                with contextlib.suppress(RuntimeError):  # its loop has closed
                    future.get_loop().call_soon_threadsafe(_resolve, future)


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


wakeups = Wakeups()


# -- the story's concurrency settings ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Concurrency:
    """What a story's next turn runs under, read when the request starts (prospective)."""

    mode: TurnConcurrency
    queue_max: int
    turn_deadline: float
    max_model_calls_per_turn: int | None = None
    """The story's own call cap (#69; ``None``: the deployment's, else the Lite formula)."""
    deployment_max_calls: int | None = None
    """The deployment's default call cap (#69; ``None``: the Lite formula)."""

    @property
    def lease_s(self) -> float:
        return lease_seconds(self.turn_deadline)


def turn_queue_defaults(document: Mapping[str, Any] | None) -> TurnQueueDefaults:
    """The deployment's ``turn_queue_max`` in a ``deployment_state.settings`` document; an
    invalid stored value falls back to the default (a warning names only the key)."""
    values = {k: v for k, v in (document or {}).items() if k in TurnQueueDefaults.model_fields}
    try:
        return TurnQueueDefaults.model_validate(values)
    except ValidationError:
        logger.warning("invalid turn queue defaults %s; using the defaults", sorted(values))
        return TurnQueueDefaults()


def turn_budget_defaults(document: Mapping[str, Any] | None) -> TurnBudgetDefaults:
    """The deployment's ``max_model_calls_per_turn`` (#69) in a ``deployment_state.settings``
    document; an invalid stored value falls back to the default (a warning names only the key)."""
    values = {k: v for k, v in (document or {}).items() if k in TurnBudgetDefaults.model_fields}
    try:
        return TurnBudgetDefaults.model_validate(values)
    except ValidationError:
        logger.warning("invalid turn budget defaults %s; using the defaults", sorted(values))
        return TurnBudgetDefaults()


async def read_concurrency(db: AsyncEngine | AsyncConnection, story_id: str) -> Concurrency:
    """The story's ``turn_concurrency``, effective ``turn_queue_max``, ``turn_deadline`` and
    call caps (#69); 404 for an unknown story."""
    query = select(
        stories.c.settings,
        stories.c.turn_concurrency,
        stories.c.turn_queue_max,
        select(deployment_state.c.settings)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .scalar_subquery()
        .label("deployment"),
    ).where(stories.c.id == story_id)
    if isinstance(db, AsyncConnection):
        row = (await db.execute(query)).one_or_none()
    else:
        async with db.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    settings = StorySettings.from_row(
        row.settings, turn_concurrency=row.turn_concurrency, turn_queue_max=row.turn_queue_max
    )
    queue_max = settings.turn_queue_max or turn_queue_defaults(row.deployment).turn_queue_max
    return Concurrency(
        mode=settings.turn_concurrency,
        queue_max=queue_max or DEFAULT_TURN_QUEUE_MAX,
        turn_deadline=settings.turn_deadline,
        max_model_calls_per_turn=settings.max_model_calls_per_turn,
        deployment_max_calls=turn_budget_defaults(row.deployment).max_model_calls_per_turn,
    )


# -- the slot --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Held:
    """The slot is taken (or turns are queued ahead): who holds it and for how long."""

    operation_id: str | None
    origin: str | None
    """The holder's ``operations.origin`` (``generated`` or ``ingested``)."""
    retry_after: int
    queue_depth: int

    def busy(self, story_id: str) -> StoryBusy:
        """423 ``story_busy`` for a turn refused under ``reject``."""
        return StoryBusy(
            "a turn of the story is in flight; retry when it finishes",
            {
                "reason": "in_flight",
                "operation_id": self.operation_id,
                "queue_depth": self.queue_depth,
                "story_id": story_id,
            },
            retry_after=self.retry_after,
        )


async def _lock_story(conn: AsyncConnection, story_id: str) -> Any:
    row = (
        await conn.execute(
            select(
                stories.c.in_flight_turn,
                stories.c.in_flight_lease_until,
                func.clock_timestamp().label("now"),
            )
            .where(stories.c.id == story_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return row


async def _depth(conn: AsyncConnection, story_id: str) -> int:
    return int(
        await conn.scalar(
            select(func.count()).select_from(turn_queue).where(turn_queue.c.story_id == story_id)
        )
        or 0
    )


async def acquire(
    db: AsyncEngine, story_id: str, operation_id: str, *, lease_s: float, newcomer: bool = True
) -> Held | None:
    """Take the story's turn slot for ``operation_id`` (``None``), or say who holds it.

    One short transaction under the story row lock. A holder whose lease ran out is reclaimed
    first (``_reclaim``). A ``newcomer`` (any request, as opposed to the queue's drainer
    starting the head of the queue) does not take a free slot while turns are queued."""
    async with db.begin() as conn:
        row = await _lock_story(conn, story_id)
        holder = row.in_flight_turn
        if holder is not None and row.in_flight_lease_until > row.now:
            remaining = (row.in_flight_lease_until - row.now).total_seconds()
            origin = await conn.scalar(
                select(operations.c.origin).where(operations.c.operation_id == holder)
            )
            return Held(holder, origin, max(1, math.ceil(remaining)), await _depth(conn, story_id))
        if holder is not None:
            await _reclaim(conn, story_id, holder)
        if newcomer:
            depth = await _depth(conn, story_id)
            if depth:
                return Held(None, None, 1, depth)
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(
                in_flight_turn=operation_id,
                in_flight_lease_until=func.clock_timestamp() + _interval(lease_s),
            )
        )
    return None


def _interval(seconds: float) -> Any:
    return literal_column(f"make_interval(secs => {float(seconds)!r})")


async def _reclaim(conn: AsyncConnection, story_id: str, holder: str) -> None:
    """The holder's lease ran out (its process died, or it hung): a queued turn goes back to
    the queue (``MAX_RUNS`` interrupted runs fail it); any other turn is marked ``failed``. A
    late commit of the dead turn is refused (its operation is no longer ``in_flight``)."""
    queued = (
        await conn.execute(
            select(turn_queue.c.attempts)
            .where(turn_queue.c.operation_id == holder)
            .with_for_update()
        )
    ).one_or_none()
    logger.warning("turn slot of a story reclaimed from an expired lease (%s)", holder)
    if queued is not None and int(queued.attempts) + 1 < MAX_RUNS:
        await conn.execute(
            update(turn_queue)
            .where(turn_queue.c.operation_id == holder)
            .values(attempts=turn_queue.c.attempts + 1)
        )
        await conn.execute(
            update(operations)
            .where(
                operations.c.operation_id == holder,
                operations.c.status == OperationStatus.IN_FLIGHT.value,
            )
            .values(status=OperationStatus.QUEUED.value)
        )
    else:
        error = TurnInterrupted(
            "the turn was interrupted (its engine stopped) and did not complete",
            {"operation_id": holder},
        )
        await _fail(conn, holder, error, statuses=(OperationStatus.IN_FLIGHT,))
        if queued is not None:
            await _forget_item(conn, holder)
    await release(conn, story_id, holder)


async def reclaim(conn: AsyncConnection, story_id: str, holder: str) -> None:
    """Reclaim ``holder``'s expired slot (the caller holds the story row lock; #75's
    start-up sweep, ``core.orphans``)."""
    await _reclaim(conn, story_id, holder)


class TurnInterrupted(EpistrelError):
    """A turn's engine stopped while it ran (its lease ran out); nothing of it was recorded."""

    code = "turn_interrupted"
    http_status = 503


async def release(conn: AsyncConnection, story_id: str, operation_id: str) -> None:
    """Give the slot back if ``operation_id`` holds it (in the caller's transaction)."""
    await conn.execute(
        update(stories)
        .where(stories.c.id == story_id, stories.c.in_flight_turn == operation_id)
        .values(in_flight_turn=None, in_flight_lease_until=None)
    )
    await conn.execute(select(func.pg_notify(NOTIFY_CHANNEL, story_id)))


async def release_now(db: AsyncEngine, story_id: str, operation_id: str) -> None:
    """``release`` in its own transaction (a turn that took the slot but never began)."""
    try:
        async with db.begin() as conn:
            await release(conn, story_id, operation_id)
    finally:
        wakeups.wake(story_id)


@contextlib.asynccontextmanager
async def renewing(
    db: AsyncEngine, story_id: str, operation_id: str, lease_s: float
) -> AsyncIterator[None]:
    """Renew the slot's lease every third of it while the block runs."""

    async def renew() -> None:
        while True:
            await asyncio.sleep(lease_s / 3)
            try:
                async with db.begin() as conn:
                    await conn.execute(
                        update(stories)
                        .where(stories.c.id == story_id, stories.c.in_flight_turn == operation_id)
                        .values(in_flight_lease_until=func.clock_timestamp() + _interval(lease_s))
                    )
            except Exception as exc:  # pragma: no cover - the database went away; retry later
                logger.warning("could not renew a turn lease: %s", type(exc).__name__)

    task = asyncio.create_task(renew())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# -- the queue -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One queued turn as the drainer runs it."""

    story_id: str
    position: int
    operation_id: str
    kind: str
    request: dict[str, Any]
    principal: str
    idempotency_key: str | None
    attempts: int


class TurnAccepted(Exception):
    """The turn is queued and the caller is not waiting for it (``Prefer: respond-async``, or
    the long-poll reached ``turn_deadline``): REST answers 202 ``{operation_id, position}`` with
    ``Location: /operations/{id}``. ``position`` is the number of turns ahead of it. The turn
    stays queued; a retry under the same ``Idempotency-Key`` waits for it again."""

    def __init__(self, *, operation_id: str, position: int, key: str | None) -> None:
        super().__init__(f"turn {operation_id} is queued ({position} ahead)")
        self.operation_id = operation_id
        self.position = position
        self.key = key

    def body(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "position": self.position}


async def enqueue(
    db: AsyncEngine,
    story_id: str,
    *,
    operation_id: str,
    kind: str,
    request: dict[str, Any],
    principal: str,
    idempotency_key: str | None,
    viewpoint: str | None,
    origin: str,
    queue_max: int,
    hand_over: Any = None,
) -> int:
    """Queue a turn; return its position (turns ahead of it).

    One transaction under the story row lock: 423 ``story_busy`` ``{reason: queue_full}``
    when ``queue_max`` turns already wait; else the ``operations`` row (``queued``, the
    request's key and viewpoint) and the ``turn_queue`` row, and ``hand_over(conn, owner)``
    hands the request's ``Idempotency-Key`` to the queue (#61) in the same transaction."""
    async with db.begin() as conn:
        row = await _lock_story(conn, story_id)
        depth = await _depth(conn, story_id)
        if depth >= queue_max:
            held = row.in_flight_turn is not None and row.in_flight_lease_until > row.now
            remaining = (row.in_flight_lease_until - row.now).total_seconds() if held else 1.0
            raise StoryBusy(
                "the story's turn queue is full; retry later",
                {
                    "reason": "queue_full",
                    "operation_id": row.in_flight_turn if held else None,
                    "queue_depth": depth,
                    "story_id": story_id,
                },
                retry_after=max(1, math.ceil(remaining)),
            )
        await conn.execute(
            insert(operations).values(
                operation_id=operation_id,
                story_id=story_id,
                kind=OperationKind.TURN.value,
                principal=principal,
                status=OperationStatus.QUEUED.value,
                idempotency_key=idempotency_key,
                viewpoint=viewpoint,
                origin=origin,
            )
        )
        await conn.execute(
            insert(turn_queue).values(
                story_id=story_id,
                operation_id=operation_id,
                kind=kind,
                request=request,
                principal=principal,
                idempotency_key=idempotency_key,
            )
        )
        if hand_over is not None:
            await hand_over(conn, queue_owner(operation_id))
        position = depth + (1 if row.in_flight_turn is not None else 0)
        await conn.execute(select(func.pg_notify(NOTIFY_CHANNEL, story_id)))
    return position


async def head(db: AsyncEngine, story_id: str) -> QueueItem | None:
    """The story's first queued turn, if any."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(turn_queue)
                .where(turn_queue.c.story_id == story_id)
                .order_by(turn_queue.c.position)
                .limit(1)
            )
        ).one_or_none()
    if row is None:
        return None
    return QueueItem(
        story_id=row.story_id,
        position=int(row.position),
        operation_id=row.operation_id,
        kind=row.kind,
        request=dict(row.request),
        principal=row.principal,
        idempotency_key=row.idempotency_key,
        attempts=int(row.attempts),
    )


async def queued_stories(db: AsyncEngine) -> list[str]:
    """Every story with queued turns (the recovery pass drains them)."""
    async with db.connect() as conn:
        found = await conn.execute(select(turn_queue.c.story_id).distinct())
        return [r.story_id for r in found]


async def _forget_item(conn: AsyncConnection, operation_id: str) -> None:
    """Drop a queued turn that ended without a commit: its queue row, and its key's claim
    unless a commit was bound to it (failures are never stored, #61)."""
    await conn.execute(delete(turn_queue).where(turn_queue.c.operation_id == operation_id))
    await conn.execute(
        delete(idempotency_keys).where(
            idempotency_keys.c.owner_token == queue_owner(operation_id),
            idempotency_keys.c.operation_id.is_(None),
        )
    )


def error_record(error: BaseException) -> tuple[str, dict[str, Any]]:
    """``(error_code, error)`` for a failed queued turn: what its waiters answer with."""
    if isinstance(error, EpistrelError):
        return error.code, {
            "status": error.http_status,
            "message": error.message,
            "details": error.details,
        }
    return "internal_error", {
        "status": 500,
        "message": "the queued turn failed",
        "details": {"error": type(error).__name__},
    }


async def _fail(
    conn: AsyncConnection,
    operation_id: str,
    error: BaseException,
    *,
    statuses: tuple[OperationStatus, ...] = (OperationStatus.QUEUED, OperationStatus.IN_FLIGHT),
) -> None:
    code, record = error_record(error)
    await conn.execute(
        update(operations)
        .where(
            operations.c.operation_id == operation_id,
            operations.c.status.in_([s.value for s in statuses]),
        )
        .values(status=OperationStatus.FAILED.value)
    )
    await conn.execute(
        update(operations)
        .where(
            operations.c.operation_id == operation_id,
            operations.c.status.in_(
                [OperationStatus.FAILED.value, OperationStatus.ABANDONED.value]
            ),
            operations.c.error_code.is_(None),
        )
        .values(error_code=code, error=record)
    )


async def fail_item(
    db: AsyncEngine, story_id: str, operation_id: str, error: BaseException
) -> None:
    """A queued turn failed (before or after its operation began): the operation ends
    ``failed`` (an ``abandoned`` one stays so) with the error its waiters answer with, and its
    queue row and slot go, in one transaction."""
    try:
        async with db.begin() as conn:
            await _fail(conn, operation_id, error)
            await _forget_item(conn, operation_id)
            await release(conn, story_id, operation_id)
    finally:
        wakeups.wake(story_id)


async def fail_queued(conn: AsyncConnection, story_id: str, error: BaseException) -> list[str]:
    """Fail every turn still waiting in the story's queue with ``error``, inside the caller's
    transaction (a truncation, #65: the turns were asked for against a timeline that is gone).
    A turn the drainer already started is left to its own revalidation. Returns the failed
    operations' ids; the caller wakes the story's waiters after its commit."""
    waiting = list(
        await conn.scalars(
            select(turn_queue.c.operation_id)
            .join(operations, operations.c.operation_id == turn_queue.c.operation_id)
            .where(
                turn_queue.c.story_id == story_id,
                operations.c.status == OperationStatus.QUEUED.value,
            )
            .order_by(turn_queue.c.position)
        )
    )
    for operation_id in waiting:
        await _fail(conn, operation_id, error, statuses=(OperationStatus.QUEUED,))
        await _forget_item(conn, operation_id)
    return waiting


async def close_item(db: AsyncEngine, story_id: str, operation_id: str) -> None:
    """A queued turn finished: its queue row and slot go (both already went if it committed).
    One that finished without committing anything (an ingest whose every event was a replay)
    ends ``abandoned``."""
    try:
        async with db.begin() as conn:
            await conn.execute(
                update(operations)
                .where(
                    operations.c.operation_id == operation_id,
                    operations.c.status.in_(
                        [OperationStatus.QUEUED.value, OperationStatus.IN_FLIGHT.value]
                    ),
                )
                .values(status=OperationStatus.ABANDONED.value)
            )
            await conn.execute(delete(turn_queue).where(turn_queue.c.operation_id == operation_id))
            await release(conn, story_id, operation_id)
    finally:
        wakeups.wake(story_id)


# -- waiting for a queued turn -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """Where a queued turn is: still queued or running (``done`` false, with ``position``), or
    finished with its operation's status and, for a failure, the recorded error."""

    done: bool
    status: str | None
    position: int
    error_code: str | None = None
    error: dict[str, Any] | None = None


async def outcome(db: AsyncEngine, story_id: str, operation_id: str) -> Outcome:
    async with db.connect() as conn:
        op = (
            await conn.execute(
                select(operations.c.status, operations.c.error_code, operations.c.error).where(
                    operations.c.operation_id == operation_id
                )
            )
        ).one_or_none()
        if op is None:  # the story was deleted with its queue
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        mine = await conn.scalar(
            select(turn_queue.c.position).where(turn_queue.c.operation_id == operation_id)
        )
        if mine is None and op.status not in (
            OperationStatus.QUEUED.value,
            OperationStatus.IN_FLIGHT.value,
        ):
            return Outcome(True, op.status, 0, op.error_code, op.error)
        position = 0
        if mine is not None:
            ahead = int(
                await conn.scalar(
                    select(func.count())
                    .select_from(turn_queue)
                    .where(turn_queue.c.story_id == story_id, turn_queue.c.position < mine)
                )
                or 0
            )
            holder = await conn.scalar(
                select(stories.c.in_flight_turn).where(stories.c.id == story_id)
            )
            position = ahead + (1 if holder is not None and holder != operation_id else 0)
    return Outcome(False, op.status, position)


def stored_error(record: Outcome) -> EpistrelError:
    """The error a failed queued turn's waiters answer with (the one the turn raised)."""
    error = record.error or {}
    code = record.error_code or "internal_error"
    status = int(error.get("status", 500))
    cls = type("QueuedTurnError", (EpistrelError,), {"code": code, "http_status": status})
    failure: EpistrelError = cls(
        str(error.get("message", "the queued turn failed")), dict(error.get("details") or {})
    )
    return failure


__all__ = [
    "INGEST",
    "LEASE_SLACK_S",
    "MAX_RUNS",
    "NOTIFY_CHANNEL",
    "POLL_S",
    "QUEUE_OWNER_PREFIX",
    "RESPOND",
    "Concurrency",
    "Held",
    "Outcome",
    "QueueItem",
    "TurnAccepted",
    "TurnInterrupted",
    "Wakeups",
    "acquire",
    "close_item",
    "enqueue",
    "error_record",
    "fail_item",
    "fail_queued",
    "head",
    "lease_seconds",
    "outcome",
    "queue_owner",
    "queued_stories",
    "read_concurrency",
    "reclaim",
    "release",
    "release_now",
    "renewing",
    "stored_error",
    "turn_queue_defaults",
    "wakeups",
]
