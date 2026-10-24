"""a story character's participation role (#176)

FR-MULTI-7 (docs/02 §4.4, §8): each character of a story has a participation role for a turn,
``primary``, ``reactive`` or ``silent``, either a default the engine may change for a turn
(escalation, FR-MULTI-10; budget planning, NFR-PERF-7) or fixed (a bound the engine never
crosses on its own).

- ``characters.participation_role`` (``text``, NOT NULL, default ``reactive``, CHECK ``primary``,
  ``reactive``, ``silent``): a story's cast, a character added mid-story and a minor modeled
  lazily (#163) all start ``reactive``; the author sets a lead ``primary`` with
  ``PATCH /stories/{id}/characters/{cid}/participation``. The player's character has no role (the
  column is ignored for it).
- ``characters.participation_fixed`` (``boolean``, NOT NULL, default ``false``).

Downgrade drops the columns.

Revision ID: 0078
Revises: 0077
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "participation_role", sa.Text(), server_default=sa.text("'reactive'"), nullable=False
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "participation_fixed", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_characters_participation_role",
        "characters",
        "participation_role IN ('primary', 'reactive', 'silent')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_characters_participation_role", "characters", type_="check")
    op.drop_column("characters", "participation_fixed")
    op.drop_column("characters", "participation_role")
