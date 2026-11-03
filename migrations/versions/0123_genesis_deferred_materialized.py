"""genesis_deferred.materialized_operation_id (#231)

FR-PROF-18: a deferred genesis item records the operation that materialized it when the story
gains its capability (a backfill's, reset when the backfill is cancelled, or a configuration
change's).

Revision ID: 0123
Revises: 0122
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0123"
down_revision: str | None = "0122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "genesis_deferred",
        sa.Column("materialized_operation_id", sa.CHAR(26), nullable=True),
    )
    op.create_index(
        "ix_genesis_deferred_materialized_operation",
        "genesis_deferred",
        ["materialized_operation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_genesis_deferred_materialized_operation", table_name="genesis_deferred")
    op.drop_column("genesis_deferred", "materialized_operation_id")
