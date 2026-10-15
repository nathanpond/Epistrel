"""idempotency keys keep the hash of the request as sent (#375)

``idempotency_keys.sent_hash``: the hash of the request as the client sent it, before the cast
and viewpoint were resolved. After an erasure changed what ``request_hash`` resolved, only a
retry of the same request as sent gets the original's reference; a different one is 409.
Nullable: rows written before this migration have none and keep #365's behaviour.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("idempotency_keys", sa.Column("sent_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("idempotency_keys", "sent_hash")
