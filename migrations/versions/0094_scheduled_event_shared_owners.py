"""scheduled events: shared owners (#198)

FR-CLOCK-19 (docs/02 §7.1): ownership defaults to the event's creator and may be declared
shared. ``scheduled_events`` gains ``shared_owners``: the characters an event is shared with,
who may change it as its owner may (``core.world_clock.schedule_statements``). Empty by default,
so every existing version keeps its sole owner. In M6 only the scheduled-events API (``POST`` and
``PATCH``) declares it.

Downgrade drops the column.

Revision ID: 0094
Revises: 0093
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0094"
down_revision: str | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_events",
        sa.Column(
            "shared_owners",
            postgresql.ARRAY(sa.CHAR(26)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("scheduled_events", "shared_owners")
