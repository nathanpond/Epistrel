"""seed the global minors policy (FR-GUARD-11)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.orchestrator.policies.seeds.policies import SEED_POLICIES

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _policies() -> sa.TableClause:
    return sa.table(
        "policies",
        sa.column("id"),
        sa.column("scope"),
        sa.column("scope_id"),
        sa.column("story_id"),
        sa.column("name"),
        sa.column("applies_to", postgresql.ARRAY(sa.Text())),
        sa.column("kind"),
        sa.column("effect"),
        sa.column("rule", postgresql.JSONB()),
        sa.column("age_condition"),
        sa.column("message"),
        sa.column("created_by"),
        sa.column("updated_by"),
    )


def upgrade() -> None:
    """Insert the seeded policies silently: no ``config_changes`` row, revision unchanged."""
    op.bulk_insert(_policies(), [dict(row) for row in SEED_POLICIES])


def downgrade() -> None:
    ids = [row["id"] for row in SEED_POLICIES]
    policies = _policies()
    op.execute(policies.delete().where(policies.c.id.in_(ids)))
