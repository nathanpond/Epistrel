"""subject references record each mention's position (ADR-154; #372)

``subject_references`` gains ``span_start``, ``span_end`` (offsets in the field's NFC text, in
code points) and ``rule`` (``name``, ``part`` or ``candidate``: ``core.erasure.matcher``). Only a
mention has them, and never the text it matched. A record now has one row per mention, so the
unique key gains ``span_start``.

Rows indexed before this migration keep no position (the check allows it) until the one-time
re-index, ``python -m epistrel.tools.reindex_subjects --write`` (docs/OPERATIONS.md); an
erasure re-indexes its own story first in any case.

Downgrade drops the columns, after deleting all but one mention row per field (the old key).

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "subject_references"
OLD_KEY = "uq_subject_references_subject_record_field"
NEW_KEY = "uq_subject_references_subject_record_field_span"
KEY = ["subject_kind", "subject_id", "record_table", "record_id", "field"]
RULE_CHECK = "rule IS NULL OR rule IN ('name', 'part', 'candidate')"
SPAN_CHECK = (
    "(span_start IS NULL) = (span_end IS NULL) AND (span_start IS NULL) = (rule IS NULL) "
    "AND (span_start IS NULL OR (ref_kind = 'mentioned' AND 0 <= span_start "
    "AND span_start < span_end))"
)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("span_start", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("span_end", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("rule", sa.Text(), nullable=True))
    op.create_check_constraint("ck_subject_references_rule", TABLE, RULE_CHECK)
    op.create_check_constraint("ck_subject_references_span", TABLE, SPAN_CHECK)
    op.drop_constraint(OLD_KEY, TABLE, type_="unique")
    op.create_unique_constraint(
        NEW_KEY, TABLE, [*KEY, "span_start"], postgresql_nulls_not_distinct=True
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM subject_references a USING subject_references b "
        "WHERE a.subject_kind = b.subject_kind AND a.subject_id = b.subject_id "
        "AND a.record_table = b.record_table AND a.record_id = b.record_id "
        "AND a.field IS NOT DISTINCT FROM b.field AND a.id > b.id"
    )
    op.drop_constraint(NEW_KEY, TABLE, type_="unique")
    op.create_unique_constraint(OLD_KEY, TABLE, KEY, postgresql_nulls_not_distinct=True)
    op.drop_constraint("ck_subject_references_span", TABLE, type_="check")
    op.drop_constraint("ck_subject_references_rule", TABLE, type_="check")
    op.drop_column(TABLE, "rule")
    op.drop_column(TABLE, "span_end")
    op.drop_column(TABLE, "span_start")
