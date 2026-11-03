"""the persona traits' auto ``trait`` predicate takes a string literal object (#523)

#173 registered the ``trait`` predicate (``core.genesis.epistemic.TRAIT``) a persona's traits
are believed under with the auto default object type ``any``, though every proposition on it
holds a string literal. A claim on it ("Sam is not conflict-avoidant") then normalized its object
as an entity and came back ``unresolved_entity``. Genesis now registers it with object type
``str``; this migration gives the stories that already have it the same type, wherever no
proposition on it names an entity object (one that does keeps ``any``). It records no
configuration change: the type only states what the predicate already holds.

Downgrade puts ``any`` back on the predicates it changed (every auto ``trait`` predicate typed
``str``).

Revision ID: 0111
Revises: 0110
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0111"
down_revision: str | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE predicates SET object_type = 'str' WHERE name = 'trait' AND layer = 'auto' "
            "AND object_type = 'any' AND NOT EXISTS (SELECT 1 FROM propositions p "
            "WHERE p.predicate_id = predicates.id AND p.object_id IS NOT NULL)"
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE predicates SET object_type = 'any' "
            "WHERE name = 'trait' AND layer = 'auto' AND object_type = 'str'"
        )
    )
