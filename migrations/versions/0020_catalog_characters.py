"""catalog: character drafts and immutable published versions (#103)

Schema ``catalog`` (docs/02 §4.10) with the enum ``catalog.entry_status`` and two tables:
``character_catalog`` (one row per entry: owner, listing status, status, current published
version, the draft and its revision) and ``character_versions`` (one immutable row per publish,
keyed ``(catalog_id, version)``, cascading with its entry). A trigger refuses every UPDATE,
DELETE and TRUNCATE of a version with SQLSTATE ``EPI01``, except an UPDATE or DELETE under
``epistrel.erasure = on`` (catalog delete; #124's erasure).

Also: ``characters.source_catalog_id``/``source_catalog_version`` (FR-CAT-9 provenance, identifiers
only, no foreign key), and ``catalog_entry_deleted`` joins the ``audit_log.kind`` check.

Downgrade drops the schema's objects, the two columns, and the ``catalog_entry_deleted`` audit
rows (deleted under ``epistrel.allow_audit_expiry``, since the restored check forbids them).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
SCHEMA = "catalog"
# A frozen copy of epistrel.core.enums.EntryStatus at this revision.
ENTRY_STATUS = ("draft", "published", "archived")

KIND_CHECK_BEFORE = "kind IN ('guardrail_input_rejected', 'story_deleted')"
KIND_CHECK = "kind IN ('guardrail_input_rejected', 'story_deleted', 'catalog_entry_deleted')"

VERSIONS_GUARD = """
CREATE FUNCTION catalog.epistrel_character_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'character_versions are immutable: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    IF current_setting('epistrel.erasure', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'character_versions are immutable: % of version % of % refused',
        TG_OP, OLD.version, OLD.catalog_id
        USING ERRCODE = 'EPI01';
END
$$;

CREATE TRIGGER character_versions_immutable
    BEFORE UPDATE OR DELETE ON catalog.character_versions
    FOR EACH ROW EXECUTE FUNCTION catalog.epistrel_character_versions_immutable();

CREATE TRIGGER character_versions_no_truncate
    BEFORE TRUNCATE ON catalog.character_versions
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.epistrel_character_versions_immutable();
"""


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {SCHEMA}")
    postgresql.ENUM(*ENTRY_STATUS, name="entry_status", schema=SCHEMA).create(op.get_bind())
    op.create_table(
        "character_catalog",
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
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_character_catalog"),
        schema=SCHEMA,
    )
    op.create_table(
        "character_versions",
        sa.Column("catalog_id", ULID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_rating_version_id", ULID, nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("published_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            [f"{SCHEMA}.character_catalog.id"],
            name="fk_character_versions_catalog_id_character_catalog",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("catalog_id", "version", name="pk_character_versions"),
        schema=SCHEMA,
    )
    op.execute(VERSIONS_GUARD)

    op.add_column("characters", sa.Column("source_catalog_id", ULID, nullable=True))
    op.add_column("characters", sa.Column("source_catalog_version", sa.Integer(), nullable=True))

    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK)


def downgrade() -> None:
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'on', true)")
    op.execute("DELETE FROM audit_log WHERE kind = 'catalog_entry_deleted'")
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'off', true)")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK_BEFORE)

    op.drop_column("characters", "source_catalog_version")
    op.drop_column("characters", "source_catalog_id")

    op.execute(
        "DROP TRIGGER IF EXISTS character_versions_no_truncate ON catalog.character_versions"
    )
    op.execute("DROP TRIGGER IF EXISTS character_versions_immutable ON catalog.character_versions")
    op.execute("DROP FUNCTION IF EXISTS catalog.epistrel_character_versions_immutable()")
    op.drop_table("character_versions", schema=SCHEMA)
    op.drop_table("character_catalog", schema=SCHEMA)
    postgresql.ENUM(name="entry_status", schema=SCHEMA).drop(op.get_bind())
    op.execute(f"DROP SCHEMA {SCHEMA}")
