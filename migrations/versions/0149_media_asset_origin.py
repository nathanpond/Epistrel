"""media_assets gains origin: a subject image the media sweep keeps while it is live (#830)

FR-IMG-14 and ADR-186: an image generated for a consumer-described subject is a live asset
owned by its requester, kept until it is deleted or its owner is offboarded. ``origin`` is
``subject`` for such an asset and null for every other; ``source`` keeps its FR-IMG-10 value.
The media sweep skips live assets whose origin is ``subject``.

Downgrade drops the column (a subject image then reads as any generated image).

Revision ID: 0149
Revises: 0148
Create Date: 2026-11-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0149"
down_revision: str | None = "0148"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_assets", sa.Column("origin", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_media_assets_origin", "media_assets", "origin IS NULL OR origin IN ('subject')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_media_assets_origin", "media_assets", type_="check")
    op.drop_column("media_assets", "origin")
