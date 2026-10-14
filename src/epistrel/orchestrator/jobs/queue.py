"""The durable job queue: enqueue, claim, heartbeat, reap, and the fenced commit (NFR-REL-5,
FR-CONC-8; #70). Postgres only — no broker (invariant 5).

- ``enqueue`` writes a ``jobs`` row in the caller's transaction and sends
  ``NOTIFY epistrel_jobs, '<kind>'`` (delivered when that transaction commits). The payload is
  validated against the kind's model first. A ``coalesce_key`` already held by a ``queued``
  row returns that job (keeping the earlier ``due_at``).
- ``claim`` is one statement: ``UPDATE jobs SET status = 'running', lease_until = now + lease,
  attempt = attempt + 1 WHERE id = (SELECT … WHERE status = 'queued' AND due_at <= now ORDER BY
  due_at FOR UPDATE SKIP LOCKED LIMIT 1)``. ``attempt`` is the claim's fencing token.
- ``heartbeat`` renews the lease only while the token still matches; ``False`` means the claim
  was fenced (the worker cancels its handler).
- ``reap`` sends every running job whose lease has lapsed back to ``queued`` and advances its
  ``attempt`` (fencing the lost claim); every worker runs it each poll tick and in its start-up
  recovery pass. A reclaim does not count as a failure.
- ``commit_job`` commits a handler's output as one ``maintenance``/``consolidation`` operation
  (story-less jobs: a story-less ``maintenance`` operation), which bumps no revision, plus any
  ``positioned`` operations of the same kind (#72's summary segments) in the same transaction.
  Its commit transaction, under the story lock, first checks the job row still carries the
  claim's token (else the commit is **refused**: ``REFUSED``), then rechecks every declared input
  (``core.input_versions``): one that is gone, inactive or changed drops the output (nothing is
  written, the operation ends ``abandoned``) and puts the same row back to ``queued`` due now
  (``DROPPED``; not a failure). Otherwise the output is written and the job is ``done``. A
  handler that records no operation may instead continue later (``JobOutput.requeue_in``): its
  row goes back to ``queued`` under the same fence, and ``hold`` fences each of the handler's
  own transactions (#117).
- ``fail_job`` records a failed attempt: retried with backoff ``min(5·2^n, 300)`` s ± 20 %
  until ``job_max_attempts`` failures, then ``failed`` with the exception class (never its
  message) as ``result``.

Every time here is ``core.clock.now()``, so tests move leases and due times without sleeping.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Row, and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.events.operation import LOCK_TIMEOUT, Operation, Withdrawn
from epistrel.core.ids import new_ulid
from epistrel.core.input_versions import stale_inputs
from epistrel.core.tables import jobs
from epistrel.orchestrator.jobs import handlers
from epistrel.orchestrator.jobs.types import ClaimedJob, JobHandler, JobOutput

logger = logging.getLogger(__name__)

NOTIFY_CHANNEL = "epistrel_jobs"
"""The channel ``enqueue`` notifies and workers ``LISTEN`` on (payload: the job's kind)."""

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 300.0
BACKOFF_JITTER = 0.2


class UnknownJobKind(LookupError):
    """No handler is registered for the job's kind."""


class Fenced(Withdrawn):
    """The job row no longer carries this claim's ``attempt``: the commit is refused."""


class StaleInputs(Withdrawn):
    """A declared input is gone, inactive or changed: the output is dropped (FR-CONC-8)."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        super().__init__(f"{len(keys)} input(s) changed")
        self.keys = keys


class CommitOutcome(StrEnum):
    COMMITTED = "committed"
    DROPPED = "dropped"
    REFUSED = "refused"


# -- enqueue ---------------------------------------------------------------------------------


def _handler(kind: str) -> JobHandler:
    handler = handlers.lookup(kind)
    if handler is None:
        raise UnknownJobKind(f"no handler is registered for job kind {kind!r}")
    return handler


async def enqueue(
    conn: AsyncConnection,
    kind: str,
    payload: BaseModel | Mapping[str, Any],
    *,
    due_at: datetime | None = None,
    story_id: str | None = None,
    coalesce_key: str | None = None,
) -> str:
    """Write a ``jobs`` row in ``conn``'s transaction and notify the workers; return its id.

    The payload is validated against the kind's model now (``UnknownJobKind`` or a pydantic
    ``ValidationError``). With ``coalesce_key``, a ``queued`` job holding the key is returned
    instead, its ``due_at`` moved to the earlier of the two.
    """
    handler = _handler(kind)
    data = handler.payload.model_validate(
        payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    ).model_dump(mode="json")
    now = clock.now()
    statement = insert(jobs).values(
        id=new_ulid(),
        kind=kind,
        story_id=story_id,
        payload=data,
        due_at=due_at or now,
        coalesce_key=coalesce_key,
        created_at=now,
        updated_at=now,
    )
    if coalesce_key is not None:
        statement = statement.on_conflict_do_update(
            index_elements=[jobs.c.coalesce_key],
            index_where=text("status = 'queued'"),
            set_={"due_at": func.least(jobs.c.due_at, statement.excluded.due_at)},
        )
    job_id = await conn.scalar(statement.returning(jobs.c.id))
    await conn.execute(select(func.pg_notify(NOTIFY_CHANNEL, kind)))
    assert job_id is not None  # noqa: S101 - an insert or upsert returns its row
    return str(job_id)


# -- claim, heartbeat, reap ------------------------------------------------------------------


def _claimed(row: Row[Any]) -> ClaimedJob:
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        story_id=row.story_id,
        payload=dict(row.payload or {}),
        attempt=int(row.attempt),
        failures=int(row.failures),
        due_at=row.due_at,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        coalesce_key=row.coalesce_key,
    )


async def claim(db: AsyncEngine, worker_id: str, *, lease_s: float) -> ClaimedJob | None:
    """Claim the earliest due ``queued`` job (``SKIP LOCKED``), or ``None`` when none is due."""
    now = clock.now()
    due = (
        select(jobs.c.id)
        .where(jobs.c.status == QUEUED, jobs.c.due_at <= now)
        .order_by(jobs.c.due_at, jobs.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    async with db.begin() as conn:
        row = (
            await conn.execute(
                update(jobs)
                .where(jobs.c.id == due)
                .values(
                    status=RUNNING,
                    lease_until=now + timedelta(seconds=lease_s),
                    attempt=jobs.c.attempt + 1,
                    worker_id=worker_id,
                    updated_at=now,
                )
                .returning(*jobs.c)
            )
        ).one_or_none()
    return _claimed(row) if row is not None else None


def _held(job: ClaimedJob) -> Any:
    """The rows this claim still holds: same id, same token, still running."""
    return and_(jobs.c.id == job.id, jobs.c.attempt == job.attempt, jobs.c.status == RUNNING)


async def hold(conn: AsyncConnection, job: ClaimedJob) -> None:
    """In ``conn``'s transaction, lock the job's row and check that this claim still holds it
    (``Fenced`` otherwise): the fence a handler that commits its own work in several
    transactions puts first in each (#117's offboarding)."""
    token = await conn.scalar(select(jobs.c.attempt).where(_held(job)).with_for_update())
    if token is None:
        raise Fenced(f"job {job.id} attempt {job.attempt} was fenced")


async def heartbeat(db: AsyncEngine, job: ClaimedJob, *, lease_s: float) -> bool:
    """Renew the claim's lease; ``False`` when it was fenced (reclaimed, finished, or gone)."""
    now = clock.now()
    async with db.begin() as conn:
        renewed = await conn.scalar(
            update(jobs)
            .where(_held(job))
            .values(lease_until=now + timedelta(seconds=lease_s), updated_at=now)
            .returning(jobs.c.id)
        )
    return renewed is not None


async def _back_to_queue(
    conn: AsyncConnection, job_id: str, key: str | None, values: Mapping[str, Any]
) -> str:
    """Put a locked running row back to ``queued`` with ``values``. When another ``queued`` row
    already holds its ``coalesce_key`` that row will do the work, so this one ends ``done``
    (``result.coalesced_into``). Returns the row's new status."""
    now = clock.now()
    if key is not None:
        sibling = await conn.scalar(
            select(jobs.c.id).where(
                jobs.c.coalesce_key == key, jobs.c.status == QUEUED, jobs.c.id != job_id
            )
        )
        if sibling is not None:
            await conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status=DONE,
                    result={"coalesced_into": sibling},
                    lease_until=None,
                    updated_at=now,
                )
            )
            return DONE
    await conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .values(status=QUEUED, lease_until=None, worker_id=None, updated_at=now, **values)
    )
    return QUEUED


async def reap(db: AsyncEngine) -> list[str]:
    """Fence and requeue every running job whose lease has lapsed; returns their ids.

    Each lapsed claim is reclaimed exactly once: the row's ``attempt`` advances (so the lost
    claim's heartbeat and commit are refused) and it is ``queued`` again, due as before. Rows
    another worker is reaping right now are skipped (``SKIP LOCKED``)."""
    now = clock.now()
    reclaimed: list[str] = []
    async with db.begin() as conn:
        expired = (
            await conn.execute(
                select(jobs.c.id, jobs.c.coalesce_key)
                .where(jobs.c.status == RUNNING, jobs.c.lease_until < now)
                .order_by(jobs.c.id)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in expired:
            await _back_to_queue(conn, row.id, row.coalesce_key, {"attempt": jobs.c.attempt + 1})
            reclaimed.append(row.id)
    if reclaimed:
        logger.info("reclaimed %d job(s) whose lease lapsed", len(reclaimed))
    return reclaimed


async def next_due(db: AsyncEngine) -> datetime | None:
    """When the earliest ``queued`` job is due (``None`` when nothing is queued)."""
    async with db.connect() as conn:
        found: datetime | None = await conn.scalar(
            select(func.min(jobs.c.due_at)).where(jobs.c.status == QUEUED)
        )
    return found


# -- finishing a claim -----------------------------------------------------------------------


def backoff_s(failures: int, rng: random.Random | None = None) -> float:
    """Seconds before retry ``failures`` (0-based): ``min(5·2^n, 300)`` ± 20 % jitter."""
    base = min(BACKOFF_BASE_S * 2.0**failures, BACKOFF_CAP_S)
    jitter: float = (rng or random).uniform(-BACKOFF_JITTER, BACKOFF_JITTER)
    return base * (1 + jitter)


async def fail_job(
    db: AsyncEngine,
    job: ClaimedJob,
    error: str,
    *,
    max_attempts: int,
    terminal: bool = False,
) -> str | None:
    """Record a failed attempt (``error`` is an exception class name, never a message).

    Retries with backoff until ``max_attempts`` failures, then ``failed``; ``terminal`` fails
    at once (an unknown kind, a payload that no longer validates). Returns the row's new status,
    or ``None`` when the claim was fenced (nothing is written)."""
    now = clock.now()
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(jobs.c.failures, jobs.c.coalesce_key).where(_held(job)).with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None
        failures = int(row.failures) + 1
        if terminal or failures >= max_attempts:
            await conn.execute(
                update(jobs)
                .where(jobs.c.id == job.id)
                .values(
                    status=FAILED,
                    failures=failures,
                    result={"error": error},
                    lease_until=None,
                    updated_at=now,
                )
            )
            status = FAILED
        else:
            due = now + timedelta(seconds=backoff_s(int(row.failures)))
            status = await _back_to_queue(
                conn,
                job.id,
                row.coalesce_key,
                {"failures": failures, "due_at": due, "result": {"error": error}},
            )
    logger.info("job %s (%s) attempt failed: %s -> %s", job.id, job.kind, error, status)
    return status


async def commit_job(
    db: AsyncEngine,
    job: ClaimedJob,
    handler: JobHandler,
    output: JobOutput,
    input_versions: Mapping[str, Any],
) -> CommitOutcome:
    """Commit ``output`` under the claim's fencing token and the input recheck (see the module
    docstring). Raises whatever else the commit raised (the attempt then fails)."""
    declared = dict(input_versions)
    if not handler.records_operation:
        return await _finish_plain(db, job, output, declared)
    if output.positioned and job.story_id is None:
        raise ValueError(f"job kind {job.kind!r} has no story to position operations in")
    op = Operation(
        db,
        story_id=job.story_id,
        kind=handler.operation_kind,
        principal=SYSTEM_PRINCIPAL,
        source_window=output.source_window,
    )
    await op.begin()
    extra: list[Operation] = []
    try:
        for positioned in output.positioned:
            more = Operation(
                db,
                story_id=job.story_id,
                kind=handler.operation_kind,
                principal=SYSTEM_PRINCIPAL,
                turn=positioned.turn,
                operation_id=positioned.operation_id,
                source_window=positioned.source_window,
            )
            await more.begin()
            extra.append(more)
    except BaseException as exc:
        for begun in (op, *extra):
            await begun.settle(exc)
        raise

    async def guard(conn: AsyncConnection) -> None:
        token = await conn.scalar(select(jobs.c.attempt).where(_held(job)).with_for_update())
        if token is None:
            raise Fenced(f"job {job.id} attempt {job.attempt} was fenced")
        stale = await stale_inputs(conn, declared)
        if stale:
            raise StaleInputs(stale)

    async def finish(conn: AsyncConnection) -> None:
        result = dict(output.result or {})
        if output.result_of is not None:
            result.update(output.result_of())
        result["operation_id"] = op.operation_id
        await conn.execute(
            update(jobs)
            .where(_held(job))
            .values(
                status=DONE,
                result=result,
                input_versions=declared,
                lease_until=None,
                updated_at=clock.now(),
            )
        )

    error: BaseException | None = None
    try:
        op.add_commit_step(guard)
        if output.stage is not None:
            output.stage(op)
        op.add_commit_step(finish)
        for more, positioned in zip(extra, output.positioned, strict=True):
            positioned.stage(more)
        async with db.connect() as conn, conn.begin():
            await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            await op.write_in(conn)
            for more in extra:
                await more.write_in(conn)
    except BaseException as exc:
        error = exc
    for written in (op, *extra):
        await written.settle(error)
    if error is None:
        return CommitOutcome.COMMITTED
    if isinstance(error, Fenced):
        logger.info("job %s (%s): commit refused, the claim was fenced", job.id, job.kind)
        return CommitOutcome.REFUSED
    if isinstance(error, StaleInputs):
        await _drop(db, job, declared)
        logger.info(
            "job %s (%s): %d input(s) changed; output dropped, job requeued",
            job.id,
            job.kind,
            len(error.keys),
        )
        return CommitOutcome.DROPPED
    raise error


async def _finish_plain(
    db: AsyncEngine, job: ClaimedJob, output: JobOutput, declared: Mapping[str, Any]
) -> CommitOutcome:
    """Finish a job whose handler records no operation (``records_operation=False``): ``done``
    under the claim's token, or ``REFUSED`` when it was fenced. Its ``stage`` must be empty."""
    if output.stage is not None:
        raise ValueError(f"job kind {job.kind!r} records no operation but staged output")
    result = dict(output.result or {})
    if output.result_of is not None:
        result.update(output.result_of())
    if output.requeue_in is not None:
        return await _requeue(db, job, result, declared, output.requeue_in)
    async with db.begin() as conn:
        done = await conn.scalar(
            update(jobs)
            .where(_held(job))
            .values(
                status=DONE,
                result=result,
                input_versions=dict(declared),
                lease_until=None,
                updated_at=clock.now(),
            )
            .returning(jobs.c.id)
        )
    if done is None:
        logger.info("job %s (%s): commit refused, the claim was fenced", job.id, job.kind)
        return CommitOutcome.REFUSED
    return CommitOutcome.COMMITTED


async def _requeue(
    db: AsyncEngine,
    job: ClaimedJob,
    result: Mapping[str, Any],
    declared: Mapping[str, Any],
    delay_s: float,
) -> CommitOutcome:
    """Put a held job back to ``queued``, due in ``delay_s`` (``JobOutput.requeue_in``):
    ``COMMITTED`` (the handler's work so far is its own), or ``REFUSED`` when the claim was
    fenced."""
    async with db.begin() as conn:
        key = (
            await conn.execute(select(jobs.c.coalesce_key).where(_held(job)).with_for_update())
        ).one_or_none()
        if key is None:
            logger.info("job %s (%s): requeue refused, the claim was fenced", job.id, job.kind)
            return CommitOutcome.REFUSED
        await _back_to_queue(
            conn,
            job.id,
            key.coalesce_key,
            {
                "due_at": clock.now() + timedelta(seconds=max(0.0, delay_s)),
                "result": dict(result),
                "input_versions": dict(declared),
            },
        )
    return CommitOutcome.COMMITTED


async def _drop(db: AsyncEngine, job: ClaimedJob, declared: Mapping[str, Any]) -> None:
    """Put the dropped job's row back to ``queued``, due now (fenced: a lost claim does not)."""
    async with db.begin() as conn:
        key = (
            await conn.execute(select(jobs.c.coalesce_key).where(_held(job)).with_for_update())
        ).one_or_none()
        if key is None:
            return
        await _back_to_queue(
            conn,
            job.id,
            key.coalesce_key,
            {"due_at": clock.now(), "input_versions": dict(declared)},
        )


__all__ = [
    "NOTIFY_CHANNEL",
    "CommitOutcome",
    "Fenced",
    "StaleInputs",
    "UnknownJobKind",
    "backoff_s",
    "claim",
    "commit_job",
    "enqueue",
    "fail_job",
    "heartbeat",
    "hold",
    "next_due",
    "reap",
]
