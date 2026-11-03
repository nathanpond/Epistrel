"""message_feedback.compacted_at is renamed pruned_at (#246)

The owner's decision (round two, 2026-10-10): the FR-STORE-32 operation is called pruning. M3's
#112 shipped its FR-FB-2 hook under the old name; the column a pruning sets on a rating whose
message it removed is renamed with it. Migration 0029 is history and is not edited.

Downgrade renames it back.

Revision ID: 0109
Revises: 0108
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0109"
down_revision: str | None = "0108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("message_feedback", "compacted_at", new_column_name="pruned_at")


def downgrade() -> None:
    op.alter_column("message_feedback", "pruned_at", new_column_name="compacted_at")
