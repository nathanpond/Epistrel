"""placement and relationships: indexes for the rows still in force at a turn (#892)

A turn's commit re-derives its scene from the story's ``placement`` and ``scene``
``relationships`` rows a scene from that turn on can use (``structured._from_turn_on``): not
closed by the turn, and valid past its story time. No index served that filter, so each turn
filtered every placement and relationship row of the story. Each table gets two partial
indexes, one per arm of the filter (``relationships`` by predicate first, since the scene reads
only the ``scene`` predicates'):

- ``ix_placement_story_open_valid_to``: ``(story_id, valid_to)`` and
  ``ix_relationships_story_predicate_open_valid_to``: ``(story_id, predicate_id, valid_to)``,
  ``WHERE closed_by_fact_id IS NULL`` (the open rows; ``valid_to IS NULL`` is valid for ever).
- ``ix_placement_story_closed_turn``: ``(story_id, closed_turn)`` and
  ``ix_relationships_story_predicate_closed_turn``: ``(story_id, predicate_id, closed_turn)``,
  ``WHERE closed_by_fact_id IS NOT NULL`` (the closed rows, by the turn that closed them).

The tables are caches over canon; nothing is backfilled. Downgrade drops the four indexes.

Revision ID: 0153
Revises: 0152
Create Date: 2026-11-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0153"
down_revision: str | None = "0152"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = (
    ("placement", "ix_placement_story", ()),
    ("relationships", "ix_relationships_story_predicate", ("predicate_id",)),
)


def upgrade() -> None:
    for table, name, keys in INDEXES:
        op.create_index(
            f"{name}_open_valid_to",
            table,
            ["story_id", *keys, "valid_to"],
            postgresql_where=sa.text("closed_by_fact_id IS NULL"),
        )
        op.create_index(
            f"{name}_closed_turn",
            table,
            ["story_id", *keys, "closed_turn"],
            postgresql_where=sa.text("closed_by_fact_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table, name, _ in INDEXES:
        op.drop_index(f"{name}_closed_turn", table_name=table)
        op.drop_index(f"{name}_open_valid_to", table_name=table)
