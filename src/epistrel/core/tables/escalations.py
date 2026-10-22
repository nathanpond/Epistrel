"""``escalations``, ``gate_thresholds`` and ``gate_threshold_history``: escalation yield and
shadow evaluation of a gate tightening (FR-OBS-2,4, NFR-PERF-5; #155, migration 0065).

**``escalations``** holds one row per uncertain residual that reached #142's escalation gate
with a reason that may escalate, in a committed structured turn: every entailment call (the
gate said ``escalate``) and every claim a tightened gate skipped (``skipped_by_gate``). It is a
derived table of the turn's operation (``core.membership``), so a truncated turn's rows leave
the timeline with it (#130), and it holds no text: ids, the claim's ``gate_reason`` and
``predicate_shape`` key (``layer|cardinality|temporal|polarity``), the deterministic and model
confidences, the gate's threshold, the tentative verdict (the literal ``uncertain``), the
model's verdict (``None`` when no valid answer came back), ``flipped`` (whether the model's
answer was taken: ``None`` with no valid answer), how the claim ended (``final_kind``,
``final_disposition``), the downstream ``consequence`` (``none``, ``repair_kept``,
``repair_discarded``), which verification pass wrote it (``render``, ``repair_<rung>``,
``composed``), and the shadow columns: the proposed threshold in force for the shape, whether
the tightened gate would still have escalated (``shadow_decision``) and, when it would have
skipped the call, whether skipping agreed with the model (``shadow_agreed``).

**``gate_thresholds``** is one row per tightened shape: the proposed ``threshold`` and its
``mode`` (``shadow``: under evaluation, the live gate unchanged; ``live``: in force), the
``live_threshold`` #142's gate reads (``None`` until a promotion or a ``live`` write; a new
proposal for a live shape keeps it), when the proposal and the promotion were made and the
shadow window's start (a new proposal resets it). **``gate_threshold_history``** records every
write (``put``, ``promote``, ``delete``) with its values and principal. Neither has a
``story_id``: they are deployment configuration.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    Index,
    Table,
    Text,
    text,
)

from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, ulid_type

CONSEQUENCES: tuple[str, ...] = ("none", "repair_kept", "repair_discarded")
GATE_MODES: tuple[str, ...] = ("shadow", "live")
HISTORY_ACTIONS: tuple[str, ...] = ("put", "promote", "delete")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


escalations = Table(
    "escalations",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    *derived_columns(),
    Column("claim_id", Text, nullable=True),
    Column("pass", Text, nullable=False, server_default=text("'render'")),
    Column("gate_reason", Text, nullable=False),
    Column("predicate_shape", Text, nullable=False),
    Column("deterministic_confidence", Float, nullable=False),
    Column("threshold", Float, nullable=True),
    Column("called", Boolean, nullable=False),
    Column("reused", Boolean, nullable=False, server_default=text("false")),
    Column("skipped_by_gate", Boolean, nullable=False, server_default=text("false")),
    Column("tentative_verdict", Text, nullable=False, server_default=text("'uncertain'")),
    Column("model_verdict", Text, nullable=True),
    Column("model_confidence", Float, nullable=True),
    Column("model_uncertain", Text, nullable=True),
    Column("flipped", Boolean, nullable=True),
    Column("final_kind", Text, nullable=False),
    Column("final_disposition", Text, nullable=True),
    Column("consequence", Text, nullable=False, server_default=text("'none'")),
    Column("shadow_threshold", Float, nullable=True),
    Column("shadow_decision", Boolean, nullable=True),
    Column("shadow_agreed", Boolean, nullable=True),
    Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(f"consequence IN ({_in(CONSEQUENCES)})", name="consequence"),
    *derived_indexes("escalations"),
)
Index("ix_escalations_at", escalations.c.at)
Index("ix_escalations_shape_at", escalations.c.predicate_shape, escalations.c.at)

ESCALATIONS = register_derived(DerivedTable(escalations))
"""``escalations`` in ``core.membership.DERIVED_TABLES``: a row follows its turn."""

gate_thresholds = Table(
    "gate_thresholds",
    metadata,
    Column("shape", Text, primary_key=True),
    Column("threshold", Float, nullable=False),
    Column("mode", Text, nullable=False),
    Column("live_threshold", Float, nullable=True),
    Column("proposed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("promoted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("window_start", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(f"mode IN ({_in(GATE_MODES)})", name="mode"),
    CheckConstraint("threshold >= 0 AND threshold <= 1", name="threshold"),
)

gate_threshold_history = Table(
    "gate_threshold_history",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("shape", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("threshold", Float, nullable=True),
    Column("mode", Text, nullable=True),
    Column("live_threshold", Float, nullable=True),
    Column("window_start", TIMESTAMP(timezone=True), nullable=True),
    Column("actor", Text, nullable=False),
    Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(f"action IN ({_in(HISTORY_ACTIONS)})", name="action"),
)
Index(
    "ix_gate_threshold_history_shape_at",
    gate_threshold_history.c.shape,
    gate_threshold_history.c.at,
)

__all__ = [
    "CONSEQUENCES",
    "ESCALATIONS",
    "GATE_MODES",
    "HISTORY_ACTIONS",
    "escalations",
    "gate_threshold_history",
    "gate_thresholds",
]
