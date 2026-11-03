"""indexes for the object side of canon link reads (#617)

NFR-PERF-2: ``verify.neighborhood.fact_neighborhood`` reads each frontier entity's links by
subject (indexed) and by object (``canon_links(object=…)``: the proposition's ``object_id`` or a
set object holding it). Nothing indexed the object side, so Postgres walked every active fact of
the story and probed ``propositions`` once per fact. Three plain indexes:

- ``ix_propositions_story_object`` on ``propositions (story_id, object_id)`` where
  ``object_id IS NOT NULL``;
- ``ix_propositions_object_set`` on ``propositions`` using ``gin (object_set jsonb_path_ops)``
  where ``object_set IS NOT NULL`` (a set object's ``@>`` membership test);
- ``ix_facts_proposition`` on ``facts (proposition_id)``: from a matched proposition to its fact
  versions.

No data change. Downgrade drops all three.

Revision ID: 0107
Revises: 0106
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0107"
down_revision: str | None = "0106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_propositions_story_object",
        "propositions",
        ["story_id", "object_id"],
        postgresql_where=sa.text("object_id IS NOT NULL"),
    )
    op.create_index(
        "ix_propositions_object_set",
        "propositions",
        [sa.text("object_set jsonb_path_ops")],
        postgresql_using="gin",
        postgresql_where=sa.text("object_set IS NOT NULL"),
    )
    op.create_index("ix_facts_proposition", "facts", ["proposition_id"])


def downgrade() -> None:
    op.drop_index("ix_facts_proposition", table_name="facts")
    op.drop_index("ix_propositions_object_set", table_name="propositions")
    op.drop_index("ix_propositions_story_object", table_name="propositions")
