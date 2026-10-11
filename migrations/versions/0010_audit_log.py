"""audit_log: deployment-level audit records (guardrail input rejections, story deletions)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("transaction_timestamp()")
KIND_CHECK = "kind IN ('guardrail_input_rejected', 'story_deleted')"

# Append-only: the one update allowed is nulling ``story_id`` (story deletion, #50) with every
# other column unchanged; a delete only under ``epistrel.allow_audit_expiry`` (#71's sweep).
AUDIT_GUARD = """
CREATE FUNCTION epistrel_audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.story_id IS NULL
           AND (to_jsonb(NEW) - 'story_id') = (to_jsonb(OLD) - 'story_id') THEN
            RETURN NEW;
        END IF;
    END IF;
    IF TG_OP = 'DELETE'
       AND current_setting('epistrel.allow_audit_expiry', true) = 'on' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'audit_log is append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'audit_log is append-only: % of row % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;

CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION epistrel_audit_log_append_only();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION epistrel_audit_log_append_only();
"""


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.CHAR(26), nullable=False),
        sa.Column("at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("story_id", sa.CHAR(26), nullable=True),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "policy_ids",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.CheckConstraint(KIND_CHECK, name="ck_audit_log_kind"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_story_at", "audit_log", ["story_id", "at"])
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.execute(AUDIT_GUARD)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS epistrel_audit_log_append_only()")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_index("ix_audit_log_story_at", table_name="audit_log")
    op.drop_table("audit_log")
