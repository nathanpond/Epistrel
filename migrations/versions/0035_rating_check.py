"""rating checks: changing a story's rating, the history check, the pause (#118)

- ``rating_check_jobs`` (FR-RATE-7; docs/02 §4.10): one row per tightening check of a story's
  rating. ``id`` is the #70 job's id, with no foreign key: finished ``jobs`` rows are purged
  after 7 days, while a check's status and violations are kept for
  ``rating_check_retention_days`` (30). The columns are ``story_id``, ``from_rating_id``,
  ``target_rating_id``, ``target_rating_version`` (the version id the check evaluates),
  ``status`` (``checking``, ``applied``, ``refused``, ``failed``), ``progress`` (``{done,
  total}``), ``violations`` (positions and policy ids, never text), ``result``, ``error``,
  ``started_by``, ``started_at``, ``finished_at``, ``operation_id`` (the request's committed
  ``control`` operation) and ``checked_revision`` (the timeline revision the evaluation read).
  ``uq_rating_check_jobs_story_checking``: at most one check per story at a time.
- ``guardrail_check_results``: the history check's verdict cache, one row per (story, record,
  policy, policy version), deleted with the story.
- ``operations.checked_guardrails``: the effective guardrail set a turn variant last passed
  (``{rating_id, rating_version_id, global, rating, story, character}``, each ``{policy id:
  version}``). It joins the operations trigger's allow-list, so a tightening that applies can
  restamp the active turns.
- ``stories.paused_by``: the check job that paused the story (``status = 'paused'``).
- ``rating_change_refused`` joins the ``audit_log.kind`` check.

Downgrade drops them all, restores 0016's trigger allow-list, and deletes the
``rating_change_refused`` audit rows (under ``epistrel.allow_audit_expiry``).

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
STATUS_CHECK = "status IN ('checking', 'applied', 'refused', 'failed')"
VERDICT_CHECK = "verdict IN ('pass', 'violation')"
KIND_CHECK_BEFORE = (
    "kind IN ('guardrail_input_rejected', 'story_deleted', 'catalog_entry_deleted', "
    "'persona_deleted')"
)
KIND_CHECK = (
    "kind IN ('guardrail_input_rejected', 'story_deleted', 'catalog_entry_deleted', "
    "'persona_deleted', 'rating_change_refused')"
)

BEFORE = (
    "status",
    "membership",
    "membership_tx",
    "narrative_turn",
    "turn_variant",
    "base_revision",
    "read_set",
    "config_revisions",
    "committed_at",
    "attempts",
    "turn_report",
    "viewpoint",
    "timeline_revision",
    "error_code",
    "error",
    "parent_variant",
)
AFTER = (*BEFORE, "checked_guardrails")


def _guard(columns: Sequence[str]) -> str:
    """The operations trigger function with ``columns`` as its allow-list (0016's body)."""
    array = ", ".join(f"'{c}'" for c in columns)
    return f"""
CREATE OR REPLACE FUNCTION epistrel_operations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY[
        {array}
    ];
BEGIN
    IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
        RAISE EXCEPTION 'operation %: only % may change', OLD.operation_id, mutable
            USING ERRCODE = 'EPI02';
    END IF;
    RETURN NEW;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "rating_check_jobs",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("from_rating_id", ULID, nullable=False),
        sa.Column("target_rating_id", ULID, nullable=False),
        sa.Column("target_rating_version", ULID, nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'checking'"), nullable=False),
        sa.Column("progress", postgresql.JSONB(), nullable=False),
        sa.Column("violations", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("checked_revision", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(STATUS_CHECK, name="ck_rating_check_jobs_status"),
        sa.PrimaryKeyConstraint("id", name="pk_rating_check_jobs"),
    )
    op.create_index("ix_rating_check_jobs_story_id", "rating_check_jobs", ["story_id"])
    op.create_index("ix_rating_check_jobs_finished_at", "rating_check_jobs", ["finished_at"])
    op.create_index(
        "uq_rating_check_jobs_story_checking",
        "rating_check_jobs",
        ["story_id"],
        unique=True,
        postgresql_where=sa.text("status = 'checking'"),
    )
    op.create_table(
        "guardrail_check_results",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column(
            "at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(VERDICT_CHECK, name="ck_guardrail_check_results_verdict"),
        sa.PrimaryKeyConstraint(
            "story_id",
            "record_key",
            "policy_id",
            "policy_version",
            name="pk_guardrail_check_results",
        ),
    )
    op.add_column("operations", sa.Column("checked_guardrails", postgresql.JSONB(), nullable=True))
    op.execute(_guard(AFTER))
    op.add_column("stories", sa.Column("paused_by", ULID, nullable=True))
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK)


def downgrade() -> None:
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'on', true)")
    op.execute("DELETE FROM audit_log WHERE kind = 'rating_change_refused'")
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'off', true)")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK_BEFORE)
    op.drop_column("stories", "paused_by")
    op.execute(_guard(BEFORE))
    op.drop_column("operations", "checked_guardrails")
    op.drop_table("guardrail_check_results")
    op.drop_index("uq_rating_check_jobs_story_checking", table_name="rating_check_jobs")
    op.drop_index("ix_rating_check_jobs_finished_at", table_name="rating_check_jobs")
    op.drop_index("ix_rating_check_jobs_story_id", table_name="rating_check_jobs")
    op.drop_table("rating_check_jobs")
