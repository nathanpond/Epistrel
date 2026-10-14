"""message feedback: ratings, their history, and the deployment's reason tags (#112)

- ``message_feedback``: each principal's current rating of one generated message, unique on
  ``(story_id, message_id, principal)``: ``turn`` and ``variant`` (denormalised), ``rating`` 1 to
  5, ``tags`` (``text[]``), the free ``text``, and the context the message was produced under,
  copied from its operation when the rating is first written (``operation_id``,
  ``config_fingerprint``, ``profile_version``, ``models``, ``rating_version_id``,
  ``quality_status``); ``compacted_at`` (M7) and ``redacted_at`` (#124) start null.
- ``feedback_history``: every value a re-rating replaced or a retraction removed (``kind``),
  with when it was given (``rated_at``) and when it stopped being current (``replaced_at``).
- ``deployment_state.feedback_tags`` (``text[]``, NOT NULL): the reason tags a rating may carry.
  The existing row gets FR-FB-1's nine (``out_of_character``, ``repetitive``,
  ``forgot_something``, ``factual_error``, ``ignored_my_action``, ``too_long``, ``too_short``,
  ``should_have_been_blocked``, ``other``) through the column's default.

Both tables are story-scoped (``story_id`` references ``stories``) and ordinary: no trigger.
``message_id`` and ``operation_id`` are plain ids.

Downgrade drops the two tables and the column.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
SEEDED_TAGS = (
    "'{out_of_character,repetitive,forgot_something,factual_error,ignored_my_action,"
    "too_long,too_short,should_have_been_blocked,other}'::text[]"
)
NO_TAGS = "'{}'::text[]"
RATING_CHECK = "rating BETWEEN 1 AND 5"


def _rated_message(table: str) -> list[Any]:
    """What both tables hold: the id, the story, the message and its position, the principal,
    the value, the rating check and the story's foreign key."""
    return [
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("message_id", ULID, nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("variant", sa.SmallInteger(), nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default=sa.text(NO_TAGS), nullable=False
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.CheckConstraint(RATING_CHECK, name=f"ck_{table}_rating"),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"], name=f"fk_{table}_story_id_stories"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]


def upgrade() -> None:
    op.add_column(
        "deployment_state",
        sa.Column(
            "feedback_tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text(SEEDED_TAGS),
            nullable=False,
        ),
    )
    op.create_table(
        "message_feedback",
        *_rated_message("message_feedback"),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("config_fingerprint", sa.Text(), nullable=True),
        sa.Column("profile_version", sa.Text(), nullable=True),
        sa.Column("models", postgresql.JSONB(), nullable=True),
        sa.Column("rating_version_id", ULID, nullable=True),
        sa.Column("quality_status", sa.Text(), nullable=True),
        sa.Column("compacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("redacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "story_id", "message_id", "principal", name="uq_message_feedback_story_id"
        ),
    )
    op.create_index("ix_message_feedback_created_at", "message_feedback", ["created_at"])
    op.create_table(
        "feedback_history",
        *_rated_message("feedback_history"),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("rated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("replaced_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("redacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('replaced', 'retracted')", name="ck_feedback_history_kind"),
    )
    op.create_index(
        "ix_feedback_history_story_id_message_id",
        "feedback_history",
        ["story_id", "message_id", "principal"],
    )
    op.create_index("ix_feedback_history_replaced_at", "feedback_history", ["replaced_at"])


def downgrade() -> None:
    op.drop_index("ix_feedback_history_replaced_at", table_name="feedback_history")
    op.drop_index("ix_feedback_history_story_id_message_id", table_name="feedback_history")
    op.drop_table("feedback_history")
    op.drop_index("ix_message_feedback_created_at", table_name="message_feedback")
    op.drop_table("message_feedback")
    op.drop_column("deployment_state", "feedback_tags")
