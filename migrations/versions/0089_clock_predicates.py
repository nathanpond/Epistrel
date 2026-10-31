"""the clock's core predicates: ``clock_anchor`` and the three clock checks (#195)

FR-CLOCK-3,10 (docs/02 §7.1): ``clock_anchor`` (fixed id ``01JPRED0000000000000000036``; a
statement anchoring the world clock, a commitment with ``visibility = engine``),
``time_of_day_band`` (``…0037``), ``elapsed_since`` (``…0038``) and ``scheduled_status``
(``…0039``) are inserted unless already there (0048 seeds the vocabulary as the code has it, so a
database created after this release has them already). The checks, and ``time_is`` while the
story runs the clock, are answered by the clock's owner lookup (``core.world_clock.verify``),
which is code, not data: no column changes.

Downgrade leaves the rows (a predicate is never deleted, 0048's guard), as an older release's
re-sync leaves rows it does not know alone.

Revision ID: 0089
Revises: 0088
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.propositions.codec import predicate_row
from epistrel.core.propositions.vocabulary import (
    CLOCK_ANCHOR,
    CORE_BY_ID,
    ELAPSED_SINCE,
    SCHEDULED_STATUS,
    TIME_OF_DAY_BAND,
)

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = frozenset({"qualifiers", "exclusions", "implications"})
SEEDED = (CLOCK_ANCHOR, TIME_OF_DAY_BAND, ELAPSED_SINCE, SCHEDULED_STATUS)


def upgrade() -> None:
    rows = [predicate_row(CORE_BY_ID[pid]) for pid in SEEDED]
    table = sa.table(
        "predicates",
        *(
            sa.column(name, postgresql.JSONB(none_as_null=True) if name in _JSON else None)
            for name in rows[0]
        ),
    )
    op.execute(postgresql.insert(table).values(rows).on_conflict_do_nothing(index_elements=["id"]))


def downgrade() -> None:
    """The predicate rows stay (see the module docstring)."""
