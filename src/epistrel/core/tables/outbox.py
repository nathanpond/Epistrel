"""``external_effect_outbox``, ``outbox_outcomes`` and ``reconciliation_marks``: the
transactional outbox of write-capable providers (FR-EXT-10,12,16..18; docs/02 §4.8; #210,
migration 0095; #211, migration 0098).

- **``external_effect_outbox``** — one row per effect a turn asks a write-capable provider to
  apply, written ``intended`` in the turn's own transaction (``core.outbox.write_intended``). Its
  ``id`` is the provider idempotency key, stable across re-sends (FR-EXT-18). It names the
  ``provider_name``, the provider's ``value_name``, the ``scope_key`` of a character-scoped
  value and the ``effect`` (``{kind, state, delta|value, reason}``: the render's reason is story
  text, indexed for erasure by position). ``status`` moves ``intended → claimed → confirmed |
  failed | unknown``, ``unknown → confirmed | failed``, ``intended → cancelled``;
  ``imported_unsettled`` is a bundle import's row without a terminal outcome (M7). The claim
  sets ``claimed_at`` and ``claim_lease_until`` (the lease a timer watches), each send moves
  ``attempts`` and sets ``sent_at``. ``outcome_event_id`` is the outcome event a settlement
  writes (#217; null until then). ``executable`` is false on a fork's copies (FR-EXT-13). The
  derived-record columns (``core.membership``) and ``slot_k``/``beat``/``step`` (FR-STORE-31)
  place the row at its declaring beat: it follows its turn's membership like any derived
  record, and the dispatcher re-reads the operation's membership when it claims it
  (FR-EXT-12). ``created_at`` is when the turn committed it.
- **``outbox_outcomes``** — what a provider answered for a row, append-only (#217: or what the
  consumer reported for a consumer-applied #209 effect, which has no outbox row: exactly one of
  ``outbox_id`` and ``effect_id`` is set, migration 0102): ``kind``
  (``confirmed``, ``failed``, ``unknown``, and ``duplicate``/``conflict`` for a later terminal
  answer, FR-EXT-22), the answer's ``payload`` (its resulting values, or why it is unknown;
  never the raw body), its ``raw_hash``, ``received_via`` (``dispatcher`` or the consumer's
  ``ingest``, #217) and when, and whether a settlement ``settled`` it (#217, the one column an
  update may change). A trigger refuses every other update and delete outside erasure and
  story deletion, and any TRUNCATE (0068's pattern). #218 (migration 0103): at most one settled
  terminal outcome (``confirmed`` or ``failed``) per row, and per consumer-applied effect, within
  a story (two partial unique indexes): a later answer is ``duplicate`` or ``conflict``
  (FR-EXT-22).

- **``reconciliation_marks``** (#211, migration 0098) — why a row is listed when its status
  alone does not say: ``reconciliation_required`` (a claimed, unknown or confirmed row whose
  turn left the active timeline), ``cancelled_on_reactivation`` (a row cancelled while its turn
  was out, whose turn is active again), and the later stories' reasons (#216, #217, #218).
  ``since`` is when it was marked and ``resolved_by`` the explicit action that resolved it
  (#218; null while open). One open mark per row and reason (a partial unique index).
  ``detail`` (#218, migration 0103) carries a ``late_success_after_expiry`` mark's
  re-verification (``{reverification, reasons}``, codes only). ``resolved_at`` and
  ``resolved_operation_id`` (#777, migration 0144) say when and by which operation the action
  resolved it (null while open; the operation only for #661's resolve action). Ids, reason
  codes and times only, no story content; a fork does not copy it (the fork's outbox copies
  are records, never listed).

``external_effect_outbox.compensates`` (#218, migration 0103) names the row a compensating effect
(a refund the author chose, FR-EXT-22) answers; null on every other row.

``external_effect_outbox.erased_at`` (#211) is set when an erasure redacted a claimed or
``unknown`` row (listed as ``{outbox_id, provider, status}`` only), or abandoned its dispatch
mid-flight: such a row is never sent again.

None of the tables has a foreign key to another story table (``outbox_id`` is a plain column; the
outbox's ``operation_id`` is the derived columns' reference to ``operations``); in
``core.stories.STORY_TABLES`` the outcomes come before the outbox, both before ``operations``.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, ulid_type

INTENDED = "intended"
CLAIMED = "claimed"
CONFIRMED = "confirmed"
FAILED = "failed"
UNKNOWN = "unknown"
CANCELLED = "cancelled"
IMPORTED_UNSETTLED = "imported_unsettled"
OUTBOX_STATUSES: tuple[str, ...] = (
    INTENDED,
    CLAIMED,
    CONFIRMED,
    FAILED,
    UNKNOWN,
    CANCELLED,
    IMPORTED_UNSETTLED,
)
"""An outbox row's statuses (docs/02 §4.8's row states)."""

DUPLICATE = "duplicate"
CONFLICT = "conflict"
OUTCOME_KINDS: tuple[str, ...] = (CONFIRMED, FAILED, UNKNOWN, DUPLICATE, CONFLICT)
"""An outcome record's kinds."""

VIA_DISPATCHER = "dispatcher"
VIA_INGEST = "ingest"
RECEIVED_VIA: tuple[str, ...] = (VIA_DISPATCHER, VIA_INGEST)


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


external_effect_outbox = Table(
    "external_effect_outbox",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("provider_name", Text, nullable=False),
    Column("value_name", Text, nullable=False),
    Column("scope_key", Text, nullable=True),
    Column("effect", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default=text(f"'{INTENDED}'")),
    Column("claimed_at", TIMESTAMP(timezone=True), nullable=True),
    Column("claim_lease_until", TIMESTAMP(timezone=True), nullable=True),
    Column("sent_at", TIMESTAMP(timezone=True), nullable=True),
    Column("outcome_event_id", ulid_type(), nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("executable", Boolean, nullable=False, server_default=text("true")),
    Column("erased_at", TIMESTAMP(timezone=True), nullable=True),
    Column("compensates", ulid_type(), nullable=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    *derived_columns(),
    Column("slot_k", SmallInteger, nullable=False, server_default=text("0")),
    Column("beat", SmallInteger, nullable=False, server_default=text("0")),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    CheckConstraint(_in("status", OUTBOX_STATUSES), name="status"),
    CheckConstraint("jsonb_typeof(effect) = 'object'", name="effect"),
    CheckConstraint("attempts >= 0", name="attempts"),
    *derived_indexes("external_effect_outbox"),
)
Index(
    "ix_external_effect_outbox_status",
    external_effect_outbox.c.status,
    external_effect_outbox.c.story_id,
)
Index("ix_external_effect_outbox_compensates", external_effect_outbox.c.compensates)

outbox_outcomes = Table(
    "outbox_outcomes",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("outbox_id", ulid_type(), nullable=True),
    Column("effect_id", ulid_type(), nullable=True),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("raw_hash", Text, nullable=False, server_default=text("''")),
    Column("received_via", Text, nullable=False),
    Column("received_at", TIMESTAMP(timezone=True), nullable=False),
    Column("settled", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint(_in("kind", OUTCOME_KINDS), name="kind"),
    CheckConstraint(_in("received_via", RECEIVED_VIA), name="received_via"),
    CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload"),
    CheckConstraint("(outbox_id IS NULL) <> (effect_id IS NULL)", name="one_effect"),
)
Index("ix_outbox_outcomes_outbox", outbox_outcomes.c.outbox_id, outbox_outcomes.c.received_at)
Index("ix_outbox_outcomes_effect", outbox_outcomes.c.effect_id, outbox_outcomes.c.received_at)
Index("ix_outbox_outcomes_story", outbox_outcomes.c.story_id)
_SETTLED_KIND = outbox_outcomes.c.kind.in_((CONFIRMED, FAILED))
Index(
    "uq_outbox_outcomes_terminal_outbox",
    outbox_outcomes.c.story_id,
    outbox_outcomes.c.outbox_id,
    unique=True,
    postgresql_where=_SETTLED_KIND & outbox_outcomes.c.outbox_id.is_not(None),
)
Index(
    "uq_outbox_outcomes_terminal_effect",
    outbox_outcomes.c.story_id,
    outbox_outcomes.c.effect_id,
    unique=True,
    postgresql_where=_SETTLED_KIND & outbox_outcomes.c.effect_id.is_not(None),
)

REASONS: tuple[str, ...] = (
    "unconfirmed",
    "failed",
    "unknown",
    "reconciliation_required",
    "cancelled_on_reactivation",
    "confirmed_unsettled",
    "confirmed_discarded_origin",
    "settlement_conflict",
    "late_success_after_expiry",
    "outcome_conflict",
    "reservation_expired",
    "imported_unsettled",
)
"""Every reconciliation reason (``core.outbox.REASONS``; a mark's reason is one of them)."""

reconciliation_marks = Table(
    "reconciliation_marks",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    # #661 (migration 0105): a mark names an outbox row or a consumer-applied effect's request.
    Column("outbox_id", ulid_type(), nullable=True),
    Column("request_id", ulid_type(), nullable=True),
    Column("reason", Text, nullable=False),
    Column(
        "since",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("resolved_by", Text, nullable=True),
    Column("detail", JSONB, nullable=True),
    # #777 (migration 0144): when, and by which operation, an explicit action resolved it.
    Column("resolved_at", TIMESTAMP(timezone=True), nullable=True),
    Column("resolved_operation_id", ulid_type(), nullable=True),
    CheckConstraint(_in("reason", REASONS), name="reason"),
    CheckConstraint("(outbox_id IS NULL) <> (request_id IS NULL)", name="one_effect"),
)
Index(
    "ix_reconciliation_marks_story_request",
    reconciliation_marks.c.story_id,
    reconciliation_marks.c.request_id,
)
Index(
    "uq_reconciliation_marks_open_request",
    reconciliation_marks.c.request_id,
    reconciliation_marks.c.reason,
    unique=True,
    postgresql_where=reconciliation_marks.c.resolved_by.is_(None)
    & reconciliation_marks.c.request_id.is_not(None),
)
Index(
    "ix_reconciliation_marks_story",
    reconciliation_marks.c.story_id,
    reconciliation_marks.c.outbox_id,
)
Index(
    "uq_reconciliation_marks_open",
    reconciliation_marks.c.outbox_id,
    reconciliation_marks.c.reason,
    unique=True,
    postgresql_where=reconciliation_marks.c.resolved_by.is_(None),
)

EXTERNAL_EFFECT_OUTBOX = register_derived(DerivedTable(external_effect_outbox))
"""``external_effect_outbox`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation,
swap, regenerate and fork move each row with the turn that wrote it."""

__all__ = [
    "CANCELLED",
    "CLAIMED",
    "CONFIRMED",
    "CONFLICT",
    "DUPLICATE",
    "EXTERNAL_EFFECT_OUTBOX",
    "FAILED",
    "IMPORTED_UNSETTLED",
    "INTENDED",
    "OUTBOX_STATUSES",
    "OUTCOME_KINDS",
    "REASONS",
    "RECEIVED_VIA",
    "UNKNOWN",
    "VIA_DISPATCHER",
    "VIA_INGEST",
    "external_effect_outbox",
    "outbox_outcomes",
    "reconciliation_marks",
]
