"""escalation yield and shadow tightening: ``escalations``, ``gate_thresholds`` and
``gate_threshold_history`` (#155)

FR-OBS-2,4, NFR-PERF-5 (docs/02 §4.9): ``core.tables.escalations`` describes them.

- ``escalations`` is created: an id, the story, the derived-record columns (the turn's
  operation), the claim id and verification pass, the gate reason and predicate-shape key, the
  deterministic and model confidences, the gate's threshold, whether the model was called (or
  reused, or skipped by a tightened gate), the tentative and model verdicts, ``flipped``, the
  final kind and disposition, the consequence (CHECK), the shadow threshold, decision and
  agreement, and ``at``; with the two derived indexes, ``at`` and ``(predicate_shape, at)``.
- ``gate_thresholds`` is created: one row per shape (its key is the primary key), the proposed
  threshold (CHECK 0 to 1), the mode (CHECK ``shadow``/``live``), the live threshold, when it
  was proposed and promoted, and the shadow window's start.
- ``gate_threshold_history`` is created: every write's action (CHECK ``put``, ``promote``,
  ``delete``), values, principal and time, indexed by ``(shape, at)``.

The three new deployment settings (``gate_promotion_min_cases``,
``gate_promotion_min_agreement``, ``escalation_retention_days``) are ``deployment_state.settings``
keys read with their defaults, so nothing is stored for them.

Downgrade drops the three tables.

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-19

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def _derived() -> list[sa.Column[Any]]:
    return [
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column(
            "source_event_ids",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("narrative_turn", sa.Integer(), nullable=True),
        sa.Column("turn_variant", sa.SmallInteger(), nullable=True),
        sa.Column(
            "membership",
            postgresql.ENUM(name="membership", create_type=False),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "escalations",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        *_derived(),
        sa.Column("claim_id", sa.Text(), nullable=True),
        sa.Column("pass", sa.Text(), server_default=sa.text("'render'"), nullable=False),
        sa.Column("gate_reason", sa.Text(), nullable=False),
        sa.Column("predicate_shape", sa.Text(), nullable=False),
        sa.Column("deterministic_confidence", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("called", sa.Boolean(), nullable=False),
        sa.Column("reused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("skipped_by_gate", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "tentative_verdict",
            sa.Text(),
            server_default=sa.text("'uncertain'"),
            nullable=False,
        ),
        sa.Column("model_verdict", sa.Text(), nullable=True),
        sa.Column("model_confidence", sa.Float(), nullable=True),
        sa.Column("model_uncertain", sa.Text(), nullable=True),
        sa.Column("flipped", sa.Boolean(), nullable=True),
        sa.Column("final_kind", sa.Text(), nullable=False),
        sa.Column("final_disposition", sa.Text(), nullable=True),
        sa.Column("consequence", sa.Text(), server_default=sa.text("'none'"), nullable=False),
        sa.Column("shadow_threshold", sa.Float(), nullable=True),
        sa.Column("shadow_decision", sa.Boolean(), nullable=True),
        sa.Column("shadow_agreed", sa.Boolean(), nullable=True),
        sa.Column(
            "at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "consequence IN ('none', 'repair_kept', 'repair_discarded')", name="consequence"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_escalations_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_escalations"),
    )
    op.create_index("ix_escalations_story_operation", "escalations", ["story_id", "operation_id"])
    op.create_index("ix_escalations_story_membership", "escalations", ["story_id", "membership"])
    op.create_index("ix_escalations_at", "escalations", ["at"])
    op.create_index("ix_escalations_shape_at", "escalations", ["predicate_shape", "at"])
    op.create_table(
        "gate_thresholds",
        sa.Column("shape", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("live_threshold", sa.Float(), nullable=True),
        sa.Column("proposed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("promoted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('shadow', 'live')", name="mode"),
        sa.CheckConstraint("threshold >= 0 AND threshold <= 1", name="threshold"),
        sa.PrimaryKeyConstraint("shape", name="pk_gate_thresholds"),
    )
    op.create_table(
        "gate_threshold_history",
        sa.Column("id", ULID, nullable=False),
        sa.Column("shape", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=True),
        sa.Column("live_threshold", sa.Float(), nullable=True),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("action IN ('put', 'promote', 'delete')", name="action"),
        sa.PrimaryKeyConstraint("id", name="pk_gate_threshold_history"),
    )
    op.create_index("ix_gate_threshold_history_shape_at", "gate_threshold_history", ["shape", "at"])


def downgrade() -> None:
    op.drop_table("gate_threshold_history")
    op.drop_table("gate_thresholds")
    op.drop_table("escalations")
