"""the core predicate ``agreed_to``: a party's consent to an action (#189)

FR-STATE-11 (docs/02 §4.9, §7.1): ``agreed_to`` (fixed id ``01JPRED0000000000000000034``; an
event, ``multi``) is inserted unless already there (0048 seeds the vocabulary as the code has
it, so a database created after this release has it already). An action's preconditions read it
as the counterparty's consent (``core.states.preconditions``).

Downgrade leaves the row (a predicate is never deleted, 0048's guard), as an older release's
re-sync leaves rows it does not know alone.

Revision ID: 0092
Revises: 0091
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.propositions.codec import predicate_row
from epistrel.core.propositions.vocabulary import AGREED_TO, CORE_BY_ID

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = frozenset({"qualifiers", "exclusions", "implications"})


def upgrade() -> None:
    row = predicate_row(CORE_BY_ID[AGREED_TO])
    table = sa.table(
        "predicates",
        *(
            sa.column(name, postgresql.JSONB(none_as_null=True) if name in _JSON else None)
            for name in row
        ),
    )
    op.execute(postgresql.insert(table).values([row]).on_conflict_do_nothing(index_elements=["id"]))


def downgrade() -> None:
    """The predicate row stays (see the module docstring)."""
