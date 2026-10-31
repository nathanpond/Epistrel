"""reconciliation marks, and the outbox's ``erased_at`` (#211)

FR-EXT-12,13,14,21 (docs/02 §4.8): ``core.tables.outbox`` describes both changes.

- ``reconciliation_marks`` is created: why an outbox row is listed for reconciliation when its
  status alone does not say (``reconciliation_required``, ``cancelled_on_reactivation``, and the
  later stories' reasons), since when, and the explicit action that resolved it (null while
  open). One open mark per row and reason (the partial unique index
  ``uq_reconciliation_marks_open``). Ids and reason codes only; no foreign key.
- ``external_effect_outbox.erased_at`` is added (nullable): when an erasure redacted a claimed
  or ``unknown`` row, or abandoned its dispatch mid-flight. Such a row is never sent again.

No seed rows, no data change.

Downgrade drops the table and the column.

Revision ID: 0098
Revises: 0097
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
MARKS = "reconciliation_marks"
OUTBOX = "external_effect_outbox"
REASONS = (
    "unconfirmed",
    "failed",
    "unknown",
    "reconciliation_required",
    "cancelled_on_reactivation",
    "confirmed_unsettled",
    "confirmed_discarded_origin",
    "settlement_conflict",
    "late_success_after_expiry",
    "outcome_conflict",
    "reservation_expired",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.add_column(OUTBOX, sa.Column("erased_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_table(
        MARKS,
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("outbox_id", ULID, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "since",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.CheckConstraint(_in("reason", REASONS), name=f"ck_{MARKS}_reason"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{MARKS}"),
    )
    op.create_index(f"ix_{MARKS}_story", MARKS, ["story_id", "outbox_id"])
    op.create_index(
        f"uq_{MARKS}_open",
        MARKS,
        ["outbox_id", "reason"],
        unique=True,
        postgresql_where=sa.text("resolved_by IS NULL"),
    )


def downgrade() -> None:
    op.drop_table(MARKS)
    op.drop_column(OUTBOX, "erased_at")
