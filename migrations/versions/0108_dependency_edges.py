"""dependency_edges: typed dependency edges for every commit (#220)

FR-DIRECT-7, FR-REWRITE-11 (docs/02's rewrite blast radius): ``core.tables.edges`` describes
the table.

- The enums ``dependency_edge_type`` (``causes``, ``presupposes``, ``derives``, ``mentions``,
  ``asserts``) and ``dependency_target`` (``fact``, ``proposition``, ``entity``,
  ``amendment_part``, ``state``).
- ``dependency_edges``: one row per (edge type, dependent record, target), with the target's
  proposition key, a ``derives`` edge's ``acquisition_kind`` (CHECK: set exactly on
  ``derives``), the dependent's position (a nullable ``position_*`` group: an amendment's
  edges are position-less) and the derived-record columns of ``core.membership``. Indexes: the
  standard derived pair, ``uq_dependency_edges_edge`` (one edge per type, dependent and
  target), the target, the proposition key and the dependent. Append-only
  (``epistrel_dependency_edges_append_only``): an UPDATE may change only the membership pair; a
  DELETE passes only under an erasure or the story's deletion.

No data change: edges are written for records committed from now on (no retroactive
derivation). Downgrade drops the table, its function and the enums.

Revision ID: 0108
Revises: 0107
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0108"
down_revision: str | None = "0107"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
TABLE = "dependency_edges"

ENUMS: dict[str, tuple[str, ...]] = {
    "dependency_edge_type": ("causes", "presupposes", "derives", "mentions", "asserts"),
    "dependency_target": ("fact", "proposition", "entity", "amendment_part", "state"),
}

POSITION = ("turn", "variant", "slot_k", "beat", "step")

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_dependency_edges_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'dependency edges are append-only: update of edge % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'dependency edges are append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'dependency edges are append-only: % of edge % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    f"CREATE TRIGGER {TABLE}_append_only BEFORE UPDATE OR DELETE ON {TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_dependency_edges_append_only()",
    f"CREATE TRIGGER {TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_dependency_edges_append_only()",
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)
    op.create_table(
        TABLE,
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("edge_type", _enum("dependency_edge_type"), nullable=False),
        sa.Column("dependent_table", sa.Text(), nullable=False),
        sa.Column("dependent_id", ULID, nullable=False),
        sa.Column("target_kind", _enum("dependency_target"), nullable=False),
        sa.Column("target_id", ULID, nullable=False),
        sa.Column("target_subject_id", ULID, nullable=True),
        sa.Column("target_predicate_id", ULID, nullable=True),
        sa.Column("acquisition_kind", sa.Text(), nullable=True),
        sa.Column("position_turn", sa.Integer(), nullable=True),
        *(sa.Column(f"position_{name}", sa.SmallInteger(), nullable=True) for name in POSITION[1:]),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column(
            "source_event_ids",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("narrative_turn", sa.Integer(), nullable=True),
        sa.Column("turn_variant", sa.SmallInteger(), nullable=True),
        sa.Column(
            "membership", _enum("membership"), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(edge_type = 'derives') = (acquisition_kind IS NOT NULL)",
            name=f"ck_{TABLE}_acquisition_kind",
        ),
        sa.CheckConstraint(
            "num_nulls(position_turn, position_variant, position_slot_k, position_beat, "
            "position_step) IN (0, 5)",
            name=f"ck_{TABLE}_position_group",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name=f"fk_{TABLE}_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{TABLE}"),
    )
    op.create_index(f"ix_{TABLE}_story_operation", TABLE, ["story_id", "operation_id"])
    op.create_index(f"ix_{TABLE}_story_membership", TABLE, ["story_id", "membership"])
    op.create_index(
        f"uq_{TABLE}_edge",
        TABLE,
        ["edge_type", "dependent_table", "dependent_id", "target_kind", "target_id"],
        unique=True,
    )
    op.create_index(f"ix_{TABLE}_story_target", TABLE, ["story_id", "target_kind", "target_id"])
    op.create_index(
        f"ix_{TABLE}_story_key", TABLE, ["story_id", "target_subject_id", "target_predicate_id"]
    )
    op.create_index(
        f"ix_{TABLE}_story_dependent", TABLE, ["story_id", "dependent_table", "dependent_id"]
    )
    op.execute(GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(TABLE)
    op.execute("DROP FUNCTION epistrel_dependency_edges_append_only()")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
