"""revisions: control-action tracking, story settings, attempts, one active variant per turn

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
ACTIVE_TURN_PREDICATE = "kind = 'turn' AND membership = 'active' AND status = 'committed'"


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "last_control_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column("stories", sa.Column("last_control_operation_id", ULID, nullable=True))
    op.add_column(
        "stories",
        sa.Column(
            "settings",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column("operations", "attempts", server_default=None)
    op.alter_column(
        "operations",
        "attempts",
        type_=postgresql.JSONB(),
        postgresql_using="'[]'::jsonb",
        existing_nullable=False,
    )
    op.alter_column("operations", "attempts", server_default=sa.text("'[]'::jsonb"))
    op.create_index(
        "uq_operations_active_turn",
        "operations",
        ["story_id", "narrative_turn"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_TURN_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_operations_active_turn", table_name="operations")
    op.alter_column("operations", "attempts", server_default=None)
    op.alter_column(
        "operations",
        "attempts",
        type_=sa.Integer(),
        postgresql_using="jsonb_array_length(attempts)",
        existing_nullable=False,
    )
    op.alter_column("operations", "attempts", server_default=sa.text("0"))
    op.drop_column("stories", "settings")
    op.drop_column("stories", "last_control_operation_id")
    op.drop_column("stories", "last_control_revision")
