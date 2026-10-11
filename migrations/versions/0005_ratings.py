"""ratings: content ratings and versions, deployment state, config changes, story rating

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.ratings import DEFAULT_RATING_ID, SEED_PRINCIPAL, SEED_RATINGS

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")
EMPTY_ARRAY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.create_table(
        "content_ratings",
        sa.Column("id", ULID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("current_version_id", ULID, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retired_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_content_ratings"),
    )
    op.create_index(
        "uq_content_ratings_live_name",
        "content_ratings",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_table(
        "content_rating_versions",
        sa.Column("version_id", ULID, nullable=False),
        sa.Column("rating_id", ULID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("prompt_guidance", sa.Text(), nullable=False),
        sa.Column("image_guidance", sa.Text(), nullable=False),
        sa.Column("policy_ids", postgresql.JSONB(), server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("inherits_from", postgresql.JSONB(), server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("version_id", name="pk_content_rating_versions"),
        sa.UniqueConstraint(
            "rating_id", "version", name="uq_content_rating_versions_rating_version"
        ),
    )
    op.create_table(
        "deployment_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("config_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("default_content_rating_id", ULID, nullable=False),
        sa.Column(
            "minor_age_threshold", sa.Integer(), server_default=sa.text("18"), nullable=False
        ),
        sa.Column(
            "settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_deployment_state_single_row"),
        sa.PrimaryKeyConstraint("id", name="pk_deployment_state"),
    )
    op.create_table(
        "config_changes",
        sa.Column("change_id", ULID, nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("config_revision", sa.BigInteger(), nullable=False),
        sa.Column("setting", sa.Text(), nullable=False),
        sa.Column("old", postgresql.JSONB(), nullable=True),
        sa.Column("new", postgresql.JSONB(), nullable=True),
        sa.Column("class", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "class IN ('interpretive', 'prospective')", name="ck_config_changes_change_class"
        ),
        sa.PrimaryKeyConstraint("change_id", name="pk_config_changes"),
    )
    op.create_index(
        "ix_config_changes_scope_revision",
        "config_changes",
        ["scope", "scope_id", "config_revision"],
    )
    op.add_column(
        "stories",
        sa.Column(
            "content_rating_id",
            ULID,
            server_default=sa.text(f"'{DEFAULT_RATING_ID}'"),
            nullable=False,
        ),
    )
    _seed()


def _seed() -> None:
    """The four starter ratings and the deployment row; no ``config_changes`` rows, revision 0."""
    heads = sa.table(
        "content_ratings",
        sa.column("id"),
        sa.column("name"),
        sa.column("current_version_id"),
    )
    versions = sa.table(
        "content_rating_versions",
        sa.column("version_id"),
        sa.column("rating_id"),
        sa.column("version"),
        sa.column("name"),
        sa.column("description"),
        sa.column("display_order"),
        sa.column("prompt_guidance"),
        sa.column("image_guidance"),
        sa.column("policy_ids", postgresql.JSONB()),
        sa.column("inherits_from", postgresql.JSONB()),
        sa.column("created_by"),
    )
    state = sa.table("deployment_state", sa.column("id"), sa.column("default_content_rating_id"))
    op.bulk_insert(
        heads,
        [{"id": r.id, "name": r.name, "current_version_id": r.version_id} for r in SEED_RATINGS],
    )
    op.bulk_insert(
        versions,
        [
            {
                "version_id": r.version_id,
                "rating_id": r.id,
                "version": 1,
                "name": r.name,
                "description": r.description,
                "display_order": r.display_order,
                "prompt_guidance": r.prompt_guidance,
                "image_guidance": r.image_guidance,
                "policy_ids": list(r.policy_ids),
                "inherits_from": list(r.inherits_from),
                "created_by": SEED_PRINCIPAL,
            }
            for r in SEED_RATINGS
        ],
    )
    op.bulk_insert(state, [{"id": 1, "default_content_rating_id": DEFAULT_RATING_ID}])


def downgrade() -> None:
    op.drop_column("stories", "content_rating_id")
    op.drop_index("ix_config_changes_scope_revision", table_name="config_changes")
    op.drop_table("config_changes")
    op.drop_table("deployment_state")
    op.drop_table("content_rating_versions")
    op.drop_index("uq_content_ratings_live_name", table_name="content_ratings")
    op.drop_table("content_ratings")
