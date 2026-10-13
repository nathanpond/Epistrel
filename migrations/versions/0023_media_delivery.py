"""media delivery: video posters, resumable upload sessions and the store kind (#107)

- ``media_assets.poster_media_id``: a video's poster (FR-MEDIA-3), a plain reference to an image
  asset, indexed for the media sweep's reference check. ``source`` gains ``extracted`` (a poster
  ffmpeg took from the video's first frame).
- ``media_upload_sessions``: the resumable uploads (``POST /media/uploads``): the asset's
  metadata, declared size, MIME type and SHA-256, the part size and the parts received, the
  state (``open``, ``completed`` or ``aborted``), the expiry, the created asset, and an
  S3-compatible store's multipart ``UploadId``. Indexed by ``(state, expires_at)`` for the sweep.
- ``deployment_state.media_store``: the blob store kind (``local`` or ``s3``) the media live
  in, recorded at start-up. It is set to ``local`` here when any asset exists, because the local
  store was the only one before this revision.

Downgrade drops the sessions table and both columns; extracted posters become ``uploaded`` so
the earlier ``source`` check holds again (their rows stay; blobs are the operator's).

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

# Frozen copies of epistrel.core.tables.media's vocabularies at this revision (and the one
# before, which the downgrade restores).
SOURCE_CHECK = "source IN ('uploaded', 'generated', 'extracted')"
SOURCE_CHECK_0022 = "source IN ('uploaded', 'generated')"
UPLOAD_STATE_CHECK = "state IN ('open', 'completed', 'aborted')"
DECLARED_SHA256_CHECK = "declared_sha256 ~ '^[0-9a-f]{64}$'"
MEDIA_STORE_CHECK = "media_store IN ('local', 's3')"


def upgrade() -> None:
    op.add_column("media_assets", sa.Column("poster_media_id", ULID, nullable=True))
    op.create_index("ix_media_assets_poster_media_id", "media_assets", ["poster_media_id"])
    op.drop_constraint("ck_media_assets_source", "media_assets", type_="check")
    op.create_check_constraint("ck_media_assets_source", "media_assets", SOURCE_CHECK)

    op.create_table(
        "media_upload_sessions",
        sa.Column("id", ULID, nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("declared_sha256", sa.CHAR(64), nullable=False),
        sa.Column("part_size", sa.Integer(), nullable=False),
        sa.Column("received_parts", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("s3_upload_id", sa.Text(), nullable=True),
        sa.Column("media_id", ULID, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(UPLOAD_STATE_CHECK, name="ck_media_upload_sessions_state"),
        sa.CheckConstraint(DECLARED_SHA256_CHECK, name="ck_media_upload_sessions_declared_sha256"),
        sa.CheckConstraint("size > 0", name="ck_media_upload_sessions_size"),
        sa.CheckConstraint("part_size > 0", name="ck_media_upload_sessions_part_size"),
        sa.PrimaryKeyConstraint("id", name="pk_media_upload_sessions"),
    )
    op.create_index(
        "ix_media_upload_sessions_state_expires_at",
        "media_upload_sessions",
        ["state", "expires_at"],
    )

    op.add_column("deployment_state", sa.Column("media_store", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_deployment_state_media_store", "deployment_state", MEDIA_STORE_CHECK
    )
    op.execute(
        "UPDATE deployment_state SET media_store = 'local' "
        "WHERE EXISTS (SELECT 1 FROM media_assets)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_deployment_state_media_store", "deployment_state", type_="check")
    op.drop_column("deployment_state", "media_store")

    op.drop_index("ix_media_upload_sessions_state_expires_at", table_name="media_upload_sessions")
    op.drop_table("media_upload_sessions")

    op.execute("UPDATE media_assets SET source = 'uploaded' WHERE source = 'extracted'")
    op.drop_constraint("ck_media_assets_source", "media_assets", type_="check")
    op.create_check_constraint("ck_media_assets_source", "media_assets", SOURCE_CHECK_0022)
    op.drop_index("ix_media_assets_poster_media_id", table_name="media_assets")
    op.drop_column("media_assets", "poster_media_id")
