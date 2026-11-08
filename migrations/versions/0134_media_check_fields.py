"""The output check's fields on media assets, and image-checker certifications (#258)

FR-IMG-3,10: ``media_assets.source`` gains ``generated_from_consumer_prompt``; every asset gains
``check_status`` (``passed`` or ``unchecked``; existing rows ``unchecked``, keeping their
``source``), ``check_model`` and ``check_endpoint`` (both set when ``passed``) and
``policy_results`` (per applicable policy: its id, version, name, ``result: pass``, whether it
was uncertain, and whether the checker is certified for it; passes only, as a violating image is
never stored). ``image_check_certifications`` records which image-output policies a checker
fingerprint is certified for (NFR-QUAL-12; empty until #270). ``audit_log`` gains the kind
``image_upload_rejected`` (ids and policy ids only, never the image or its hash).

Revision ID: 0134
Revises: 0133
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0134"
down_revision: str | None = "0133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = ("uploaded", "generated", "extracted")
ADDED_SOURCES = ("generated_from_consumer_prompt",)
KINDS = (
    "guardrail_input_rejected",
    "story_deleted",
    "catalog_entry_deleted",
    "persona_deleted",
    "rating_change_refused",
    "erasure",
    "story_import",
    "backfill_cancelled",
    "backfill_failed",
    "image_output_rejected",
    "image_job_ended",
    "image_prompt_rejected",
)
ADDED_KINDS = ("image_upload_rejected",)


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.drop_constraint("ck_media_assets_source", "media_assets", type_="check")
    op.create_check_constraint(
        "ck_media_assets_source", "media_assets", _in("source", (*SOURCES, *ADDED_SOURCES))
    )
    op.add_column(
        "media_assets",
        sa.Column("check_status", sa.Text(), nullable=False, server_default="unchecked"),
    )
    op.add_column("media_assets", sa.Column("check_model", sa.Text(), nullable=True))
    op.add_column("media_assets", sa.Column("check_endpoint", sa.Text(), nullable=True))
    op.add_column(
        "media_assets",
        sa.Column(
            "policy_results",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_media_assets_check_status", "media_assets", _in("check_status", ("passed", "unchecked"))
    )
    op.create_check_constraint(
        "ck_media_assets_checked_by",
        "media_assets",
        "check_status <> 'passed' OR (check_model IS NOT NULL AND check_endpoint IS NOT NULL)",
    )
    op.create_table(
        "image_check_certifications",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("checker_fingerprint", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("report_sha256", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("transaction_timestamp()"),
        ),
        sa.Column("recorded_via", sa.Text(), nullable=False, server_default="job"),
        sa.Column("status", sa.Text(), nullable=False, server_default="valid"),
        sa.CheckConstraint(
            "status IN ('valid', 'stale')", name="ck_image_check_certifications_status"
        ),
        sa.CheckConstraint(
            "recorded_via IN ('job', 'import')", name="ck_image_check_certifications_recorded_via"
        ),
        sa.CheckConstraint(
            "checker_fingerprint ~ '^[0-9a-f]{12}$'",
            name="ck_image_check_certifications_checker_fingerprint",
        ),
    )
    op.create_index(
        "ix_image_check_certifications_fingerprint",
        "image_check_certifications",
        ["checker_fingerprint", "policy_id", "status"],
    )
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, *ADDED_KINDS))
    )


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE kind = 'image_upload_rejected'")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))
    op.drop_index("ix_image_check_certifications_fingerprint", "image_check_certifications")
    op.drop_table("image_check_certifications")
    op.drop_constraint("ck_media_assets_checked_by", "media_assets", type_="check")
    op.drop_constraint("ck_media_assets_check_status", "media_assets", type_="check")
    op.drop_column("media_assets", "policy_results")
    op.drop_column("media_assets", "check_endpoint")
    op.drop_column("media_assets", "check_model")
    op.drop_column("media_assets", "check_status")
    op.execute(
        "UPDATE media_assets SET source = 'generated' "
        "WHERE source = 'generated_from_consumer_prompt'"
    )
    op.drop_constraint("ck_media_assets_source", "media_assets", type_="check")
    op.create_check_constraint("ck_media_assets_source", "media_assets", _in("source", SOURCES))
