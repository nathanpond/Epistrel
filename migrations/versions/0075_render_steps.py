"""the step index on the M4 position-bearing records (#168)

FR-STORE-31, FR-TURN-12 (docs/02 §4.4): within a turn a narrative position is ``(turn, beat,
step)``; ``step`` is the declared causal step of a render (the ``act`` step that owns a beat; 0
outside declared steps). ``facts`` (0072), ``beliefs``, ``belief_transitions``, ``exposures``,
``divergences`` and ``events`` already carry it; this adds ``step`` (NOT NULL, default 0) to
``memories``, ``utterances``, ``thunks`` and ``promissory_ledger``, so every record a beat derives
sits at the beat's step. Existing rows are outside declared steps (0).

Downgrade drops the columns.

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("memories", "utterances", "thunks", "promissory_ledger")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("step", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_column(table, "step")
