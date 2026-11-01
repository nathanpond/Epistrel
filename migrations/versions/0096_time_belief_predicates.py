"""the time-belief core predicates: ``current_time`` and ``unconscious`` (#200)

FR-CLOCK-10,11,15 (docs/02 §7.1): ``current_time`` (fixed id ``01JPRED0000000000000000040``; a
character's belief about what time it is, viewpoint-free, held only as a belief written through
``transition()``) and ``unconscious`` (``…0041``; a fluent with the ``kind`` qualifier, read by
perception routing) are inserted unless already there (0048 seeds the vocabulary as the code has
it, so a database created after this release has them already). A character's time belief
itself is computed (``core.world_clock.time_belief``), never stored: no column changes.

Downgrade leaves the rows (a predicate is never deleted, 0048's guard), as an older release's
re-sync leaves rows it does not know alone.

Revision ID: 0096
Revises: 0095
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.propositions.codec import predicate_row
from epistrel.core.propositions.vocabulary import CORE_BY_ID, CURRENT_TIME, UNCONSCIOUS

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = frozenset({"qualifiers", "exclusions", "implications"})
SEEDED = (CURRENT_TIME, UNCONSCIOUS)


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
