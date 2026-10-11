"""timers, recurring_tasks, worker_heartbeats: in-process timed lifecycle work (#71)

``timers`` holds one-shot due times (unique ``(kind, target)``), ``recurring_tasks`` the sweeps
with their interval and next due time, ``worker_heartbeats`` one row per live worker loop
(NFR-REL-5). Seeds: ``idempotency_purge`` (hourly) and ``retention_sweep`` (daily), both due now,
so the first worker to start runs them.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")

SEEDS = (
    ("idempotency_purge", "1 hour"),
    ("retention_sweep", "1 day"),
)


def upgrade() -> None:
    op.create_table(
        "timers",
        sa.Column("id", ULID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_timers"),
        sa.UniqueConstraint("kind", "target", name="uq_timers_kind_target"),
    )
    op.create_index("ix_timers_due_at", "timers", ["due_at"])
    op.create_index("ix_timers_story_id", "timers", ["story_id"])
    op.create_table(
        "recurring_tasks",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("interval", sa.Interval(), nullable=False),
        sa.Column("next_run_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name", name="pk_recurring_tasks"),
        sa.CheckConstraint(
            "last_status IN ('ok', 'failed')", name="ck_recurring_tasks_last_status"
        ),
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name="pk_worker_heartbeats"),
    )
    op.create_index("ix_worker_heartbeats_at", "worker_heartbeats", ["at"])
    for name, interval in SEEDS:
        op.execute(
            sa.text(
                "INSERT INTO recurring_tasks (name, interval, next_run_at) "
                "VALUES (:name, CAST(:interval AS interval), transaction_timestamp())"
            ).bindparams(name=name, interval=interval)
        )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("recurring_tasks")
    op.drop_table("timers")
