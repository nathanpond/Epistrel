"""``jobs``: durable background work (NFR-REL-5, FR-CONC-8; #70).

One row per job: its ``kind`` (text; handlers are registered by kind in
``orchestrator.jobs.handlers``), the story it belongs to when it has one (plain ``story_id``, no
foreign key: story deletion enumerates it), the validated ``payload`` (ids and parameters only,
never story text), when it is ``due_at``, and the claim: ``lease_until`` (renewed by heartbeat),
``attempt`` (the fencing token: every claim and every reclaim advances it, and a commit is
accepted only from the current one), ``worker_id`` (``host:pid:loop``). ``failures`` counts
failed attempts only (reclaims and dropped results do not count toward ``job_max_attempts``).
``input_versions`` is ``{"<table>:<id>": version}`` as the handler declared it
(``core.input_versions``), ``result`` the handler's result, or ``{"error": <exception class>}``
for a failure. ``coalesce_key`` is unique among ``queued`` rows, so re-enqueueing the same work
returns the existing job.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

STATUS_CHECK = "status IN ('queued', 'running', 'done', 'failed')"

jobs = Table(
    "jobs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("story_id", ulid_type(), nullable=True),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("due_at", TIMESTAMP(timezone=True), nullable=False),
    Column("lease_until", TIMESTAMP(timezone=True), nullable=True),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("failures", SmallInteger, nullable=False, server_default=text("0")),
    Column("worker_id", Text, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("input_versions", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("result", JSONB, nullable=True),
    Column("coalesce_key", Text, nullable=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column(
        "updated_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    CheckConstraint(STATUS_CHECK, name="status"),
    Index("ix_jobs_status_due_at", "status", "due_at"),
    Index("ix_jobs_story_id", "story_id"),
    Index(
        "uq_jobs_coalesce_key_queued",
        "coalesce_key",
        unique=True,
        postgresql_where=text("status = 'queued'"),
    ),
)
