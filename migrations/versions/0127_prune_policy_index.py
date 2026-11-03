"""The prune policy's index (#703): the daily ``prune_run`` finds the opted-in stories

FR-STORE-32: a partial expression index on ``stories ((settings->'prune_policy'))``, over the
stories whose policy is set, so the daily run's scan does not read every story.

Revision ID: 0127
Revises: 0126
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0127"
down_revision: str | None = "0126"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_stories_prune_policy ON stories ((settings -> 'prune_policy')) "
        "WHERE (settings -> 'prune_policy') IS NOT NULL "
        "AND jsonb_typeof(settings -> 'prune_policy') = 'object'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_stories_prune_policy")
