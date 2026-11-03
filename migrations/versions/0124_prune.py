"""Manual pruning (#247): the ``pruned`` membership and the events guard's prune exemption

FR-STORE-32: a pruned operation keeps its ``operations`` row as a marker with membership
``pruned`` (terminal), and its non-audit, non-amendment, unredacted events may be deleted, but
only inside a pruning transaction that names the story (``epistrel.allow_prune``) and only for
an operation already flipped to ``pruned``.

Revision ID: 0124
Revises: 0123
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0124"
down_revision: str | None = "0123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENTS_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    redactable text[] := ARRAY['payload', 'character_id', 'redacted_at'];
BEGIN
    IF TG_OP = 'UPDATE'
       AND current_setting('epistrel.erasure', true) = 'on'
       AND (to_jsonb(NEW) - redactable) = (to_jsonb(OLD) - redactable) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.story_id IS NOT NULL
           AND current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text THEN
            RETURN OLD;
        END IF;
        IF OLD.category = 'audit'
           AND current_setting('epistrel.allow_audit_expiry', true) = 'on' THEN
            RETURN OLD;
        END IF;
        IF OLD.story_id IS NOT NULL
           AND current_setting('epistrel.allow_prune', true) = OLD.story_id::text
           AND OLD.category NOT IN ('audit', 'amendment')
           AND OLD.redacted_at IS NULL
           AND EXISTS (
               SELECT 1 FROM operations o
               WHERE o.operation_id = OLD.operation_id AND o.membership::text = 'pruned'
           ) THEN
            RETURN OLD;
        END IF;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'events are append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'events are append-only: % of event % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

PREVIOUS = EVENTS_GUARD.replace(
    """        IF OLD.story_id IS NOT NULL
           AND current_setting('epistrel.allow_prune', true) = OLD.story_id::text
           AND OLD.category NOT IN ('audit', 'amendment')
           AND OLD.redacted_at IS NULL
           AND EXISTS (
               SELECT 1 FROM operations o
               WHERE o.operation_id = OLD.operation_id AND o.membership::text = 'pruned'
           ) THEN
            RETURN OLD;
        END IF;
""",
    "",
)


def upgrade() -> None:
    op.execute("ALTER TYPE membership ADD VALUE IF NOT EXISTS 'pruned'")
    op.execute(EVENTS_GUARD)


def downgrade() -> None:
    op.execute(PREVIOUS)
    # An enum value cannot be dropped; ``pruned`` stays (unused) on a downgrade.
