"""Lite rewrites' world notes (#700): the ``lite_note`` amendment part

FR-LITE-8: a Lite history rewrite that states a world change records one ``lite_note`` part
("Author correction: ...") beside its record parts. It carries ``patch_after = {text}`` only:
no record, proposition or State column (``ck_amendment_parts_lite_note``).

Revision ID: 0128
Revises: 0127
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0128"
down_revision: str | None = "0127"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOTE = (
    "kind <> 'lite_note' OR (subject_id IS NULL AND predicate_id IS NULL "
    "AND proposition_id IS NULL AND state_id IS NULL AND record_table IS NULL "
    "AND record_id IS NULL AND action IS NULL AND strategy IS NULL "
    "AND written_fact_id IS NULL AND written_state_change_id IS NULL "
    "AND patch_after ? 'text' AND patch_after - 'text' = '{}'::jsonb)"
)


def upgrade() -> None:
    op.drop_constraint("ck_amendment_parts_kind", "amendment_parts", type_="check")
    op.create_check_constraint(
        "ck_amendment_parts_kind",
        "amendment_parts",
        "kind IN ('proposition', 'record', 'lite_note')",
    )
    op.create_check_constraint("ck_amendment_parts_lite_note", "amendment_parts", NOTE)


def downgrade() -> None:
    op.execute("DELETE FROM amendment_parts WHERE kind = 'lite_note'")
    op.drop_constraint("ck_amendment_parts_lite_note", "amendment_parts", type_="check")
    op.drop_constraint("ck_amendment_parts_kind", "amendment_parts", type_="check")
    op.create_check_constraint(
        "ck_amendment_parts_kind", "amendment_parts", "kind IN ('proposition', 'record')"
    )
