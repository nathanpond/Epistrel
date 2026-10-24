"""a consolidation output may be a holder's belief; an erasure prunes a memory's visibility
(#178)

- S-CONSOLIDATION-SCOPE M5 (FR-MEM-9): a statement resting on a character's own (held) memory
  stays that character's belief, so ``memories.modality`` gains ``belief`` beside ``objective``,
  ``attributed_claim`` and ``observation`` (the row's ``holder_character_id`` names the holder).
- S-ERASURE-BOUNDARY M5 (FR-ADMIN-6): erasing a character removes its id from the
  ``visibility.characters`` of the memories other characters hold, so ``epistrel_memories_guard``
  lets an erasure (``epistrel.erasure = on``) rewrite ``visibility`` as well as ``text`` and
  ``quarantine_reason``. Nothing else changes in the guard (0061's, fired after the row by 0067).

Downgrade restores the three-value CHECK (it fails while a ``belief`` row exists) and 0061's
guard.

Revision ID: 0079
Revises: 0078
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = ("objective", "attributed_claim", "observation")
AFTER = (*BEFORE, "belief")


def _check(values: tuple[str, ...]) -> str:
    return "modality IS NULL OR modality IN (" + ", ".join(f"'{m}'" for m in values) + ")"


GUARD = """
CREATE OR REPLACE FUNCTION epistrel_memories_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['importance', 'importance_at', 'last_accessed_at', 'retrieval_tier',
                            'embedding', 'embedding_model_version', 'membership',
                            'membership_tx', 'search', 'covered_by', 'quarantined_at',
                            'quarantine_reason', 'quarantined_by', 'implication_count',
                            'pinned', 'pinned_by', 'retired_reason', 'retired_at',
                            'retired_turn'];
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
"""0061's ``epistrel_memories_guard`` (current since 0067 moved its trigger after the row)."""

ERASURE_BEFORE = "- 'text' - 'quarantine_reason')"
ERASURE_AFTER = "- 'text' - 'quarantine_reason' - 'visibility')"


def _guard(*, pruning: bool) -> str:
    return GUARD.replace(ERASURE_BEFORE, ERASURE_AFTER) if pruning else GUARD


def upgrade() -> None:
    op.drop_constraint("ck_memories_modality", "memories", type_="check")
    op.create_check_constraint("ck_memories_modality", "memories", _check(AFTER))
    op.execute(_guard(pruning=True))


def downgrade() -> None:
    op.execute(_guard(pruning=False))
    op.drop_constraint("ck_memories_modality", "memories", type_="check")
    op.create_check_constraint("ck_memories_modality", "memories", _check(BEFORE))
