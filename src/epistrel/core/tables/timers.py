"""``timers``, ``recurring_tasks`` and ``worker_heartbeats``: the timed lifecycle work Epistrel's
own workers own, and their liveness (NFR-REL-5, NFR-OPS-1,2; #71).

- ``timers``: one-shot due times. One row per ``(kind, target)`` (``schedule`` upserts it). A
  worker fires a due row by enqueueing a job of its ``kind`` with ``{"target": target}`` and
  deleting the row in the same transaction. ``story_id`` is plain (no foreign key; story deletion
  enumerates it). ``lease_until`` and ``attempts`` are kept for kinds that hold a row while they
  run; M2's firing deletes the row, so they stay null and 0.
- ``recurring_tasks``: one row per sweep, by ``name`` (the job kind it enqueues). A worker claims
  a due row (``SKIP LOCKED``) and, in one transaction, enqueues its job, sets ``last_run_at`` and
  moves ``next_run_at`` to ``now + interval``, so a task that missed several runs runs once. The
  job records ``last_status`` (``ok`` / ``failed``) and the error class.
- ``worker_heartbeats``: one row per worker loop (``host:pid:loop``), upserted every poll tick,
  so ``/health`` in any process (``api`` mode included) can read how many workers are alive.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Interval,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from epistrel.core.tables.base import metadata, ulid_type

NOW = text("transaction_timestamp()")
LAST_STATUS_CHECK = "last_status IN ('ok', 'failed')"

timers = Table(
    "timers",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("story_id", ulid_type(), nullable=True),
    Column("due_at", TIMESTAMP(timezone=True), nullable=False),
    Column("lease_until", TIMESTAMP(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    UniqueConstraint("kind", "target", name="uq_timers_kind_target"),
    Index("ix_timers_due_at", "due_at"),
    Index("ix_timers_story_id", "story_id"),
)

recurring_tasks = Table(
    "recurring_tasks",
    metadata,
    Column("name", Text, primary_key=True),
    Column("interval", Interval, nullable=False),
    Column("next_run_at", TIMESTAMP(timezone=True), nullable=False),
    Column("last_run_at", TIMESTAMP(timezone=True), nullable=True),
    Column("last_status", Text, nullable=True),
    Column("last_error", Text, nullable=True),
    CheckConstraint(LAST_STATUS_CHECK, name="last_status"),
)

worker_heartbeats = Table(
    "worker_heartbeats",
    metadata,
    Column("worker_id", Text, primary_key=True),
    Column("mode", Text, nullable=False),
    Column("pid", Integer, nullable=False),
    Column("started_at", TIMESTAMP(timezone=True), nullable=False),
    Column("at", TIMESTAMP(timezone=True), nullable=False),
    Index("ix_worker_heartbeats_at", "at"),
)
