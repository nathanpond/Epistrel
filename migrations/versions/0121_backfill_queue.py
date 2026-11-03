"""story_backfills: the profile-edit queue (#229)

FR-PROF-8: a confirmed profile edit that adds data-producing capability to stories with history
queues one backfill per story (``origin = profile_edit``), claimed one at a time in ``seq`` order
by the backfill handler; a single-story change or gap fill (``origin = single_story``) pauses its
story at request time, as before.

Revision ID: 0121
Revises: 0120
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0121"
down_revision: str | None = "0120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "story_backfills",
        sa.Column("origin", sa.Text(), nullable=False, server_default=sa.text("'single_story'")),
    )
    op.create_check_constraint(
        "ck_story_backfills_origin", "story_backfills", "origin IN ('single_story', 'profile_edit')"
    )
    op.add_column(
        "story_backfills",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.create_index("ix_story_backfills_queue", "story_backfills", ["status", "seq"])


def downgrade() -> None:
    op.drop_index("ix_story_backfills_queue", table_name="story_backfills")
    op.drop_column("story_backfills", "seq")
    op.drop_constraint("ck_story_backfills_origin", "story_backfills", type_="check")
    op.drop_column("story_backfills", "origin")
