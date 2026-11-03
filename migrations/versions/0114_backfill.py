"""backfill: re-deriving a story's structured data from its transcript (#228)

FR-PROF-8,9, ADR-086: ``core.tables.backfill`` describes the tables.

- ``story_backfills``, ``backfill_chunks``, ``backfill_conflicts`` and ``capability_coverage``.
- ``memory_trust_tier`` gains ``backfill``, the lowest tier (a backfilled memory's).
- ``exposures`` gains ``inferred`` (an exposure a backfill read from the text).
- Every existing story gets one ``live`` coverage interval from turn 1, open-ended, for each
  data-producing capability its effective configuration has on (the clock and States with
  their level): its history has that capability's data from its first turn.

Downgrade drops the tables and the column; the enum value stays (Postgres cannot drop one).

Revision ID: 0114
Revises: 0113
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.ids import new_ulid
from epistrel.core.profiles import schema
from epistrel.core.profiles.effective import merged_settings

revision: str = "0114"
down_revision: str | None = "0113"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")
LEVELLED = ("clock", "states", "pipeline")


def _tables() -> None:
    op.create_table(
        "story_backfills",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("job_id", ULID, nullable=True),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'upgrade'")),
        sa.Column("source", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("target", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("capabilities", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("range_from_turn", sa.Integer, nullable=False),
        sa.Column("range_to_turn", sa.Integer, nullable=False),
        sa.Column("chunks_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("chunks_done", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("reconcile_cursor", sa.Integer, nullable=True),
        sa.Column("requested_by", sa.Text, nullable=False),
        sa.Column("report", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'extracting', 'reconciling', 'paused_for_conflicts', "
            "'completed', 'cancelled', 'failed')",
            name="ck_story_backfills_status",
        ),
        sa.CheckConstraint(
            "kind IN ('upgrade', 'gap_fill', 'resummarize')", name="ck_story_backfills_kind"
        ),
    )
    op.create_index("ix_story_backfills_story", "story_backfills", ["story_id", "created_at"])
    op.create_index(
        "uq_story_backfills_running",
        "story_backfills",
        ["story_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'extracting', 'reconciling', 'paused_for_conflicts')"
        ),
    )
    op.create_table(
        "backfill_chunks",
        sa.Column("backfill_id", ULID, sa.ForeignKey("story_backfills.id"), nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("from_turn", sa.Integer, nullable=False),
        sa.Column("to_turn", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("extraction", postgresql.JSONB, nullable=True),
        sa.Column("extracted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("backfill_id", "chunk_index"),
        sa.CheckConstraint("status IN ('pending', 'extracted')", name="ck_backfill_chunks_status"),
    )
    op.create_table(
        "backfill_conflicts",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("backfill_id", ULID, sa.ForeignKey("story_backfills.id"), nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("subject", postgresql.JSONB, nullable=False),
        sa.Column("statements", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("valid_time", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'resolved'")),
        sa.Column("resolution", postgresql.JSONB, nullable=True),
        sa.Column("resolved_by", sa.Text, nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'uncertain')", name="ck_backfill_conflicts_status"
        ),
    )
    op.create_index(
        "ix_backfill_conflicts_backfill", "backfill_conflicts", ["story_id", "backfill_id"]
    )
    op.create_table(
        "capability_coverage",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("capability", sa.Text, nullable=False),
        sa.Column("from_turn", sa.Integer, nullable=False),
        sa.Column("to_turn", sa.Integer, nullable=True),
        sa.Column("level", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("source IN ('live', 'backfill')", name="ck_capability_coverage_source"),
        sa.CheckConstraint(
            "level IS NULL OR level IN ('prompt_only', 'on', 'lite', 'structured')",
            name="ck_capability_coverage_level",
        ),
        sa.CheckConstraint(
            "to_turn IS NULL OR to_turn >= from_turn", name="ck_capability_coverage_range"
        ),
    )
    op.create_index(
        "ix_capability_coverage_story",
        "capability_coverage",
        ["story_id", "capability", "from_turn"],
    )


def _seed_coverage() -> None:
    bind = op.get_bind()
    defaults = bind.execute(sa.text("SELECT profile_defaults FROM deployment_state")).scalar()
    rows = bind.execute(
        sa.text(
            "SELECT s.id, v.settings, o.settings AS overrides FROM stories s "
            "JOIN engine_profiles p ON p.id = s.engine_profile_id "
            "JOIN engine_profile_versions v ON v.profile_id = p.id AND v.version = "
            "COALESCE(s.engine_profile_version_pin, p.current_version) "
            "LEFT JOIN story_profile_overrides o ON o.story_id = s.id"
        )
    ).all()
    values: list[dict[str, Any]] = []
    for row in rows:
        try:
            settings = merged_settings(dict(defaults or {}), dict(row.settings), row.overrides)
        except Exception:  # noqa: S112 - a story whose stored settings no longer parse
            continue
        for module, level in schema.enabled_modules(settings).items():
            if module not in schema.DATA_PRODUCING_MODULES:
                continue
            values.append(
                {
                    "id": new_ulid(),
                    "story_id": row.id,
                    "capability": module,
                    "level": level if module in LEVELLED else None,
                }
            )
    if values:
        bind.execute(
            sa.text(
                "INSERT INTO capability_coverage (id, story_id, capability, from_turn, level, "
                "source) VALUES (:id, :story_id, :capability, 1, :level, 'live')"
            ),
            values,
        )


def upgrade() -> None:
    op.execute("ALTER TYPE memory_trust_tier ADD VALUE IF NOT EXISTS 'backfill'")
    op.add_column(
        "exposures",
        sa.Column("inferred", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    _tables()
    _seed_coverage()


def downgrade() -> None:
    op.drop_table("capability_coverage")
    op.drop_table("backfill_conflicts")
    op.drop_table("backfill_chunks")
    op.drop_table("story_backfills")
    op.drop_column("exposures", "inferred")
