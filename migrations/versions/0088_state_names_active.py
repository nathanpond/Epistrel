"""A State's name is unique among the story's active States only (#186)

FR-STATE-3,9 (docs/02 §7.1): a State declared through the API sits in an inter-turn slot and
follows that slot's membership, so a truncation that discards the declaration frees its name.
0084's ``uq_state_definitions_story_id`` (unique ``(story_id, name)`` over every membership) is
replaced by ``ex_state_definitions_active_name``: an exclusion constraint (btree, ``=`` on
``story_id`` and ``name``) over the ``active`` rows only, deferred to the commit, so that a
timeline edit's membership flip is checked once it is complete (``core.states`` refuses an
untruncation or a swap that would reactivate a taken name first, with 409
``state_name_taken``).

Downgrade restores the unique constraint; it fails while two definitions of a story share a
name (one of them discarded), which only this revision allows.

Revision ID: 0088
Revises: 0087
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_state_definitions_story_id", "state_definitions", type_="unique")
    op.execute(
        "ALTER TABLE state_definitions ADD CONSTRAINT ex_state_definitions_active_name "
        "EXCLUDE USING btree (story_id WITH =, name WITH =) WHERE (membership = 'active') "
        "DEFERRABLE INITIALLY DEFERRED"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE state_definitions DROP CONSTRAINT ex_state_definitions_active_name")
    op.create_unique_constraint(
        "uq_state_definitions_story_id", "state_definitions", ["story_id", "name"]
    )
