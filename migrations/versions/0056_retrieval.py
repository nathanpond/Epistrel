"""retrieval: a memory's occasion, its lexical vector, and the ranking dials (#141)

FR-RET-3,4,5,7 (docs/02 §4.2): ``core.retrieval.pipeline`` reads these.

- ``memories`` gains ``event_type`` (``text``, at most 64 characters) and ``participants``
  (``ulid[]``, GIN-indexed): the occasion an episodic memory belongs to and the entities it
  involves, which the occasion step queries by ``(story_id, event_type, narrative_turn)``
  (``ix_memories_story_occasion``) and ``participants &&``. Existing rows get ``NULL`` and
  ``{}``.
- ``memories`` gains ``search``, a stored generated ``tsvector`` (``to_tsvector('english',
  text)``), GIN-indexed: the lexical and exact-alias arms read it. An erasure that rewrites
  ``text`` recomputes it.
- The guard function lets ``search`` through: PostgreSQL computes a generated column after
  the BEFORE trigger runs, so the trigger cannot compare its new value. ``text`` stays guarded,
  and ``search`` follows it.
- ``deployment_state.profile_defaults`` gains the retrieval dials
  (``epistrel.core.seeds.profiles.RETRIEVAL_DEFAULTS``: ``memory.episodic.weight_similarity``,
  ``weight_recency``, ``weight_importance``, ``recency_half_life_turns``,
  ``retrieval_token_budget``) where it does not name them yet, at the schema's defaults. No
  ``config_changes`` row is written, and ``config_revision`` does not move: every story's
  effective configuration is what it was.

Downgrade drops the columns and indexes, restores 0050's guard and removes the dials from the
defaults.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-17

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.profiles import RETRIEVAL_DEFAULTS

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_memories_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['importance', 'importance_at', 'last_accessed_at', 'retrieval_tier',
                            'embedding', 'embedding_model_version', 'membership',
                            'membership_tx'{generated}];
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
                "d": json.dumps(change(dict(row.profile_defaults), RETRIEVAL_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    op.add_column("memories", sa.Column("event_type", sa.Text(), nullable=True))
    op.add_column(
        "memories",
        sa.Column(
            "participants",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "memories",
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_memories_event_type_length",
        "memories",
        "event_type IS NULL OR char_length(event_type) BETWEEN 1 AND 64",
    )
    op.create_index(
        "ix_memories_participants", "memories", ["participants"], postgresql_using="gin"
    )
    op.create_index("ix_memories_search", "memories", ["search"], postgresql_using="gin")
    op.create_index(
        "ix_memories_story_occasion", "memories", ["story_id", "event_type", "narrative_turn"]
    )
    op.execute(GUARD.replace("{generated}", ", 'search'"))
    _rewrite(_added)


def downgrade() -> None:
    _rewrite(_removed)
    op.execute(GUARD.replace("{generated}", ""))
    op.drop_index("ix_memories_story_occasion", table_name="memories")
    op.drop_index("ix_memories_search", table_name="memories")
    op.drop_index("ix_memories_participants", table_name="memories")
    op.drop_constraint("ck_memories_event_type_length", "memories", type_="check")
    op.drop_column("memories", "search")
    op.drop_column("memories", "participants")
    op.drop_column("memories", "event_type")
