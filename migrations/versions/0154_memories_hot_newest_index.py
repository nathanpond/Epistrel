"""memories: an index for the alias arm's newest-first read (#900)

The exact-alias arm (``core.retrieval.pipeline._alias_arm``) asks for a story's newest ``k``
active hot memories holding one of the aliases (``ORDER BY narrative_turn DESC NULLS LAST,
beat DESC NULLS LAST, text, id LIMIT k``). No index served that order, so when an alias matched
most of the story the plan was a parallel scan of the story's partition and a sort: 19-21 ms at
50k memories (#897). ``ix_memories_hot_newest`` on ``(story_id, narrative_turn DESC NULLS
LAST, beat DESC NULLS LAST) WHERE retrieval_tier = 'hot' AND membership = 'active'`` lets the
plan walk the story's hot memories newest first, sort each turn's beats by ``text, id``
incrementally, and stop at ``k``: 0.07-0.17 ms. For a rare alias the planner still takes the
``search`` GIN index.

``memories`` is partitioned by story (ADR-187): the index is created on the parent, and
Postgres builds one on every story's partition, in one transaction. Like any migration that
alters ``memories``, it locks every partition, so a deployment of more than 500 stories sizes
``max_locks_per_transaction`` by ADR-188's rule first (docs/OPERATIONS.md). A partition now has
15 indexes, 16 relations: ADR-188's 16 lock slots a story, with no rounding left (ADR-192).

Downgrade drops the index.

Revision ID: 0154
Revises: 0153
Create Date: 2026-11-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0154"
down_revision: str | None = "0153"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ix_memories_hot_newest"


def upgrade() -> None:
    op.create_index(
        NAME,
        "memories",
        [
            "story_id",
            sa.text("narrative_turn DESC NULLS LAST"),
            sa.text("beat DESC NULLS LAST"),
        ],
        postgresql_where=sa.text("retrieval_tier = 'hot' AND membership = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(NAME, table_name="memories")
