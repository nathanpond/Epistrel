"""erasure ledger reconcile: blocked stories, settled ledger rows, append-only ledger (#125)

- ``stories.status`` gains ``blocked_by_erasure`` (FR-ADMIN-7): a story the erasure reconciler
  holds until it has reapplied an erasure the ledger file records and the database (a restored
  backup) did not. ``stories.status_before_block`` keeps the status to restore, set exactly
  while the story is blocked (``ck_stories_status_before_block``).
- ``erasure_ledger.applied_note`` (how a reconciled row was settled: ``reapplied``,
  ``story_missing``, ``subject_missing``, ``story_deleted``) and ``erasure_ledger.copied_from``
  (a row a fork copied, #127).
- ``erasure_ledger`` becomes append-only: the trigger ``epistrel_erasure_ledger_append_only``
  refuses DELETE and TRUNCATE (SQLSTATE ``EPI01``), and an UPDATE that changes anything but
  ``applied_at``, ``applied_note``, ``ledger_hash_prev`` and ``ledger_hash``.
- ``deployment_state.erasure_reconcile_state``: the reconciler's last run (``in_progress``,
  ``last_run_at``, ``last_error``, ``attempts`` per pending erasure).

Downgrade removes them all; a blocked story gets back the status it had.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

# Frozen here: migrations never change when ``epistrel.core.enums.StoryStatus`` does.
STATUS_CHECK_BEFORE = "status IN ('active', 'archived', 'paused')"
STATUS_CHECK = "status IN ('active', 'archived', 'paused', 'blocked_by_erasure')"
STATUS_BEFORE_BLOCK_CHECK = (
    "(status = 'blocked_by_erasure') = (status_before_block IS NOT NULL) AND "
    "(status_before_block IS NULL OR status_before_block IN ('active', 'archived', 'paused'))"
)
APPLIED_NOTE_CHECK = (
    "applied_note IS NULL OR applied_note IN "
    "('reapplied', 'story_missing', 'subject_missing', 'story_deleted')"
)

LEDGER_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_erasure_ledger_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    settled text[] := ARRAY['applied_at', 'applied_note', 'ledger_hash_prev', 'ledger_hash'];
BEGIN
    IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - settled) = (to_jsonb(OLD) - settled) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'the erasure ledger is append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'the erasure ledger is append-only: % of entry % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""


def upgrade() -> None:
    op.drop_constraint("ck_stories_status", "stories", type_="check")
    op.create_check_constraint("ck_stories_status", "stories", STATUS_CHECK)
    op.add_column("stories", sa.Column("status_before_block", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_stories_status_before_block", "stories", STATUS_BEFORE_BLOCK_CHECK
    )
    op.add_column("erasure_ledger", sa.Column("applied_note", sa.Text(), nullable=True))
    op.add_column("erasure_ledger", sa.Column("copied_from", ULID, nullable=True))
    op.create_check_constraint(
        "ck_erasure_ledger_applied_note", "erasure_ledger", APPLIED_NOTE_CHECK
    )
    op.execute(LEDGER_GUARD)
    op.execute(
        "CREATE TRIGGER erasure_ledger_append_only BEFORE UPDATE OR DELETE ON erasure_ledger "
        "FOR EACH ROW EXECUTE FUNCTION epistrel_erasure_ledger_append_only()"
    )
    op.execute(
        "CREATE TRIGGER erasure_ledger_no_truncate BEFORE TRUNCATE ON erasure_ledger "
        "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_erasure_ledger_append_only()"
    )
    op.add_column(
        "deployment_state",
        sa.Column(
            "erasure_reconcile_state",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("deployment_state", "erasure_reconcile_state")
    op.execute("DROP TRIGGER erasure_ledger_no_truncate ON erasure_ledger")
    op.execute("DROP TRIGGER erasure_ledger_append_only ON erasure_ledger")
    op.execute("DROP FUNCTION epistrel_erasure_ledger_append_only()")
    op.drop_constraint("ck_erasure_ledger_applied_note", "erasure_ledger", type_="check")
    op.drop_column("erasure_ledger", "copied_from")
    op.drop_column("erasure_ledger", "applied_note")
    op.drop_constraint("ck_stories_status_before_block", "stories", type_="check")
    op.execute(
        "UPDATE stories SET status = status_before_block WHERE status = 'blocked_by_erasure'"
    )
    op.drop_column("stories", "status_before_block")
    op.drop_constraint("ck_stories_status", "stories", type_="check")
    op.create_check_constraint("ck_stories_status", "stories", STATUS_CHECK_BEFORE)
