"""Story import as a background job (#250, #251): the ``importing`` status, import jobs and
the ``imported_unsettled`` reconciliation reason

FR-STORE-7: an import request creates the story row (``status = importing``) and a
``bundle_jobs`` row of ``kind = import``; the ``story_import`` job then inserts the bundle's rows,
runs #251's gates and rebuilds the projections, and the story becomes ``active``. A refused
import deletes the story and keeps its ``bundle_jobs`` row (``story_id`` null) so the operator
can read why: ``story_id`` becomes nullable, ``status`` gains ``refused``, and the row gains the
import's ``request`` (its choices and the frozen limits), ``report``, ``stage`` and
``progress``. #251: an imported outbox row with no terminal outcome is parked with a
``reconciliation_marks`` row of reason ``imported_unsettled``.

Revision ID: 0130
Revises: 0129
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0130"
down_revision: str | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = "status IN ('active', 'archived', 'paused', 'blocked_by_erasure', 'importing')"
STATUS_BEFORE = "status IN ('active', 'archived', 'paused', 'blocked_by_erasure')"
REASONS = (
    "unconfirmed",
    "failed",
    "unknown",
    "reconciliation_required",
    "cancelled_on_reactivation",
    "confirmed_unsettled",
    "confirmed_discarded_origin",
    "settlement_conflict",
    "late_success_after_expiry",
    "outcome_conflict",
    "reservation_expired",
)
KINDS = (
    "guardrail_input_rejected",
    "story_deleted",
    "catalog_entry_deleted",
    "persona_deleted",
    "rating_change_refused",
    "erasure",
)
JOB_STATUS = "status IN ('queued', 'running', 'ready', 'failed', 'expired', 'refused')"
JOB_STATUS_BEFORE = "status IN ('queued', 'running', 'ready', 'failed', 'expired')"


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    # #228, #709: a backfill's belief transitions are flagged as inferred from the transcript.
    op.add_column(
        "belief_transitions",
        sa.Column("inferred", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_constraint("ck_stories_status", "stories", type_="check")
    op.create_check_constraint("ck_stories_status", "stories", STATUS)
    op.alter_column("bundle_jobs", "story_id", nullable=True)
    op.drop_constraint("ck_bundle_jobs_kind", "bundle_jobs", type_="check")
    op.create_check_constraint("ck_bundle_jobs_kind", "bundle_jobs", "kind IN ('export', 'import')")
    op.drop_constraint("ck_bundle_jobs_status", "bundle_jobs", type_="check")
    op.create_check_constraint("ck_bundle_jobs_status", "bundle_jobs", JOB_STATUS)
    for name in ("request", "report", "progress"):
        op.add_column("bundle_jobs", sa.Column(name, postgresql.JSONB(), nullable=True))
    op.add_column("bundle_jobs", sa.Column("stage", sa.Text(), nullable=True))
    op.drop_constraint("ck_reconciliation_marks_reason", "reconciliation_marks", type_="check")
    op.create_check_constraint(
        "ck_reconciliation_marks_reason",
        "reconciliation_marks",
        _in("reason", (*REASONS, "imported_unsettled")),
    )
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, "story_import"))
    )


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE kind = 'story_import'")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))
    op.execute("DELETE FROM reconciliation_marks WHERE reason = 'imported_unsettled'")
    op.drop_constraint("ck_reconciliation_marks_reason", "reconciliation_marks", type_="check")
    op.create_check_constraint(
        "ck_reconciliation_marks_reason", "reconciliation_marks", _in("reason", REASONS)
    )
    op.execute("DELETE FROM bundle_jobs WHERE kind = 'import'")
    op.drop_column("bundle_jobs", "stage")
    for name in ("progress", "report", "request"):
        op.drop_column("bundle_jobs", name)
    op.drop_constraint("ck_bundle_jobs_status", "bundle_jobs", type_="check")
    op.create_check_constraint("ck_bundle_jobs_status", "bundle_jobs", JOB_STATUS_BEFORE)
    op.drop_constraint("ck_bundle_jobs_kind", "bundle_jobs", type_="check")
    op.create_check_constraint("ck_bundle_jobs_kind", "bundle_jobs", "kind IN ('export')")
    op.alter_column("bundle_jobs", "story_id", nullable=False)
    op.execute("UPDATE stories SET status = 'active' WHERE status = 'importing'")
    op.drop_constraint("ck_stories_status", "stories", type_="check")
    op.create_check_constraint("ck_stories_status", "stories", STATUS_BEFORE)
    op.drop_column("belief_transitions", "inferred")
