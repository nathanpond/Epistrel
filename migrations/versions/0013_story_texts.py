"""story_texts: the pinned memories and custom instructions projection (#60)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    # One row per story, created by its genesis commit from now on; a plain ``story_id`` (no
    # foreign key) like the other projections, so story deletion enumerates it.
    op.create_table(
        "story_texts",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("pinned", postgresql.JSONB(), nullable=False),
        sa.Column("custom_instructions", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("story_id", name="pk_story_texts"),
    )
    # Stories created before this revision get their (empty) row; their texts were never set
    # through a released path.
    op.execute(
        "INSERT INTO story_texts (story_id, pinned, custom_instructions) "
        "SELECT id, '[]'::jsonb, '' FROM stories"
    )


def downgrade() -> None:
    op.drop_table("story_texts")
