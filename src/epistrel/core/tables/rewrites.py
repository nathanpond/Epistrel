"""``rewrite_sessions`` and ``rewrite_questions``: the history side-channel's sessions and the
questions they ask the author (FR-REWRITE-1,2,4,7,8; docs/02 §9; #223, migration 0116).

A **rewrite session** is a story resource, separate from the turn flow: an author's
instruction (``kind = rewrite``; later stories add ``undo``, ``redo``, ``directed_resolution``,
``support_check``, ``entity_merge`` and ``directive``), how it resolves conflicts
(``resolution_mode``: ``synchronous``, ``best_effort``, ``threshold`` with
``threshold_severity``) and whether it previews first (``preview``), its ``state``
(``awaiting_answers``, ``awaiting_approval``, ``applying``, ``applied``, ``cancelled``,
``expired``, ``abandoned``), the plan computed at ``base_revision`` (``plan``, ``plan_hash`` and
the ``plan_read_set`` #237 rechecks) and the pipeline it was planned under (``pipeline``,
``structured`` or ``lite``). Opening, answering and cancelling one writes these rows only:
never story truth, the clock or the revision (FR-REWRITE-7).

A **question** asks the author something (``kind``: ``clarification``, ``conflict``,
``timing``, ``dependent``, ``port``, ``support``) with its options and, always, a recommended
option and the reason for it (owner decision 4, 2026-10-10: both NOT NULL). A re-plan marks
the questions it replaces ``superseded_by``; nothing is deleted. Both tables belong to their
story (deleted with it, never copied by a fork). ``orchestrator.rewrite.session`` is the only
writer.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

NOW = text("transaction_timestamp()")

SESSION_KINDS = (
    "rewrite",
    "undo",
    "redo",
    "directed_resolution",
    "support_check",
    "entity_merge",
    "directive",
)
SESSION_STATES = (
    "awaiting_answers",
    "awaiting_approval",
    "applying",
    "applied",
    "cancelled",
    "expired",
    "abandoned",
)
OPEN_STATES = ("awaiting_answers", "awaiting_approval", "applying")
"""A session still open: counted against ``rewrite_max_open_sessions`` and taking actions."""
RESOLUTION_MODES = ("synchronous", "best_effort", "threshold")
PREVIEWS = ("preview_first", "apply_directly")
QUESTION_KINDS = ("clarification", "conflict", "timing", "dependent", "port", "support")
SEVERITIES = ("high", "low")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


rewrite_sessions = Table(
    "rewrite_sessions",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("kind", Text, nullable=False, server_default=text("'rewrite'")),
    Column("instruction", Text, nullable=True),
    Column("target_amendment_id", ulid_type(), nullable=True),
    Column("principal", Text, nullable=False),
    Column("resolution_mode", Text, nullable=False, server_default=text("'synchronous'")),
    Column("threshold_severity", Text, nullable=True),
    Column("preview", Text, nullable=False, server_default=text("'preview_first'")),
    Column("state", Text, nullable=False),
    Column("pipeline", Text, nullable=False, server_default=text("'structured'")),
    Column("base_revision", BigInteger, nullable=True),
    Column("plan", JSONB, nullable=True),
    Column("plan_hash", Text, nullable=True),
    Column("plan_read_set", JSONB, nullable=True),
    Column("stale_count", Integer, nullable=False, server_default=text("0")),
    Column("last_error", JSONB, nullable=True),
    Column("held_render", JSONB, nullable=True),
    Column("operation_id", ulid_type(), nullable=True),
    Column("result_amendment_id", ulid_type(), nullable=True),
    Column("report", JSONB, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(_in("kind", SESSION_KINDS), name="kind"),
    CheckConstraint(_in("state", SESSION_STATES), name="state"),
    CheckConstraint(_in("resolution_mode", RESOLUTION_MODES), name="resolution_mode"),
    CheckConstraint(
        "(resolution_mode = 'threshold') = (threshold_severity IS NOT NULL) "
        f"AND (threshold_severity IS NULL OR {_in('threshold_severity', SEVERITIES)})",
        name="threshold",
    ),
    CheckConstraint(_in("preview", PREVIEWS), name="preview"),
    CheckConstraint("pipeline IN ('structured', 'lite')", name="pipeline"),
    Index("ix_rewrite_sessions_story_state", "story_id", "state"),
    Index(
        "ix_rewrite_sessions_expiry",
        "expires_at",
        postgresql_where=text("state IN ('awaiting_answers', 'awaiting_approval')"),
    ),
)

rewrite_questions = Table(
    "rewrite_questions",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column(
        "session_id",
        ulid_type(),
        ForeignKey("rewrite_sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("kind", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("options", JSONB, nullable=False, server_default=text("'[]'")),
    Column("recommended_option_id", Text, nullable=False),
    Column("recommendation_reason", Text, nullable=False),
    Column("severity", Text, nullable=True),
    Column("required", Boolean, nullable=False, server_default=text("true")),
    Column("answer_option_id", Text, nullable=True),
    Column("answer_text", Text, nullable=True),
    Column("accepted_recommendation", Boolean, nullable=True),
    Column("answered_by", Text, nullable=True),
    Column("answered_at", TIMESTAMP(timezone=True), nullable=True),
    Column("carried_from", ulid_type(), nullable=True),
    Column("superseded_by", ulid_type(), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    CheckConstraint(_in("kind", QUESTION_KINDS), name="kind"),
    CheckConstraint(f"severity IS NULL OR {_in('severity', SEVERITIES)}", name="severity"),
    CheckConstraint(
        "length(recommended_option_id) > 0 AND length(recommendation_reason) > 0",
        name="recommended",
    ),
    Index("ix_rewrite_questions_session", "story_id", "session_id"),
    Index(
        "uq_rewrite_questions_key",
        "session_id",
        "key",
        unique=True,
        postgresql_where=text("superseded_by IS NULL"),
    ),
)

__all__ = [
    "OPEN_STATES",
    "PREVIEWS",
    "QUESTION_KINDS",
    "RESOLUTION_MODES",
    "SESSION_KINDS",
    "SESSION_STATES",
    "SEVERITIES",
    "rewrite_questions",
    "rewrite_sessions",
]
