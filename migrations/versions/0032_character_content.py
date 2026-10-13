"""a catalog character's full document, and story characters' deflection styles (#115)

- Catalog character documents (FR-CAT-2): #103's interim drafts kept the public card's
  ``name`` and ``public_description`` at the top level; #115's ``CharacterDraft`` keeps them
  under ``public_card``, beside the images #114 put there. Every draft
  (``catalog.character_catalog.draft``) and every published version
  (``catalog.character_versions.content``) that holds either key at the top level has it moved
  under ``public_card``, so every reader sees one shape. Rewriting a version changes its
  representation, not what was published: the versions' immutability trigger lets it through
  under the transaction-local ``epistrel.erasure`` setting (the same exemption a delete takes),
  which the migration turns off again. The generated ``search`` column follows the content.
- ``characters.deflection_style`` (text, nullable; FR-GUARD-7): how a story character refuses or
  changes the subject. The Lite prompt renders it with the character's author notes.

Downgrade moves ``public_card.name`` and ``public_card.public_description`` back to the top
level (the rest of #115's fields stay where they are) and drops the column.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = (
    """
    UPDATE catalog.character_catalog
    SET draft = (draft - 'name' - 'public_description') || jsonb_build_object(
        'public_card',
        CASE WHEN jsonb_typeof(draft -> 'public_card') = 'object'
            THEN draft -> 'public_card' ELSE '{}'::jsonb END
        || jsonb_strip_nulls(jsonb_build_object(
            'name', draft -> 'name', 'public_description', draft -> 'public_description')))
    WHERE draft ?| array['name', 'public_description']
    """,
    """
    UPDATE catalog.character_versions
    SET content = (content - 'name' - 'public_description') || jsonb_build_object(
        'public_card',
        CASE WHEN jsonb_typeof(content -> 'public_card') = 'object'
            THEN content -> 'public_card' ELSE '{}'::jsonb END
        || jsonb_strip_nulls(jsonb_build_object(
            'name', content -> 'name', 'public_description', content -> 'public_description')))
    WHERE content ?| array['name', 'public_description']
    """,
)
"""The public card's name and public description move under ``public_card``, in every draft and
every version that holds them at the top level."""

DOWNGRADE = (
    """
    UPDATE catalog.character_catalog
    SET draft = (draft #- '{public_card,name}' #- '{public_card,public_description}')
        || jsonb_strip_nulls(jsonb_build_object(
            'name', draft #> '{public_card,name}',
            'public_description', draft #> '{public_card,public_description}'))
    WHERE jsonb_typeof(draft -> 'public_card') = 'object'
        AND draft -> 'public_card' ?| array['name', 'public_description']
    """,
    """
    UPDATE catalog.character_versions
    SET content = (content #- '{public_card,name}' #- '{public_card,public_description}')
        || jsonb_strip_nulls(jsonb_build_object(
            'name', content #> '{public_card,name}',
            'public_description', content #> '{public_card,public_description}'))
    WHERE jsonb_typeof(content -> 'public_card') = 'object'
        AND content -> 'public_card' ?| array['name', 'public_description']
    """,
)
"""The reverse: back to the top level."""


def _rewrite(statements: Sequence[str]) -> None:
    """Run ``statements`` with the versions' immutability exemption on, then off again."""
    op.execute("SELECT set_config('epistrel.erasure', 'on', true)")
    for statement in statements:
        op.execute(statement)
    op.execute("SELECT set_config('epistrel.erasure', 'off', true)")


def upgrade() -> None:
    _rewrite(UPGRADE)
    op.add_column("characters", sa.Column("deflection_style", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("characters", "deflection_style")
    _rewrite(DOWNGRADE)
