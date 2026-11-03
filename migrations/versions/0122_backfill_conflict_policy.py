"""backfill_conflict_policy: active for every story (#230)

FR-PROF-10: a story created before #230 that holds the scenario's ``backfill_conflict_policy``
under ``stories.settings.inactive`` has it promoted to the active setting, with the inactive
entry removed (``core.genesis.settings_map`` no longer gates it on a capability).

Revision ID: 0122
Revises: 0121
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0122"
down_revision: str | None = "0121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE stories
        SET settings = jsonb_set(
            settings #- '{inactive,backfill_conflict_policy}',
            '{backfill_conflict_policy}',
            settings -> 'inactive' -> 'backfill_conflict_policy' -> 'value'
        )
        WHERE settings -> 'inactive' -> 'backfill_conflict_policy' ? 'value'
          AND NOT settings ? 'backfill_conflict_policy'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE stories
        SET settings = jsonb_set(
            settings - 'backfill_conflict_policy',
            '{inactive,backfill_conflict_policy}',
            jsonb_build_object(
                'value', settings -> 'backfill_conflict_policy', 'capability', 'history_rewrite'
            )
        )
        WHERE settings ? 'backfill_conflict_policy' AND settings ? 'inactive'
        """
    )
