"""erasure: the ledger, redaction marks and the erasure exemptions (#124)

- ``erasure_ledger`` (FR-ADMIN-2,7; docs/02 §4.10): one row per applied erasure, holding
  identifiers and counts only, never content: the subject (``subject_kind``, ``subject_id``),
  ``scope`` (``story`` or ``catalog``), the story (``story_ref``: not ``story_id``, so story
  deletion, which enumerates the ``story_id`` tables, never drops it), the acting ``principal``,
  the erasure's ``operation_id``, ``counts``, ``scan_term_hashes`` (keyed hashes of the terms the
  residue scan looked for), ``ledger_hash_prev``/``ledger_hash`` (null until #125 chains them),
  ``applied_at`` and ``at``. #125 keeps the authoritative copy outside the database.
- ``events.redacted_at``: when erasure redacted the event in place. The events trigger now lets
  an UPDATE through when the transaction set ``epistrel.erasure = on`` and the update changes
  only ``payload``, ``character_id`` and ``redacted_at``; every other UPDATE, DELETE and
  TRUNCATE is refused as before.
- ``operations.stage_log``: the stages an erasure's operations record (``redacted``,
  ``ledger_written``, ``regenerated``, ``rebuilt``), joining the operations trigger's allow-list.
- ``transcript_messages.redacted`` and ``lite_summary_segments.redacted_at``: the projections
  carry their event's redaction (refolded from ``events.redacted_at``).
- ``catalog.scenario_versions.redacted_at``: when erasure rewrote the version.
- ``erasure`` joins the ``audit_log.kind`` check.

Downgrade removes them all, restores 0002's events trigger and 0035's operations allow-list, and
deletes the ``erasure`` audit rows (under ``epistrel.allow_audit_expiry``). Redacted texts stay
redacted.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
SCOPE_CHECK = "scope IN ('story', 'catalog')"
SUBJECT_KIND_CHECK = "subject_kind IN ('character', 'catalog_character', 'persona')"
STORY_REF_CHECK = "(story_ref IS NOT NULL) = (scope = 'story')"
KIND_CHECK_BEFORE = (
    "kind IN ('guardrail_input_rejected', 'story_deleted', 'catalog_entry_deleted', "
    "'persona_deleted', 'rating_change_refused')"
)
KIND_CHECK = (
    "kind IN ('guardrail_input_rejected', 'story_deleted', 'catalog_entry_deleted', "
    "'persona_deleted', 'rating_change_refused', 'erasure')"
)

OPERATIONS_BEFORE = (
    "status",
    "membership",
    "membership_tx",
    "narrative_turn",
    "turn_variant",
    "base_revision",
    "read_set",
    "config_revisions",
    "committed_at",
    "attempts",
    "turn_report",
    "viewpoint",
    "timeline_revision",
    "error_code",
    "error",
    "parent_variant",
    "checked_guardrails",
)
OPERATIONS_AFTER = (*OPERATIONS_BEFORE, "stage_log")


def _operations_guard(columns: Sequence[str]) -> str:
    """The operations trigger function with ``columns`` as its allow-list (0035's body)."""
    array = ", ".join(f"'{c}'" for c in columns)
    return f"""
CREATE OR REPLACE FUNCTION epistrel_operations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY[
        {array}
    ];
BEGIN
    IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
        RAISE EXCEPTION 'operation %: only % may change', OLD.operation_id, mutable
            USING ERRCODE = 'EPI02';
    END IF;
    RETURN NEW;
END
$$;
"""


EVENTS_GUARD_BEFORE = """
CREATE OR REPLACE FUNCTION epistrel_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.story_id IS NOT NULL
           AND current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text THEN
            RETURN OLD;
        END IF;
        IF OLD.category = 'audit'
           AND current_setting('epistrel.allow_audit_expiry', true) = 'on' THEN
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
"""0002's function, restored by the downgrade."""

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
"""Erasure (#124) may rewrite an event's payload, speaker and redaction mark in place, and
nothing else; every other change is refused as before."""


def upgrade() -> None:
    op.create_table(
        "erasure_ledger",
        sa.Column("id", ULID, nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("story_ref", ULID, nullable=True),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        sa.Column(
            "scan_term_hashes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("ledger_hash_prev", sa.Text(), nullable=True),
        sa.Column("ledger_hash", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(SCOPE_CHECK, name="ck_erasure_ledger_scope"),
        sa.CheckConstraint(SUBJECT_KIND_CHECK, name="ck_erasure_ledger_subject_kind"),
        sa.CheckConstraint(STORY_REF_CHECK, name="ck_erasure_ledger_story_ref"),
        sa.PrimaryKeyConstraint("id", name="pk_erasure_ledger"),
    )
    op.create_index("ix_erasure_ledger_subject", "erasure_ledger", ["subject_kind", "subject_id"])
    op.create_index("ix_erasure_ledger_story_ref", "erasure_ledger", ["story_ref"])
    op.add_column("events", sa.Column("redacted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.execute(EVENTS_GUARD)
    op.add_column("operations", sa.Column("stage_log", postgresql.JSONB(), nullable=True))
    op.execute(_operations_guard(OPERATIONS_AFTER))
    op.add_column(
        "transcript_messages",
        sa.Column("redacted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "lite_summary_segments",
        sa.Column("redacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "scenario_versions",
        sa.Column("redacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="catalog",
    )
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK)


def downgrade() -> None:
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'on', true)")
    op.execute("DELETE FROM audit_log WHERE kind = 'erasure'")
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'off', true)")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK_BEFORE)
    op.drop_column("scenario_versions", "redacted_at", schema="catalog")
    op.drop_column("lite_summary_segments", "redacted_at")
    op.drop_column("transcript_messages", "redacted")
    op.execute(_operations_guard(OPERATIONS_BEFORE))
    op.drop_column("operations", "stage_log")
    op.execute(EVENTS_GUARD_BEFORE)
    op.drop_column("events", "redacted_at")
    op.drop_index("ix_erasure_ledger_story_ref", table_name="erasure_ledger")
    op.drop_index("ix_erasure_ledger_subject", table_name="erasure_ledger")
    op.drop_table("erasure_ledger")
