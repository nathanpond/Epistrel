"""scenario catalog: scenario drafts and immutable published versions (#108)

Two tables in the ``catalog`` schema (docs/02 §4.10):

- ``scenario_catalog``: one row per scenario, like ``character_catalog`` (owner, listing status,
  status, current published version, the draft and its revision), plus ``companion_of``: the
  catalog character whose companion it is (#116), a foreign key that cascades.
- ``scenario_versions``: one immutable row per publish, keyed ``(catalog_id, version)`` and
  cascading with its scenario, with the content rating version it was published under, and a
  companion's pin ``(pinned_character_id, pinned_character_version)``: a foreign key to
  ``character_versions`` that is set to NULL when that character version is erased. A trigger
  refuses every UPDATE, DELETE and TRUNCATE of a version with SQLSTATE ``EPI01``, except an
  UPDATE or DELETE under ``epistrel.erasure = on`` (catalog delete; #124's erasure).

A scenario's roles, seeds and relationships live in the version's ``content`` document (jsonb);
there is no ``scenario_roles`` table.

Also two ``deployment_state`` columns (FR-SCN-4): ``max_characters_at_start_default`` (16) and
``max_characters_at_start_ceiling`` (NULL: no ceiling).

Downgrade drops both tables, their trigger and function and the two columns, after removing what
names a scenario elsewhere: its relations and its subject-reference rows (its ownership events
and audit rows are history and stay).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
SCHEMA = "catalog"
PIN_COMPLETE_CHECK = "(pinned_character_id IS NULL) = (pinned_character_version IS NULL)"

VERSIONS_GUARD = """
CREATE FUNCTION catalog.epistrel_scenario_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'scenario_versions are immutable: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    IF current_setting('epistrel.erasure', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'scenario_versions are immutable: % of version % of % refused',
        TG_OP, OLD.version, OLD.catalog_id
        USING ERRCODE = 'EPI01';
END
$$;

CREATE TRIGGER scenario_versions_immutable
    BEFORE UPDATE OR DELETE ON catalog.scenario_versions
    FOR EACH ROW EXECUTE FUNCTION catalog.epistrel_scenario_versions_immutable();

CREATE TRIGGER scenario_versions_no_truncate
    BEFORE TRUNCATE ON catalog.scenario_versions
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.epistrel_scenario_versions_immutable();
"""


def upgrade() -> None:
    op.create_table(
        "scenario_catalog",
        sa.Column("id", ULID, nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=False),
        sa.Column("listing_status", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="entry_status", schema=SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column("current_version", sa.Integer(), nullable=True),
        sa.Column("draft", postgresql.JSONB(), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("companion_of", ULID, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["companion_of"],
            [f"{SCHEMA}.character_catalog.id"],
            name="fk_scenario_catalog_companion_of_character_catalog",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scenario_catalog"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_scenario_catalog_owner_principal",
        "scenario_catalog",
        ["owner_principal"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_scenario_catalog_companion_of", "scenario_catalog", ["companion_of"], schema=SCHEMA
    )
    op.create_table(
        "scenario_versions",
        sa.Column("catalog_id", ULID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_rating_version_id", ULID, nullable=True),
        sa.Column("pinned_character_id", ULID, nullable=True),
        sa.Column("pinned_character_version", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("published_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            [f"{SCHEMA}.scenario_catalog.id"],
            name="fk_scenario_versions_catalog_id_scenario_catalog",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pinned_character_id", "pinned_character_version"],
            [f"{SCHEMA}.character_versions.catalog_id", f"{SCHEMA}.character_versions.version"],
            name="fk_scenario_versions_pinned_character_id_character_versions",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(PIN_COMPLETE_CHECK, name="ck_scenario_versions_pin_complete"),
        sa.PrimaryKeyConstraint("catalog_id", "version", name="pk_scenario_versions"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_scenario_versions_pinned_character",
        "scenario_versions",
        ["pinned_character_id", "pinned_character_version"],
        schema=SCHEMA,
    )
    op.execute(VERSIONS_GUARD)

    op.add_column(
        "deployment_state",
        sa.Column(
            "max_characters_at_start_default",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("16"),
        ),
    )
    op.add_column(
        "deployment_state",
        sa.Column("max_characters_at_start_ceiling", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployment_state", "max_characters_at_start_ceiling")
    op.drop_column("deployment_state", "max_characters_at_start_default")

    op.execute("DELETE FROM entity_relations WHERE entity_type = 'scenario'")
    op.execute(
        "DELETE FROM subject_references "
        "WHERE record_table IN ('scenario_catalog', 'scenario_versions')"
    )
    op.execute("DROP TRIGGER IF EXISTS scenario_versions_no_truncate ON catalog.scenario_versions")
    op.execute("DROP TRIGGER IF EXISTS scenario_versions_immutable ON catalog.scenario_versions")
    op.execute("DROP FUNCTION IF EXISTS catalog.epistrel_scenario_versions_immutable()")
    op.drop_index(
        "ix_scenario_versions_pinned_character", table_name="scenario_versions", schema=SCHEMA
    )
    op.drop_table("scenario_versions", schema=SCHEMA)
    op.drop_index("ix_scenario_catalog_companion_of", table_name="scenario_catalog", schema=SCHEMA)
    op.drop_index(
        "ix_scenario_catalog_owner_principal", table_name="scenario_catalog", schema=SCHEMA
    )
    op.drop_table("scenario_catalog", schema=SCHEMA)
