"""Image generation jobs and their provider attempts (#256)

FR-IMG-3,4: ``image_jobs`` holds one row per generation request (its kind, story, target,
origin, prompt, parameters, status, current attempt token, policy results and promoted assets),
and ``image_reservations`` one row per provider attempt (its send state, the provider's
reference, applied parameters and outcome), unique on ``(job_id, attempt_token)``. #262 adds the
accounting columns; #267/#268 extend the attempts. ``audit_log`` gains the kinds
``image_output_rejected`` and ``image_job_ended`` (ids, codes and policy ids only).

Revision ID: 0132
Revises: 0131
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0132"
down_revision: str | None = "0131"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
)
ADDED = ("image_output_rejected", "image_job_ended")
STATUSES = (
    "queued",
    "generating",
    "checking",
    "promoted",
    "candidates_ready",
    "failed",
    "stale",
    "target_gone",
    "cap_reached",
    "cancelled",
)
SEND_STATES = ("prepared", "sending", "accepted", "returned", "ambiguous")
OUTCOMES = ("promoted", "rejected", "failed", "discarded")


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.create_table(
        "image_jobs",
        sa.Column("id", sa.CHAR(26), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("story_id", sa.CHAR(26), nullable=True),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("origin", postgresql.JSONB(), nullable=False),
        sa.Column("viewpoint", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("attempt_token", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "policy_results",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "asset_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('catalog', 'story')", name="ck_image_jobs_kind"),
        sa.CheckConstraint(_in("status", STATUSES), name="ck_image_jobs_status"),
        sa.CheckConstraint("(story_id IS NOT NULL) = (kind = 'story')", name="ck_image_jobs_story"),
        sa.CheckConstraint(
            "kind <> 'story' OR (params->>'n')::int = 1", name="ck_image_jobs_story_n"
        ),
        sa.CheckConstraint(
            "status <> 'promoted' OR kind = 'story'", name="ck_image_jobs_promoted_story"
        ),
        sa.CheckConstraint(
            "status <> 'candidates_ready' OR kind = 'catalog'",
            name="ck_image_jobs_candidates_catalog",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_image_jobs_attempts"),
        sa.PrimaryKeyConstraint("id", name="pk_image_jobs"),
    )
    op.create_index("ix_image_jobs_status", "image_jobs", ["status"])
    op.create_index("ix_image_jobs_idempotency_key", "image_jobs", ["idempotency_key"])
    op.create_index("ix_image_jobs_story_id", "image_jobs", ["story_id"])
    op.create_table(
        "image_reservations",
        sa.Column("job_id", sa.CHAR(26), nullable=False),
        sa.Column("attempt_token", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("horizon_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("send_state", sa.Text(), nullable=False, server_default=sa.text("'prepared'")),
        sa.Column("attempt_ref", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("applied_parameters", postgresql.JSONB(), nullable=True),
        sa.Column("outputs_returned", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_id", "attempt_token", name="pk_image_reservations"),
        sa.CheckConstraint(_in("send_state", SEND_STATES), name="ck_image_reservations_send_state"),
        sa.CheckConstraint(
            "outcome IS NULL OR " + _in("outcome", OUTCOMES), name="ck_image_reservations_outcome"
        ),
    )
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, *ADDED)))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE kind IN ('image_output_rejected', 'image_job_ended')")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))
    op.drop_table("image_reservations")
    op.drop_index("ix_image_jobs_story_id", table_name="image_jobs")
    op.drop_index("ix_image_jobs_idempotency_key", table_name="image_jobs")
    op.drop_index("ix_image_jobs_status", table_name="image_jobs")
    op.drop_table("image_jobs")
