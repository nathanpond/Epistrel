"""entity resolution: what the mention resolver reads off an entity and its aliases (#139)

FR-PROP-6 (docs/02 §4.9): ``core.resolution`` reads these columns; ``core.propositions.store``
writes them.

- ``entities`` gains ``gender`` (``masculine``, ``feminine`` or ``NULL``: the pronoun source,
  ``NULL`` being compatible with any pronoun), ``descriptors`` (a JSON list of the words a
  description of the entity can match: the stopword-filtered words of a character's appearance
  or a scenario entity's description) and ``created_by_operation_id`` (the operation that created
  the entity; ``NULL`` for a backfilled one).
- ``entity_aliases`` gains ``source``: where the alias comes from (``canonical``,
  ``catalog_alias``, ``name_override``, ``scenario_entity``, ``persona``, ``canonized``,
  ``detected``). Rows from before this migration are ``detected``.
- **Backfill:** every ``characters`` row without an entity gets one (#131's 0048 created the
  tables but nothing filled them): the same id as the character, ``kind = person``, its name as
  the canonical name and as a ``canonical`` alias, and the gender of its persona snapshot, if
  any. These rows are not in the erasure index (no ``subject_references``); erasure still
  removes an erased character's entity aliases by the ``entities.character_id`` join.

Downgrade drops the columns and leaves the backfilled rows (#131's tables hold entities without
them).

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.ids import new_ulid

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = (
    "canonical",
    "catalog_alias",
    "name_override",
    "scenario_entity",
    "persona",
    "canonized",
    "detected",
)

MASCULINE = frozenset({"man", "male", "boy", "m", "he", "him", "masculine"})
FEMININE = frozenset({"woman", "female", "girl", "f", "she", "her", "feminine"})

UNMATCHED = sa.text(
    "SELECT c.id, c.story_id, c.name, c.persona_snapshot ->> 'gender' AS gender "
    "FROM characters c WHERE NOT EXISTS (SELECT 1 FROM entities e WHERE e.id = c.id) "
    "ORDER BY c.story_id, c.ordinal"
)
INSERT_ENTITY = sa.text(
    "INSERT INTO entities (id, story_id, kind, canonical_name, character_id, gender) "
    "VALUES (:id, :story_id, 'person', :name, :id, :gender)"
)
INSERT_ALIAS = sa.text(
    "INSERT INTO entity_aliases (id, story_id, entity_id, alias, source) "
    "VALUES (:alias_id, :story_id, :id, :name, 'canonical')"
)


def _gender(value: str | None) -> str | None:
    word = (value or "").strip().casefold()
    if word in MASCULINE:
        return "masculine"
    if word in FEMININE:
        return "feminine"
    return None


def upgrade() -> None:
    op.add_column("entities", sa.Column("gender", sa.Text(), nullable=True))
    op.add_column(
        "entities",
        sa.Column(
            "descriptors",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("entities", sa.Column("created_by_operation_id", sa.CHAR(26), nullable=True))
    op.create_check_constraint(
        "ck_entities_gender", "entities", "gender IS NULL OR gender IN ('masculine', 'feminine')"
    )
    op.add_column(
        "entity_aliases",
        sa.Column("source", sa.Text(), server_default=sa.text("'detected'"), nullable=False),
    )
    allowed = ", ".join(f"'{s}'" for s in SOURCES)
    op.create_check_constraint(
        "ck_entity_aliases_source", "entity_aliases", f"source IN ({allowed})"
    )
    bind = op.get_bind()
    for row in bind.execute(UNMATCHED).all():
        values = {
            "id": row.id,
            "story_id": row.story_id,
            "name": row.name,
            "gender": _gender(row.gender),
            "alias_id": new_ulid(),
        }
        bind.execute(INSERT_ENTITY, values)
        bind.execute(INSERT_ALIAS, values)


def downgrade() -> None:
    op.drop_constraint("ck_entity_aliases_source", "entity_aliases", type_="check")
    op.drop_column("entity_aliases", "source")
    op.drop_constraint("ck_entities_gender", "entities", type_="check")
    op.drop_column("entities", "created_by_operation_id")
    op.drop_column("entities", "descriptors")
    op.drop_column("entities", "gender")
