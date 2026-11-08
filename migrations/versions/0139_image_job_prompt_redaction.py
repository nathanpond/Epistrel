"""Image job prompts can be erased: ``image_jobs.prompt`` becomes nullable (#266)

FR-ADMIN-6, FR-IMG-9: an erasure redacts a story image job's ``prompt`` and ``negative_prompt``
to null when they name the subject or the job depicts them; the job row and its accounting stay.
``negative_prompt`` is nullable already. The downgrade writes an empty prompt where an erasure
left none (an automatic job's prompt is empty until it prepares, #265), then restores the
constraint.

Revision ID: 0139
Revises: 0138
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0139"
down_revision: str | None = "0138"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("image_jobs", "prompt", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE image_jobs SET prompt = '' WHERE prompt IS NULL")
    op.alter_column("image_jobs", "prompt", existing_type=sa.Text(), nullable=False)
