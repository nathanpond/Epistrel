"""Image send recovery: re-sends, presumed settlements and promotion at most once (#268)

FR-IMG-11, part C. ``image_reservations`` gains ``resend_of`` (an idempotent re-send's row names
the stalled attempt whose key it re-sent, and whose record carries its reservation and
settlement), ``request_snapshot`` (the request the attempt sent, so a re-send sends it again) and
``capabilities_snapshot`` (the adapter's ``{submission, idempotency_keys}`` at the ``sending``
commit, which recovery reads). Its ``outcome`` CHECK takes ``presumed``, ``resent`` and
``late_after_presumed``. ``media_assets`` gains ``source_job_id``, ``source_attempt_token`` and
``output_index`` (the output's index in the provider's return order) with a unique index, so no
output of an attempt is promoted twice. The downgrade maps the new outcomes back onto #267's
(``presumed`` → ``settled``, ``resent`` → ``settled``, ``late_after_presumed`` →
``late_after_horizon``) before it restores the CHECK.

Revision ID: 0141
Revises: 0140
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0141"
down_revision: str | None = "0140"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEPT = (
    "promoted",
    "rejected",
    "failed",
    "discarded",
    "settled",
    "adopted",
    "cancelled",
    "released_at_horizon",
    "abandoned_fallback",
    "late_discarded",
    "late_after_horizon",
    "late_duplicate",
)
ADDED = {"presumed": "settled", "resent": "settled", "late_after_presumed": "late_after_horizon"}
CHECK = "ck_image_reservations_outcome"
INDEX = "ux_media_assets_source_output"


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column("image_reservations", sa.Column("resend_of", UUID(as_uuid=False), nullable=True))
    op.add_column("image_reservations", sa.Column("request_snapshot", JSONB(), nullable=True))
    op.add_column("image_reservations", sa.Column("capabilities_snapshot", JSONB(), nullable=True))
    op.drop_constraint(CHECK, "image_reservations", type_="check")
    op.create_check_constraint(
        CHECK, "image_reservations", "outcome IS NULL OR " + _in("outcome", (*KEPT, *ADDED))
    )
    op.add_column("media_assets", sa.Column("source_job_id", sa.CHAR(26), nullable=True))
    op.add_column(
        "media_assets", sa.Column("source_attempt_token", UUID(as_uuid=False), nullable=True)
    )
    op.add_column("media_assets", sa.Column("output_index", sa.Integer(), nullable=True))
    op.create_index(
        INDEX,
        "media_assets",
        ["source_job_id", "source_attempt_token", "output_index"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="media_assets")
    op.drop_column("media_assets", "output_index")
    op.drop_column("media_assets", "source_attempt_token")
    op.drop_column("media_assets", "source_job_id")
    op.drop_constraint(CHECK, "image_reservations", type_="check")
    for added, kept in ADDED.items():
        op.execute(
            f"UPDATE image_reservations SET outcome = '{kept}' WHERE outcome = '{added}'"  # noqa: S608 - constants
        )
    op.create_check_constraint(
        CHECK, "image_reservations", "outcome IS NULL OR " + _in("outcome", KEPT)
    )
    op.drop_column("image_reservations", "capabilities_snapshot")
    op.drop_column("image_reservations", "request_snapshot")
    op.drop_column("image_reservations", "resend_of")
