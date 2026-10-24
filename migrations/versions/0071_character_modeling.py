"""character modeling: narrative weight and the position a character was modeled at (#163)

FR-EPI-9,10,12, FR-CAT-7 (docs/02 §4.4): ``core.epistemic.modeling`` models a character lazily,
by interaction or salience, at ``weight = minor``.

- The Postgres enum ``character_weight`` (``minor``, ``major``) and ``characters.weight`` (NOT
  NULL, default ``major``): a story's cast, its player character and a character the author adds
  mid-story are ``major``; a character a structured render introduced before this release (an M4
  ad-hoc row: its ``character_defined`` event belongs to a ``turn`` operation) is backfilled
  ``minor``.
- ``characters.modeled_turn``, ``modeled_variant``, ``modeled_slot_k``, ``modeled_beat`` and
  ``modeled_step``: the position the character was modeled at (``core.membership.Position``'s
  five columns; NOT NULL, default 0: the genesis cast is modeled at position 0). The backfilled
  rows take their introducing turn and variant.
- ``ck_characters_player_major``: the player's character is never ``minor`` (FR-EPI-12: the
  player is not modeled as anyone's minor; it holds no partition at all).

Downgrade drops the columns, the check and the enum.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEIGHTS = ("minor", "major")
POSITION = ("turn", "variant", "slot_k", "beat", "step")

BACKFILL = """
UPDATE characters AS c
SET weight = 'minor',
    modeled_turn = COALESCE(o.narrative_turn, 0),
    modeled_variant = COALESCE(o.turn_variant, 0)
FROM events AS e
JOIN operations AS o ON o.operation_id = e.operation_id
WHERE e.story_id = c.story_id
  AND e.type = 'character_defined'
  AND e.payload ->> 'character_id' = c.id
  AND o.kind = 'turn'
  AND NOT c.is_player
"""


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*WEIGHTS, name="character_weight").create(bind)
    op.add_column(
        "characters",
        sa.Column(
            "weight",
            postgresql.ENUM(name="character_weight", create_type=False),
            server_default=sa.text("'major'"),
            nullable=False,
        ),
    )
    for part in POSITION:
        column_type = sa.Integer() if part == "turn" else sa.SmallInteger()
        op.add_column(
            "characters",
            sa.Column(f"modeled_{part}", column_type, server_default=sa.text("0"), nullable=False),
        )
    op.execute(BACKFILL)
    op.create_check_constraint(
        "ck_characters_player_major", "characters", "NOT is_player OR weight = 'major'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_characters_player_major", "characters", type_="check")
    for part in reversed(POSITION):
        op.drop_column("characters", f"modeled_{part}")
    op.drop_column("characters", "weight")
    postgresql.ENUM(name="character_weight").drop(op.get_bind())
