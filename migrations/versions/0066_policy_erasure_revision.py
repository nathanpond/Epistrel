"""the effective-policy cache's erasure revisions: ``deployment_state.policy_erasure_revision``
and ``stories.policy_erasure_revision`` (#469; NFR-PRIV-2, FR-ADMIN-6)

An erasure that redacts or deletes a policy moves none of the configuration revisions (it
records no configuration change), so the in-process effective-policy cache
(``orchestrator.policies.store``), keyed by those revisions, kept serving the unredacted policy.
Each column counts the erasures that rewrote or deleted a policy row in its scope: a
catalog-scope erasure moves the deployment's (global and rating policies), a story-scope erasure
the story's (its story and character policies). The cache keys on both, read from the database,
so every process stops serving the old text once the erasure commits. Both start at 0.

Downgrade drops the two columns.

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "policy_erasure_revision"


def upgrade() -> None:
    for table in ("deployment_state", "stories"):
        op.add_column(
            table,
            sa.Column(COLUMN, sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    for table in ("stories", "deployment_state"):
        op.drop_column(table, COLUMN)
