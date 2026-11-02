"""consumer-applied effects in the reconciliation status (#661)

FR-EXT-22 (docs/02 §4.8): ``core.tables.outbox`` describes the change (owner decision on #661,
2026-11-01). A consumer-applied external effect (#209's ``requested_effect``, no outbox row)
is listed for reconciliation as an outbox row is, so a ``reconciliation_marks`` row names one of
the two:

- ``reconciliation_marks.outbox_id`` becomes nullable and ``request_id`` (the consumer-applied
  effect's id, a ULID; a plain column, no foreign key) is added, with the CHECK
  ``ck_reconciliation_marks_one_effect`` (exactly one of them);
- ``uq_reconciliation_marks_open_request``: one open mark per request and reason (the outbox
  rows' ``uq_reconciliation_marks_open`` is unchanged), and
  ``ix_reconciliation_marks_story_request``.

No data change: every existing mark names an outbox row.

Downgrade deletes the request marks, drops the indexes, the CHECK and the column, and makes
``outbox_id`` NOT NULL again.

Revision ID: 0105
Revises: 0104
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0105"
down_revision: str | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
MARKS = "reconciliation_marks"


def upgrade() -> None:
    op.alter_column(MARKS, "outbox_id", existing_type=ULID, nullable=True)
    op.add_column(MARKS, sa.Column("request_id", ULID, nullable=True))
    op.create_check_constraint(
        f"ck_{MARKS}_one_effect", MARKS, "(outbox_id IS NULL) <> (request_id IS NULL)"
    )
    op.create_index(
        f"uq_{MARKS}_open_request",
        MARKS,
        ["request_id", "reason"],
        unique=True,
        postgresql_where=sa.text("resolved_by IS NULL AND request_id IS NOT NULL"),
    )
    op.create_index(f"ix_{MARKS}_story_request", MARKS, ["story_id", "request_id"])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM reconciliation_marks WHERE request_id IS NOT NULL"))
    op.drop_index(f"ix_{MARKS}_story_request", table_name=MARKS)
    op.drop_index(f"uq_{MARKS}_open_request", table_name=MARKS)
    op.drop_constraint(f"ck_{MARKS}_one_effect", MARKS, type_="check")
    op.drop_column(MARKS, "request_id")
    op.alter_column(MARKS, "outbox_id", existing_type=ULID, nullable=False)
