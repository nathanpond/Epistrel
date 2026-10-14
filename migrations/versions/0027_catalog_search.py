"""catalog listing status values and search (#110)

- ``deployment_state.listing_status_values`` (``text[]``, NOT NULL): the listing status values
  a catalog entry may hold, in order; a new entry takes the first (FR-CAT-11). The existing row
  gets ``private``, ``in_review``, ``public``. The values are checked by the application, never
  by a constraint on the catalog tables, so an operator adds one without a migration.
- ``search`` on ``catalog.character_versions`` and ``catalog.scenario_versions``: a generated
  ``tsvector`` of the version's public card, GIN indexed, which ``q`` searches (FR-API-6). Two
  ``IMMUTABLE`` SQL functions build it from the content, through
  ``catalog.epistrel_search_words`` (the strings a jsonpath finds, joined):
  ``catalog.epistrel_character_search`` (the name, weight A; the aliases and labels, B; the
  public description, C) and ``catalog.epistrel_scenario_search`` (the name, A; the labels and
  every role's public label, B; the public description, C). A character's name and public
  description are read at the top level (the interim draft, #103) or under ``public_card``, and
  its aliases and labels under ``metadata`` (#115's planned shape). Both parse with the
  ``simple`` configuration (words as written, lower-cased; no stemming, no stop words). No
  private field is indexed: a scenario's world (entities and their aliases included), seeds,
  drivers, and a character's definition never reach a vector.
- ``(updated_at, id)`` indexes on both catalog tables: the listings' keyset.

Downgrade drops the indexes, the generated columns, the functions and the deployment column.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "catalog"
DEFAULT_VALUES = "'{private,in_review,public}'::text[]"

FUNCTIONS = """
CREATE FUNCTION catalog.epistrel_search_words(document jsonb, path jsonpath) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
    SELECT string_agg(item #>> '{}', ' ') FROM jsonb_path_query(document, path) AS item
$$;

CREATE FUNCTION catalog.epistrel_character_search(content jsonb) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT
        setweight(to_tsvector('simple'::regconfig, coalesce(
            catalog.epistrel_search_words(
                content, '$.public_card.name ? (@.type() == "string")'),
            catalog.epistrel_search_words(content, '$.name ? (@.type() == "string")'),
            '')), 'A')
        || setweight(to_tsvector('simple'::regconfig,
            coalesce(catalog.epistrel_search_words(
                content, '$.metadata.aliases[*] ? (@.type() == "string")'), '')
            || ' '
            || coalesce(catalog.epistrel_search_words(
                content, '$.metadata.labels[*] ? (@.type() == "string")'), '')
            ), 'B')
        || setweight(to_tsvector('simple'::regconfig, coalesce(
            catalog.epistrel_search_words(
                content, '$.public_card.public_description ? (@.type() == "string")'),
            catalog.epistrel_search_words(
                content, '$.public_description ? (@.type() == "string")'),
            '')), 'C')
$$;

CREATE FUNCTION catalog.epistrel_scenario_search(content jsonb) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT
        setweight(to_tsvector('simple'::regconfig, coalesce(
            catalog.epistrel_search_words(
                content, '$.public_card.name ? (@.type() == "string")'),
            '')), 'A')
        || setweight(to_tsvector('simple'::regconfig,
            coalesce(catalog.epistrel_search_words(
                content, '$.metadata.labels[*] ? (@.type() == "string")'), '')
            || ' '
            || coalesce(catalog.epistrel_search_words(
                content, '$.cast.roles[*].public_label ? (@.type() == "string")'), '')
            ), 'B')
        || setweight(to_tsvector('simple'::regconfig, coalesce(
            catalog.epistrel_search_words(
                content, '$.public_card.public_description ? (@.type() == "string")'),
            '')), 'C')
$$;
"""
"""The search functions. Each jsonpath keeps strings only (``? (@.type() == "string")``), so
a malformed document never breaks a publish: it only indexes less."""


def upgrade() -> None:
    op.add_column(
        "deployment_state",
        sa.Column(
            "listing_status_values",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text(DEFAULT_VALUES),
        ),
    )
    op.execute(FUNCTIONS)
    for kind in ("character", "scenario"):
        op.add_column(
            f"{kind}_versions",
            sa.Column(
                "search",
                postgresql.TSVECTOR(),
                sa.Computed(f"catalog.epistrel_{kind}_search(content)", persisted=True),
            ),
            schema=SCHEMA,
        )
        op.create_index(
            f"ix_{kind}_versions_search",
            f"{kind}_versions",
            ["search"],
            schema=SCHEMA,
            postgresql_using="gin",
        )
        op.create_index(
            f"ix_{kind}_catalog_updated_at_id",
            f"{kind}_catalog",
            ["updated_at", "id"],
            schema=SCHEMA,
        )


def downgrade() -> None:
    for kind in ("scenario", "character"):
        op.drop_index(
            f"ix_{kind}_catalog_updated_at_id", table_name=f"{kind}_catalog", schema=SCHEMA
        )
        op.drop_index(f"ix_{kind}_versions_search", table_name=f"{kind}_versions", schema=SCHEMA)
        op.drop_column(f"{kind}_versions", "search", schema=SCHEMA)
    op.execute("DROP FUNCTION IF EXISTS catalog.epistrel_scenario_search(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS catalog.epistrel_character_search(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS catalog.epistrel_search_words(jsonb, jsonpath)")
    op.drop_column("deployment_state", "listing_status_values")
