"""the hot set: retirement, overflow episodes, pins and their counters (#147)

FR-MEM-8, NFR-SCALE-2, FR-STORE-30 (docs/02 §4.2): ``core.memory.retirement`` and
``core.memory.pins`` write these; ``core.tables.hot_set`` describes the new table.

- ``memories`` gains ``pinned`` (``false``) and ``pinned_by`` (an author pin, never retired: a
  CHECK keeps a pinned row ``hot``), and ``retired_reason`` (``covered``, ``floor`` or
  ``uncovered``, CHECK), ``retired_at`` and ``retired_turn`` (set when a row is retired). Three
  partial indexes: ``ix_memories_hot_unpinned`` and ``ix_memories_hot_vectors`` (the counted
  rows, so an exact recount reads only the hot set's index entries) and ``ix_memories_pinned``.
  The guard function lets the five columns change.
- ``stories`` gains ``hot_counts`` (``NULL``: not computed yet; every existing story's counters
  are computed the first time they are needed).
- ``overflow_episodes`` is created with its checks, the unique partial index that keeps one
  open episode per set, and a ``(story_id, opened_turn)`` index.
- ``deployment_state.profile_defaults`` gains ``memory.hot_set.importance_floor`` (0.2),
  ``retirement_max_lag`` (50), ``retain_retired_vectors`` and ``retain_dormant_vectors``
  (``false``; ``epistrel.core.seeds.profiles.HOT_SET_DEFAULTS``) where it does not name them
  yet. No ``config_changes`` row is written and ``config_revision`` does not move: the dials are
  the schema's own defaults and prospective. The pin ceilings (``max_pinned_per_character``,
  ``max_pinned_per_story``) are ``deployment_state.settings`` keys read with their defaults, so
  nothing is stored for them.

Downgrade removes the dials, drops the table, restores 0059's guard and drops the columns.

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-17

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.profiles import HOT_SET_DEFAULTS

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
RETIRED_REASONS = ("covered", "floor", "uncovered")
HOT_UNPINNED_WHERE = "retrieval_tier = 'hot' AND NOT pinned AND membership = 'active'"
HOT_VECTORS_WHERE = f"{HOT_UNPINNED_WHERE} AND embedding IS NOT NULL"

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_memories_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['importance', 'importance_at', 'last_accessed_at', 'retrieval_tier',
                            'embedding', 'embedding_model_version', 'membership',
                            'membership_tx', 'search', 'covered_by', 'quarantined_at',
                            'quarantine_reason', 'quarantined_by', 'implication_count'{hot_set}];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        IF current_setting('epistrel.erasure', true) = 'on'
           AND (to_jsonb(NEW) - mutable - 'text' - 'quarantine_reason')
               = (to_jsonb(OLD) - mutable - 'text' - 'quarantine_reason') THEN
            RETURN NEW;
        END IF;
        IF current_setting('epistrel.sanitize', true) = OLD.id::text
           AND (to_jsonb(NEW) - mutable - 'text' - 'inputs_hash')
               = (to_jsonb(OLD) - mutable - 'text' - 'inputs_hash') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'memories: update of memory % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'memories: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'memories: % of memory % refused', TG_OP, OLD.id USING ERRCODE = 'EPI01';
END
$$;
"""

HOT_SET_COLUMNS = ", 'pinned', 'pinned_by', 'retired_reason', 'retired_at', 'retired_turn'"


def guard(*, current: bool) -> str:
    """0061's guard (``current``) or 0059's."""
    return GUARD.replace("{hot_set}", HOT_SET_COLUMNS if current else "")


def _added(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    """``stored`` with every leaf of ``seed`` it does not name added."""
    out = dict(stored)
    for key, value in seed.items():
        if isinstance(value, Mapping):
            inner = out.get(key)
            out[key] = _added(inner if isinstance(inner, Mapping) else {}, value)
        elif key not in out:
            out[key] = value
    return out


def _removed(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    """``stored`` without the leaves ``seed`` names (and the groups they leave empty)."""
    out = dict(stored)
    for key, value in seed.items():
        if key not in out:
            continue
        if isinstance(value, Mapping) and isinstance(out[key], Mapping):
            inner = _removed(out[key], value)
            if inner:
                out[key] = inner
            else:
                del out[key]
        elif not isinstance(value, Mapping):
            del out[key]
    return out


def _rewrite(change: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]) -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id, profile_defaults FROM deployment_state")).all():
        bind.execute(
            sa.text(
                "UPDATE deployment_state SET profile_defaults = CAST(:d AS jsonb) WHERE id = :i"
            ),
            {
                "d": json.dumps(change(dict(row.profile_defaults), HOT_SET_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("memories", sa.Column("pinned_by", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("retired_reason", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("retired_turn", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_memories_pinned_hot", "memories", "NOT pinned OR retrieval_tier = 'hot'"
    )
    op.create_check_constraint(
        "ck_memories_retired_reason",
        "memories",
        "retired_reason IS NULL OR retired_reason IN ("
        + ", ".join(f"'{r}'" for r in RETIRED_REASONS)
        + ")",
    )
    op.create_index(
        "ix_memories_hot_unpinned",
        "memories",
        ["story_id", "holder_character_id"],
        postgresql_where=sa.text(HOT_UNPINNED_WHERE),
    )
    op.create_index(
        "ix_memories_hot_vectors",
        "memories",
        ["story_id"],
        postgresql_where=sa.text(HOT_VECTORS_WHERE),
    )
    op.create_index(
        "ix_memories_pinned",
        "memories",
        ["story_id", "holder_character_id"],
        postgresql_where=sa.text("pinned"),
    )
    op.execute(guard(current=True))

    op.add_column("stories", sa.Column("hot_counts", postgresql.JSONB(), nullable=True))

    op.create_table(
        "overflow_episodes",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", ULID, nullable=False),
        sa.Column("opened_turn", sa.Integer(), nullable=False),
        sa.Column("deadline_turn", sa.Integer(), nullable=False),
        sa.Column("closed_turn", sa.Integer(), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("excess_at_open", sa.Integer(), nullable=False),
        sa.Column("excess", sa.Integer(), nullable=False),
        sa.Column(
            "opened_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('character', 'story')", name="ck_overflow_episodes_scope"),
        sa.CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('within_budget', 'forced')",
            name="ck_overflow_episodes_closed_reason",
        ),
        sa.CheckConstraint(
            "(closed_turn IS NULL) = (closed_reason IS NULL) "
            "AND (closed_turn IS NULL) = (closed_at IS NULL)",
            name="ck_overflow_episodes_closed",
        ),
        sa.CheckConstraint("deadline_turn >= opened_turn", name="ck_overflow_episodes_deadline"),
        sa.CheckConstraint(
            "excess_at_open > 0 AND excess >= 0", name="ck_overflow_episodes_excess"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_overflow_episodes"),
    )
    op.create_index(
        "ux_overflow_episodes_open",
        "overflow_episodes",
        ["story_id", "scope", "scope_id"],
        unique=True,
        postgresql_where=sa.text("closed_turn IS NULL"),
    )
    op.create_index("ix_overflow_episodes_story", "overflow_episodes", ["story_id", "opened_turn"])
    _rewrite(_added)


def downgrade() -> None:
    _rewrite(_removed)
    op.drop_table("overflow_episodes")
    op.drop_column("stories", "hot_counts")
    op.execute(guard(current=False))
    for index in ("ix_memories_pinned", "ix_memories_hot_vectors", "ix_memories_hot_unpinned"):
        op.drop_index(index, table_name="memories")
    op.drop_constraint("ck_memories_retired_reason", "memories", type_="check")
    op.drop_constraint("ck_memories_pinned_hot", "memories", type_="check")
    for column in ("retired_turn", "retired_at", "retired_reason", "pinned_by", "pinned"):
        op.drop_column("memories", column)
