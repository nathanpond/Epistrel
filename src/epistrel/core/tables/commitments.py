"""``thunks`` and ``promissory_ledger``: undetermined references and the obligations salience
creates (FR-COMMIT-1..3,5..7,10,13; docs/02 §4.4; #146, migration 0060).

**A thunk** is a global, introduced-but-undetermined reference: a proposition with one open
argument ("someone knocked at the door"), which no character's partition holds a value for
because the world has none yet (FR-COMMIT-6,10). One row:

- **its proposition**: ``proposition_sketch`` (#140's pre-normalization form, with the open
  slot's field ``null`` and its ``candidates``: ``core.commit.sketch.ThunkSketch``), and the
  normalized columns matching reads: ``predicate_id``, the known arguments ``subject_id`` and
  ``object_id``, and ``open_slot`` (``subject``, ``object`` or ``literal``; a subject-open
  thunk has no ``subject_id``, CHECK). ``valid_time`` is the story-minute the reference is
  about (the position's, unless the sketch said when): a resolution's fact starts there.
- **where it came from**: the derived-record columns of ``core.membership`` (the introducing
  operation, ``source_event_ids``, ``narrative_turn``/``turn_variant``) plus ``beat`` and
  ``step`` (#168, migration 0075: the introducing beat's declared causal step), and
  ``introduced_event_id`` (its ``thunk_introduced`` event; ``NULL`` for a genesis seed).
- **what became of it**: ``state`` (``open``, ``resolved`` or ``expired``), ``resolved_fact_id``
  and ``resolution_operation_id`` (the fact a dependence committed and the operation that
  committed it; both set exactly when it was resolved, CHECK), and ``salient`` (it carries, or
  carried, a ledger entry).

Membership follows the **introducing** operation only (``operation_id``): truncating the turn
that resolved a thunk introduced earlier leaves the thunk in the timeline and makes it open
again, because ``core.commit.lifecycle`` reads a ``resolved`` row as resolved only while its
``resolution_operation_id`` is active (#154's pattern; #130's alternative, flipping the whole
row on the resolution's operation, would drop the earlier turn's thunk with it).

**A ledger entry** is one salient reference awaiting commitment, per proposition (FR-COMMIT-7,
13): ``thunk_id`` (a salient open thunk: the only kind that blocks a turn's close) or
``proposition_id`` (the existence of a hidden fact named without its value), exactly one
(CHECK); ``kind`` (``salient_reference``); its position (the derived columns plus ``beat``
and ``step``); ``discharged_by_operation`` and ``discharged_mode`` (``dependence``, ``render`` or
``minimal``), set together (CHECK). An entry counts as discharged only while that operation is
active, so reverting the discharging turn reopens the obligation.

Neither table has a foreign key beyond ``operation_id`` (story tables are emptied by
``core.stories.STORY_TABLES``). ``core.commit.lifecycle`` is the only writer; an erasure
redacts a sketch and expires its thunk.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    SmallInteger,
    Table,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.enums import DischargeMode, LedgerKind, OpenSlot, ThunkState
from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

thunks = Table(
    "thunks",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("proposition_sketch", JSONB, nullable=False),
    Column("predicate_id", ulid_type(), nullable=False),
    Column("subject_id", ulid_type(), nullable=True),
    Column("object_id", ulid_type(), nullable=True),
    Column("open_slot", pg_enum(OpenSlot, "thunk_open_slot"), nullable=False),
    Column("valid_time", BigInteger, nullable=True),
    Column("introduced_event_id", ulid_type(), nullable=True),
    *derived_columns(),
    Column("beat", SmallInteger, nullable=True),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    Column(
        "state",
        pg_enum(ThunkState, "thunk_state"),
        nullable=False,
        server_default=text("'open'"),
    ),
    Column("resolved_fact_id", ulid_type(), nullable=True),
    Column("resolution_operation_id", ulid_type(), nullable=True),
    Column("salient", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint("(open_slot = 'subject') = (subject_id IS NULL)", name="open_subject"),
    CheckConstraint(
        "(resolved_fact_id IS NULL) = (resolution_operation_id IS NULL)", name="resolution_pair"
    ),
    CheckConstraint("state <> 'resolved' OR resolved_fact_id IS NOT NULL", name="resolved_fact"),
    *derived_indexes("thunks"),
)
Index("ix_thunks_story_state_predicate", thunks.c.story_id, thunks.c.state, thunks.c.predicate_id)

promissory_ledger = Table(
    "promissory_ledger",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("thunk_id", ulid_type(), nullable=True),
    Column("proposition_id", ulid_type(), nullable=True),
    Column(
        "kind",
        pg_enum(LedgerKind, "ledger_kind"),
        nullable=False,
        server_default=text("'salient_reference'"),
    ),
    *derived_columns(),
    Column("beat", SmallInteger, nullable=True),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    Column("discharged_by_operation", ulid_type(), nullable=True),
    Column("discharged_mode", pg_enum(DischargeMode, "ledger_discharge_mode"), nullable=True),
    CheckConstraint("num_nonnulls(thunk_id, proposition_id) = 1", name="one_target"),
    CheckConstraint(
        "(discharged_by_operation IS NULL) = (discharged_mode IS NULL)", name="discharge_pair"
    ),
    *derived_indexes("promissory_ledger"),
)
Index(
    "ix_promissory_ledger_story_thunk", promissory_ledger.c.story_id, promissory_ledger.c.thunk_id
)

THUNKS = register_derived(DerivedTable(thunks))
"""``thunks`` in ``core.membership.DERIVED_TABLES``: a thunk follows the operation that
introduced it (not the one that resolved it; see the module docstring)."""

PROMISSORY_LEDGER = register_derived(DerivedTable(promissory_ledger))
"""``promissory_ledger`` in ``core.membership.DERIVED_TABLES``: an entry follows the operation
that created it; its discharge is read through the discharging operation's membership."""

__all__ = ["PROMISSORY_LEDGER", "THUNKS", "promissory_ledger", "thunks"]
