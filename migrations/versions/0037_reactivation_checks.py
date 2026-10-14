"""reactivation checks: re-check turns before untruncate, swap or a dormant fork (#120)

- ``rating_check_jobs.kind`` (FR-RATE-7): ``tightening`` (a rating change's history check, #118,
  every existing row) or ``reactivation`` (a check of the turns an untruncate, swap or fork of a
  dormant continuation would make active, against the story's current guardrails).
- ``rating_check_jobs.status`` gains ``reactivated``: the turns passed and the edit the check
  held back was applied.

Downgrade deletes the reactivation checks (a story still paused by one resumes) and restores
0035's status check.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_BEFORE = "status IN ('checking', 'applied', 'refused', 'failed')"
STATUS_CHECK = "status IN ('checking', 'applied', 'refused', 'failed', 'reactivated')"
KIND_CHECK = "kind IN ('tightening', 'reactivation')"


def upgrade() -> None:
    op.add_column(
        "rating_check_jobs",
        sa.Column("kind", sa.Text(), server_default=sa.text("'tightening'"), nullable=False),
    )
    op.create_check_constraint("ck_rating_check_jobs_kind", "rating_check_jobs", KIND_CHECK)
    op.drop_constraint("ck_rating_check_jobs_status", "rating_check_jobs", type_="check")
    op.create_check_constraint("ck_rating_check_jobs_status", "rating_check_jobs", STATUS_CHECK)


def downgrade() -> None:
    op.execute(
        "UPDATE stories SET status = 'active', paused_by = NULL WHERE paused_by IN "
        "(SELECT id FROM rating_check_jobs WHERE kind = 'reactivation')"
    )
    op.execute("DELETE FROM rating_check_jobs WHERE kind = 'reactivation'")
    op.drop_constraint("ck_rating_check_jobs_status", "rating_check_jobs", type_="check")
    op.create_check_constraint("ck_rating_check_jobs_status", "rating_check_jobs", STATUS_BEFORE)
    op.drop_constraint("ck_rating_check_jobs_kind", "rating_check_jobs", type_="check")
    op.drop_column("rating_check_jobs", "kind")
