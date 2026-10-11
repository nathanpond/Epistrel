"""jobs: durable background work with leases and fencing (#70)

``jobs`` holds every background job (NFR-REL-5): a due time, the lease and its fencing token
(``attempt``), failed-attempt count, the input versions the handler read (FR-CONC-8) and its
result. ``coalesce_key`` is unique among ``queued`` rows. ``story_id`` is plain (no foreign
key); story deletion enumerates it.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")
EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", ULID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("payload", postgresql.JSONB(), server_default=EMPTY, nullable=False),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failures", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("input_versions", postgresql.JSONB(), server_default=EMPTY, nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("coalesce_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name="ck_jobs_status"
        ),
    )
    op.create_index("ix_jobs_status_due_at", "jobs", ["status", "due_at"])
    op.create_index("ix_jobs_story_id", "jobs", ["story_id"])
    op.create_index(
        "uq_jobs_coalesce_key_queued",
        "jobs",
        ["coalesce_key"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_table("jobs")
