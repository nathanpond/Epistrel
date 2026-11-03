"""amendments: history corrections stored outside the narrative and folded into it (#221)

FR-REWRITE-9,12, FR-STORE-20,21 (docs/02 §4.4): ``core.tables.amendments`` describes the
tables.

- ``amendments``: one row per amendment, a transaction of the ``rewrite_session`` operation that
  wrote it, positioned nowhere: author, authority, kind (``rewrite``, ``undo``, ``redo``,
  ``directed_resolution``), payload, status (``active`` / ``reverted``), ``reverted_by``,
  ``undoes``/``redoes`` and the rationale records.
- ``amendment_parts``: the amendment's parts, a ``proposition`` part (a correction to a
  proposition key or a story-owned State, with its valid-time as clock offsets and the
  expression it came from, the fact it wrote and those it superseded) or a ``record`` part (a
  correction to one exact record: table, id, action, strategy and the before and after
  patches). Both tables are append-only except ``status``, ``tx_to``, ``reverted_by`` and
  ``written_state_change_id``.
- ``facts``, ``state_changes`` and ``scheduled_events`` gain ``amendment_part_id``; the
  ``positioned`` CHECKs of ``state_changes`` (0084) and ``scheduled_events`` (0093) admit a
  position-less row only when it carries one (the owner's decision 1, 2026-11-01).
- The guards of ``memories`` (0079), ``utterances`` (0063) and ``state_changes`` (0099) admit an
  in-place record correction of the allow-listed columns of the row the transaction-local
  setting ``epistrel.allow_record_correction`` names (``memories.text``/``inputs_hash``,
  ``utterances``' relations and classification, ``state_changes.value``/``delta``/
  ``new_value``).

Downgrade restores the guards and the CHECKs, and drops the columns and the tables (refused
while a position-less corrected row exists).

Revision ID: 0112
Revises: 0111
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0112"
down_revision: str | None = "0111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_amendments_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['status', 'tx_to', 'reverted_by', 'written_state_change_id'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF current_setting('epistrel.erasure', true) = 'on' THEN
            mutable := mutable || ARRAY['payload', 'patch_before', 'patch_after', 'valid_spec'];
        END IF;
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '%: update of % refused', TG_TABLE_NAME, OLD.id USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION '%: TRUNCATE refused', TG_TABLE_NAME USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION '%: % of % refused', TG_TABLE_NAME, TG_OP, OLD.id USING ERRCODE = 'EPI01';
END
$$;
"""

CORRECTION = "current_setting('epistrel.allow_record_correction', true) = OLD.id::text"

MEMORIES_OLD = """        IF current_setting('epistrel.sanitize', true) = OLD.id::text
           AND (to_jsonb(NEW) - mutable - 'text' - 'inputs_hash')
               = (to_jsonb(OLD) - mutable - 'text' - 'inputs_hash') THEN
            RETURN NEW;
        END IF;"""
MEMORIES_NEW = (
    MEMORIES_OLD
    + f"""
        IF {CORRECTION}
           AND (to_jsonb(NEW) - mutable - 'text' - 'inputs_hash')
               = (to_jsonb(OLD) - mutable - 'text' - 'inputs_hash') THEN
            RETURN NEW;
        END IF;"""
)

UTTERANCES_OLD = """        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'utterances are append-only: update of utterance % refused', OLD.id"""
UTTERANCES_NEW = f"""        IF {CORRECTION} THEN
            mutable := mutable || ARRAY['objective_relation', 'belief_relation', 'classification',
                                        'attribution_objective_relation'];
        END IF;
""" + UTTERANCES_OLD.replace("        IF (to_jsonb", "        IF (to_jsonb", 1)

STATE_OLD = """        IF current_setting('epistrel.erasure', true) = 'on' THEN
            mutable := mutable || ARRAY['reason', 'value', 'old_value', 'new_value'];
        END IF;"""
STATE_NEW = (
    STATE_OLD
    + f"""
        IF {CORRECTION} THEN
            mutable := mutable || ARRAY['value', 'delta', 'new_value'];
        END IF;"""
)

POSITIONED_BEFORE = "narrative_turn IS NOT NULL AND turn_variant IS NOT NULL"
POSITIONED_AFTER = f"({POSITIONED_BEFORE}) OR amendment_part_id IS NOT NULL"


def _function(name: str) -> str:
    """The current definition of the plpgsql function ``name``."""
    found = op.get_bind().execute(
        sa.text("SELECT pg_get_functiondef(p.oid) FROM pg_proc p WHERE p.proname = :n"),
        {"n": name},
    )
    return str(found.scalar_one())


def _patch(name: str, before: str, after: str) -> None:
    body = _function(name)
    if before not in body:
        raise RuntimeError(f"{name}: the expected clause is not in its current definition")
    op.execute(body.replace(before, after, 1))


def _tables() -> None:
    op.create_table(
        "amendments",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("session_id", ULID, nullable=True),
        sa.Column("tx_time", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("authority", sa.Text(), nullable=False, server_default=sa.text("'author'")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("reverted_by", ULID, nullable=True),
        sa.Column("undoes", ULID, nullable=True),
        sa.Column("redoes", ULID, nullable=True),
        sa.Column(
            "rationale_record_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "kind IN ('rewrite', 'undo', 'redo', 'directed_resolution')", name="ck_amendments_kind"
        ),
        sa.CheckConstraint("status IN ('active', 'reverted')", name="ck_amendments_status"),
        sa.CheckConstraint("authority IN ('author')", name="ck_amendments_authority"),
    )
    op.create_index("ix_amendments_story", "amendments", ["story_id", "tx_time"])
    op.create_table(
        "amendment_parts",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("amendment_id", ULID, sa.ForeignKey("amendments.id"), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_id", ULID, nullable=True),
        sa.Column("predicate_id", ULID, nullable=True),
        sa.Column("qualifiers", postgresql.JSONB(), nullable=True),
        sa.Column("proposition_id", ULID, nullable=True),
        sa.Column("state_id", ULID, nullable=True),
        sa.Column("scope_key", sa.Text(), nullable=True),
        sa.Column("valid_from_minutes", sa.BigInteger(), nullable=True),
        sa.Column("valid_to_minutes", sa.BigInteger(), nullable=True),
        sa.Column("valid_spec", postgresql.JSONB(), nullable=True),
        sa.Column("written_fact_id", ULID, nullable=True),
        sa.Column(
            "superseded_fact_ids",
            postgresql.ARRAY(ULID),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("written_state_change_id", ULID, nullable=True),
        sa.Column("timing_uncertain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position_bound", postgresql.JSONB(), nullable=True),
        sa.Column("record_table", sa.Text(), nullable=True),
        sa.Column("record_id", ULID, nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("patch_before", postgresql.JSONB(), nullable=True),
        sa.Column("patch_after", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("tx_from", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("tx_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('proposition', 'record')", name="ck_amendment_parts_kind"),
        sa.CheckConstraint("status IN ('active', 'reverted')", name="ck_amendment_parts_status"),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('replace', 'drop', 'relabel')",
            name="ck_amendment_parts_action",
        ),
        sa.CheckConstraint(
            "strategy IS NULL OR strategy IN ('overlay', 'version', 'in_place')",
            name="ck_amendment_parts_strategy",
        ),
        sa.CheckConstraint(
            "kind <> 'proposition' OR ((subject_id IS NOT NULL AND predicate_id IS NOT NULL) "
            "OR state_id IS NOT NULL)",
            name="ck_amendment_parts_proposition",
        ),
        sa.CheckConstraint(
            "kind <> 'record' OR (record_table IS NOT NULL AND record_id IS NOT NULL "
            "AND action IS NOT NULL AND strategy IS NOT NULL)",
            name="ck_amendment_parts_record",
        ),
    )
    op.create_index("ix_amendment_parts_amendment", "amendment_parts", ["story_id", "amendment_id"])
    op.create_index(
        "ix_amendment_parts_record",
        "amendment_parts",
        ["story_id", "record_table", "record_id"],
        postgresql_where=sa.text("kind = 'record'"),
    )
    op.create_index(
        "ix_amendment_parts_key",
        "amendment_parts",
        ["story_id", "subject_id", "predicate_id"],
        postgresql_where=sa.text("kind = 'proposition'"),
    )
    op.execute(GUARD)
    for table in ("amendments", "amendment_parts"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION epistrel_amendments_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_amendments_append_only()"
        )


def upgrade() -> None:
    _tables()
    op.add_column(
        "facts",
        sa.Column(
            "amendment_part_id",
            ULID,
            sa.ForeignKey(
                "amendment_parts.id",
                name="fk_facts_amendment_part",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=True,
        ),
    )
    for table, check in (
        ("state_changes", "ck_state_changes_positioned"),
        ("scheduled_events", "ck_scheduled_events_positioned"),
    ):
        op.add_column(
            table,
            sa.Column(
                "amendment_part_id",
                ULID,
                sa.ForeignKey(
                    "amendment_parts.id",
                    name=f"fk_{table}_amendment_part",
                    deferrable=True,
                    initially="DEFERRED",
                ),
                nullable=True,
            ),
        )
        op.drop_constraint(check, table, type_="check")
        op.create_check_constraint(check, table, POSITIONED_AFTER)
    _patch("epistrel_memories_guard", MEMORIES_OLD, MEMORIES_NEW)
    _patch("epistrel_utterances_append_only", UTTERANCES_OLD, UTTERANCES_NEW)
    _patch("epistrel_state_changes_append_only", STATE_OLD, STATE_NEW)


def downgrade() -> None:
    _patch("epistrel_state_changes_append_only", STATE_NEW, STATE_OLD)
    _patch("epistrel_utterances_append_only", UTTERANCES_NEW, UTTERANCES_OLD)
    _patch("epistrel_memories_guard", MEMORIES_NEW, MEMORIES_OLD)
    bind = op.get_bind()
    for table, check in (
        ("state_changes", "ck_state_changes_positioned"),
        ("scheduled_events", "ck_scheduled_events_positioned"),
    ):
        if bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE amendment_part_id IS NOT NULL")  # noqa: S608
        ).scalar_one():
            raise RuntimeError(f"{table} holds amendment rows: delete the amendments first")
        op.drop_constraint(check, table, type_="check")
        op.create_check_constraint(check, table, POSITIONED_BEFORE)
        op.drop_column(table, "amendment_part_id")
    op.drop_column("facts", "amendment_part_id")
    op.drop_table("amendment_parts")
    op.drop_table("amendments")
    op.execute("DROP FUNCTION IF EXISTS epistrel_amendments_append_only()")
