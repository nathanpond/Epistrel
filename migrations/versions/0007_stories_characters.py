"""stories and characters: story name, archive, status vocabulary; the inline cast

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")

# Frozen here: migrations never change when ``epistrel.core.enums.StoryStatus`` does.
STATUS_CHECK = "status IN ('active', 'archived', 'paused')"


def upgrade() -> None:
    op.add_column("stories", sa.Column("name", sa.Text(), nullable=True))
    op.add_column("stories", sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.execute("UPDATE stories SET status = 'active' WHERE status = 'ready'")
    op.alter_column("stories", "status", server_default=sa.text("'active'"))
    op.create_check_constraint("ck_stories_status", "stories", STATUS_CHECK)

    op.create_table(
        "characters",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("true_age", sa.Integer(), nullable=False),
        sa.Column("age_recorded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_player", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("safe_fallback_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("true_age BETWEEN 0 AND 150", name="ck_characters_true_age"),
        sa.PrimaryKeyConstraint("id", name="pk_characters"),
    )
    op.create_index(
        "uq_characters_story_name",
        "characters",
        ["story_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "uq_characters_story_ordinal", "characters", ["story_id", "ordinal"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_characters_story_ordinal", table_name="characters")
    op.drop_index("uq_characters_story_name", table_name="characters")
    op.drop_table("characters")
    op.drop_constraint("ck_stories_status", "stories", type_="check")
    op.alter_column("stories", "status", server_default=sa.text("'ready'"))
    op.execute("UPDATE stories SET status = 'ready' WHERE status = 'active'")
    op.drop_column("stories", "archived_at")
    op.drop_column("stories", "name")
