"""model_config: per-purpose model configuration with a seeded ``default`` row

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("transaction_timestamp()")


def upgrade() -> None:
    op.create_table(
        "model_config",
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column(
            "params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_by", sa.Text(), server_default=sa.text("'system'"), nullable=False),
        sa.PrimaryKeyConstraint("purpose", name="pk_model_config"),
    )
    # The single default model every unconfigured purpose falls back to (FR-MODEL-3). It starts
    # unset: an operator names the endpoint and model before the first model call.
    op.execute("INSERT INTO model_config (purpose) VALUES ('default')")


def downgrade() -> None:
    op.drop_table("model_config")
