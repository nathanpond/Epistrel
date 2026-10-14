"""feedback reporting and export: the export log and the aggregates' indexes (#126)

- ``feedback_export_log``: every feedback export (FR-FB-4): its id, the exporting principal, when,
  the window and filters it was asked for, the case count, and its file in the blob store
  (``blob_key`` ``exports/<id>.jsonl``, ``bytes``, ``sha256``); ``downloaded_at``,
  ``deleted_at``/``deleted_by`` and ``redacted_at`` start null. Deployment-level: no
  ``story_id``, so a story's deletion keeps the record that an export happened.
- ``message_feedback`` gains the aggregates' indexes (FR-FB-3): ``config_fingerprint``,
  ``profile_version`` and a GIN index on ``tags``. ``created_at`` is indexed already (#112).

The deployment settings ``feedback_export`` and ``feedback_regen_window_s`` are fields of the
``settings`` document (``FeedbackSettings``), so they need no column.

Downgrade drops the table and the three indexes.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    op.create_table(
        "feedback_export_log",
        sa.Column("id", ULID, nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("window_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "filters",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("blob_key", sa.Text(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("downloaded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Text(), nullable=True),
        sa.Column("redacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_feedback_export_log"),
    )
    op.create_index("ix_feedback_export_log_at", "feedback_export_log", ["at"])
    op.create_index(
        "ix_message_feedback_config_fingerprint", "message_feedback", ["config_fingerprint"]
    )
    op.create_index("ix_message_feedback_profile_version", "message_feedback", ["profile_version"])
    op.create_index(
        "ix_message_feedback_tags", "message_feedback", ["tags"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_message_feedback_tags", table_name="message_feedback")
    op.drop_index("ix_message_feedback_profile_version", table_name="message_feedback")
    op.drop_index("ix_message_feedback_config_fingerprint", table_name="message_feedback")
    op.drop_index("ix_feedback_export_log_at", table_name="feedback_export_log")
    op.drop_table("feedback_export_log")
