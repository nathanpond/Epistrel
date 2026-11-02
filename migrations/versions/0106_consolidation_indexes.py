"""indexes for the consolidation scheduling reads inside a turn's commit (#622)

NFR-PERF-3: a turn that writes a perceived memory asks ``core.memory.consolidation`` whether a
window is due, inside its commit. Its two reads scaled with the story's length (a bitmap scan
of every memory of the story, sorted; every committed operation, sorted). Two partial indexes
match them exactly:

- ``ix_memories_shared_episodic_turn`` on ``memories (story_id, narrative_turn)`` where
  ``kind = 'episodic' AND holder_character_id IS NULL AND membership = 'active'``: the shared
  scope's next window (``next_window``) is an ordered range read with its ``LIMIT``;
- ``ix_operations_consolidation_turn`` on ``operations (story_id, narrative_turn)`` where
  ``kind = 'consolidation' AND status = 'committed' AND membership = 'active'``: the newest
  covered turn (``covered_to``) is the first row of a backward scan.

No data change. Downgrade drops both indexes.

Revision ID: 0106
Revises: 0105
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0106"
down_revision: str | None = "0105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHARED_EPISODIC = "kind = 'episodic' AND holder_character_id IS NULL AND membership = 'active'"
CONSOLIDATED = "kind = 'consolidation' AND status = 'committed' AND membership = 'active'"


def upgrade() -> None:
    op.create_index(
        "ix_memories_shared_episodic_turn",
        "memories",
        ["story_id", "narrative_turn"],
        postgresql_where=sa.text(SHARED_EPISODIC),
    )
    op.create_index(
        "ix_operations_consolidation_turn",
        "operations",
        ["story_id", "narrative_turn"],
        postgresql_where=sa.text(CONSOLIDATED),
    )


def downgrade() -> None:
    op.drop_index("ix_operations_consolidation_turn", table_name="operations")
    op.drop_index("ix_memories_shared_episodic_turn", table_name="memories")
