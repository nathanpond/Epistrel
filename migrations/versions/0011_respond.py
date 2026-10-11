"""respond: the operation's idempotency key and a reply's render characters (#57)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written once at ``begin()``; #61's ``idempotency_keys`` table adds replay on top.
    op.add_column("operations", sa.Column("idempotency_key", sa.Text(), nullable=True))
    # The render characters of a reply beat (null on input and narration beats).
    op.add_column(
        "transcript_messages",
        sa.Column("render_character_ids", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcript_messages", "render_character_ids")
    op.drop_column("operations", "idempotency_key")
