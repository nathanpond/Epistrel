"""image_jobs gains the subject kind and its completed status (#830)

FR-IMG-14: ``POST /subject-images`` generates images of a consumer-described subject that is in
no story or catalog, as a background image job of kind ``subject``. Its passing outputs become
live media assets directly, so the job ends ``completed`` (only a subject job does). Its
reservation and cost records are attributed to ``entry_kind = subject`` and the subject's id.

Downgrade deletes the subject jobs and their records, then restores the previous checks.

Revision ID: 0148
Revises: 0147
Create Date: 2026-11-10

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0148"
down_revision: str | None = "0147"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("catalog", "story")
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
ENTRY_KINDS = ("character", "scenario")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _entry_kind(values: Sequence[str]) -> str:
    return "entry_kind IS NULL OR " + _in("entry_kind", values)


def _checks(kinds: Sequence[str], statuses: Sequence[str], entry_kinds: Sequence[str]) -> None:
    op.drop_constraint("ck_image_jobs_kind", "image_jobs", type_="check")
    op.create_check_constraint("ck_image_jobs_kind", "image_jobs", _in("kind", kinds))
    op.drop_constraint("ck_image_jobs_status", "image_jobs", type_="check")
    op.create_check_constraint("ck_image_jobs_status", "image_jobs", _in("status", statuses))
    for table in ("image_reservations", "image_costs"):
        op.drop_constraint(f"ck_{table}_entry_kind", table, type_="check")
        op.create_check_constraint(f"ck_{table}_entry_kind", table, _entry_kind(entry_kinds))


def upgrade() -> None:
    _checks((*KINDS, "subject"), (*STATUSES, "completed"), (*ENTRY_KINDS, "subject"))
    op.create_check_constraint(
        "ck_image_jobs_completed_subject", "image_jobs", "status <> 'completed' OR kind = 'subject'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_image_jobs_completed_subject", "image_jobs", type_="check")
    op.execute("DELETE FROM image_costs WHERE entry_kind = 'subject'")
    op.execute("DELETE FROM image_reservations WHERE entry_kind = 'subject'")
    op.execute("DELETE FROM image_jobs WHERE kind = 'subject'")
    _checks(KINDS, STATUSES, ENTRY_KINDS)
