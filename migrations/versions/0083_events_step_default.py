"""``events.step`` defaults to 0 for new rows (#548; #168's FR-STORE-31)

FR-STORE-31 (#168 AC2a) stores ``(turn, variant, beat, step)`` on every position-bearing record,
and a record outside a render's declared steps has ``step = 0``. The other position-bearing tables
carry ``step smallint NOT NULL DEFAULT 0`` (0069, 0072, 0075); ``events.step`` (0002) is nullable
with no default, so an event outside declared steps was stored ``NULL``.

- ``events.step`` gets ``DEFAULT 0``: an insert that leaves it out stores 0. A catalog-only
  change; no row is rewritten.

The column stays nullable and the existing rows keep their ``NULL``: the event log is append-only
and the truth (invariant 6), so this migration never rewrites events, and a ``NOT NULL`` (or a
``NOT VALID`` check, which Postgres still enforces when erasure rewrites an old row in place)
would need exactly that. Readers treat a legacy ``NULL`` as 0 (``core.views``).

Downgrade drops the default.

Revision ID: 0083
Revises: 0082
Create Date: 2026-10-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("events", "step", server_default=sa.text("0"))


def downgrade() -> None:
    op.alter_column("events", "step", server_default=None)
