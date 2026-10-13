"""engine profiles: profiles and their versions, the Lite preset, and the default (#106)

Two tables (docs/02 §4.11; FR-PROF-1):

- ``engine_profiles``: one row per profile: name, owner (FR-AUTH-5), ``preset`` (provenance
  only), latest version, created/updated times, and ``deleted_at`` once it was deleted.
- ``engine_profile_versions``: one row per version, ``(profile_id, version)`` from 1, with the
  settings document, who made it and when; it cascades with its profile.

The Lite preset (``epistrel.core.seeds.profiles``) is inserted at version 1, owned by
``system``; no ``config_changes`` row is written and ``config_revision`` does not move.

Two columns reference ``engine_profiles``:

- ``deployment_state.default_engine_profile_id`` (NOT NULL): the profile a story is created
  under, set to Lite;
- ``stories.engine_profile_id`` (NOT NULL, indexed): the profile each story runs under,
  backfilled to Lite for every existing story. It has no server default: story creation names
  the deployment's default and a fork copies its source's.

Downgrade drops the two columns, removes what names a profile elsewhere (its relations; its
ownership events are history and stay), and drops both tables.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.profiles import (
    LITE_NAME,
    LITE_PRESET_ID,
    SEED_PRINCIPAL,
    lite_settings,
)

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.func.transaction_timestamp()


def upgrade() -> None:
    op.create_table(
        "engine_profiles",
        sa.Column("id", ULID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=False),
        sa.Column("preset", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("current_version >= 1", name="ck_engine_profiles_current_version"),
        sa.PrimaryKeyConstraint("id", name="pk_engine_profiles"),
    )
    op.create_index("ix_engine_profiles_owner_principal", "engine_profiles", ["owner_principal"])
    op.create_table(
        "engine_profile_versions",
        sa.Column("profile_id", ULID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_engine_profile_versions_version"),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["engine_profiles.id"],
            name="fk_engine_profile_versions_profile_id_engine_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("profile_id", "version", name="pk_engine_profile_versions"),
    )
    _seed()

    op.add_column("deployment_state", sa.Column("default_engine_profile_id", ULID, nullable=True))
    op.execute(
        sa.text("UPDATE deployment_state SET default_engine_profile_id = :lite").bindparams(
            lite=LITE_PRESET_ID
        )
    )
    op.alter_column("deployment_state", "default_engine_profile_id", nullable=False)
    op.create_foreign_key(
        "fk_deployment_state_default_engine_profile_id_engine_profiles",
        "deployment_state",
        "engine_profiles",
        ["default_engine_profile_id"],
        ["id"],
    )

    op.add_column(
        "stories",
        sa.Column(
            "engine_profile_id",
            ULID,
            server_default=sa.text(f"'{LITE_PRESET_ID}'"),
            nullable=False,
        ),
    )
    op.alter_column("stories", "engine_profile_id", server_default=None)
    op.create_foreign_key(
        "fk_stories_engine_profile_id_engine_profiles",
        "stories",
        "engine_profiles",
        ["engine_profile_id"],
        ["id"],
    )
    op.create_index("ix_stories_engine_profile_id", "stories", ["engine_profile_id"])


def _seed() -> None:
    """Lite, owned by ``system``, at version 1; no ``config_changes`` row."""
    heads = sa.table(
        "engine_profiles",
        sa.column("id"),
        sa.column("name"),
        sa.column("owner_principal"),
        sa.column("preset"),
        sa.column("current_version"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    versions = sa.table(
        "engine_profile_versions",
        sa.column("profile_id"),
        sa.column("version"),
        sa.column("settings", postgresql.JSONB()),
        sa.column("created_by"),
        sa.column("created_at"),
    )
    op.execute(
        heads.insert().values(
            id=LITE_PRESET_ID,
            name=LITE_NAME,
            owner_principal=SEED_PRINCIPAL,
            preset=True,
            current_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    op.execute(
        versions.insert().values(
            profile_id=LITE_PRESET_ID,
            version=1,
            settings=lite_settings(),
            created_by=SEED_PRINCIPAL,
            created_at=NOW,
        )
    )


def downgrade() -> None:
    op.drop_index("ix_stories_engine_profile_id", table_name="stories")
    op.drop_constraint(
        "fk_stories_engine_profile_id_engine_profiles", "stories", type_="foreignkey"
    )
    op.drop_column("stories", "engine_profile_id")
    op.drop_constraint(
        "fk_deployment_state_default_engine_profile_id_engine_profiles",
        "deployment_state",
        type_="foreignkey",
    )
    op.drop_column("deployment_state", "default_engine_profile_id")

    op.execute("DELETE FROM entity_relations WHERE entity_type = 'engine_profile'")
    op.drop_table("engine_profile_versions")
    op.drop_index("ix_engine_profiles_owner_principal", table_name="engine_profiles")
    op.drop_table("engine_profiles")
