"""rewrite sessions: the history side-channel's sessions and questions (#223)

FR-REWRITE-1,2,4,7: ``core.tables.rewrites`` describes the tables.

- ``rewrite_sessions``: one row per session (its instruction, modes, state, pipeline and the
  plan computed at ``base_revision``).
- ``rewrite_questions``: the questions a session asks, each with its recommended option and the
  reason for it (both NOT NULL).
- ``amendments.session_id`` gains its foreign key to ``rewrite_sessions``.

Revision ID: 0116
Revises: 0115
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0116"
down_revision: str | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")
KINDS = (
    "rewrite",
    "undo",
    "redo",
    "directed_resolution",
    "support_check",
    "entity_merge",
    "directive",
)
STATES = (
    "awaiting_answers",
    "awaiting_approval",
    "applying",
    "applied",
    "cancelled",
    "expired",
    "abandoned",
)
QUESTION_KINDS = ("clarification", "conflict", "timing", "dependent", "port", "support")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "rewrite_sessions",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'rewrite'")),
        sa.Column("instruction", sa.Text, nullable=True),
        sa.Column("target_amendment_id", ULID, nullable=True),
        sa.Column("principal", sa.Text, nullable=False),
        sa.Column(
            "resolution_mode", sa.Text, nullable=False, server_default=sa.text("'synchronous'")
        ),
        sa.Column("threshold_severity", sa.Text, nullable=True),
        sa.Column("preview", sa.Text, nullable=False, server_default=sa.text("'preview_first'")),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("pipeline", sa.Text, nullable=False, server_default=sa.text("'structured'")),
        sa.Column("base_revision", sa.BigInteger, nullable=True),
        sa.Column("plan", postgresql.JSONB, nullable=True),
        sa.Column("plan_hash", sa.Text, nullable=True),
        sa.Column("plan_read_set", postgresql.JSONB, nullable=True),
        sa.Column("stale_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", postgresql.JSONB, nullable=True),
        sa.Column("held_render", postgresql.JSONB, nullable=True),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("result_amendment_id", ULID, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(_in("kind", KINDS), name="ck_rewrite_sessions_kind"),
        sa.CheckConstraint(_in("state", STATES), name="ck_rewrite_sessions_state"),
        sa.CheckConstraint(
            _in("resolution_mode", ("synchronous", "best_effort", "threshold")),
            name="ck_rewrite_sessions_resolution_mode",
        ),
        sa.CheckConstraint(
            "(resolution_mode = 'threshold') = (threshold_severity IS NOT NULL) "
            "AND (threshold_severity IS NULL OR threshold_severity IN ('high', 'low'))",
            name="ck_rewrite_sessions_threshold",
        ),
        sa.CheckConstraint(
            "preview IN ('preview_first', 'apply_directly')", name="ck_rewrite_sessions_preview"
        ),
        sa.CheckConstraint(
            "pipeline IN ('structured', 'lite')", name="ck_rewrite_sessions_pipeline"
        ),
    )
    op.create_index("ix_rewrite_sessions_story_state", "rewrite_sessions", ["story_id", "state"])
    op.create_index(
        "ix_rewrite_sessions_expiry",
        "rewrite_sessions",
        ["expires_at"],
        postgresql_where=sa.text("state IN ('awaiting_answers', 'awaiting_approval')"),
    )
    op.create_table(
        "rewrite_questions",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column(
            "session_id",
            ULID,
            sa.ForeignKey(
                "rewrite_sessions.id",
                name="fk_rewrite_questions_session_id_rewrite_sessions",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("options", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("recommended_option_id", sa.Text, nullable=False),
        sa.Column("recommendation_reason", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=True),
        sa.Column("required", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("answer_option_id", sa.Text, nullable=True),
        sa.Column("answer_text", sa.Text, nullable=True),
        sa.Column("accepted_recommendation", sa.Boolean, nullable=True),
        sa.Column("answered_by", sa.Text, nullable=True),
        sa.Column("answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("carried_from", ULID, nullable=True),
        sa.Column("superseded_by", ULID, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(_in("kind", QUESTION_KINDS), name="ck_rewrite_questions_kind"),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN ('high', 'low')", name="ck_rewrite_questions_severity"
        ),
        sa.CheckConstraint(
            "length(recommended_option_id) > 0 AND length(recommendation_reason) > 0",
            name="ck_rewrite_questions_recommended",
        ),
    )
    op.create_index("ix_rewrite_questions_session", "rewrite_questions", ["story_id", "session_id"])
    op.create_index(
        "uq_rewrite_questions_key",
        "rewrite_questions",
        ["session_id", "key"],
        unique=True,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.create_foreign_key(
        "fk_amendments_session", "amendments", "rewrite_sessions", ["session_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_amendments_session", "amendments", type_="foreignkey")
    op.drop_table("rewrite_questions")
    op.drop_table("rewrite_sessions")
