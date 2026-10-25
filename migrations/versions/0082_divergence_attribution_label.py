"""the attribution label on a divergence (#545)

FR-BELIEF-15, NFR-PRIV-1 (docs/02 §4.4): the objective relation of an attribution (does the
attributed holder actually believe what the speaker attributed?) is a label for the utterance
and the divergence records only. ``core.tables.divergences`` describes the change.

- ``divergences.attribution_objective_relation`` (nullable, ``true``/``false``/``unresolved``):
  for a lie that is an attribution claim, the label the verifier computed against the
  attributed holder's partition; ``NULL`` for any other lie. Written at insert (the append-only
  guard is unchanged).

Downgrade drops the column.

Revision ID: 0082
Revises: 0081
Create Date: 2026-10-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RELATIONS = ("true", "false", "unresolved")


def upgrade() -> None:
    op.add_column(
        "divergences", sa.Column("attribution_objective_relation", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "ck_divergences_attribution_objective_relation",
        "divergences",
        "attribution_objective_relation IN ({})".format(", ".join(repr(v) for v in RELATIONS)),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_divergences_attribution_objective_relation", "divergences", type_="check"
    )
    op.drop_column("divergences", "attribution_objective_relation")
