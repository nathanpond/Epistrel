"""the core predicate ``state_value``: a story State's value as a proposition (#191)

FR-STATE-8, FR-VERIFY-7 (docs/02 §7.1): ``state_value`` (fixed id
``01JPRED0000000000000000035``; functional and fluent, keyed by its subject and its ``state``
qualifier) is inserted unless already there (0048 seeds the vocabulary as the code has it, so a
database created after this release has it already). Its canon is the State's fold, read
through the owner lookup ``core.states.verify`` registers, never a ``facts`` row.

Downgrade leaves the row (a predicate is never deleted, 0048's guard), as an older release's
re-sync leaves rows it does not know alone.

Revision ID: 0087
Revises: 0086
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.propositions.codec import predicate_row
from epistrel.core.propositions.vocabulary import CORE_BY_ID, STATE_VALUE

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = frozenset({"qualifiers", "exclusions", "implications"})


def upgrade() -> None:
    row = predicate_row(CORE_BY_ID[STATE_VALUE])
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
