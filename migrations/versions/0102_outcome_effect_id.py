"""outbox_outcomes: a consumer-applied effect's outcome (#217)

FR-EXT-21: a late outcome is recorded as an ``outbox_outcomes`` row. A consumer-applied effect
(#209's ``requested_effect`` on a read-only provider's value) has no outbox row, so its outcome,
reported through ``ingest {kind: outcome, effect_id}``, names the effect instead:

- ``outbox_outcomes.effect_id`` (nullable) beside ``outbox_id``, which becomes nullable;
- a check that exactly one of them is set (``ck_outbox_outcomes_one_effect``);
- ``ix_outbox_outcomes_effect`` on ``(effect_id, received_at)``.

The append-only trigger (0095) is unchanged: it compares whole rows less ``settled``.

Downgrade deletes the consumer-applied outcomes (they have no outbox row), then restores the
column as it was.

Revision ID: 0102
Revises: 0101
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0102"
down_revision: str | None = "0101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = "outbox_outcomes"


def upgrade() -> None:
    op.add_column(OUTCOMES, sa.Column("effect_id", sa.CHAR(26), nullable=True))
    op.alter_column(OUTCOMES, "outbox_id", existing_type=sa.CHAR(26), nullable=True)
    op.create_check_constraint(
        f"ck_{OUTCOMES}_one_effect", OUTCOMES, "(outbox_id IS NULL) <> (effect_id IS NULL)"
    )
    op.create_index(f"ix_{OUTCOMES}_effect", OUTCOMES, ["effect_id", "received_at"])


def downgrade() -> None:
    op.execute("SET LOCAL epistrel.erasure = 'on'")
    op.execute(f"DELETE FROM {OUTCOMES} WHERE outbox_id IS NULL")  # noqa: S608 - fixed name
    op.drop_index(f"ix_{OUTCOMES}_effect", table_name=OUTCOMES)
    op.drop_constraint(f"ck_{OUTCOMES}_one_effect", OUTCOMES, type_="check")
    op.alter_column(OUTCOMES, "outbox_id", existing_type=sa.CHAR(26), nullable=False)
    op.drop_column(OUTCOMES, "effect_id")
