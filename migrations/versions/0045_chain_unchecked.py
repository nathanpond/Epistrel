"""the chain check's change marker: ``stories.chain_unchecked`` and its trigger (#281)

A turn commit used to check FR-STORE-28's single chain by reading every active turn of the
story. Now it checks only the head of the chain (``Operation._check_chain``). That is enough
only while the chain was valid before the commit. So the database records when the active turn
set changed in any other way:

- ``stories.chain_unchecked`` (boolean, default ``false``): ``true`` once a change that the head
  check cannot see may have reshaped the story's active turn chain. The next commit that checks
  the chain runs the full check and clears the flag.
- ``operations_chain_changed_insert``, ``_update`` and ``_delete``: ``AFTER`` row triggers that
  set the flag when a row enters or leaves the active committed turn set
  (``kind = 'turn' AND status = 'committed' AND membership = 'active'``), or changes its
  position, variant or parent while it is in the set. The exception is the finalising update of
  the operation whose commit is running (``in_flight`` to ``committed``, named by the
  transaction-local ``epistrel.chain_operation``), because the head check covers it. A
  whole-story deletion (``epistrel.allow_story_delete``) sets nothing.

Upgrade sets the flag on every existing story, so each story gets one full check at its next
commit. Downgrade drops the triggers, the function and the column.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = """
CREATE FUNCTION epistrel_operations_chain_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    story stories.id%TYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        story := OLD.story_id;
        IF current_setting('epistrel.allow_story_delete', true) = story::text THEN
            RETURN NULL;
        END IF;
    ELSE
        story := NEW.story_id;
    END IF;
    UPDATE stories SET chain_unchecked = true WHERE id = story AND NOT chain_unchecked;
    RETURN NULL;
END
$$;
"""


def _active(row: str) -> str:
    return f"{row}.kind = 'turn' AND {row}.status = 'committed' AND {row}.membership = 'active'"


CHAIN_COLUMNS = ("status", "membership", "narrative_turn", "turn_variant", "parent_variant")

TRIGGERS = f"""
CREATE TRIGGER operations_chain_changed_insert
    AFTER INSERT ON operations
    FOR EACH ROW WHEN ({_active("NEW")})
    EXECUTE FUNCTION epistrel_operations_chain_changed();

CREATE TRIGGER operations_chain_changed_delete
    AFTER DELETE ON operations
    FOR EACH ROW WHEN ({_active("OLD")})
    EXECUTE FUNCTION epistrel_operations_chain_changed();

CREATE TRIGGER operations_chain_changed_update
    AFTER UPDATE OF {", ".join(CHAIN_COLUMNS)} ON operations
    FOR EACH ROW WHEN (
        ({_active("OLD")}
         AND ({", ".join(f"NEW.{c}" for c in CHAIN_COLUMNS)})
             IS DISTINCT FROM ({", ".join(f"OLD.{c}" for c in CHAIN_COLUMNS)}))
        OR ({_active("NEW")}
            AND NOT ({_active("OLD")})
            AND NOT (OLD.status = 'in_flight'
                     AND current_setting('epistrel.chain_operation', true)
                         IS NOT DISTINCT FROM NEW.operation_id::text))
    )
    EXECUTE FUNCTION epistrel_operations_chain_changed();
"""


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column("chain_unchecked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE stories SET chain_unchecked = true")
    op.execute(FUNCTION)
    op.execute(TRIGGERS)


def downgrade() -> None:
    for name in ("insert", "delete", "update"):
        op.execute(f"DROP TRIGGER IF EXISTS operations_chain_changed_{name} ON operations")
    op.execute("DROP FUNCTION IF EXISTS epistrel_operations_chain_changed()")
    op.drop_column("stories", "chain_unchecked")
