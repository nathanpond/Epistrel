"""Story images (#261)

FR-IMG-5, FR-IMG-10: ``core.tables.story_images`` describes the table: one row per image placed
in a story (the five ``at_*`` position columns, the audience viewpoint, the camera POV, the
asset, the stored prompt, the source and viewpoint basis, the job, and the deleting operation)
with the derived-record columns of ``core.membership``. ``media_assets`` gains
``viewpoint_basis`` (``NULL`` for an asset that is no story image), and ``operation_kind`` the
value ``story_image`` (an image placed or deleted at a beat, anchored at its turn).

Downgrade drops the table and the column; the enum value stays (a value cannot be dropped).

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0136"
down_revision: str | None = "0135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
POSITION = ("turn", "variant", "slot_k", "beat", "step")
CAMERAS = ("third_person", "establishing", "over_the_shoulder", "first_person")
SOURCES = ("uploaded", "generated", "generated_from_consumer_prompt", "extracted")
BASES = ("filtered_inputs", "protected_term_match", "none")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute("ALTER TYPE operation_kind ADD VALUE IF NOT EXISTS 'story_image'")
    op.add_column("media_assets", sa.Column("viewpoint_basis", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_media_assets_viewpoint_basis",
        "media_assets",
        f"viewpoint_basis IS NULL OR {_in('viewpoint_basis', BASES)}",
    )
    op.create_table(
        "story_images",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
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
            "membership",
            postgresql.ENUM(name="membership", create_type=False),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("at_turn", sa.Integer(), nullable=False),
        *(
            sa.Column(f"at_{name}", sa.SmallInteger(), server_default=sa.text("0"), nullable=False)
            for name in POSITION[1:]
        ),
        sa.Column("audience_viewpoint", sa.Text(), nullable=False),
        sa.Column("camera_pov", postgresql.JSONB(), nullable=False),
        sa.Column("asset_id", ULID, nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("viewpoint_basis", sa.Text(), nullable=False),
        sa.Column("job_id", ULID, nullable=True),
        sa.Column("deleted_operation_id", ULID, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_story_images_operation_id_operations",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_operation_id"],
            ["operations.operation_id"],
            name="fk_story_images_deleted_operation_id_operations",
        ),
        sa.CheckConstraint(
            "(at_slot_k = 0 AND at_beat >= 1) OR (at_slot_k >= 1 AND at_beat = 0)",
            name="ck_story_images_position_shape",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(camera_pov) = 'object' AND "
            + _in("camera_pov->>'kind'", CAMERAS)
            + " AND ((camera_pov->>'kind') IN ('over_the_shoulder', 'first_person')) "
            "= (camera_pov ? 'character_id')",
            name="ck_story_images_camera_pov",
        ),
        sa.CheckConstraint(
            "audience_viewpoint = 'omniscient' "
            "OR audience_viewpoint ~ '^character:[0-9A-HJKMNP-TV-Z]{26}$'",
            name="ck_story_images_audience_viewpoint",
        ),
        sa.CheckConstraint(_in("source", SOURCES), name="ck_story_images_source"),
        sa.CheckConstraint(_in("viewpoint_basis", BASES), name="ck_story_images_viewpoint_basis"),
        sa.PrimaryKeyConstraint("id", name="pk_story_images"),
    )
    op.create_index("ix_story_images_story_operation", "story_images", ["story_id", "operation_id"])
    op.create_index("ix_story_images_story_membership", "story_images", ["story_id", "membership"])
    op.create_index("ix_story_images_story_at_turn", "story_images", ["story_id", "at_turn"])
    op.create_index("ix_story_images_asset_id", "story_images", ["asset_id"])


def downgrade() -> None:
    op.drop_table("story_images")
    op.drop_constraint("ck_media_assets_viewpoint_basis", "media_assets", type_="check")
    op.drop_column("media_assets", "viewpoint_basis")
