"""idempotency results purged to SQL NULL (#361)

Purging a stored result wrote JSON ``null`` into ``idempotency_keys.result``, which the sweep's
``result IS NOT NULL`` found, rewrote and counted again on every run. The column now stores
SQL NULL for a purged result (``JSONB(none_as_null=True)``, a Python-side change: the DDL is
the same); this sets the rows already written as JSON ``null`` to SQL NULL.

Downgrade does nothing: SQL NULL is what every row without a result should hold.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-15

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE idempotency_keys SET result = NULL WHERE jsonb_typeof(result) = 'null'")


def downgrade() -> None:
    pass
