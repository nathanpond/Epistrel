"""A story's rating checks and the history check's verdict cache (FR-RATE-7; #118).

``rating_check_jobs``: one row per tightening of a story's rating (``PUT
/stories/{id}/content-rating`` with a target whose effective guardrails add a policy). The
row's ``id`` is its #70 job's id, but no foreign key joins them: the ``retention_sweep`` purges
finished ``jobs`` rows after ``job_retention_days`` (7), while a check's status and violations
are kept for ``rating_check_retention_days`` (30). So this row holds its own terminal
``status``.

- ``story_id``: the story checked; ``from_rating_id``: its rating when the check started.
- ``target_rating_id`` / ``target_rating_version``: the rating asked for and the version id the
  check evaluates (an edit of that rating mid-check restarts the check on the new version).
- ``status``: ``checking`` while it runs (the story is paused), then ``applied``, ``refused``
  or ``failed`` (the story has resumed).
- ``progress``: ``{done, total}`` records checked.
- ``violations``: ``[{position, policy_id, policy_name, code?}]`` in position order. A position
  names a turn (``{turn, variant, beat}``) or a record outside the turns (``{kind, id}``);
  never any text (invariant 3).
- ``result``: ``{rating_version, config_revision}`` once applied. ``error``: ``{class,
  message}`` once failed.
- ``started_by``, ``started_at``, ``finished_at``; ``operation_id``: the request's committed
  ``control`` operation (the pause); ``checked_revision``: the story's timeline revision the
  evaluation read (a write that moved it since restarts the evaluation).

``uq_rating_check_jobs_story_checking`` allows one running check per story.

``guardrail_check_results``: per story, record and policy version, the verdict the history
check reached (``pass`` or ``violation``), deterministic and model-assisted alike; an uncertain
verdict is not stored. A record is an event (keyed by its id) or a pinned memory or custom
instructions text (keyed by id and content hash). Both tables are story tables
(``core.stories.STORY_TABLES``): deleted with the story, and a fork copies neither.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type


class RatingCheckStatus(StrEnum):
    CHECKING = "checking"
    APPLIED = "applied"
    REFUSED = "refused"
    FAILED = "failed"


STATUS_CHECK = "status IN (" + ", ".join(f"'{s.value}'" for s in RatingCheckStatus) + ")"
CHECKING_WHERE = "status = 'checking'"

rating_check_jobs = Table(
    "rating_check_jobs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("from_rating_id", ulid_type(), nullable=False),
    Column("target_rating_id", ulid_type(), nullable=False),
    Column("target_rating_version", ulid_type(), nullable=False),
    Column("status", Text, nullable=False, server_default=text("'checking'")),
    Column("progress", JSONB, nullable=False),
    Column("violations", JSONB, nullable=False),
    Column("result", JSONB, nullable=True),
    Column("error", JSONB, nullable=True),
    Column("started_by", Text, nullable=False),
    Column("started_at", TIMESTAMP(timezone=True), nullable=False),
    Column("finished_at", TIMESTAMP(timezone=True), nullable=True),
    Column("operation_id", ulid_type(), nullable=True),
    Column("checked_revision", BigInteger, nullable=True),
    CheckConstraint(STATUS_CHECK, name="status"),
)

Index("ix_rating_check_jobs_story_id", rating_check_jobs.c.story_id)
Index("ix_rating_check_jobs_finished_at", rating_check_jobs.c.finished_at)
Index(
    "uq_rating_check_jobs_story_checking",
    rating_check_jobs.c.story_id,
    unique=True,
    postgresql_where=text(CHECKING_WHERE),
)

VERDICTS = ("pass", "violation")

guardrail_check_results = Table(
    "guardrail_check_results",
    metadata,
    Column("story_id", ulid_type(), nullable=False),
    Column("record_key", Text, nullable=False),
    Column("policy_id", Text, nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("verdict", Text, nullable=False),
    Column(
        "at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    PrimaryKeyConstraint("story_id", "record_key", "policy_id", "policy_version"),
    CheckConstraint("verdict IN ('pass', 'violation')", name="verdict"),
)

__all__ = [
    "VERDICTS",
    "RatingCheckStatus",
    "guardrail_check_results",
    "rating_check_jobs",
]
