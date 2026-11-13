"""facts: ``closed_by``, the stored inverse of ``supersedes`` (#471, ADR-189)

A canon read decided whether a version is closed with an anti-join over every row whose
``supersedes`` contains the version's id: one ``ix_facts_supersedes`` GIN probe per candidate
row. ``closed_by`` stores that set on the closed version itself (every row whose
``supersedes`` names it, appended in the closer's own transaction), so the read looks its
closers up by primary key and tests their membership, position and record-time there.

Upgrade:

1. ``facts.closed_by char(26)[] NOT NULL DEFAULT '{}'``.
2. The append-only guard (``epistrel_facts_append_only``, 0049, 0060) lets ``closed_by``
   change, only by growing (``NEW.closed_by @> OLD.closed_by``): an id once there stays.
3. Backfill: each version's ``closed_by`` is the ids of its story's rows whose ``supersedes``
   names it, ordered by their ``tx_from`` and id.
4. ``ix_facts_supersedes`` is dropped: no read or write searches ``supersedes`` by
   containment any more (a fork maps it per row with ``unnest``).

Downgrade restores 0060's guard and the GIN index and drops the column.

Revision ID: 0152
Revises: 0151
Create Date: 2026-11-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0152"
down_revision: str | None = "0151"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

_EDGES = (
    "'introduced>pending', 'introduced>committed', 'pending>committed', "
    "'pending>introduced', 'committed>revised', 'revised>committed'"
)
"""0060's lifecycle edges (``core.commit.lifecycle.TRANSITIONS``), unchanged here."""

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_facts_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY[{mutable}];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.tx_to IS NOT NULL AND NEW.tx_to IS DISTINCT FROM OLD.tx_to)
           OR (OLD.superseded_by IS NOT NULL
               AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by) THEN
            RAISE EXCEPTION 'facts: a closed version keeps its tx_to and superseded_by (fact %)',
                OLD.id USING ERRCODE = 'EPI01';
        END IF;{closing}
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        IF (to_jsonb(NEW) - mutable - 'commit_state' - 'committed_at_position')
               = (to_jsonb(OLD) - mutable - 'commit_state' - 'committed_at_position')
           AND (OLD.commit_state::text || '>' || NEW.commit_state::text) = ANY(ARRAY[{edges}])
        THEN
            RETURN NEW;
        END IF;
        IF current_setting('epistrel.erasure', true) = 'on'
           AND (to_jsonb(NEW) - mutable - 'visibility')
               = (to_jsonb(OLD) - mutable - 'visibility') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'facts are append-only: update of fact % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'facts are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'facts are append-only: % of fact % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

CLOSING = """
        IF NOT (NEW.closed_by @> OLD.closed_by) THEN
            RAISE EXCEPTION 'facts: a version keeps every closer in closed_by (fact %)',
                OLD.id USING ERRCODE = 'EPI01';
        END IF;"""

_BEFORE = ("tx_to", "superseded_by", "membership", "membership_tx")
_AFTER = ("tx_to", "superseded_by", "closed_by", "membership", "membership_tx")


def guard(*, current: bool) -> str:
    """0152's guard (``current``) or 0060's."""
    mutable = ", ".join(f"'{c}'" for c in (_AFTER if current else _BEFORE))
    return (
        GUARD.replace("{mutable}", mutable)
        .replace("{closing}", CLOSING if current else "")
        .replace("{edges}", _EDGES)
    )


BACKFILL = """
UPDATE facts f SET closed_by = c.ids
FROM (
    SELECT s.story_id, x.closed, array_agg(s.id ORDER BY s.tx_from, s.id) AS ids
    FROM facts s CROSS JOIN LATERAL unnest(s.supersedes) AS x(closed)
    GROUP BY s.story_id, x.closed
) c
WHERE f.story_id = c.story_id AND f.id = c.closed
"""


PRUNE_CLAUSE = """
    -- epistrel prune exemption (0126)
    IF TG_OP = 'DELETE'
       AND OLD.story_id IS NOT NULL
       AND current_setting('epistrel.allow_prune', true) = OLD.story_id::text
       AND EXISTS (
           SELECT 1 FROM operations o
           WHERE o.operation_id = OLD.operation_id AND o.membership::text = 'pruned'
       ) THEN
        RETURN OLD;
    END IF;
    -- epistrel prune exemption (0126) end
"""
"""0126's pruning exemption, frozen here. ``GUARD`` replaces the whole function, so the clause
0126 spliced in at the head of its body has to be spliced in again, the same way, or a pruning
could no longer delete a fact and the round trip (I-08) would not give back 0126's function."""

REINJECT = """
DO $do$
DECLARE
    body text := pg_get_functiondef('epistrel_facts_append_only()'::regprocedure);
BEGIN
    IF strpos(body, '(0126)') = 0 THEN
        EXECUTE regexp_replace(body, E'\\nBEGIN\\n', E'\\nBEGIN\\n' || {clause}, '');
    END IF;
END
$do$;
"""


def replace_guard(*, current: bool) -> None:
    """Replace the guard with 0152's (``current``) or 0060's, keeping 0126's prune exemption."""
    op.execute(guard(current=current))
    op.execute(REINJECT.replace("{clause}", "'" + PRUNE_CLAUSE.replace("'", "''") + "'"))


def upgrade() -> None:
    op.add_column(
        "facts",
        sa.Column(
            "closed_by", postgresql.ARRAY(ULID), server_default=sa.text("'{}'"), nullable=False
        ),
    )
    replace_guard(current=True)
    op.execute(BACKFILL)
    op.drop_index("ix_facts_supersedes", table_name="facts", postgresql_using="gin")


def downgrade() -> None:
    op.create_index("ix_facts_supersedes", "facts", ["supersedes"], postgresql_using="gin")
    replace_guard(current=False)
    op.drop_column("facts", "closed_by")
