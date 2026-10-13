"""scenario versions keep their validation report (#111)

``catalog.scenario_versions.report`` (``jsonb``, nullable): the validation report the publish
that made the version produced (FR-SCN-5): no errors, and the warnings and the checks the
deployment could not run, as the publish answered them. Versions published before have none
(``NULL``). The versions stay immutable: the column is written with the row, never after.

Downgrade drops the column.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.add_column(
        "scenario_versions",
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("scenario_versions", "report", schema=SCHEMA)
