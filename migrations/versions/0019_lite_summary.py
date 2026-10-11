"""lite_summary_segments: the Lite rolling summary's projection (#72)

One row per ``lite_summary_segment`` event: the turns it covers, its level, the variant of the
turn it is positioned at, its text, its sources and its membership (``superseded`` while an
active merged segment holds it). A cache over the log, rebuilt by fold like the other
projections; no seed.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    op.create_table(
        "lite_summary_segments",
        sa.Column("segment_id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("covers_from", sa.Integer(), nullable=False),
        sa.Column("covers_to", sa.Integer(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("turn_variant", sa.SmallInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "membership", postgresql.ENUM(name="membership", create_type=False), nullable=False
        ),
        sa.Column("source_versions", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_hash", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("segment_id", name="pk_lite_summary_segments"),
    )
    op.create_index(
        "ix_lite_summary_segments_story_covers",
        "lite_summary_segments",
        ["story_id", "covers_to"],
    )
    op.create_index(
        "uq_lite_summary_segments_position",
        "lite_summary_segments",
        ["story_id", "covers_from", "covers_to", "level", "turn_variant"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("lite_summary_segments")
