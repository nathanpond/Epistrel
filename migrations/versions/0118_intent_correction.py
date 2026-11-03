"""intent correction: an author's explicit change of what a speaker meant (#225)

FR-DIRECT-8: a retcon never changes an utterance's intent; only an explicit instruction about
intent does, through an amendment part of action ``set_intent``.

- ``ck_amendment_parts_action`` admits ``set_intent``.
- ``utterances``' append-only guard admits a change of ``intent`` only while the transaction
  setting ``epistrel.allow_intent_correction`` names the row (the intent path alone sets it).

Revision ID: 0118
Revises: 0117
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0118"
down_revision: str | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = """        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'utterances are append-only: update of utterance % refused', OLD.id"""
AFTER = (
    """        IF current_setting('epistrel.allow_intent_correction', true) = OLD.id::text THEN
            mutable := mutable || ARRAY['intent', 'classification'];
        END IF;
"""
    + BEFORE
)


def _patch(before: str, after: str) -> None:
    body = str(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
                "WHERE p.proname = 'epistrel_utterances_append_only'"
            )
        )
        .scalar_one()
    )
    if before not in body:
        raise RuntimeError("epistrel_utterances_append_only: the expected clause is missing")
    op.execute(body.replace(before, after, 1))


def upgrade() -> None:
    op.drop_constraint("ck_amendment_parts_action", "amendment_parts", type_="check")
    op.create_check_constraint(
        "ck_amendment_parts_action",
        "amendment_parts",
        "action IS NULL OR action IN ('replace', 'drop', 'relabel', 'set_intent')",
    )
    _patch(BEFORE, AFTER)


def downgrade() -> None:
    _patch(AFTER, BEFORE)
    op.drop_constraint("ck_amendment_parts_action", "amendment_parts", type_="check")
    op.create_check_constraint(
        "ck_amendment_parts_action",
        "amendment_parts",
        "action IS NULL OR action IN ('replace', 'drop', 'relabel')",
    )
