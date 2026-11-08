"""Image costs: one cost row per provider attempt (#269)

FR-IMG-11, part D. ``image_costs`` holds one row per provider attempt that reached the provider,
unique on ``(job_id, attempt_token)``: its attribution (``story_id``, ``entry_kind``,
``entry_id``), its ``cost`` (``numeric(18,8)``; null exactly when ``cost_status`` is
``unknown``, 0 when ``zero``), ``currency`` (set only when ``known``), ``outputs_billed``,
``accounting`` (``normal`` or ``late_unaccounted``), the ``period_date`` it reports on, and when
it was ``recorded_at`` and ``filled_at``. No foreign keys: the rows are deployment accounting and
outlive their job, story and entry. Attempts settled before this migration have no row (their
cost stays in ``image_reservations.applied_parameters``).

Revision ID: 0142
Revises: 0141
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0142"
down_revision: str | None = "0141"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "image_costs"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.CHAR(26), nullable=False),
        sa.Column("job_id", sa.CHAR(26), nullable=False),
        sa.Column("attempt_token", UUID(as_uuid=False), nullable=False),
        sa.Column("story_id", sa.CHAR(26), nullable=True),
        sa.Column("entry_kind", sa.Text(), nullable=True),
        sa.Column("entry_id", sa.CHAR(26), nullable=True),
        sa.Column("cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("cost_status", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("outputs_billed", sa.Integer(), nullable=True),
        sa.Column("accounting", sa.Text(), nullable=False, server_default=sa.text("'normal'")),
        sa.Column("period_date", sa.Date(), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_image_costs"),
        sa.UniqueConstraint("job_id", "attempt_token", name="uq_image_costs_attempt"),
        sa.CheckConstraint(
            "cost_status IN ('known', 'zero', 'unknown')", name="ck_image_costs_cost_status"
        ),
        sa.CheckConstraint(
            "accounting IN ('normal', 'late_unaccounted')", name="ck_image_costs_accounting"
        ),
        sa.CheckConstraint(
            "(cost IS NULL) = (cost_status = 'unknown') AND (cost_status <> 'zero' OR cost = 0) "
            "AND (cost IS NULL OR cost >= 0)",
            name="ck_image_costs_cost_value",
        ),
        sa.CheckConstraint(
            "(currency IS NULL OR currency ~ '^[A-Z]{3}$') "
            "AND ((currency IS NOT NULL) = (cost_status = 'known'))",
            name="ck_image_costs_currency",
        ),
        sa.CheckConstraint(
            "(outputs_billed IS NULL OR outputs_billed >= 0) "
            "AND (cost_status <> 'zero' OR outputs_billed = 0)",
            name="ck_image_costs_outputs_billed",
        ),
        sa.CheckConstraint(
            "entry_kind IS NULL OR entry_kind IN ('character', 'scenario')",
            name="ck_image_costs_entry_kind",
        ),
    )
    op.create_index("ix_image_costs_period_date", TABLE, ["period_date"])
    op.create_index("ix_image_costs_story_id", TABLE, ["story_id"])
    op.create_index("ix_image_costs_entry", TABLE, ["entry_kind", "entry_id"])


def downgrade() -> None:
    op.drop_index("ix_image_costs_entry", table_name=TABLE)
    op.drop_index("ix_image_costs_story_id", table_name=TABLE)
    op.drop_index("ix_image_costs_period_date", table_name=TABLE)
    op.drop_table(TABLE)
