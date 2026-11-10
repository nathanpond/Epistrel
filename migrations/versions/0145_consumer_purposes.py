"""consumer purposes: registered model purposes and their versions (#824)

FR-MODEL-14: ``consumer_purposes`` holds one row per model purpose a consumer registered
(``/admin/purposes``): a ULID ``id``, a unique case-sensitive ``name``, who created it and when,
and ``archived_at`` (null while live). ``consumer_purpose_versions`` holds each immutable
version: ``(purpose_id, version)``, the ``description``, the ``template``, the optional
``system_template``, the ``output_schema`` (JSONB) and who created it and when. Neither table is
story-scoped, and neither holds story content.

Downgrade drops both tables (the versions first).

Revision ID: 0145
Revises: 0144
Create Date: 2026-11-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0145"
down_revision: str | None = "0144"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
PURPOSES = "consumer_purposes"
VERSIONS = "consumer_purpose_versions"
NOW = sa.text("transaction_timestamp()")


def upgrade() -> None:
    op.create_table(
        PURPOSES,
        sa.Column("id", ULID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_consumer_purposes"),
        sa.UniqueConstraint("name", name="uq_consumer_purposes_name"),
    )
    op.create_table(
        VERSIONS,
        sa.Column("purpose_id", ULID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("system_template", sa.Text(), nullable=True),
        sa.Column("output_schema", JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("purpose_id", "version", name="pk_consumer_purpose_versions"),
        sa.ForeignKeyConstraint(
            ["purpose_id"],
            [f"{PURPOSES}.id"],
            name="fk_consumer_purpose_versions_purpose_id_consumer_purposes",
        ),
        sa.CheckConstraint("version >= 1", name="ck_consumer_purpose_versions_version_positive"),
    )


def downgrade() -> None:
    op.drop_table(VERSIONS)
    op.drop_table(PURPOSES)
