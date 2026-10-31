"""Engine-proposed States: ``state_proposals`` (#192)

FR-STATE-3 (docs/02 §7.1): the engine may propose a tracked value for approval but never
creates one. ``core.tables.states`` describes the table.

- The enum type ``state_proposal_status`` (``proposed``, ``approved``, ``dismissed``) is created.
- ``state_proposals`` is created: one row per recurring quantity unit or possessed entity the
  on-demand history analysis found with no State, unique ``(story_id, name)`` (the analysis's
  fingerprint), its suggested declaration, its ``evidence`` (a JSON array), its status and the
  analysis request's operation. No foreign keys (no plain foreign keys between story tables).

Downgrade drops the table and the enum type.

Revision ID: 0091
Revises: 0088
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0091"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
STATUS = ("proposed", "approved", "dismissed")


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    postgresql.ENUM(*STATUS, name="state_proposal_status").create(op.get_bind())
    op.create_table(
        "state_proposals",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("suggested_type", _enum("state_type"), nullable=False),
        sa.Column("suggested_scope", _enum("state_scope"), nullable=False),
        sa.Column("suggested_unit", sa.Text(), nullable=True),
        sa.Column("suggested_entity_id", ULID, nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("state_proposal_status"),
            server_default=sa.text("'proposed'"),
            nullable=False,
        ),
        sa.Column("created_by_operation_id", ULID, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("definition_id", ULID, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_state_proposals"),
        sa.UniqueConstraint("story_id", "name", name="uq_state_proposals_story_id"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="ck_state_proposals_name"),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'array'", name="ck_state_proposals_evidence"),
        sa.CheckConstraint(
            "(status = 'approved') = (definition_id IS NOT NULL)",
            name="ck_state_proposals_approved_definition",
        ),
    )


def downgrade() -> None:
    op.drop_table("state_proposals")
    postgresql.ENUM(name="state_proposal_status").drop(op.get_bind())
