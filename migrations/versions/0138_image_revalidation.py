"""Story image revalidation: the pause wait's start and the checked policy set (#264)

FR-IMG-4: a story image job revalidates against the story inside the attaching transaction.
``image_jobs`` gains two columns. ``wait_started_at`` is when the job first waited on a paused
story (set once; ``image_pause_max_wait_s`` counts from it). ``checked_policies`` is the
story's image-prompt and image-output policy set, as ``[[policy_id, version], …]``, at the job's
last prompt and output check: a policy in force at promotion that it does not hold is evaluated
before the image is attached. A ``stale`` job's ``{cause, policy_ids, amendment_id}`` goes in the
existing ``status_detail``.

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0138"
down_revision: str | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "image_jobs", sa.Column("wait_started_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("image_jobs", sa.Column("checked_policies", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("image_jobs", "checked_policies")
    op.drop_column("image_jobs", "wait_started_at")
