"""``offboarding_jobs``: one row per principal offboarding (FR-AUTH-7; docs/02 §4.10; #117).

The row's ``id`` is its #70 job's id (``jobs.id``), but no foreign key joins them. The
``retention_sweep`` purges finished ``jobs`` rows after ``job_retention_days`` (7), while an
offboarding's status and report are kept for ``offboarding_report_retention_days`` (90). So
this row holds its own terminal ``status`` and ``report``.

- ``principal``: the principal being offboarded. ``created_by``: who asked.
- ``actions``: the per-type actions as resolved (every type named, a missing one ``keep``).
- ``transfer_to``: the new owner for every ``transfer``, or null.
- ``request``: the request as validated.
- ``status``: #70's enum (``queued``, ``running``, ``done``, ``failed``).
- ``snapshot``: the entities taken at the request, in processing order. Each is
  ``{type, id, action}``, with ``reason`` on media moved by a transferred entry. Companions
  are absent: each goes with its character.
- ``progress``: ``{index, current, deferred}``. ``index`` is the number of snapshot entries
  done. ``current`` is the entry whose action may have run but is not yet recorded, the resume
  marker. ``deferred`` lists the stories that were busy, with their tries.
- ``counts``: the dry run's per-type counts.
- ``report``: the outcomes so far (``deleted``, ``transferred``, ``removed``, ``kept`` and
  ``skipped``, each by type). Once the job is done it also holds ``remaining``, the live
  entities still owned by or related to the principal.
- ``error``: ``{class, message}`` when the job failed.
- ``operation_id``: the request's committed ``control`` operation.

The partial unique index ``uq_offboarding_jobs_principal_active`` allows one queued or running
offboarding per principal (409 ``offboarding_in_progress``).
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type


class OffboardingStatus(StrEnum):
    """An offboarding's state: #70's job statuses."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


ACTIVE_STATUSES = (OffboardingStatus.QUEUED.value, OffboardingStatus.RUNNING.value)
STATUS_CHECK = "status IN (" + ", ".join(f"'{s.value}'" for s in OffboardingStatus) + ")"
ACTIVE_WHERE = "status IN ('queued', 'running')"

offboarding_jobs = Table(
    "offboarding_jobs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("principal", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("actions", JSONB, nullable=False),
    Column("transfer_to", Text, nullable=True),
    Column("request", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("snapshot", JSONB, nullable=False),
    Column("progress", JSONB, nullable=False),
    Column("counts", JSONB, nullable=False),
    Column("report", JSONB, nullable=False),
    Column("error", JSONB, nullable=True),
    Column("operation_id", ulid_type(), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    Column("finished_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint(STATUS_CHECK, name="status"),
)

Index(
    "uq_offboarding_jobs_principal_active",
    offboarding_jobs.c.principal,
    unique=True,
    postgresql_where=text(ACTIVE_WHERE),
)
Index("ix_offboarding_jobs_finished_at", offboarding_jobs.c.finished_at)

__all__ = ["ACTIVE_STATUSES", "OffboardingStatus", "offboarding_jobs"]
