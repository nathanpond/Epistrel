"""A backfill's narrated State changes and its end records (#228)

FR-PROF-9: a backfill that gains States re-derives the changes a turn narrates for story-owned
States (#190's completeness detector, validated by #187 and authorized by #188) and writes them
with ``source = backfill``. ``state_changes`` gains ``inferred`` to mark a change read from the
transcript rather than declared by a render. A cancelled or failed backfill writes an
``audit_log`` row (kinds ``backfill_cancelled`` and ``backfill_failed``: ids and counts only).

Revision ID: 0131
Revises: 0130
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0131"
down_revision: str | None = "0130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "guardrail_input_rejected",
    "story_deleted",
    "catalog_entry_deleted",
    "persona_deleted",
    "rating_change_refused",
    "erasure",
    "story_import",
)
ADDED = ("backfill_cancelled", "backfill_failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.add_column(
        "state_changes",
        sa.Column("inferred", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, *ADDED)))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE kind IN ('backfill_cancelled', 'backfill_failed')")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))
    op.drop_column("state_changes", "inferred")
