"""stories keep a pinned profile version when an upgrade is unavailable (#109)

``stories.engine_profile_version_pin`` (nullable): ``NULL`` while a story follows its engine
profile's latest version (FR-PROF-6); the version it keeps when a confirmed data-producing edit
of its profile could not apply to it, because it has history and M3 has no backfill (FR-PROF-7's
``upgrade_unavailable``, the interim rule until M7, FR-PROF-8). ``(engine_profile_id,
engine_profile_version_pin)`` references ``engine_profile_versions``, so a pinned version always
exists. Every existing story follows its profile (``NULL``).

Downgrade drops the foreign key and the column: a pinned story then follows its profile's latest
version again.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PIN_FOREIGN_KEY = "fk_stories_engine_profile_version_pin"


def upgrade() -> None:
    op.add_column("stories", sa.Column("engine_profile_version_pin", sa.Integer(), nullable=True))
    op.create_foreign_key(
        PIN_FOREIGN_KEY,
        "stories",
        "engine_profile_versions",
        ["engine_profile_id", "engine_profile_version_pin"],
        ["profile_id", "version"],
    )


def downgrade() -> None:
    op.drop_constraint(PIN_FOREIGN_KEY, "stories", type_="foreignkey")
    op.drop_column("stories", "engine_profile_version_pin")
