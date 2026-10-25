"""how a belief's holder treats it (``beliefs.disclosure``) and a relationship's trust (#173)

FR-SCN-8 (docs/02 §4.10): a seeded belief keeps the seed's disclosure (``open``, ``guarded`` or
``secret``), so a ``secret`` holder's later denial classifies as a deliberate lie, not a
hallucination (#166). ``beliefs.disclosure`` (``text``, nullable, CHECK ``open``, ``guarded``,
``secret``): set by a structured genesis on every seeded belief (an audience member's is
``open``), ``NULL`` on every other belief.

The core relationship predicates (``parent_of``, ``child_of``, ``sibling_of``, ``married_to``,
``knows``, ``friend_of``, ``enemy_of``) gain the optional ``trust`` qualifier (``float``, 0 to 1,
not part of the key): a scenario relationship's ``trust`` is seeded on its fact, where #162's
``testimony.seed_trust`` reads it.

Downgrade drops the column and the qualifier.

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RELATIONSHIPS = (
    "01JPRED0000000000000000011",
    "01JPRED0000000000000000012",
    "01JPRED0000000000000000013",
    "01JPRED0000000000000000014",
    "01JPRED0000000000000000015",
    "01JPRED0000000000000000016",
    "01JPRED0000000000000000017",
)
"""``parent_of`` … ``enemy_of`` (``core.propositions.vocabulary``'s fixed ids)."""


def _qualifiers(value: str) -> None:
    ids = ", ".join(f"'{i}'" for i in RELATIONSHIPS)
    op.execute(
        f"UPDATE predicates SET qualifiers = '{value}'::jsonb "  # noqa: S608 - constants
        f"WHERE story_id IS NULL AND id IN ({ids})"
    )


def upgrade() -> None:
    _qualifiers('{"trust": "float"}')
    op.add_column("beliefs", sa.Column("disclosure", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_beliefs_disclosure",
        "beliefs",
        "disclosure IS NULL OR disclosure IN ('open', 'guarded', 'secret')",
    )


def downgrade() -> None:
    _qualifiers("{}")
    op.drop_constraint("ck_beliefs_disclosure", "beliefs", type_="check")
    op.drop_column("beliefs", "disclosure")
