"""``state_definitions``, ``state_changes`` and ``state_values``: story States (FR-STATE-1,2,4,6;
docs/02 §7.1; #185, migration 0084).

- **``state_definitions``** — one row per State a story declares: its ``name`` (unique in the
  story), ``type`` (``state_type``), ``scope`` (``story`` or ``character``), ``constraints``
  (``{min, max, non_negative, allowed_values, tolerance}``), ``initial_value`` (the fold's base;
  ``NULL``: undefined until set), the ``aliases`` and ``unit`` that let prose name it, the
  ``entity_id`` a possession-style State tracks, ``visibility``, ``owner`` (``story`` or
  ``external``, with its ``provider_id``), ``silent``, the effect rules (``rules``,
  ``authorization``, ``transfer``; #188), ``reservation_expiry_minutes`` (#216) and
  ``created_by``. The built-in ``world_clock`` (#193, migration 0085; type ``clock``) also
  fills the clock's settings, one column each (``mode``, ``anchor``, ``pacing_table``,
  ``max_unprompted_skip_minutes``, ``stop_at_due_events``, ``grace_minutes``,
  ``time_statement_tolerance_minutes``, ``elapsed_tolerance_factor``, ``time_awareness``,
  ``bands``, ``calendar``); they are ``NULL`` for every other State (CHECK ``clock_settings``).
  It is a derived record (``core.membership``): ``operation_id`` is the operation
  that defined it, and its membership follows that operation only. ``core.states``'s
  ``update_definition`` changes the row in place; its history is the ``config_changes`` log.
- **``state_changes``** — one row per change: the State (``state_id``, no foreign key: integrity
  is ``core.states.apply_change``'s), the character it is scoped to (``scope_key``), its
  ``kind`` and ``value`` or signed ``delta``, the ``old_value``/``new_value`` it left, a
  ``reason``, its ``source``, ``contingency``, ``status`` and ``prerequisite_effect_ids``; its
  narrative position (``narrative_turn``/``turn_variant`` of the derived columns, plus ``slot_k``,
  ``beat`` and ``step``; read as a ``core.membership.Position``), its story-minute
  (``valid_time_minutes``), when it was recorded (``tx_from``) and, for a clock advance the
  Orchestrator estimated, the estimate and its reason (``estimate_meta``; #193, #197). Append-only
  (``epistrel_state_changes_append_only``): an UPDATE may change only ``membership`` and
  ``membership_tx`` (#216 adds ``status``), an erasure may also redact ``reason`` and the
  values; a DELETE passes only under an erasure or the story's deletion.
- **``state_values``** — the projection (``core.projections.states``): one row per
  ``(state, scope key)`` with at least one counted active change, its folded ``value``, the
  position of its last change (``since_*``) and ``change_count``. ``scope_key`` is ``''`` for a
  story-scoped State (a primary key column cannot be ``NULL``).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.enums import (
    ClockMode,
    StateChangeKind,
    StateChangeSource,
    StateChangeStatus,
    StateContingency,
    StateCreatedBy,
    StateOwner,
    StateScope,
    StateType,
    StateVisibility,
    TimeAwareness,
)
from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    position_group,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

STORY_SCOPE_KEY = ""
"""``state_values.scope_key`` of a story-scoped State (``state_changes.scope_key`` is ``NULL``)."""

MUTABLE_STATE_CHANGE_COLUMNS: tuple[str, ...] = ("membership", "membership_tx")
"""The columns an UPDATE of ``state_changes`` may change (#216 adds ``status``)."""

ERASURE_STATE_CHANGE_COLUMNS: tuple[str, ...] = ("reason", "value", "old_value", "new_value")
"""The columns an erasure may also rewrite (redaction of a subject's name)."""

CLOCK_SETTINGS_CHECK = (
    "(type = 'clock') = (mode IS NOT NULL) AND (type = 'clock' OR (anchor IS NULL "
    "AND pacing_table IS NULL AND max_unprompted_skip_minutes IS NULL "
    "AND stop_at_due_events IS NULL AND grace_minutes IS NULL "
    "AND time_statement_tolerance_minutes IS NULL AND elapsed_tolerance_factor IS NULL "
    "AND time_awareness IS NULL AND bands IS NULL AND calendar IS NULL))"
)
"""The clock's settings belong to the clock (``state_definitions.ck_..._clock_settings``)."""

state_definitions = Table(
    "state_definitions",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("name", Text, nullable=False),
    Column("type", pg_enum(StateType, "state_type"), nullable=False),
    Column("scope", pg_enum(StateScope, "state_scope"), nullable=False),
    Column("constraints", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("initial_value", JSONB(none_as_null=True), nullable=True),
    Column("aliases", ARRAY(Text), nullable=False, server_default=text("'{}'")),
    Column("unit", Text, nullable=True),
    Column("entity_id", ulid_type(), nullable=True),
    Column("visibility", pg_enum(StateVisibility, "state_visibility"), nullable=False),
    Column("owner", pg_enum(StateOwner, "state_owner"), nullable=False),
    Column("provider_id", Text, nullable=True),
    Column("silent", Boolean, nullable=False, server_default=text("false")),
    Column("rules", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("authorization", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("transfer", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("reservation_expiry_minutes", Integer, nullable=True),
    Column("created_by", pg_enum(StateCreatedBy, "state_created_by"), nullable=False),
    Column("mode", pg_enum(ClockMode, "clock_mode"), nullable=True),
    Column("anchor", JSONB(none_as_null=True), nullable=True),
    Column("pacing_table", JSONB(none_as_null=True), nullable=True),
    Column("max_unprompted_skip_minutes", Integer, nullable=True),
    Column("stop_at_due_events", Boolean, nullable=True),
    Column("grace_minutes", Integer, nullable=True),
    Column("time_statement_tolerance_minutes", Integer, nullable=True),
    Column("elapsed_tolerance_factor", Numeric, nullable=True),
    Column("time_awareness", pg_enum(TimeAwareness, "time_awareness"), nullable=True),
    Column("bands", JSONB(none_as_null=True), nullable=True),
    Column("calendar", Text, nullable=True),
    *derived_columns(),
    UniqueConstraint("story_id", "name"),
    CheckConstraint("length(name) BETWEEN 1 AND 80", name="name_length"),
    CheckConstraint("owner <> 'external' OR provider_id IS NOT NULL", name="external_provider"),
    CheckConstraint(
        "reservation_expiry_minutes IS NULL OR reservation_expiry_minutes > 0",
        name="reservation_expiry",
    ),
    CheckConstraint(
        "jsonb_typeof(constraints) = 'object' AND jsonb_typeof(rules) = 'object' "
        "AND jsonb_typeof(\"authorization\") = 'object' AND jsonb_typeof(transfer) = 'object'",
        name="documents",
    ),
    CheckConstraint(CLOCK_SETTINGS_CHECK, name="clock_settings"),
    *derived_indexes("state_definitions"),
)

STATE_DEFINITIONS = register_derived(DerivedTable(state_definitions))
"""``state_definitions`` in ``core.membership.DERIVED_TABLES``: a definition follows the
operation that defined it (a truncation past a slot's definition discards it)."""

state_changes = Table(
    "state_changes",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("state_id", ulid_type(), nullable=False),
    Column("scope_key", ulid_type(), nullable=True),
    Column("kind", pg_enum(StateChangeKind, "state_change_kind"), nullable=False),
    Column("value", JSONB(none_as_null=True), nullable=True),
    Column("delta", Numeric, nullable=True),
    Column("old_value", JSONB(none_as_null=True), nullable=True),
    Column("new_value", JSONB(none_as_null=True), nullable=True),
    Column("reason", Text, nullable=False),
    Column("source", pg_enum(StateChangeSource, "state_change_source"), nullable=False),
    Column(
        "contingency",
        pg_enum(StateContingency, "state_contingency"),
        nullable=False,
        server_default=text("'unconditional'"),
    ),
    Column(
        "status",
        pg_enum(StateChangeStatus, "state_change_status"),
        nullable=False,
        server_default=text("'committed'"),
    ),
    Column(
        "prerequisite_effect_ids", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")
    ),
    *derived_columns(),
    Column("slot_k", SmallInteger, nullable=False, server_default=text("0")),
    Column("beat", SmallInteger, nullable=False, server_default=text("0")),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    Column("valid_time_minutes", BigInteger, nullable=True),
    Column("tx_from", TIMESTAMP(timezone=True), nullable=False),
    Column("estimate_meta", JSONB(none_as_null=True), nullable=True),
    CheckConstraint("narrative_turn IS NOT NULL AND turn_variant IS NOT NULL", name="positioned"),
    CheckConstraint(
        "(kind = 'set') = (value IS NOT NULL) AND (kind = 'set') = (delta IS NULL)",
        name="kind_shape",
    ),
    CheckConstraint("length(reason) <= 200", name="reason_length"),
    *derived_indexes("state_changes"),
)
Index(
    "ix_state_changes_story_state_scope",
    state_changes.c.story_id,
    state_changes.c.state_id,
    state_changes.c.scope_key,
)

STATE_CHANGES = register_derived(
    DerivedTable(state_changes, valid_time=("valid_time_minutes", "valid_time_minutes"))
)
"""``state_changes`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation, swap,
regenerate and fork move a change with its operation, so a State's value in any view is the fold
of its active changes there (FR-STATE-4)."""

state_values = Table(
    "state_values",
    metadata,
    Column("story_id", ulid_type(), nullable=False),
    Column("state_id", ulid_type(), nullable=False),
    Column("scope_key", Text, nullable=False, server_default=text("''")),
    Column("value", JSONB(none_as_null=True), nullable=True),
    *position_group("since", nullable=False),
    Column("change_count", Integer, nullable=False),
    PrimaryKeyConstraint("story_id", "state_id", "scope_key"),
    CheckConstraint("change_count > 0", name="change_count"),
)

__all__ = [
    "CLOCK_SETTINGS_CHECK",
    "ERASURE_STATE_CHANGE_COLUMNS",
    "MUTABLE_STATE_CHANGE_COLUMNS",
    "STATE_CHANGES",
    "STATE_DEFINITIONS",
    "STORY_SCOPE_KEY",
    "state_changes",
    "state_definitions",
    "state_values",
]
