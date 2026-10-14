"""genesis snapshots: what a story created from a scenario keeps of its catalog versions (#119)

- ``stories.scenario_id``, ``stories.scenario_version`` and ``stories.scenario_snapshot``
  (FR-SCN-9): the scenario version a story was created from, by identifiers only (no foreign
  key: the catalog never participates in a running story), and that version's whole document,
  copied at genesis. All three are set together or not at all
  (``ck_stories_scenario_snapshot``); a blank story and a fork's source-less copy keep them as
  their source has them.
- ``characters.role_id``: the scenario role a character fills (``NULL`` for a blank story's cast
  and a character added later).
- ``character_snapshots`` (FR-SCN-9, FR-CAT-3,9): one row per catalog character a story was
  created with, ``(story_id, character_id)``: the catalog id and version (``catalog_id@version``
  provenance), the version's whole ``content``, when it was published and the rating version it
  was published under. A story table (no foreign key; story deletion enumerates it, a fork
  copies it). Ad-hoc characters and the player's persona get no row.

Downgrade drops them.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

SNAPSHOT_CHECK = (
    "(scenario_id IS NULL AND scenario_version IS NULL AND scenario_snapshot IS NULL) "
    "OR (scenario_id IS NOT NULL AND scenario_version IS NOT NULL "
    "AND scenario_snapshot IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column("stories", sa.Column("scenario_id", ULID, nullable=True))
    op.add_column("stories", sa.Column("scenario_version", sa.Integer(), nullable=True))
    op.add_column("stories", sa.Column("scenario_snapshot", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("ck_stories_scenario_snapshot", "stories", SNAPSHOT_CHECK)
    op.add_column("characters", sa.Column("role_id", sa.Text(), nullable=True))
    op.create_table(
        "character_snapshots",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("character_id", ULID, nullable=False),
        sa.Column("source_catalog_id", ULID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("content_rating_version_id", ULID, nullable=True),
        sa.PrimaryKeyConstraint("story_id", "character_id", name="pk_character_snapshots"),
    )


def downgrade() -> None:
    op.drop_table("character_snapshots")
    op.drop_column("characters", "role_id")
    op.drop_constraint("ck_stories_scenario_snapshot", "stories", type_="check")
    op.drop_column("stories", "scenario_snapshot")
    op.drop_column("stories", "scenario_version")
    op.drop_column("stories", "scenario_id")
