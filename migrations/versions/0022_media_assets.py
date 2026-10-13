"""media: immutable media assets and the subject-reference index (#105)

- ``media_assets``: one row per asset (FR-MEDIA-1,2): owner, kind, MIME type, size, dimensions
  or duration, SHA-256 checksum (the blob's address), content-rating version, alt text, caption,
  generation metadata, source, state (``live``, ``retired`` or ``candidate``) and the declared
  depicted subjects, with who created it when. Indexed by checksum, by owner, and by
  ``(state, created_at, id)`` for listings.
- ``subject_references``: the subject-reference index (FR-ADMIN-6) in #123's shape, unique per
  ``(subject_kind, subject_id, record_table, record_id, field)`` with NULLs not distinct, and
  indexed by ``(subject_id, scope, story_id)`` and ``(record_table, record_id)``.

Downgrade drops both tables (their rows go with them; blobs on disk are the operator's).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

# Frozen copies of epistrel.core.tables.media's vocabularies at this revision.
KIND_CHECK = "kind IN ('image', 'video')"
SOURCE_CHECK = "source IN ('uploaded', 'generated')"
STATE_CHECK = "state IN ('live', 'retired', 'candidate')"
CHECKSUM_CHECK = "checksum ~ '^[0-9a-f]{64}$'"
SUBJECT_KIND_CHECK = "subject_kind IN ('character', 'catalog_character', 'persona')"
SCOPE_CHECK = "scope IN ('catalog', 'story')"
REF_KIND_CHECK = "ref_kind IN ('authored', 'mentioned', 'declared')"
STORY_SCOPE_CHECK = "(story_id IS NOT NULL) = (scope = 'story')"


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", ULID, nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("checksum", sa.CHAR(64), nullable=False),
        sa.Column("content_rating_version_id", ULID, nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("generation_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("depicted_subjects", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint(KIND_CHECK, name="ck_media_assets_kind"),
        sa.CheckConstraint(SOURCE_CHECK, name="ck_media_assets_source"),
        sa.CheckConstraint(STATE_CHECK, name="ck_media_assets_state"),
        sa.CheckConstraint(CHECKSUM_CHECK, name="ck_media_assets_checksum"),
        sa.CheckConstraint("size >= 0", name="ck_media_assets_size"),
        sa.PrimaryKeyConstraint("id", name="pk_media_assets"),
    )
    op.create_index("ix_media_assets_checksum", "media_assets", ["checksum"])
    op.create_index("ix_media_assets_owner_principal", "media_assets", ["owner_principal"])
    op.create_index(
        "ix_media_assets_state_created_at", "media_assets", ["state", "created_at", "id"]
    )

    op.create_table(
        "subject_references",
        sa.Column("id", ULID, nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_id", ULID, nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("record_table", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("ref_kind", sa.Text(), nullable=False),
        sa.CheckConstraint(SUBJECT_KIND_CHECK, name="ck_subject_references_subject_kind"),
        sa.CheckConstraint(SCOPE_CHECK, name="ck_subject_references_scope"),
        sa.CheckConstraint(REF_KIND_CHECK, name="ck_subject_references_ref_kind"),
        sa.CheckConstraint(STORY_SCOPE_CHECK, name="ck_subject_references_story_scope"),
        sa.PrimaryKeyConstraint("id", name="pk_subject_references"),
        sa.UniqueConstraint(
            "subject_kind",
            "subject_id",
            "record_table",
            "record_id",
            "field",
            name="uq_subject_references_subject_record_field",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_subject_references_subject",
        "subject_references",
        ["subject_id", "scope", "story_id"],
    )
    op.create_index(
        "ix_subject_references_record", "subject_references", ["record_table", "record_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_subject_references_record", table_name="subject_references")
    op.drop_index("ix_subject_references_subject", table_name="subject_references")
    op.drop_table("subject_references")
    op.drop_index("ix_media_assets_state_created_at", table_name="media_assets")
    op.drop_index("ix_media_assets_owner_principal", table_name="media_assets")
    op.drop_index("ix_media_assets_checksum", table_name="media_assets")
    op.drop_table("media_assets")
