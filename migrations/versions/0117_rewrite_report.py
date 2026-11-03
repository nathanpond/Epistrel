"""rewrite report: an applied rewrite session's report (#224)

FR-REWRITE-3,6: ``rewrite_sessions.report`` keeps the apply's report (its resolutions, what it
applied and its notifications), which ``GET /stories/{id}/rewrites/{rid}`` shows.

Revision ID: 0117
Revises: 0116
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0117"
down_revision: str | None = "0116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rewrite_sessions", sa.Column("report", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("rewrite_sessions", "report")
