"""identity as a held proposition: identity proposals and ``entities.merged_into`` (#171)

FR-PROP-7 (docs/02 §4.9): ``same_as`` is an ordinary proposition with a holder; the engine
files an **identity proposal** when a canon ``same_as`` makes two entities one, and never merges
them itself (merges and splits are M7's amendments).

- ``predicate_proposals.kind`` (``text``, NOT NULL, default ``predicate``, CHECK ``predicate``,
  ``identity``): an identity proposal is a row of #131's proposals table, its ``predicate_id``
  the core ``same_as`` and its pair in ``subject_entity_id``/``object_entity_id`` (nullable,
  set exactly when ``kind = identity``, id order); ``proposed`` holds the evidence.
- ``uq_predicate_proposals_pending`` becomes one pending ``predicate`` proposal per predicate
  (``kind = 'predicate'``), and ``uq_predicate_proposals_identity_pending`` keeps one pending
  identity proposal per pair of a story.
- ``entities.merged_into`` (nullable): the entity a later merge folds this one into (M7);
  nothing sets it in M5.

Downgrade drops the identity rows, the columns and the index, and restores the M4 index.

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    op.add_column(
        "predicate_proposals",
        sa.Column("kind", sa.Text(), server_default=sa.text("'predicate'"), nullable=False),
    )
    op.add_column("predicate_proposals", sa.Column("subject_entity_id", ULID, nullable=True))
    op.add_column("predicate_proposals", sa.Column("object_entity_id", ULID, nullable=True))
    op.create_check_constraint(
        "ck_predicate_proposals_kind", "predicate_proposals", "kind IN ('predicate', 'identity')"
    )
    op.create_check_constraint(
        "ck_predicate_proposals_identity_pair",
        "predicate_proposals",
        "(kind = 'identity') = (subject_entity_id IS NOT NULL AND object_entity_id IS NOT NULL)",
    )
    op.drop_index("uq_predicate_proposals_pending", table_name="predicate_proposals")
    op.create_index(
        "uq_predicate_proposals_pending",
        "predicate_proposals",
        ["predicate_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND kind = 'predicate'"),
    )
    op.create_index(
        "uq_predicate_proposals_identity_pending",
        "predicate_proposals",
        ["story_id", "subject_entity_id", "object_entity_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND kind = 'identity'"),
    )
    op.add_column("entities", sa.Column("merged_into", ULID, nullable=True))


def downgrade() -> None:
    op.drop_column("entities", "merged_into")
    op.execute("DELETE FROM predicate_proposals WHERE kind = 'identity'")
    op.drop_index("uq_predicate_proposals_identity_pending", table_name="predicate_proposals")
    op.drop_index("uq_predicate_proposals_pending", table_name="predicate_proposals")
    op.create_index(
        "uq_predicate_proposals_pending",
        "predicate_proposals",
        ["predicate_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_constraint("ck_predicate_proposals_identity_pair", "predicate_proposals", type_="check")
    op.drop_constraint("ck_predicate_proposals_kind", "predicate_proposals", type_="check")
    op.drop_column("predicate_proposals", "object_entity_id")
    op.drop_column("predicate_proposals", "subject_entity_id")
    op.drop_column("predicate_proposals", "kind")
