"""policies: the layered policy store

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("applies_to", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), server_default=sa.text("'deny'"), nullable=False),
        sa.Column("rule", postgresql.JSONB(), nullable=False),
        sa.Column("age_condition", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retired_by", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "scope IN ('global', 'rating', 'story', 'character')", name="ck_policies_scope"
        ),
        sa.CheckConstraint("(scope = 'global') = (scope_id IS NULL)", name="ck_policies_scope_id"),
        sa.CheckConstraint(
            "(scope IN ('story', 'character')) = (story_id IS NOT NULL)",
            name="ck_policies_story_id",
        ),
        sa.CheckConstraint("kind IN ('deterministic', 'model_assisted')", name="ck_policies_kind"),
        sa.CheckConstraint("effect = 'deny'", name="ck_policies_effect"),
        sa.CheckConstraint(
            "cardinality(applies_to) > 0 AND applies_to <@ "
            "ARRAY['text_input', 'text_output', 'image_prompt', 'image_output']::text[]",
            name="ck_policies_applies_to",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_policies"),
    )
    op.create_index(
        "uq_policies_live_name",
        "policies",
        ["scope", sa.text("coalesce(scope_id, '')"), sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index("ix_policies_story_id", "policies", ["story_id"])
    op.create_index(
        "uq_policies_idempotency_key",
        "policies",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_policies_idempotency_key", table_name="policies")
    op.drop_index("ix_policies_story_id", table_name="policies")
    op.drop_index("uq_policies_live_name", table_name="policies")
    op.drop_table("policies")
