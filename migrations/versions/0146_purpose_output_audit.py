"""audit_log gains guardrail_output_rejected: a consumer purpose call's refused output (#825)

FR-MODEL-15: a story-less consumer purpose call whose output breaks a policy twice is refused
with 422 ``output_policy_violation`` and one ``audit_log`` row of kind
``guardrail_output_rejected`` (the entry point, the purpose's name and version, the attempts;
the policy ids; never the output). A turn's refused output stays an event in its story.

Downgrade deletes those rows and restores the previous kind list.

Revision ID: 0146
Revises: 0145
Create Date: 2026-11-10

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0146"
down_revision: str | None = "0145"
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
    "image_output_rejected",
    "image_job_ended",
    "image_prompt_rejected",
    "image_upload_rejected",
    "image_counters_reconciled",
    "image_check_certification_recorded",
    "image_check_certification_invalidated",
)
ADDED = "guardrail_output_rejected"


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, ADDED)))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE kind = 'guardrail_output_rejected'")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))
