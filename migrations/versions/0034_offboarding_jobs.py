"""offboarding_jobs: one row per principal offboarding, its snapshot, progress and report (#117)

- ``offboarding_jobs`` (FR-AUTH-7; docs/02 §4.10): ``id`` is the #70 job's id, with no foreign
  key, because finished ``jobs`` rows are purged after 7 days while the report is kept for
  ``offboarding_report_retention_days``. The columns are ``principal``, ``created_by``,
  ``actions``, ``transfer_to``, ``request``, ``status`` (#70's enum, checked), ``snapshot``,
  ``progress``, ``counts``, ``report``, ``error``, ``operation_id`` (the request's committed
  ``control`` operation), ``created_at``, ``updated_at`` and ``finished_at``.
- ``uq_offboarding_jobs_principal_active``: at most one queued or running offboarding per
  principal. ``ix_offboarding_jobs_finished_at`` serves the retention sweep.

``ownership_events.job_id`` already exists (#104), so nothing is added there.

Downgrade drops the table.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
STATUS_CHECK = "status IN ('queued', 'running', 'done', 'failed')"


def upgrade() -> None:
    op.create_table(
        "offboarding_jobs",
        sa.Column("id", ULID, nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("actions", postgresql.JSONB(), nullable=False),
        sa.Column("transfer_to", sa.Text(), nullable=True),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("progress", postgresql.JSONB(), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(STATUS_CHECK, name="ck_offboarding_jobs_status"),
        sa.PrimaryKeyConstraint("id", name="pk_offboarding_jobs"),
    )
    op.create_index(
        "uq_offboarding_jobs_principal_active",
        "offboarding_jobs",
        ["principal"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_offboarding_jobs_finished_at", "offboarding_jobs", ["finished_at"])


def downgrade() -> None:
    op.drop_index("ix_offboarding_jobs_finished_at", table_name="offboarding_jobs")
    op.drop_index("uq_offboarding_jobs_principal_active", table_name="offboarding_jobs")
    op.drop_table("offboarding_jobs")
