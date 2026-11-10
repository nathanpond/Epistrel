"""record_provenance gains consumer_purpose: a purpose result stored as a memory (#827)

FR-MODEL-17: a story-bound consumer purpose call with ``store_as_memory`` writes one episodic
memory of its viewpoint character, provenance ``consumer_purpose`` (trust tier ``in_world``).

The value is added, never removed: Postgres cannot drop an enum value, and the type itself goes
with the migration that created it (0049).

Revision ID: 0147
Revises: 0146
Create Date: 2026-11-10

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0147"
down_revision: str | None = "0146"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE record_provenance ADD VALUE IF NOT EXISTS 'consumer_purpose'")


def downgrade() -> None:
    """Nothing: an enum value cannot be dropped (0049's downgrade drops the type), and a
    memory that names it is the story's record; a downgrade past 0147 leaves it as written."""
