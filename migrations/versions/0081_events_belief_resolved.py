"""an index for the ``belief_resolved`` events an exposure's state is read through (#182)

#164's exposure state follows the resolving operation's membership by way of each resolution's
``belief_resolved`` event, matched on ``payload ->> 'exposure_id'``
(``core.epistemic.exposure._resolution_event``). Every ``exposures_of`` read, and so every lazy
resolution and every could-have-known replay (#165) that reads a holder's unresolved exposures,
ran that match as two correlated subqueries per exposure row, each a sequential scan of the
story's events: P-02's ``could_have_known`` span (#182) spent about 60 ms of an exposure-bearing
check in it on the 1k scale fixture, growing with the story's events.

- ``ix_events_belief_resolved``: a partial index on ``events (story_id, (payload ->>
  'exposure_id'))`` ``WHERE type = 'belief_resolved'``. It holds one row per resolution made,
  so it stays small; the subqueries become index lookups.

Downgrade drops the index.

Revision ID: 0081
Revises: 0080
Create Date: 2026-10-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_belief_resolved",
        "events",
        ["story_id", sa.text("(payload ->> 'exposure_id')")],
        postgresql_where=sa.text("type = 'belief_resolved'"),
    )


def downgrade() -> None:
    op.drop_index("ix_events_belief_resolved", table_name="events")
