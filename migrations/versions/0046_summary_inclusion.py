"""the rolling summary's inclusion cache: ``stories.summary_epoch``, ``summary_inclusion`` and
their triggers (#282)

Scheduling the Lite summary after every turn (``core.summary.needs_work``) used to compute the
included segments from scratch: every segment row of the story and the membership of every
operation they name. What that yields (where the included run ends, its estimated tokens and
its segment count) changes only when one of those inputs changes, so the database records when
one does:

- ``stories.summary_epoch`` (bigint, default 0): bumped, at most once per story per
  transaction, by ``epistrel_summary_inputs_changed`` whenever an input of
  ``core.summary.included_segments`` changes.
- ``stories.summary_inclusion`` (jsonb, nullable): ``{epoch, covered_to, tokens, segments}``,
  what inclusion yielded when ``summary_epoch`` was ``epoch``. It is valid only while that is
  still the current epoch; ``core.summary`` recomputes and stores it otherwise.
- ``summary_inputs_changed_segments_*``: ``AFTER`` row triggers on every insert, update and
  delete of ``lite_summary_segments`` (rows, memberships, texts, sources).
- ``summary_inputs_changed_operations_*``: ``AFTER`` row triggers on ``operations`` when a row
  enters or leaves "committed and active" (the only state of a named operation inclusion
  reads). The exception is the finalising update of the operation whose commit is running
  (``in_flight`` to ``committed``, named by the transaction-local ``epistrel.chain_operation``,
  as migration 0045 uses it): no segment can name an operation before it commits, and a
  segment's own operation commits with the segment row, whose insert bumps the epoch.
- ``summary_inputs_truncated_*``: a ``TRUNCATE`` of either table bumps every story.

``epistrel.summary_changed`` (transaction-local) names the story whose epoch the transaction
last bumped: one bump is enough, and ``core.summary`` clears it when it stores a cache inside a
transaction. A whole-story deletion (``epistrel.allow_story_delete``) bumps nothing. Upgrade
leaves every cache empty, so each story's next scheduling computes it once. Downgrade drops the
triggers, the functions and the columns.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTIONS = """
CREATE FUNCTION epistrel_summary_inputs_changed() RETURNS trigger
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
    IF story IS NULL
       OR current_setting('epistrel.summary_changed', true) IS NOT DISTINCT FROM story::text THEN
        RETURN NULL;
    END IF;
    UPDATE stories SET summary_epoch = summary_epoch + 1 WHERE id = story;
    PERFORM set_config('epistrel.summary_changed', story::text, true);
    RETURN NULL;
END
$$;

CREATE FUNCTION epistrel_summary_inputs_truncated() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE stories SET summary_epoch = summary_epoch + 1;
    RETURN NULL;
END
$$;
"""


def _live(row: str) -> str:
    return f"({row}.status = 'committed' AND {row}.membership = 'active')"


TRIGGERS = f"""
CREATE TRIGGER summary_inputs_changed_segments_insert
    AFTER INSERT ON lite_summary_segments
    FOR EACH ROW EXECUTE FUNCTION epistrel_summary_inputs_changed();

CREATE TRIGGER summary_inputs_changed_segments_update
    AFTER UPDATE ON lite_summary_segments
    FOR EACH ROW EXECUTE FUNCTION epistrel_summary_inputs_changed();

CREATE TRIGGER summary_inputs_changed_segments_delete
    AFTER DELETE ON lite_summary_segments
    FOR EACH ROW EXECUTE FUNCTION epistrel_summary_inputs_changed();

CREATE TRIGGER summary_inputs_truncated_segments
    AFTER TRUNCATE ON lite_summary_segments
    FOR EACH STATEMENT EXECUTE FUNCTION epistrel_summary_inputs_truncated();

CREATE TRIGGER summary_inputs_changed_operations_insert
    AFTER INSERT ON operations
    FOR EACH ROW WHEN {_live("NEW")}
    EXECUTE FUNCTION epistrel_summary_inputs_changed();

CREATE TRIGGER summary_inputs_changed_operations_delete
    AFTER DELETE ON operations
    FOR EACH ROW WHEN {_live("OLD")}
    EXECUTE FUNCTION epistrel_summary_inputs_changed();

CREATE TRIGGER summary_inputs_changed_operations_update
    AFTER UPDATE OF status, membership ON operations
    FOR EACH ROW WHEN (
        {_live("OLD")} IS DISTINCT FROM {_live("NEW")}
        AND NOT (OLD.status = 'in_flight'
                 AND current_setting('epistrel.chain_operation', true)
                     IS NOT DISTINCT FROM NEW.operation_id::text)
    )
    EXECUTE FUNCTION epistrel_summary_inputs_changed();

CREATE TRIGGER summary_inputs_truncated_operations
    AFTER TRUNCATE ON operations
    FOR EACH STATEMENT EXECUTE FUNCTION epistrel_summary_inputs_truncated();
"""

TRIGGER_NAMES = (
    ("lite_summary_segments", "summary_inputs_changed_segments_insert"),
    ("lite_summary_segments", "summary_inputs_changed_segments_update"),
    ("lite_summary_segments", "summary_inputs_changed_segments_delete"),
    ("lite_summary_segments", "summary_inputs_truncated_segments"),
    ("operations", "summary_inputs_changed_operations_insert"),
    ("operations", "summary_inputs_changed_operations_delete"),
    ("operations", "summary_inputs_changed_operations_update"),
    ("operations", "summary_inputs_truncated_operations"),
)


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column("summary_epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("stories", sa.Column("summary_inclusion", postgresql.JSONB(), nullable=True))
    op.execute(FUNCTIONS)
    op.execute(TRIGGERS)


def downgrade() -> None:
    for table, name in TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS epistrel_summary_inputs_changed()")
    op.execute("DROP FUNCTION IF EXISTS epistrel_summary_inputs_truncated()")
    op.drop_column("stories", "summary_inclusion")
    op.drop_column("stories", "summary_epoch")
