"""the structured turn: ``utterances``, ``characters.true_age_source`` and the render dials (#150)

FR-TURN-1,4,5,6, FR-CAT-12 (docs/02 §4.4): ``orchestrator.structured.pipeline`` writes them;
``core.tables.utterances`` describes the new table.

- ``utterances`` is created: one row per ``say`` beat of a structured turn (a character's, or
  the player's own ``say``): an id, the story, the beat's event (a soft reference, no foreign
  key: an event is never deleted while its story lives), the speaker, the proposition ids the
  utterance asserts and its three labels (``objective_relation`` ∈ ``true|false|unresolved``,
  ``belief_relation`` ∈ ``consistent|inconsistent|unknown``, ``intent`` ∈
  ``sincere|deceptive|joking|unspecified``, CHECKs), with the derived-record columns of
  ``core.membership`` and their two indexes, plus ``(story_id, event_id)`` and
  ``(story_id, speaker_character_id)``. Append-only by trigger: an UPDATE may change only
  ``belief_relation`` (M5's partitions) and ``membership``/``membership_tx``; a DELETE only
  under erasure (``epistrel.erasure = on``) or the story's deletion
  (``epistrel.allow_story_delete``); TRUNCATE is refused (``EPI01``).
- ``characters.true_age_source`` ∈ ``catalog|genesis|declared|repaired`` (NOT NULL, default
  ``declared``): where the character's one true age came from (FR-CAT-12). Existing rows
  backfill ``catalog`` (a catalog character), ``genesis`` (a scenario role's character) or
  ``declared`` (a blank story's, given with the story).
- ``deployment_state.profile_defaults`` gains ``render.max_beats`` (6),
  ``render.working_memory_beats`` (20) and ``render.prompt_token_budget`` (6,000)
  (``epistrel.core.seeds.profiles.RENDER_DEFAULTS``) where it does not name them yet. No
  ``config_changes`` row is written: the dials are the schema's own defaults and prospective.

Downgrade removes the dials, drops the column and the table.

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-17

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.profiles import RENDER_DEFAULTS

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

OBJECTIVE_RELATIONS = ("true", "false", "unresolved")
BELIEF_RELATIONS = ("consistent", "inconsistent", "unknown")
INTENTS = ("sincere", "deceptive", "joking", "unspecified")
AGE_SOURCES = ("catalog", "genesis", "declared", "repaired")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


GUARD = """
CREATE OR REPLACE FUNCTION epistrel_utterances_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['belief_relation', 'membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'utterances are append-only: update of utterance % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'utterances are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'utterances are append-only: % of utterance % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    "CREATE TRIGGER utterances_append_only BEFORE UPDATE OR DELETE ON utterances "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_utterances_append_only()",
    "CREATE TRIGGER utterances_no_truncate BEFORE TRUNCATE ON utterances "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_utterances_append_only()",
)


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
                "d": json.dumps(change(dict(row.profile_defaults), RENDER_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    op.create_table(
        "utterances",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        *_derived(),
        sa.Column("event_id", ULID, nullable=False),
        sa.Column("speaker_character_id", ULID, nullable=False),
        sa.Column(
            "proposition_ids",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("objective_relation", sa.Text(), nullable=False),
        sa.Column(
            "belief_relation", sa.Text(), server_default=sa.text("'unknown'"), nullable=False
        ),
        sa.Column("intent", sa.Text(), server_default=sa.text("'unspecified'"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _in("objective_relation", OBJECTIVE_RELATIONS), name="objective_relation"
        ),
        sa.CheckConstraint(_in("belief_relation", BELIEF_RELATIONS), name="belief_relation"),
        sa.CheckConstraint(_in("intent", INTENTS), name="intent"),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_utterances_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_utterances"),
    )
    op.create_index("ix_utterances_story_operation", "utterances", ["story_id", "operation_id"])
    op.create_index("ix_utterances_story_membership", "utterances", ["story_id", "membership"])
    op.create_index("ix_utterances_story_event", "utterances", ["story_id", "event_id"])
    op.create_index(
        "ix_utterances_story_speaker", "utterances", ["story_id", "speaker_character_id"]
    )
    op.execute(GUARD)
    for statement in TRIGGERS:
        op.execute(statement)

    op.add_column(
        "characters",
        sa.Column(
            "true_age_source", sa.Text(), server_default=sa.text("'declared'"), nullable=False
        ),
    )
    op.execute(
        "UPDATE characters SET true_age_source = CASE "
        "WHEN source_catalog_id IS NOT NULL THEN 'catalog' "
        "WHEN role_id IS NOT NULL THEN 'genesis' ELSE 'declared' END"
    )
    op.create_check_constraint(
        "ck_characters_true_age_source", "characters", _in("true_age_source", AGE_SOURCES)
    )
    _rewrite(_added)


def downgrade() -> None:
    _rewrite(_removed)
    op.drop_constraint("ck_characters_true_age_source", "characters", type_="check")
    op.drop_column("characters", "true_age_source")
    op.drop_table("utterances")
    op.execute("DROP FUNCTION epistrel_utterances_append_only()")
