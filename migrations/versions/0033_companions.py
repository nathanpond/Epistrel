"""companion scenarios: one per catalog character, an explicit-rating flag (#116)

- ``catalog.scenario_catalog.companion_of`` becomes unique (``uq_scenario_catalog_companion_of``
  replaces the plain ``ix_scenario_catalog_companion_of``): a character has one companion
  scenario (FR-CAT-5). Before the index, any character that two scenarios name keeps only its
  oldest companion; the others become ordinary scenarios (only SQL could make such rows before
  #116, so this is for development databases).
- ``catalog.scenario_catalog.content_rating_explicit`` (boolean, NOT NULL, default false):
  whether a companion's content rating was set on the companion itself. Until it is, the
  companion's draft takes its character's rating whenever the character's changes.
- Every catalog character without a companion gets one, created as #116's application code
  creates it: the companion's defaults (``core.catalog.companion_slots.defaults``, written here
  in the minimal form that validates to them, so the migration does not import the catalog
  models) named and rated as the character's draft is, pinned to the character's latest
  published version (none for a character never published), owned by the character's owner, at
  its listing status, ``archived`` with an archived character and a ``draft`` otherwise, at
  draft revision 1, with no version. No operation, ownership event or subject-reference row is
  written (#123's index rebuild covers the drafts; their next save indexes them too).

Downgrade drops the flag and turns the unique index back into the plain one. Companion rows
stay: they are valid scenarios under the older schema.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-13

"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from epistrel.core.ids import new_ulid

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "catalog"
TABLE = "scenario_catalog"

DEDUPLICATE = """
    UPDATE catalog.scenario_catalog AS s SET companion_of = NULL
    WHERE s.companion_of IS NOT NULL AND EXISTS (
        SELECT 1 FROM catalog.scenario_catalog AS older
        WHERE older.companion_of = s.companion_of
            AND (older.created_at, older.id) < (s.created_at, s.id)
    )
"""
"""Keep each character's oldest companion; the others become ordinary scenarios."""

CHARACTERS = """
    SELECT c.id, c.owner_principal, c.listing_status, c.status, c.current_version, c.draft
    FROM catalog.character_catalog AS c
    WHERE NOT EXISTS (
        SELECT 1 FROM catalog.scenario_catalog AS s WHERE s.companion_of = c.id
    )
    ORDER BY c.id
"""

INSERT = """
    INSERT INTO catalog.scenario_catalog (
        id, owner_principal, listing_status, status, current_version, draft, draft_revision,
        companion_of, content_rating_explicit, created_at, updated_at
    ) VALUES (
        :id, :owner, :listing_status, CAST(:status AS catalog.entry_status), NULL,
        CAST(:draft AS jsonb), 1, :character, false, now(), now()
    )
"""


def _string(document: Any, *path: str) -> str | None:
    found = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found if isinstance(found, str) and found.strip() else None


def companion_document(
    character_id: str, name: str | None, rating_id: str | None, pin: int | None
) -> dict[str, Any]:
    """The companion's defaults (#116's ``companion_slots.defaults``) in their minimal form."""
    character: dict[str, Any] = {"catalog_id": character_id}
    if pin is not None:
        character["version"] = pin
    return {
        "public_card": {"name": name} if name else {},
        "metadata": {"content_rating_id": rating_id} if rating_id else {},
        "cast": {
            "roles": [
                {
                    "id": "npc",
                    "public_label": name or "Companion",
                    "setup_description": f"Just talk with {name}." if name else "Just talk.",
                    "kind": "npc",
                    "required": True,
                    "fill": "locked",
                    "character": character,
                    "participation": "primary",
                },
                {
                    "id": "player",
                    "public_label": "You",
                    "setup_description": "You.",
                    "kind": "player",
                    "required": True,
                    "fill": "open",
                },
            ],
            "max_characters_at_start": 2,
        },
    }


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "content_rating_explicit", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        schema=SCHEMA,
    )
    op.execute(DEDUPLICATE)
    op.drop_index("ix_scenario_catalog_companion_of", table_name=TABLE, schema=SCHEMA)
    op.create_index(
        "uq_scenario_catalog_companion_of", TABLE, ["companion_of"], unique=True, schema=SCHEMA
    )
    bind = op.get_bind()
    for row in bind.execute(sa.text(CHARACTERS)).all():
        draft = row.draft if isinstance(row.draft, Mapping) else {}
        document = companion_document(
            str(row.id),
            _string(draft, "public_card", "name"),
            _string(draft, "metadata", "content_rating_id"),
            None if row.current_version is None else int(row.current_version),
        )
        bind.execute(
            sa.text(INSERT),
            {
                "id": new_ulid(),
                "owner": row.owner_principal,
                "listing_status": row.listing_status,
                "status": "archived" if row.status == "archived" else "draft",
                "draft": json.dumps(document),
                "character": row.id,
            },
        )


def downgrade() -> None:
    op.drop_index("uq_scenario_catalog_companion_of", table_name=TABLE, schema=SCHEMA)
    op.create_index("ix_scenario_catalog_companion_of", TABLE, ["companion_of"], schema=SCHEMA)
    op.drop_column(TABLE, "content_rating_explicit", schema=SCHEMA)
