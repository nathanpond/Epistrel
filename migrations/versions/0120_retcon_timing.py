"""ambiguous_retcon_timing: active for every story (#240)

FR-STORE-20: a story created before #240 that holds the scenario's ``ambiguous_retcon_timing``
under ``stories.settings.inactive`` has it promoted to the active setting, with the inactive
entry removed (``core.genesis.settings_map`` no longer gates it on a capability).

Revision ID: 0120
Revises: 0119
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE stories
        SET settings = jsonb_set(
            settings #- '{inactive,ambiguous_retcon_timing}',
            '{ambiguous_retcon_timing}',
            settings -> 'inactive' -> 'ambiguous_retcon_timing' -> 'value'
        )
        WHERE settings -> 'inactive' -> 'ambiguous_retcon_timing' ? 'value'
          AND NOT settings ? 'ambiguous_retcon_timing'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE stories
        SET settings = jsonb_set(
            settings - 'ambiguous_retcon_timing',
            '{inactive,ambiguous_retcon_timing}',
            jsonb_build_object(
                'value', settings -> 'ambiguous_retcon_timing', 'capability', 'history_rewrite'
            )
        )
        WHERE settings ? 'ambiguous_retcon_timing' AND settings ? 'inactive'
        """
    )
