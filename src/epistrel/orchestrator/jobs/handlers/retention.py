"""The sweeps: ``retention_sweep`` (daily) and ``idempotency_purge`` (hourly) (FR-STORE-30,
FR-CONC-6, NFR-REL-5; #71).

**``retention_sweep``** deletes what may expire and nothing else:

- ``audit``-category events whose ``tx_time`` is strictly older than the story's
  ``audit_retention_days`` (its own setting, else the deployment's, default 90; FR-STORE-30),
  unless their operation is still running; and ``audit_log`` rows by their ``at`` the same way
  (a row whose story is gone uses the deployment's value). Retention expiry is the third
  exception to immutability (FR-STORE-1): the events trigger lets these deletes through only
  under ``epistrel.allow_audit_expiry``, set for each chunk's transaction, and they leave no
  marker. Discarded and dormant continuations are never touched: they are story history, kept
  until the story is deleted.
- finished (``done``/``failed``) ``jobs`` rows not updated for ``job_retention_days`` (7).
- finished principal offboardings (``offboarding_jobs``, #117) that ended more than
  ``offboarding_report_retention_days`` (90) ago: their status and report.
- deleted engine profiles that no story uses any more (FR-PROF-1; #106): each is purged with
  its versions and its relations, its ownership ``deleted`` event recorded by ``system``
  (``core.profiles.store.purge_deleted``). A deleted profile a story still uses is kept, and
  the story keeps running on its last version; a fork of that story uses it too, so the purge
  waits for the fork as well.
- message feedback past the deployment's ``feedback_retention`` when it is a number of days
  (FR-FB-2; #112): a rating by when it was first given (``created_at``), a history entry by when
  it was replaced (``replaced_at``). Feedback never follows the audit records' 90 days: under
  the default ``story_lifetime`` nothing expires, and a rating goes with its story. These are
  plain deletes of audit-category rows that no story view reads, so they leave no marker
  (FR-STORE-1's third exception). The rows are read here by their table, never through
  ``core.feedback``, which nothing in the Orchestrator imports (FR-FB-3: feedback is
  flag-only). Until #123 indexes a rating's text there are no subject-index rows to drop with
  it; #123 drops them here too.

No M2 operation kind writes only audit events, so no operation row is deleted. Every delete
runs in chunks of ``CHUNK`` rows, each its own transaction, at most ``MAX_CHUNKS`` per table and
run; the next run takes the rest. A sweep killed between chunks and run again deletes exactly
what one uninterrupted run would have: each chunk is independent and every predicate is a pure
function of the row and the cut-off.

**``idempotency_purge``** drops every stored result past its window (the row stays as the
tombstone: key, request hash, operation id; #61) and settles keys left ``in_flight`` by a dead
call whose lease lapsed more than one result window ago: a key whose operation committed
becomes a ``done`` tombstone, any other (no operation, or one that failed or was abandoned) is
deleted so the key is free; a key whose turn is still queued or running is left alone.

Results hold counts only, never story text.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    Integer,
    and_,
    case,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, offboarding
from epistrel.core.config_changes import read_section
from epistrel.core.enums import EventCategory, OperationStatus
from epistrel.core.idempotency import KeyStatus, purge_expired, read_idempotency_settings
from epistrel.core.profiles import store as profiles
from epistrel.core.settings_schema import STORY_LIFETIME, FeedbackSettings, RetentionSettings
from epistrel.core.tables import audit_log, events, idempotency_keys, jobs, operations, stories
from epistrel.core.tables.events import ALLOW_AUDIT_EXPIRY_GUC
from epistrel.core.tables.feedback import feedback_history, message_feedback
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

CHUNK = 1_000
MAX_CHUNKS = 100

RETENTION_SWEEP = "retention_sweep"
IDEMPOTENCY_PURGE = "idempotency_purge"


def _cutoff(now: datetime, default: int) -> Any:
    """``now`` minus the row's story's retention, in days (SQL)."""
    return literal(now, TIMESTAMP(timezone=True)) - func.make_interval(
        0, 0, 0, _retention_days(default)
    )


def _retention_days(default: int) -> Any:
    """The story's ``audit_retention_days`` when it set a number, else ``default`` (SQL)."""
    value = stories.c.settings["audit_retention_days"]
    return func.coalesce(
        case(
            (func.jsonb_typeof(value) == "number", cast(value.astext, Integer)),
            else_=None,
        ),
        default,
    )


async def _allow_audit_expiry(conn: AsyncConnection) -> None:
    await conn.execute(
        text("SELECT set_config(:name, 'on', true)"), {"name": ALLOW_AUDIT_EXPIRY_GUC}
    )


def expired_audit_events(now: datetime, default_days: int) -> Any:
    """The ids of up to ``CHUNK`` audit events past their story's retention (``FOR UPDATE``)."""
    return (
        select(events.c.id)
        .select_from(
            events.join(operations, operations.c.operation_id == events.c.operation_id).outerjoin(
                stories, stories.c.id == events.c.story_id
            )
        )
        .where(
            events.c.category == EventCategory.AUDIT.value,
            operations.c.status.not_in(
                [OperationStatus.IN_FLIGHT.value, OperationStatus.QUEUED.value]
            ),
            events.c.tx_time < _cutoff(now, default_days),
        )
        .limit(CHUNK)
        .with_for_update(of=events, skip_locked=True)
    )


def expired_audit_log(now: datetime, default_days: int) -> Any:
    return (
        select(audit_log.c.id)
        .select_from(audit_log.outerjoin(stories, stories.c.id == audit_log.c.story_id))
        .where(audit_log.c.at < _cutoff(now, default_days))
        .limit(CHUNK)
        .with_for_update(of=audit_log, skip_locked=True)
    )


def expired_feedback(column: Any, before: datetime) -> Any:
    """The ids of up to ``CHUNK`` rows of ``column``'s feedback table older than ``before``."""
    return (
        select(column.table.c.id)
        .where(column < before)
        .limit(CHUNK)
        .with_for_update(skip_locked=True)
    )


async def _expire_feedback(db: AsyncEngine, moment: datetime) -> dict[str, int]:
    """Delete the feedback past ``feedback_retention`` days (none under ``story_lifetime``)."""
    retention = (await read_section(db, FeedbackSettings)).feedback_retention
    if retention == STORY_LIFETIME:
        return {"feedback": 0, "feedback_history": 0}
    before = moment - timedelta(days=int(retention))
    return {
        "feedback": await _chunked(
            db,
            message_feedback,
            expired_feedback(message_feedback.c.created_at, before),
            audit=False,
        ),
        "feedback_history": await _chunked(
            db,
            feedback_history,
            expired_feedback(feedback_history.c.replaced_at, before),
            audit=False,
        ),
    }


async def _chunked(db: AsyncEngine, table: Any, ids_query: Any, *, audit: bool) -> int:
    deleted = 0
    for _ in range(MAX_CHUNKS):
        async with db.begin() as conn:
            if audit:
                await _allow_audit_expiry(conn)
            ids = list((await conn.scalars(ids_query)).all())
            if ids:
                await conn.execute(delete(table).where(table.c.id.in_(ids)))
        deleted += len(ids)
        if len(ids) < CHUNK:
            break
    return deleted


async def _purge_offboarding(db: AsyncEngine, before: datetime) -> int:
    """Finished offboardings (#117) older than ``offboarding_report_retention_days``."""
    deleted = 0
    for _ in range(MAX_CHUNKS):
        async with db.begin() as conn:
            found = await offboarding.purge_finished(conn, before, CHUNK)
        deleted += found
        if found < CHUNK:
            break
    return deleted


async def sweep_retention(db: AsyncEngine, now: datetime | None = None) -> dict[str, int]:
    """One retention run (see the module docstring); returns how many rows of each it deleted."""
    moment = now or clock.now()
    settings = await read_section(db, RetentionSettings)
    days = settings.audit_retention_days
    finished_before = moment - timedelta(days=settings.job_retention_days)
    finished_jobs = (
        select(jobs.c.id)
        .where(jobs.c.status.in_(["done", "failed"]), jobs.c.updated_at < finished_before)
        .limit(CHUNK)
        .with_for_update(skip_locked=True)
    )
    return {
        "audit_events": await _chunked(db, events, expired_audit_events(moment, days), audit=True),
        "audit_log": await _chunked(db, audit_log, expired_audit_log(moment, days), audit=True),
        "jobs": await _chunked(db, jobs, finished_jobs, audit=False),
        "engine_profiles": await profiles.purge_deleted(db),
        "offboarding_jobs": await _purge_offboarding(
            db, moment - timedelta(days=settings.offboarding_report_retention_days)
        ),
        **await _expire_feedback(db, moment),
    }


async def purge_idempotency(db: AsyncEngine, now: datetime | None = None) -> dict[str, int]:
    """One ``idempotency_purge`` run; returns the counts of purged results and settled keys."""
    moment = now or clock.now()
    purged = await purge_expired(db, moment)
    window = (await read_idempotency_settings(db)).idempotency_result_window
    lapsed_before = moment - timedelta(hours=window)
    op_status = (
        select(operations.c.status)
        .where(operations.c.operation_id == idempotency_keys.c.operation_id)
        .scalar_subquery()
    )
    stale = and_(
        idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
        idempotency_keys.c.lease_until < lapsed_before,
    )
    async with db.begin() as conn:
        settled = await conn.execute(
            update(idempotency_keys)
            .where(stale, op_status == OperationStatus.COMMITTED.value)
            .values(status=KeyStatus.DONE.value, result=None, expires_at=moment)
        )
        freed = await conn.execute(
            delete(idempotency_keys).where(
                stale,
                or_(
                    idempotency_keys.c.operation_id.is_(None),
                    op_status.is_(None),
                    op_status.in_([OperationStatus.FAILED.value, OperationStatus.ABANDONED.value]),
                ),
            )
        )
    return {
        "results_purged": purged,
        "keys_settled": int(settled.rowcount or 0),
        "keys_freed": int(freed.rowcount or 0),
    }


async def _run_retention(ctx: JobContext) -> JobOutput:
    return JobOutput(result=await sweep_retention(ctx.db))


async def _run_idempotency(ctx: JobContext) -> JobOutput:
    return JobOutput(result=await purge_idempotency(ctx.db))


RETENTION_HANDLER = JobHandler(
    kind=RETENTION_SWEEP,
    payload=NoPayload,
    run=recording(_run_retention),
    max_attempts=1,
    records_operation=False,
)
IDEMPOTENCY_HANDLER = JobHandler(
    kind=IDEMPOTENCY_PURGE,
    payload=NoPayload,
    run=recording(_run_idempotency),
    max_attempts=1,
    records_operation=False,
)
RETENTION_TASK = RecurringTask(RETENTION_SWEEP, timedelta(days=1))
IDEMPOTENCY_TASK = RecurringTask(IDEMPOTENCY_PURGE, timedelta(hours=1))

__all__ = [
    "CHUNK",
    "IDEMPOTENCY_HANDLER",
    "IDEMPOTENCY_PURGE",
    "IDEMPOTENCY_TASK",
    "MAX_CHUNKS",
    "RETENTION_HANDLER",
    "RETENTION_SWEEP",
    "RETENTION_TASK",
    "purge_idempotency",
    "sweep_retention",
]
