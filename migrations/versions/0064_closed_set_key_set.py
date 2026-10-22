"""closed sets over a key set: ``closed_sets.key_set`` (#441; FR-PROP-8,10, ADR-160)

A closed set has so far closed the **objects** of a predicate for one uniqueness key ("Bobby's
only enrollment is OSU"). ``key_set`` (nullable text) names one of the predicate's key
qualifiers; a row that sets it closes that qualifier's **values** for the subject instead
("Bobby's only undergraduate degree is ``bobby_ba``": ``degree_awarded``, ``key_set =
degree_id``, ``key = {subject_id, level: bachelor}``, ``members = ["bobby_ba"]``). Its members
are the qualifier values (JSON scalars), not entity ids. Existing rows keep ``key_set`` NULL and
mean exactly what they meant.

Downgrade deletes the key-set rows (an object closed set cannot express them) and drops the
column.

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-19

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("closed_sets", sa.Column("key_set", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM closed_sets WHERE key_set IS NOT NULL")
    op.drop_column("closed_sets", "key_set")
