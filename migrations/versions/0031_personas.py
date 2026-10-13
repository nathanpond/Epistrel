"""personas, and what a story character keeps of its persona, its images and its promotions (#114)

- ``personas`` (FR-CAT-6; docs/02 §4.10): a principal's saved player personas, in the default
  schema (personas are not catalog entries): owner, name, optional gender, age (0 to 150, the
  persona's true age) and description, with ``created_at``/``updated_at``. A name is unique per
  owner, ignoring case (``uq_personas_owner_name``), and ``(updated_at, id)`` is the list's
  keyset.
- ``characters.persona_id`` (a plain identifier, no foreign key: story tables reference no
  deployment table) and ``characters.persona_snapshot`` (jsonb): a player character's persona
  as it was when the story was created; ``ck_characters_persona_player`` keeps both on player
  characters only.
- ``characters.images`` (jsonb, default ``[]``): the media assets a character added in a story
  shows (FR-CAT-7), by reference.
- ``characters.promoted_to`` (jsonb, default ``[]``): the catalog entries promoted from the
  character (FR-CAT-8), as plain ids.
- ``persona_deleted`` joins the ``audit_log.kind`` check.

Downgrade drops them, and the ``persona_deleted`` audit rows (deleted under
``epistrel.allow_audit_expiry``, since the restored check forbids them).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

KIND_CHECK_BEFORE = "kind IN ('guardrail_input_rejected', 'story_deleted', 'catalog_entry_deleted')"
KIND_CHECK = (
    "kind IN ('guardrail_input_rejected', 'story_deleted', 'catalog_entry_deleted', "
    "'persona_deleted')"
)
PERSONA_PLAYER = "(persona_id IS NULL AND persona_snapshot IS NULL) OR is_player"
EMPTY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.create_table(
        "personas",
        sa.Column("id", ULID, nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("gender", sa.Text(), nullable=True),
        sa.Column("age", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("age BETWEEN 0 AND 150", name="ck_personas_age"),
        sa.PrimaryKeyConstraint("id", name="pk_personas"),
    )
    op.create_index(
        "uq_personas_owner_name",
        "personas",
        ["owner_principal", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index("ix_personas_updated_at_id", "personas", ["updated_at", "id"])

    op.add_column("characters", sa.Column("persona_id", ULID, nullable=True))
    op.add_column("characters", sa.Column("persona_snapshot", postgresql.JSONB(), nullable=True))
    op.add_column(
        "characters",
        sa.Column("images", postgresql.JSONB(), nullable=False, server_default=EMPTY),
    )
    op.add_column(
        "characters",
        sa.Column("promoted_to", postgresql.JSONB(), nullable=False, server_default=EMPTY),
    )
    op.create_check_constraint("ck_characters_persona_player", "characters", PERSONA_PLAYER)

    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK)


def downgrade() -> None:
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'on', true)")
    op.execute("DELETE FROM audit_log WHERE kind = 'persona_deleted'")
    op.execute("SELECT set_config('epistrel.allow_audit_expiry', 'off', true)")
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", KIND_CHECK_BEFORE)

    op.drop_constraint("ck_characters_persona_player", "characters", type_="check")
    op.drop_column("characters", "promoted_to")
    op.drop_column("characters", "images")
    op.drop_column("characters", "persona_snapshot")
    op.drop_column("characters", "persona_id")

    op.drop_index("ix_personas_updated_at_id", table_name="personas")
    op.drop_index("uq_personas_owner_name", table_name="personas")
    op.drop_table("personas")
