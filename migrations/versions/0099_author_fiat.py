"""the ``author_fiat`` belief transition kind (#598)

FR-RESP-14 (ADR-174): under ``player_authority = co_author`` with per-character partitions on, a
presupposition of the player's narration that the narrated character had no path to is accepted,
and the character's knowledge of it is recorded as an ``author_fiat`` acquisition
(``core.epistemic.transitions``), so its later renders are verified as knowing it.

- ``belief_transition_kind`` gains the label ``author_fiat``.

No table, column or seed row changes; no data change.

Downgrade records every ``author_fiat`` transition as ``author`` (author direction, the nearest
earlier cause; the beliefs they produced and every later transition stay) and rebuilds the type
without the label: PostgreSQL cannot drop an enum label.

Revision ID: 0099
Revises: 0098
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_BEFORE = (
    "observation",
    "testimony",
    "inference",
    "discovery",
    "correction",
    "forgetting",
    "revision",
    "author",
    "resolution",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE belief_transition_kind ADD VALUE IF NOT EXISTS 'author_fiat'")


def downgrade() -> None:
    op.execute("UPDATE belief_transitions SET kind = 'author' WHERE kind = 'author_fiat'")
    bind = op.get_bind()
    # The checks that compare ``kind`` (0069's) are rebuilt over the new type, as stored.
    checks = bind.execute(
        sa.text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid = 'belief_transitions'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%belief_transition_kind%'"
        )
    ).all()
    for check in checks:
        op.execute(f'ALTER TABLE belief_transitions DROP CONSTRAINT "{check.conname}"')
    op.execute("ALTER TYPE belief_transition_kind RENAME TO belief_transition_kind_0098")
    postgresql.ENUM(*KINDS_BEFORE, name="belief_transition_kind").create(bind)
    op.execute(
        "ALTER TABLE belief_transitions ALTER COLUMN kind TYPE belief_transition_kind "
        "USING kind::text::belief_transition_kind"
    )
    op.execute("DROP TYPE belief_transition_kind_0098")
    for check in checks:
        op.execute(
            f'ALTER TABLE belief_transitions ADD CONSTRAINT "{check.conname}" {check.definition}'
        )
