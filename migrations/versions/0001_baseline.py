"""baseline: pgvector extension and schema_meta

Revision ID: 0001
Revises:
Create Date: 2026-10-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from epistrel import __version__

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "schema_meta",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_schema_meta"),
    )
    op.execute(
        sa.text(
            "INSERT INTO schema_meta (key, value) VALUES ('engine_version', :version)"
        ).bindparams(version=__version__)
    )


def downgrade() -> None:
    op.drop_table("schema_meta")
    op.execute("DROP EXTENSION IF EXISTS vector")
