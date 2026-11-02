"""certification canary: stale certifications and the models' reported versions (#234)

FR-PROF-16,17, ADR-107:

- ``certification_runs`` takes ``kind = 'canary'``, the certification a canary checks
  (``checks_certification_id``) and its ``outcome`` (``held``, ``missed``, ``inconclusive``).
- ``model_reported_versions``: each version a model reported, first and last seen.

The canary and re-certification recurring tasks' rows are written by ``timers.reconcile`` when
the workers start; their handlers do nothing while the deployment leaves them off.

Revision ID: 0115
Revises: 0114
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0115"
down_revision: str | None = "0114"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")


def upgrade() -> None:
    op.add_column("certification_runs", sa.Column("checks_certification_id", ULID, nullable=True))
    op.add_column("certification_runs", sa.Column("outcome", sa.Text, nullable=True))
    op.drop_constraint("ck_certification_runs_kind", "certification_runs", type_="check")
    op.create_check_constraint(
        "ck_certification_runs_kind", "certification_runs", "kind IN ('certification', 'canary')"
    )
    op.create_check_constraint(
        "ck_certification_runs_outcome",
        "certification_runs",
        "outcome IS NULL OR outcome IN ('held', 'missed', 'inconclusive')",
    )
    op.create_table(
        "model_reported_versions",
        sa.Column("base_url_hash", sa.Text, primary_key=True),
        sa.Column("model_id", sa.Text, primary_key=True),
        sa.Column("reported_version", sa.Text, primary_key=True),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index(
        "ix_model_reported_versions_model",
        "model_reported_versions",
        ["model_id", "last_seen_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM certification_runs WHERE kind = 'canary')")
    ).scalar_one():
        raise RuntimeError(
            "0115 downgrade refused: canary runs exist (certification runs are kept)"
        )
    op.drop_table("model_reported_versions")
    op.drop_constraint("ck_certification_runs_outcome", "certification_runs", type_="check")
    op.drop_constraint("ck_certification_runs_kind", "certification_runs", type_="check")
    op.create_check_constraint(
        "ck_certification_runs_kind", "certification_runs", "kind IN ('certification')"
    )
    op.drop_column("certification_runs", "outcome")
    op.drop_column("certification_runs", "checks_certification_id")
