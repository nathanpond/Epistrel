"""state changes: contingency settlement (#216)

FR-STATE-14 (docs/02 §7.1): a State effect in a turn that requests external effects is
``unconditional``, a ``reservation`` (``held`` at intent) or ``conditional`` (``pending`` at
intent). The status values and the ``settlement`` source exist since 0084; this migration lets
a settlement move them:

- ``epistrel_state_changes_append_only`` is replaced: an UPDATE may also change ``status``, but
  only along a settlement (``held → converted | released``, ``pending → converted | dropped``);
  any other status change is refused, and every other column stays as 0084 left it.
- ``state_changes`` gains ``settles_change_id`` (no foreign key): a settlement's new
  ``committed`` row names the intent row it converts. Settlement never backdates: the new row
  sits at the settling position, and the intent row is only marked ``converted``.

Downgrade restores 0084's guard and drops the column.

Revision ID: 0099
Revises: 0098
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEAD = """
CREATE OR REPLACE FUNCTION epistrel_state_changes_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF current_setting('epistrel.erasure', true) = 'on' THEN
            mutable := mutable || ARRAY['reason', 'value', 'old_value', 'new_value'];
        END IF;
"""

_SETTLEMENT = """
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            IF NOT ((OLD.status = 'held' AND NEW.status IN ('converted', 'released'))
                    OR (OLD.status = 'pending' AND NEW.status IN ('converted', 'dropped'))) THEN
                RAISE EXCEPTION 'state changes are append-only: status % to % of % refused',
                    OLD.status, NEW.status, OLD.id USING ERRCODE = 'EPI01';
            END IF;
            mutable := mutable || ARRAY['status'];
        END IF;
"""

_TAIL = """
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'state changes are append-only: update of % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'state changes are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'state changes are append-only: % of % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

GUARD = _HEAD + _SETTLEMENT + _TAIL
"""The guard with the settlement transitions (``core.tables.states.SETTLEMENT_TRANSITIONS``)."""
GUARD_0084 = _HEAD + _TAIL
"""0084's guard, which downgrade restores."""


def upgrade() -> None:
    op.add_column("state_changes", sa.Column("settles_change_id", sa.CHAR(26), nullable=True))
    op.execute(GUARD)


def downgrade() -> None:
    op.execute(GUARD_0084)
    op.drop_column("state_changes", "settles_change_id")
