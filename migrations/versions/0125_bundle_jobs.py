"""bundle_jobs: story exports (#249)

FR-STORE-7, FR-ADMIN-4: an export of a story is a background job writing one checksummed,
versioned archive; ``bundle_jobs`` records it.

Revision ID: 0125
Revises: 0124
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0125"
down_revision: str | None = "0124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")


def upgrade() -> None:
    op.create_table(
        "bundle_jobs",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'export'"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("job_id", ULID, nullable=True),
        sa.Column("exported_revision", sa.BigInteger(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('export')", name="ck_bundle_jobs_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed', 'expired')",
            name="ck_bundle_jobs_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bundle_jobs"),
    )
    op.create_index("ix_bundle_jobs_story", "bundle_jobs", ["story_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_bundle_jobs_story", table_name="bundle_jobs")
    op.drop_table("bundle_jobs")
