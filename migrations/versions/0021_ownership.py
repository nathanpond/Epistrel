"""ownership: owners, relations and the ownership record (#104)

- ``stories.owner_principal`` (FR-AUTH-5), NOT NULL. Existing stories are backfilled from the
  principal of their newest committed ``genesis`` operation (a fork holds its source's copied
  genesis and its own, which is newer), else ``system``.
- ``entity_relations``: consumer-named (principal, relation) pairs per entity (FR-AUTH-6), keyed
  ``(entity_type, entity_id, principal, relation)``.
- ``ownership_events``: every transfer, relation change, listing-status change and deletion,
  with the acting principal and the transaction time.
- Btree indexes on every owner column that exists (``stories``,
  ``catalog.character_catalog``) and on ``entity_relations.principal``, plus
  ``ownership_events(entity_type, entity_id, at)``.

Downgrade drops both tables, the indexes and the column (their rows go with them).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")

# Frozen copies of epistrel.core.tables.ownership's vocabularies at this revision.
ENTITY_TYPE_CHECK = (
    "entity_type IN ('story', 'character', 'scenario', 'persona', 'media', 'engine_profile')"
)
KIND_CHECK = (
    "kind IN ('transfer', 'relation_added', 'relation_removed', 'listing_status', 'deleted')"
)
RELATION_CHECK = "relation ~ '^[A-Za-z0-9_-]{1,64}$'"

BACKFILL = """
UPDATE stories s SET owner_principal = coalesce(
    (
        SELECT o.principal FROM operations o
        WHERE o.story_id = s.id AND o.kind = 'genesis' AND o.status = 'committed'
        ORDER BY o.committed_at DESC NULLS LAST, o.operation_id DESC
        LIMIT 1
    ),
    'system'
)
"""
"""Each story's owner: its newest committed genesis operation's principal (a fork's own genesis
is newer than the source's it copied), else ``system``."""


def upgrade() -> None:
    op.add_column("stories", sa.Column("owner_principal", sa.Text(), nullable=True))
    op.execute(BACKFILL)
    op.alter_column("stories", "owner_principal", nullable=False)
    op.create_index("ix_stories_owner_principal", "stories", ["owner_principal"])
    op.create_index(
        "ix_character_catalog_owner_principal",
        "character_catalog",
        ["owner_principal"],
        schema="catalog",
    )

    op.create_table(
        "entity_relations",
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", ULID, nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint(ENTITY_TYPE_CHECK, name="ck_entity_relations_entity_type"),
        sa.CheckConstraint(RELATION_CHECK, name="ck_entity_relations_relation"),
        sa.PrimaryKeyConstraint(
            "entity_type", "entity_id", "principal", "relation", name="pk_entity_relations"
        ),
    )
    op.create_index("ix_entity_relations_principal", "entity_relations", ["principal"])

    op.create_table(
        "ownership_events",
        sa.Column("id", ULID, nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", ULID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("old", postgresql.JSONB(), nullable=True),
        sa.Column("new", postgresql.JSONB(), nullable=True),
        sa.Column("acting_principal", sa.Text(), nullable=False),
        sa.Column("job_id", ULID, nullable=True),
        sa.Column("at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(ENTITY_TYPE_CHECK, name="ck_ownership_events_entity_type"),
        sa.CheckConstraint(KIND_CHECK, name="ck_ownership_events_kind"),
        sa.PrimaryKeyConstraint("id", name="pk_ownership_events"),
    )
    op.create_index(
        "ix_ownership_events_entity",
        "ownership_events",
        ["entity_type", "entity_id", "at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ownership_events_entity", table_name="ownership_events")
    op.drop_table("ownership_events")
    op.drop_index("ix_entity_relations_principal", table_name="entity_relations")
    op.drop_table("entity_relations")
    op.drop_index(
        "ix_character_catalog_owner_principal", table_name="character_catalog", schema="catalog"
    )
    op.drop_index("ix_stories_owner_principal", table_name="stories")
    op.drop_column("stories", "owner_principal")
