"""The minors policy's image targets, and the image safety context (#257)

FR-GUARD-11, FR-IMG-3,12: the seeded ``minors_no_sexual_content`` policy gains ``image_prompt``
and ``image_output`` in its ``applies_to`` (four targets; #55 left them for M8). The update is a
union, so it is idempotent and keeps an administrator's own targets; a deployment that removed
the policy has nothing to update. It runs silently, as ``0009`` inserted the row: no
``config_changes`` row, the version and the deployment ``config_revision`` unchanged.

``image_jobs`` gains ``safety_context`` (the job's resolved subjects with their character ids
and true ages, FR-IMG-12; read by the output check, never returned by any read), and
``audit_log`` the kind ``image_prompt_rejected`` (a refused image prompt that created no job:
policy ids, principal, target and story, never the prompt).

Revision ID: 0133
Revises: 0132
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0133"
down_revision: str | None = "0132"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MINORS_POLICY_ID = "minors_no_sexual_content"
IMAGE_TARGETS = ("image_prompt", "image_output")
KINDS = (
    "guardrail_input_rejected",
    "story_deleted",
    "catalog_entry_deleted",
    "persona_deleted",
    "rating_change_refused",
    "erasure",
    "story_import",
    "backfill_cancelled",
    "backfill_failed",
    "image_output_rejected",
    "image_job_ended",
)
ADDED = ("image_prompt_rejected",)


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


ADD_TARGETS = sa.text(
    "UPDATE policies SET applies_to = applies_to || ARRAY(SELECT t FROM unnest("
    "CAST(:targets AS text[])) AS t WHERE NOT t = ANY(applies_to)) "
    "WHERE id = :id AND NOT CAST(:targets AS text[]) <@ applies_to"
).bindparams(targets=list(IMAGE_TARGETS), id=MINORS_POLICY_ID)
"""A union in the stored order: the existing targets first, then each image target missing (no
row: the policy was removed; every target present: nothing to do)."""


def upgrade() -> None:
    op.execute(ADD_TARGETS)
    op.add_column("image_jobs", sa.Column("safety_context", postgresql.JSONB(), nullable=True))
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, *ADDED)))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE kind = 'image_prompt_rejected'")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))
    op.drop_column("image_jobs", "safety_context")
    op.execute(
        sa.text(
            "UPDATE policies SET applies_to = ARRAY(SELECT t FROM unnest(applies_to) AS t "
            "WHERE NOT t = ANY(CAST(:targets AS text[]))) WHERE id = :id"
        ).bindparams(targets=list(IMAGE_TARGETS), id=MINORS_POLICY_ID)
    )
