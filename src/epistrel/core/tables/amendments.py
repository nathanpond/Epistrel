"""``amendments`` and ``amendment_parts``: history corrections stored outside the narrative
(FR-REWRITE-9,12, FR-STORE-20,21; docs/02 §4.4; #221, migration 0112).

An **amendment** is a transaction of a ``rewrite_session`` operation (positioned nowhere): its
author (the principal), authority (``author`` in M7), ``kind`` (``rewrite``, ``undo``,
``redo``, ``directed_resolution``), ``payload`` (the normalized changes and plan summary),
``status`` (``active`` / ``reverted``, ``reverted_by``) and the records its rationale cites
(``rationale_record_ids``, ``[{table, id}]``). It is decomposed into **parts**:

- a **proposition** part corrects a proposition key (``subject_id``, ``predicate_id``,
  ``qualifiers``) or a story-owned State (``state_id``, ``scope_key``; #236) over a valid-time
  (``valid_from_minutes``/``valid_to_minutes``, clock offsets; ``valid_spec`` the expression it
  came from), writing a position-less fact version (``written_fact_id``, closing
  ``superseded_fact_ids``); it applies in whichever continuation is active;
- a **record** part corrects one exact record (``record_table``, ``record_id``) by ``action``
  (``replace``, ``drop``, ``relabel``) under the table's ``strategy`` (``overlay``,
  ``version``, ``in_place``), keeping ``patch_before``/``patch_after``; it lives and dies with
  that record's membership.

Neither row is a derived record: amendments survive truncation (FR-STORE-21) and are reverted
only by a separate operation (``status``, ``tx_to``, ``reverted_by``). Append-only otherwise
(``epistrel_amendments_append_only``); a DELETE passes only under an erasure or the story's
deletion. ``core.amendments.store`` is the only writer.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    SmallInteger,
    Table,
    Text,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type

NOW = text("transaction_timestamp()")

amendments = Table(
    "amendments",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("operation_id", ulid_type(), nullable=False),
    Column(
        "session_id",
        ulid_type(),
        ForeignKey("rewrite_sessions.id", name="fk_amendments_session"),
        nullable=True,
    ),
    Column("tx_time", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("author", Text, nullable=False),
    Column("authority", Text, nullable=False, server_default=text("'author'")),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("reverted_by", ulid_type(), nullable=True),
    Column("undoes", ulid_type(), nullable=True),
    Column("redoes", ulid_type(), nullable=True),
    Column("rationale_record_ids", JSONB, nullable=False, server_default=text("'[]'")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    CheckConstraint("kind IN ('rewrite', 'undo', 'redo', 'directed_resolution')", name="kind"),
    CheckConstraint("status IN ('active', 'reverted')", name="status"),
    CheckConstraint("authority IN ('author')", name="authority"),
    Index("ix_amendments_story", "story_id", "tx_time"),
)

amendment_parts = Table(
    "amendment_parts",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("amendment_id", ulid_type(), ForeignKey("amendments.id"), nullable=False),
    Column("ordinal", SmallInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("subject_id", ulid_type(), nullable=True),
    Column("predicate_id", ulid_type(), nullable=True),
    Column("qualifiers", JSONB, nullable=True),
    Column("proposition_id", ulid_type(), nullable=True),
    Column("state_id", ulid_type(), nullable=True),
    Column("scope_key", Text, nullable=True),
    Column("valid_from_minutes", BigInteger, nullable=True),
    Column("valid_to_minutes", BigInteger, nullable=True),
    Column("valid_spec", JSONB, nullable=True),
    Column("written_fact_id", ulid_type(), nullable=True),
    Column("superseded_fact_ids", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("written_state_change_id", ulid_type(), nullable=True),
    Column("timing_uncertain", Boolean, nullable=False, server_default=false()),
    Column("position_bound", JSONB, nullable=True),
    Column("record_table", Text, nullable=True),
    Column("record_id", ulid_type(), nullable=True),
    Column("action", Text, nullable=True),
    Column("strategy", Text, nullable=True),
    Column("patch_before", JSONB, nullable=True),
    Column("patch_after", JSONB, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("tx_from", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("tx_to", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint("kind IN ('proposition', 'record', 'lite_note')", name="kind"),
    CheckConstraint("status IN ('active', 'reverted')", name="status"),
    CheckConstraint(
        "action IS NULL OR action IN ('replace', 'drop', 'relabel', 'set_intent')", name="action"
    ),
    CheckConstraint(
        "strategy IS NULL OR strategy IN ('overlay', 'version', 'in_place')", name="strategy"
    ),
    CheckConstraint(
        "kind <> 'proposition' OR ((subject_id IS NOT NULL AND predicate_id IS NOT NULL) "
        "OR state_id IS NOT NULL)",
        name="proposition",
    ),
    CheckConstraint(
        "kind <> 'record' OR (record_table IS NOT NULL AND record_id IS NOT NULL "
        "AND action IS NOT NULL AND strategy IS NOT NULL)",
        name="record",
    ),
    CheckConstraint(
        "kind <> 'lite_note' OR (subject_id IS NULL AND predicate_id IS NULL "
        "AND proposition_id IS NULL AND state_id IS NULL AND record_table IS NULL "
        "AND record_id IS NULL AND action IS NULL AND strategy IS NULL "
        "AND written_fact_id IS NULL AND written_state_change_id IS NULL "
        "AND patch_after ? 'text' AND patch_after - 'text' = '{}'::jsonb)",
        name="lite_note",
    ),
    Index("ix_amendment_parts_amendment", "story_id", "amendment_id"),
    Index(
        "ix_amendment_parts_record",
        "story_id",
        "record_table",
        "record_id",
        postgresql_where=text("kind = 'record'"),
    ),
    Index(
        "ix_amendment_parts_key",
        "story_id",
        "subject_id",
        "predicate_id",
        postgresql_where=text("kind = 'proposition'"),
    ),
)

__all__ = ["amendment_parts", "amendments"]
