"""Story bundles (FR-STORE-7, FR-ADMIN-4; #249, migration 0125).

``bundle_jobs``: one row per export of a story: its status (``queued``, ``running``, ``ready``,
``failed``, ``expired``), the revision it read (``exported_revision``), where its archive is
(``path``, a file under ``EPISTREL_BUNDLE_DIR``), its size and SHA-256, its manifest, the error
that ended it, who asked and when it expires. A story's deletion deletes its rows (and files);
a fork copies none.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

NOW = text("transaction_timestamp()")

bundle_jobs = Table(
    "bundle_jobs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("kind", Text, nullable=False, server_default=text("'export'")),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("job_id", ulid_type(), nullable=True),
    Column("exported_revision", BigInteger, nullable=True),
    Column("path", Text, nullable=True),
    Column("size", BigInteger, nullable=True),
    Column("sha256", Text, nullable=True),
    Column("manifest", JSONB, nullable=True),
    Column("error", JSONB, nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("requested_by", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("finished_at", TIMESTAMP(timezone=True), nullable=True),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint("kind IN ('export')", name="kind"),
    CheckConstraint("status IN ('queued', 'running', 'ready', 'failed', 'expired')", name="status"),
    Index("ix_bundle_jobs_story", "story_id", "created_at"),
)

__all__ = ["bundle_jobs"]
