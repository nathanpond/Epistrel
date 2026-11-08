"""Catalog image candidates (#260)

FR-IMG-7: ``catalog.image_candidates`` holds one row per catalog candidate (its own ULID, the
entry, the job, the ``candidate``-state asset, the requested slot, the draft revision and
image-input fingerprint it was generated from, and its expiry), indexed by entry, job and expiry.
Its asset is a foreign key that cascades: deleting the asset row deletes the candidate row.
``image_jobs.result`` holds what a finished job made beyond its asset ids (a catalog job's
``{candidate_ids}``). The ``image_candidate_expiry`` recurring task's row is reconciled from the
code registry (#71), so no row is written here.

Revision ID: 0135
Revises: 0134
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0135"
down_revision: str | None = "0134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.add_column("image_jobs", sa.Column("result", postgresql.JSONB(), nullable=True))
    op.create_table(
        "image_candidates",
        sa.Column("id", sa.CHAR(26), nullable=False),
        sa.Column("entry_kind", sa.Text(), nullable=False),
        sa.Column("entry_id", sa.CHAR(26), nullable=False),
        sa.Column("job_id", sa.CHAR(26), nullable=False),
        sa.Column("asset_id", sa.CHAR(26), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("image_input_fingerprint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_image_candidates"),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["public.media_assets.id"],
            name="fk_image_candidates_asset_id_media_assets",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "entry_kind IN ('character', 'scenario')", name="ck_image_candidates_entry_kind"
        ),
        sa.CheckConstraint(
            "slot IN ('primary', 'gallery', 'cover')", name="ck_image_candidates_slot"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_image_candidates_entry", "image_candidates", ["entry_kind", "entry_id"], schema=SCHEMA
    )
    op.create_index("ix_image_candidates_job_id", "image_candidates", ["job_id"], schema=SCHEMA)
    op.create_index(
        "ix_image_candidates_expires_at", "image_candidates", ["expires_at"], schema=SCHEMA
    )
    op.create_index("ix_image_candidates_asset_id", "image_candidates", ["asset_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("image_candidates", schema=SCHEMA)
    op.drop_column("image_jobs", "result")
