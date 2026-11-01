"""External value versions: ``post_effect_read`` and ``base_event_id`` (#215)

FR-EXT-20 (docs/02 §4.8, post-effect values): ``core.tables.states`` describes the table.

- ``external_value_source`` gains ``post_effect_read`` (in an autocommit block: a label added
  in a transaction cannot be used before it commits).
- ``external_values`` gains ``base_event_id``: the version-1 read a version extends (a read's
  own event for a read; existing rows are all reads, so the backfill is ``event_id``), and the
  unique index ``uq_external_values_version`` on ``(story_id, state_id, scope_key,
  base_event_id, version)``: a version number is unique within its base read's chain (every
  turn's read is version 1, so the version alone cannot be unique per State and scope key).

Downgrade deletes the ``post_effect_read`` rows (a rebuildable projection; their events stay in
the log), drops the index and the column, and rebuilds the enum type without the label.

Revision ID: 0097
Revises: 0096
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
SOURCES_BEFORE = ("read", "last_known", "unknown", "confirmation")
INDEX = "uq_external_values_version"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE external_value_source ADD VALUE IF NOT EXISTS 'post_effect_read'")
    op.add_column("external_values", sa.Column("base_event_id", ULID, nullable=True))
    op.execute("UPDATE external_values SET base_event_id = event_id")
    op.alter_column("external_values", "base_event_id", nullable=False)
    op.create_index(
        INDEX,
        "external_values",
        ["story_id", "state_id", "scope_key", "base_event_id", "version"],
        unique=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM external_values WHERE source = 'post_effect_read'")
    op.drop_index(INDEX, table_name="external_values")
    op.drop_column("external_values", "base_event_id")
    # PostgreSQL cannot drop an enum label: the type is rebuilt without ``post_effect_read``.
    bind = op.get_bind()
    op.execute("ALTER TYPE external_value_source RENAME TO external_value_source_0096")
    postgresql.ENUM(*SOURCES_BEFORE, name="external_value_source").create(bind)
    op.execute(
        "ALTER TABLE external_values ALTER COLUMN source TYPE external_value_source "
        "USING source::text::external_value_source"
    )
    op.execute("DROP TYPE external_value_source_0096")
