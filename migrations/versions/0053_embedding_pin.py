"""the embedding pin: the deployment's sticky vector space and each story's (#138)

FR-EMBED-2,3 (docs/02 §6): ``core.embeddings`` reads and writes these columns.

- ``deployment_state`` gains ``embedding_model_version``, ``embedding_dimension`` and
  ``embedding_pinned_at`` (all ``NULL`` until the first embedding pins them) and
  ``reembed_state`` (the last model change's ``from_version``, ``to_version`` and
  ``started_at``, for ``GET /admin/reembed/status``).
- ``stories`` gains ``embedding_model_version``: the version the story's vectors are in
  (FR-STORE-30's per-story pin), set at its first embedding and advanced by the re-embed job.

``memories.embedding`` stays the untyped ``vector`` 0050 created: the first embedding retypes it
to ``vector(d)`` and creates the HNSW index at run time (``core.embeddings.retype``), because
``d`` is the configured model's and no migration can know it.

Downgrade drops the columns, the index, and the column's type back to untyped.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployment_state", sa.Column("embedding_model_version", sa.Text(), nullable=True)
    )
    op.add_column("deployment_state", sa.Column("embedding_dimension", sa.Integer(), nullable=True))
    op.add_column(
        "deployment_state",
        sa.Column("embedding_pinned_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "deployment_state",
        sa.Column(
            "reembed_state",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_deployment_state_embedding_dimension",
        "deployment_state",
        "embedding_dimension IS NULL OR embedding_dimension > 0",
    )
    op.add_column("stories", sa.Column("embedding_model_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "embedding_model_version")
    op.drop_constraint("ck_deployment_state_embedding_dimension", "deployment_state", type_="check")
    op.drop_column("deployment_state", "reembed_state")
    op.drop_column("deployment_state", "embedding_pinned_at")
    op.drop_column("deployment_state", "embedding_dimension")
    op.drop_column("deployment_state", "embedding_model_version")
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.execute("ALTER TABLE memories ALTER COLUMN embedding TYPE vector")
