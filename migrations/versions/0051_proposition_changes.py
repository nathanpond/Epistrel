"""proposition_changes: the change index of the structured read set (#136)

FR-CONC-11,12 (docs/02 §4.4): ``core.tables.readset`` describes the table.

- ``proposition_changes`` is created with its checks, the foreign key of ``operation_id`` to
  ``operations`` and the ``(story_id, timeline_revision)``,
  ``(story_id, subject_id, predicate_id)`` and ``tx_time`` indexes. Nothing is backfilled: a
  turn in flight across the upgrade began against a base the index does not cover, so
  ``stories.changes_pruned_through_revision`` starts at each story's current revision and
  such a turn conflicts rather than rebasing unchecked.
- ``stories.changes_pruned_through_revision`` is added (default 0).

Downgrade drops both.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "changes_pruned_through_revision",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.execute("UPDATE stories SET changes_pruned_through_revision = timeline_revision")
    op.create_table(
        "proposition_changes",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("timeline_revision", sa.BigInteger(), nullable=False),
        sa.Column("proposition_id", ULID, nullable=True),
        sa.Column("subject_id", ULID, nullable=False),
        sa.Column("predicate_id", ULID, nullable=False),
        sa.Column("object_id", ULID, nullable=True),
        sa.Column("literal_hash", sa.Text(), nullable=True),
        sa.Column("object_set", postgresql.ARRAY(ULID), nullable=True),
        sa.Column("holder", sa.Text(), server_default=sa.text("'canon'"), nullable=False),
        sa.Column("speaker_id", ULID, nullable=True),
        sa.Column(
            "visibility",
            postgresql.JSONB(),
            server_default=sa.text("""'{"scope": "objective"}'::jsonb"""),
            nullable=False,
        ),
        sa.Column("valid_window", postgresql.INT8RANGE(), nullable=False),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column(
            "tx_time",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "change IN ('add', 'supersede', 'close')",
            name="ck_proposition_changes_change_kind",
        ),
        sa.CheckConstraint(
            "holder IN ('canon', 'utterance')", name="ck_proposition_changes_holder"
        ),
        sa.CheckConstraint(
            "(holder = 'utterance') = (speaker_id IS NOT NULL)",
            name="ck_proposition_changes_speaker",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_proposition_changes_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposition_changes"),
    )
    op.create_index(
        "ix_proposition_changes_story_revision",
        "proposition_changes",
        ["story_id", "timeline_revision"],
    )
    op.create_index(
        "ix_proposition_changes_story_subject_predicate",
        "proposition_changes",
        ["story_id", "subject_id", "predicate_id"],
    )
    op.create_index("ix_proposition_changes_tx_time", "proposition_changes", ["tx_time"])


def downgrade() -> None:
    op.drop_table("proposition_changes")
    op.drop_column("stories", "changes_pruned_through_revision")
