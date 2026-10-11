"""projections: transcript_messages and story_heads

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    op.create_table(
        "transcript_messages",
        sa.Column("message_id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("narrative_turn", sa.Integer(), nullable=True),
        sa.Column("turn_variant", sa.SmallInteger(), nullable=True),
        sa.Column("beat", sa.SmallInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("actor_character_id", ULID, nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "membership", postgresql.ENUM(name="membership", create_type=False), nullable=False
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_transcript_messages"),
    )
    op.create_index(
        "ix_transcript_messages_story_position",
        "transcript_messages",
        ["story_id", "narrative_turn", "turn_variant"],
    )
    op.create_index(
        "ix_transcript_messages_story_membership",
        "transcript_messages",
        ["story_id", "membership"],
    )
    op.create_table(
        "story_heads",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("next_turn_index", sa.Integer(), nullable=False),
        sa.Column("active_variants", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("story_id", name="pk_story_heads"),
    )


def downgrade() -> None:
    op.drop_table("story_heads")
    op.drop_table("transcript_messages")
