"""A backfill's clock advances (#228, #709): the ``backfill`` State-change source

FR-PROF-9: a backfill that gains the world clock re-derives each reconciled turn's advance from
its transcript (#194's pacing over the Lite turn's combined render, or a narrated skip) and
writes it with ``source = backfill``, below every live source.

The value is added, never removed: Postgres cannot drop an enum value, and the type itself goes
with the migration that created it (0084).

Revision ID: 0129
Revises: 0128
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0129"
down_revision: str | None = "0128"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE state_change_source ADD VALUE IF NOT EXISTS 'backfill'")


def downgrade() -> None:
    """Nothing: an enum value cannot be dropped (0084's downgrade drops the type), and
    ``state_changes`` is append-only, so a row that names it stays as recorded."""
