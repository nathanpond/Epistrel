"""Image limits: reservation accounting and limit counters (#262)

FR-IMG-11 (as amended by #262: deployment and catalog-entry limits only): each
``image_reservations`` row gains its accounting columns. The attribution columns are ``story_id``,
``entry_kind`` and ``entry_id``. The others are the immutable ``counter_keys`` snapshot and
``requested``, then ``used``, ``overage``, ``extra_outputs``, the settlement ``basis``, the
``state`` and ``settled_at``. Rows written before this migration were never reserved, so they
become ``released`` with nothing requested. A trigger refuses any change of the immutable columns
(``image_reservation_immutable``). The one exception is a story's deletion, which nulls
``story_id`` while ``epistrel.allow_story_delete`` names that story.
``image_limit_counters`` holds the stored sums per ``(scope, scope_key, period)``, and
``image_jobs.status_detail`` holds a ``cap_reached`` job's ``{scope, period}``. ``audit_log``
gains the kind ``image_counters_reconciled`` (a ``--fix`` run's counts per scope). The
``image_counter_reconcile`` and ``image_accounting_retention`` recurring tasks' rows are
reconciled from the code registry (#71), so no row is written here.

Revision ID: 0137
Revises: 0136
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0137"
down_revision: str | None = "0136"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "guardrail_input_rejected",
    "story_deleted",
    "catalog_entry_deleted",
    "persona_deleted",
    "rating_change_refused",
    "erasure",
    "story_import",
    "backfill_cancelled",
    "backfill_failed",
    "image_output_rejected",
    "image_job_ended",
    "image_prompt_rejected",
    "image_upload_rejected",
)
ADDED_KINDS = ("image_counters_reconciled",)

IMMUTABLE = """
CREATE FUNCTION epistrel_image_reservations_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.attempt_token IS DISTINCT FROM OLD.attempt_token
        OR NEW.requested IS DISTINCT FROM OLD.requested
        OR NEW.counter_keys IS DISTINCT FROM OLD.counter_keys
        OR NEW.entry_kind IS DISTINCT FROM OLD.entry_kind
        OR NEW.entry_id IS DISTINCT FROM OLD.entry_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR (
            NEW.story_id IS DISTINCT FROM OLD.story_id
            AND NOT (
                NEW.story_id IS NULL
                AND OLD.story_id = current_setting('epistrel.allow_story_delete', true)
            )
        )
    THEN
        RAISE EXCEPTION 'image_reservation_immutable: % %', OLD.job_id, OLD.attempt_token;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER image_reservations_immutable
    BEFORE UPDATE ON image_reservations
    FOR EACH ROW EXECUTE FUNCTION epistrel_image_reservations_immutable();
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.add_column("image_jobs", sa.Column("status_detail", postgresql.JSONB(), nullable=True))
    columns = (
        sa.Column("story_id", sa.CHAR(26), nullable=True),
        sa.Column("entry_kind", sa.Text(), nullable=True),
        sa.Column("entry_id", sa.CHAR(26), nullable=True),
        sa.Column(
            "counter_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("requested", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("overage", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("extra_outputs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("basis", sa.Text(), nullable=False, server_default=sa.text("'actual'")),
        # Rows from before this migration were never reserved: ``released``, nothing requested.
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'released'")),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    for column in columns:
        op.add_column("image_reservations", column)
    op.alter_column("image_reservations", "state", server_default=None)
    op.create_check_constraint(
        "ck_image_reservations_state",
        "image_reservations",
        _in("state", ("held", "settled", "released")),
    )
    op.create_check_constraint(
        "ck_image_reservations_basis",
        "image_reservations",
        _in("basis", ("actual", "presumed", "corrected")),
    )
    op.create_check_constraint(
        "ck_image_reservations_entry_kind",
        "image_reservations",
        "entry_kind IS NULL OR entry_kind IN ('character', 'scenario')",
    )
    op.create_check_constraint(
        "ck_image_reservations_accounting",
        "image_reservations",
        "requested >= 0 AND used >= 0 AND overage >= 0 AND extra_outputs >= 0",
    )
    op.create_index(
        "ix_image_reservations_held",
        "image_reservations",
        ["job_id"],
        postgresql_where=sa.text("state = 'held'"),
    )
    op.create_index("ix_image_reservations_story_id", "image_reservations", ["story_id"])
    op.create_index("ix_image_reservations_created_at", "image_reservations", ["created_at"])
    op.execute(IMMUTABLE)
    op.create_table(
        "image_limit_counters",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("overage", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("extra_outputs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_record_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope", "scope_key", "period", name="pk_image_limit_counters"),
        sa.CheckConstraint(
            "scope IN ('deployment', 'entry')", name="ck_image_limit_counters_scope"
        ),
        sa.CheckConstraint(
            "reserved >= 0 AND used >= 0 AND overage >= 0 AND extra_outputs >= 0",
            name="ck_image_limit_counters_counts",
        ),
        sa.CheckConstraint("limit_value >= 0", name="ck_image_limit_counters_limit_value"),
    )
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, *ADDED_KINDS))
    )


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE kind = 'image_counters_reconciled'")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))
    op.drop_table("image_limit_counters")
    op.execute("DROP TRIGGER image_reservations_immutable ON image_reservations")
    op.execute("DROP FUNCTION epistrel_image_reservations_immutable()")
    op.drop_index("ix_image_reservations_created_at", table_name="image_reservations")
    op.drop_index("ix_image_reservations_story_id", table_name="image_reservations")
    op.drop_index("ix_image_reservations_held", table_name="image_reservations")
    for name in ("accounting", "entry_kind", "basis", "state"):
        op.drop_constraint(f"ck_image_reservations_{name}", "image_reservations", type_="check")
    for column in (
        "settled_at",
        "state",
        "basis",
        "extra_outputs",
        "overage",
        "used",
        "requested",
        "counter_keys",
        "entry_id",
        "entry_kind",
        "story_id",
    ):
        op.drop_column("image_reservations", column)
    op.drop_column("image_jobs", "status_detail")
