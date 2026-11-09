"""when and by which operation a reconciliation mark was resolved (#777)

FR-EXT-22: ``reconciliation_marks`` gains ``resolved_at`` (when an explicit action resolved the
mark; null while it is open, and on marks resolved before this migration with no record of when)
and ``resolved_operation_id`` (the operation that resolved it: #661's resolve action names its
inter-turn slot operation; a plain column, no foreign key, like the table's other ids). The
outbox list shows a parked ``imported_unsettled`` row's resolution from them (#777), so it
does not depend on the ``reconciliation_resolved`` audit event, which audit retention deletes.

Data: a mark the resolve action already closed (``resolved_by`` ``applied_elsewhere`` or
``abandoned``) takes both from its ``reconciliation_resolved`` event when that event is still
kept (the event's ``request_id`` names the mark's outbox row or request).

Downgrade drops both columns.

Revision ID: 0144
Revises: 0143
Create Date: 2026-11-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0144"
down_revision: str | None = "0143"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
MARKS = "reconciliation_marks"

BACKFILL = """
UPDATE reconciliation_marks m
SET resolved_at = e.tx_time, resolved_operation_id = e.operation_id
FROM events e
WHERE e.story_id = m.story_id
  AND e.type = 'reconciliation_resolved'
  AND e.payload->>'request_id' = coalesce(m.outbox_id, m.request_id)
  AND e.payload->>'resolution' = m.resolved_by
  AND m.resolved_by IN ('applied_elsewhere', 'abandoned')
"""


def upgrade() -> None:
    op.add_column(MARKS, sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(MARKS, sa.Column("resolved_operation_id", ULID, nullable=True))
    op.execute(sa.text(BACKFILL))


def downgrade() -> None:
    op.drop_column(MARKS, "resolved_operation_id")
    op.drop_column(MARKS, "resolved_at")
