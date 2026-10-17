"""mutation primitives: quarantine, sanitize and the implication counter (#145)

FR-GUARD-4,5 (docs/02 §4.2): ``core.mutation`` writes these.

- ``memories`` gains ``quarantined_at`` (``NULL``: not quarantined), ``quarantine_reason``
  (free text of at most 500 characters; ``repeated_implication`` is the system's own),
  ``quarantined_by`` (the principal, ``system`` for an auto-quarantine) and
  ``implication_count`` (``0``: how many rejected renders implicated the memory, FR-GUARD-5).
  A check keeps the three quarantine columns set together.
- The guard function lets those four change. A sanitize sets ``epistrel.sanitize`` to the
  memory's id for its transaction, and the guard then lets that one row's ``text`` and
  ``inputs_hash`` change too (the rewritten text is re-embedded and a sanitized consolidation
  output is never replayed). An erasure (``epistrel.erasure = on``) may rewrite
  ``quarantine_reason`` as well as ``text``.
- ``deployment_state.profile_defaults`` gains ``repair.implication_threshold`` (2,
  ``epistrel.core.seeds.profiles.IMPLICATION_DEFAULTS``) where it does not name it yet. No
  ``config_changes`` row is written and ``config_revision`` does not move: the dial is the
  schema's own default and prospective.

Downgrade removes the dial, restores 0058's guard and drops the check and the columns.

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-17

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from epistrel.core.seeds.profiles import IMPLICATION_DEFAULTS

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASON_MAX = 500

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_memories_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['importance', 'importance_at', 'last_accessed_at', 'retrieval_tier',
                            'embedding', 'embedding_model_version', 'membership',
                            'membership_tx', 'search', 'covered_by'{quarantine}];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        IF current_setting('epistrel.erasure', true) = 'on'
           AND (to_jsonb(NEW) - mutable - 'text'{erased})
               = (to_jsonb(OLD) - mutable - 'text'{erased}) THEN
            RETURN NEW;
        END IF;{sanitize}
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

QUARANTINE_COLUMNS = (
    ", 'quarantined_at', 'quarantine_reason', 'quarantined_by', 'implication_count'"
)
SANITIZE = """
        IF current_setting('epistrel.sanitize', true) = OLD.id::text
           AND (to_jsonb(NEW) - mutable - 'text' - 'inputs_hash')
               = (to_jsonb(OLD) - mutable - 'text' - 'inputs_hash') THEN
            RETURN NEW;
        END IF;"""


def guard(*, current: bool) -> str:
    """0059's guard (``current``) or 0058's."""
    return (
        GUARD.replace("{quarantine}", QUARANTINE_COLUMNS if current else "")
        .replace("{erased}", " - 'quarantine_reason'" if current else "")
        .replace("{sanitize}", SANITIZE if current else "")
    )


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
                "d": json.dumps(change(dict(row.profile_defaults), IMPLICATION_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    op.add_column(
        "memories", sa.Column("quarantined_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("memories", sa.Column("quarantine_reason", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("quarantined_by", sa.Text(), nullable=True))
    op.add_column(
        "memories",
        sa.Column("implication_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        "ck_memories_quarantine",
        "memories",
        "(quarantined_at IS NULL) = (quarantine_reason IS NULL) "
        "AND (quarantined_at IS NULL) = (quarantined_by IS NULL) "
        f"AND (quarantine_reason IS NULL OR char_length(quarantine_reason) <= {REASON_MAX})",
    )
    op.create_check_constraint(
        "ck_memories_implication_count", "memories", "implication_count >= 0"
    )
    op.execute(guard(current=True))
    _rewrite(_added)


def downgrade() -> None:
    _rewrite(_removed)
    op.execute(guard(current=False))
    op.drop_constraint("ck_memories_implication_count", "memories", type_="check")
    op.drop_constraint("ck_memories_quarantine", "memories", type_="check")
    for column in ("implication_count", "quarantined_by", "quarantine_reason", "quarantined_at"):
        op.drop_column("memories", column)
