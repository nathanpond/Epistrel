"""terminal outcomes, compensations and re-verification results (#218)

FR-EXT-22 (docs/02 §4.8): ``core.tables.outbox`` describes the changes.

- Two partial unique indexes on ``outbox_outcomes``: one settled terminal outcome (``confirmed``
  or ``failed``) per outbox row (``uq_outbox_outcomes_terminal_outbox``) and per consumer-applied
  effect (``uq_outbox_outcomes_terminal_effect``), within a story (a fork copies a
  consumer-applied effect's records under the same effect id). A later answer is recorded as
  ``duplicate`` or ``conflict``.
- ``external_effect_outbox.compensates`` (nullable, a plain column, no foreign key): the outbox
  row a compensating effect (a refund) answers, with ``ix_external_effect_outbox_compensates``.
- ``reconciliation_marks.detail`` (nullable JSONB): codes only, the re-verification a
  ``late_success_after_expiry`` mark carries (``{reverification, reasons}``).

No seed rows, no data change.

Downgrade drops the indexes and the columns.

Revision ID: 0103
Revises: 0102
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0103"
down_revision: str | None = "0102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
OUTCOMES = "outbox_outcomes"
OUTBOX = "external_effect_outbox"
MARKS = "reconciliation_marks"
TERMINAL = "kind IN ('confirmed', 'failed')"


def upgrade() -> None:
    op.create_index(
        f"uq_{OUTCOMES}_terminal_outbox",
        OUTCOMES,
        ["story_id", "outbox_id"],
        unique=True,
        postgresql_where=sa.text(f"{TERMINAL} AND outbox_id IS NOT NULL"),
    )
    op.create_index(
        f"uq_{OUTCOMES}_terminal_effect",
        OUTCOMES,
        ["story_id", "effect_id"],
        unique=True,
        postgresql_where=sa.text(f"{TERMINAL} AND effect_id IS NOT NULL"),
    )
    op.add_column(OUTBOX, sa.Column("compensates", ULID, nullable=True))
    op.create_index(f"ix_{OUTBOX}_compensates", OUTBOX, ["compensates"])
    op.add_column(MARKS, sa.Column("detail", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column(MARKS, "detail")
    op.drop_index(f"ix_{OUTBOX}_compensates", table_name=OUTBOX)
    op.drop_column(OUTBOX, "compensates")
    op.drop_index(f"uq_{OUTCOMES}_terminal_effect", table_name=OUTCOMES)
    op.drop_index(f"uq_{OUTCOMES}_terminal_outbox", table_name=OUTCOMES)
