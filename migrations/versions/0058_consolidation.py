"""consolidation: what a semantic memory or reflection was made from, and the window dial (#143)

FR-MEM-2,3,6,9,10 and FR-STORE-13,27 (docs/02 §4.2): ``orchestrator.jobs.handlers.consolidation``
writes these through ``core.memory.records.write_consolidated``.

- ``memories`` gains, for a consolidation output: ``level`` (1 or 2), ``modality``
  (``objective``, ``attributed_claim`` or ``observation``), ``inputs_hash`` (the replay key),
  ``model``, ``model_version`` and ``prompt_hash`` (FR-MEM-10's provenance), each ``NULL`` on
  existing rows; and, on every row, ``covered_by`` (``ulid[]``, ``{}``): the outputs that cover
  it. ``ix_memories_story_kind_turn`` ``(story_id, kind, narrative_turn)`` serves the
  scheduler's count of a story's uncovered episodic memories after a turn.
- The guard function lets ``covered_by`` change (consolidation rewrites it on its inputs);
  every other column stays as 0056 left it.
- ``deployment_state.profile_defaults`` gains ``memory.consolidation.window_memories`` (50,
  ``epistrel.core.seeds.profiles.CONSOLIDATION_DEFAULTS``) where it does not name it yet. No
  ``config_changes`` row is written and ``config_revision`` does not move: the dial is the
  schema's own default and prospective.

Downgrade removes the dial, restores 0056's guard and drops the index, checks and columns.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-17

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.profiles import CONSOLIDATION_DEFAULTS

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_memories_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['importance', 'importance_at', 'last_accessed_at', 'retrieval_tier',
                            'embedding', 'embedding_model_version', 'membership',
                            'membership_tx', 'search'{covered}];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        IF current_setting('epistrel.erasure', true) = 'on'
           AND (to_jsonb(NEW) - mutable - 'text') = (to_jsonb(OLD) - mutable - 'text') THEN
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

MODALITIES = ("objective", "attributed_claim", "observation")


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
                "d": json.dumps(change(dict(row.profile_defaults), CONSOLIDATION_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column(
            "covered_by", postgresql.ARRAY(ULID), server_default=sa.text("'{}'"), nullable=False
        ),
    )
    op.add_column("memories", sa.Column("level", sa.SmallInteger(), nullable=True))
    op.add_column("memories", sa.Column("modality", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("inputs_hash", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("model", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("model_version", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("prompt_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_memories_modality",
        "memories",
        "modality IS NULL OR modality IN (" + ", ".join(f"'{m}'" for m in MODALITIES) + ")",
    )
    op.create_check_constraint("ck_memories_level", "memories", "level IS NULL OR level IN (1, 2)")
    op.create_index(
        "ix_memories_story_kind_turn", "memories", ["story_id", "kind", "narrative_turn"]
    )
    op.execute(GUARD.replace("{covered}", ", 'covered_by'"))
    _rewrite(_added)


def downgrade() -> None:
    _rewrite(_removed)
    op.execute(GUARD.replace("{covered}", ""))
    op.drop_index("ix_memories_story_kind_turn", table_name="memories")
    op.drop_constraint("ck_memories_level", "memories", type_="check")
    op.drop_constraint("ck_memories_modality", "memories", type_="check")
    for column in (
        "prompt_hash",
        "model_version",
        "model",
        "inputs_hash",
        "modality",
        "level",
        "covered_by",
    ):
        op.drop_column("memories", column)
