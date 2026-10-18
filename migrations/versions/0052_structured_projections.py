"""structured projections: relationships, placement and scene state over canon (#137)

FR-STORE-3 (docs/02 §3): ``core.tables.projections`` describes the tables and
``core.projections.structured`` folds them.

- ``relationships``, ``placement`` and ``scene_state`` are created with their primary keys, the
  ``placement`` target check and the ``(story_id, subject_id)``, ``(story_id, object_id)``,
  ``(story_id, entity_id)``, ``(story_id, location_id)`` and ``(story_id, container_id)``
  indexes. They are caches: nothing is backfilled here; ``POST /admin/rebuild`` (or any canon
  write) refolds a story's rows from ``facts``, and no story had canon before M4.
- The core predicates that now project (``holds`` → placement; ``parent_of``, ``child_of``,
  ``sibling_of``, ``member_of``, ``holds_position``, ``employed_by`` → relationships) get their
  ``projection`` values, as ``registry.sync_core`` would write them at the next start.

Downgrade drops the tables and puts the old ``projection`` values back.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

NEW_PROJECTIONS: dict[str, str] = {
    "01JPRED0000000000000000005": "placement",  # holds
    "01JPRED0000000000000000011": "relationships",  # parent_of
    "01JPRED0000000000000000012": "relationships",  # child_of
    "01JPRED0000000000000000013": "relationships",  # sibling_of
    "01JPRED0000000000000000018": "relationships",  # member_of
    "01JPRED0000000000000000019": "relationships",  # holds_position
    "01JPRED0000000000000000023": "relationships",  # employed_by
}
"""Core predicate id → its ``projection`` from this revision on (``NULL`` before)."""


def _structured_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("valid_from", sa.BigInteger(), nullable=True),
        sa.Column("valid_to", sa.BigInteger(), nullable=True),
        sa.Column("since_turn", sa.Integer(), nullable=True),
        sa.Column("since_slot", sa.SmallInteger(), nullable=True),
        sa.Column("closed_by_fact_id", ULID, nullable=True),
        sa.Column("closed_turn", sa.Integer(), nullable=True),
        sa.Column("closed_slot", sa.SmallInteger(), nullable=True),
        sa.Column("visibility", postgresql.JSONB(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "relationships",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("source_fact_id", ULID, nullable=False),
        sa.Column("object_id", ULID, nullable=False),
        sa.Column("subject_id", ULID, nullable=False),
        sa.Column("predicate_id", ULID, nullable=False),
        sa.Column("qualifiers", postgresql.JSONB(), nullable=False),
        *_structured_columns(),
        sa.PrimaryKeyConstraint("story_id", "source_fact_id", "object_id", name="pk_relationships"),
    )
    op.create_index("ix_relationships_story_subject", "relationships", ["story_id", "subject_id"])
    op.create_index("ix_relationships_story_object", "relationships", ["story_id", "object_id"])
    op.create_table(
        "placement",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("source_fact_id", ULID, nullable=False),
        sa.Column("entity_id", ULID, nullable=False),
        sa.Column("location_id", ULID, nullable=True),
        sa.Column("container_id", ULID, nullable=True),
        *_structured_columns(),
        sa.CheckConstraint(
            "num_nonnulls(location_id, container_id) = 1", name="ck_placement_one_target"
        ),
        sa.PrimaryKeyConstraint("story_id", "source_fact_id", name="pk_placement"),
    )
    op.create_index("ix_placement_story_entity", "placement", ["story_id", "entity_id"])
    op.create_index("ix_placement_story_location", "placement", ["story_id", "location_id"])
    op.create_index("ix_placement_story_container", "placement", ["story_id", "container_id"])
    op.create_table(
        "scene_state",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("variant", sa.SmallInteger(), nullable=False),
        sa.Column("location_id", ULID, nullable=True),
        sa.Column("present_entity_ids", postgresql.ARRAY(ULID), nullable=False),
        sa.Column("source_operation_id", ULID, nullable=False),
        sa.PrimaryKeyConstraint("story_id", "turn", "variant", name="pk_scene_state"),
    )
    for predicate_id, projection in NEW_PROJECTIONS.items():
        op.execute(
            sa.text("UPDATE predicates SET projection = :p WHERE id = :id").bindparams(
                p=projection, id=predicate_id
            )
        )


def downgrade() -> None:
    for predicate_id in NEW_PROJECTIONS:
        op.execute(
            sa.text("UPDATE predicates SET projection = NULL WHERE id = :id").bindparams(
                id=predicate_id
            )
        )
    op.drop_table("scene_state")
    op.drop_table("placement")
    op.drop_table("relationships")
