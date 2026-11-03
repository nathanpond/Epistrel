"""Pruning's own database guards (#702): ``pruned`` is terminal, and per-table prune exemptions

FR-STORE-32: an operation flipped to ``pruned`` never changes membership again
(``operations_pruned_terminal``). The append-only guards of the tables a pruning deletes from
gain their own exemption, ``epistrel.allow_prune``, scoped to the story and to rows of an
operation already ``pruned`` (as the events guard's, migration 0124), so a pruning no longer
borrows the story-delete setting. The clause is inserted at the head of each guard function
whose trigger is on one of ``TABLES`` and whose body admits ``epistrel.allow_story_delete``.

Revision ID: 0126
Revises: 0125
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0126"
down_revision: str | None = "0125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "belief_transitions",
    "divergences",
    "exposures",
    "beliefs",
    "utterances",
    "memories",
    "dependency_edges",
    "thunks",
    "promissory_ledger",
    "escalations",
    "extraction_shadow",
    "facts",
    "state_changes",
    "scheduled_events",
    "state_definitions",
    "lite_summary_segments",
    "proposition_changes",
    "transcript_messages",
    "client_events",
    "external_values",
)
"""``core.prune.PRUNABLE_TABLES`` at this revision."""

CLAUSE = """
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

TERMINAL = """
CREATE OR REPLACE FUNCTION epistrel_operations_pruned_terminal() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.membership::text = 'pruned' AND NEW.membership::text <> 'pruned' THEN
        RAISE EXCEPTION 'operation % is pruned: its membership is terminal', OLD.operation_id
            USING ERRCODE = 'EPI01';
    END IF;
    RETURN NEW;
END
$$;
"""

INJECT = """
DO $do$
DECLARE
    fn record;
    body text;
BEGIN
    FOR fn IN
        WITH guards AS MATERIALIZED (
            SELECT DISTINCT p.oid
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE NOT t.tgisinternal
              AND p.prokind = 'f'
              AND (p.proname LIKE '%%append_only' OR p.proname LIKE '%%_guard')
              AND c.relname::text = ANY(%(tables)s)
        )
        SELECT oid FROM guards
        WHERE pg_get_functiondef(oid) LIKE '%%allow_story_delete%%'
          AND pg_get_functiondef(oid) NOT LIKE '%%(0126)%%'
    LOOP
        body := pg_get_functiondef(fn.oid);
        body := regexp_replace(body, E'\\nBEGIN\\n', E'\\nBEGIN\\n' || %(clause)s, '');
        EXECUTE body;
    END LOOP;
END
$do$;
"""

REMOVE = """
DO $do$
DECLARE
    fn record;
    body text;
    head text := E'\\n    -- epistrel prune exemption (0126)\\n';
    tail text := E'-- epistrel prune exemption (0126) end\\n';
    start integer;
    stop integer;
BEGIN
    FOR fn IN
        WITH funcs AS MATERIALIZED (
            SELECT p.oid FROM pg_proc p
            WHERE p.prokind = 'f'
              AND (p.proname LIKE '%%append_only' OR p.proname LIKE '%%_guard')
        )
        SELECT oid FROM funcs WHERE pg_get_functiondef(oid) LIKE '%%(0126)%%'
    LOOP
        body := pg_get_functiondef(fn.oid);
        start := strpos(body, head);
        stop := strpos(body, tail);
        IF start > 0 AND stop > start THEN
            body := substr(body, 1, start - 1) || substr(body, stop + length(tail));
            EXECUTE body;
        END IF;
    END LOOP;
END
$do$;
"""
"""Each guard's clause cut out by position (the clause, between its two marker comments, and
the line break before it), giving back the function as it was."""


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def upgrade() -> None:
    op.execute(TERMINAL)
    op.execute(
        "CREATE TRIGGER operations_pruned_terminal BEFORE UPDATE ON operations "
        "FOR EACH ROW EXECUTE FUNCTION epistrel_operations_pruned_terminal()"
    )
    tables = "ARRAY[" + ", ".join(_literal(t) for t in TABLES) + "]::text[]"
    op.execute(
        INJECT.replace("%(tables)s", tables)
        .replace("%(clause)s", _literal(CLAUSE))
        .replace("%%", "%")
    )


def downgrade() -> None:
    op.execute(REMOVE.replace("%%", "%"))
    op.execute("DROP TRIGGER IF EXISTS operations_pruned_terminal ON operations")
    op.execute("DROP FUNCTION IF EXISTS epistrel_operations_pruned_terminal()")
