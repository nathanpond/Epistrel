"""Image attempt fencing: horizons, late returns and completion confirmation (#267)

FR-IMG-11, part B. ``image_jobs`` gains ``waiting_for_horizon`` (a lease expiry fenced an
attempt in flight; no attempt starts before its horizon), ``confirm_deadline_at`` (when a
``confirms_completion`` adapter's status polling gives up) and ``confirm_fallback`` (sticky: the
job fell back to the non-confirming rule). ``image_reservations`` gains ``cancel_result`` (the
one ``cancel`` after the horizon) and ``last_status`` (the last ``status`` answer), and its
``outcome`` CHECK takes #267's outcomes. The downgrade maps those outcomes back onto #256's four
(``adopted`` → ``promoted``; the rest → ``discarded``) before it restores the CHECK.

Revision ID: 0140
Revises: 0139
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0140"
down_revision: str | None = "0139"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = ("promoted", "rejected", "failed", "discarded")
ADDED = (
    "settled",
    "adopted",
    "cancelled",
    "released_at_horizon",
    "abandoned_fallback",
    "late_discarded",
    "late_after_horizon",
    "late_duplicate",
)
CHECK = "ck_image_reservations_outcome"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column(
        "image_jobs",
        sa.Column(
            "waiting_for_horizon", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "image_jobs",
        sa.Column("confirm_deadline_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "image_jobs",
        sa.Column(
            "confirm_fallback", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column("image_reservations", sa.Column("cancel_result", sa.Text(), nullable=True))
    op.add_column("image_reservations", sa.Column("last_status", sa.Text(), nullable=True))
    op.drop_constraint(CHECK, "image_reservations", type_="check")
    op.create_check_constraint(
        CHECK, "image_reservations", "outcome IS NULL OR " + _in("outcome", (*OUTCOMES, *ADDED))
    )


def downgrade() -> None:
    op.drop_constraint(CHECK, "image_reservations", type_="check")
    op.execute("UPDATE image_reservations SET outcome = 'promoted' WHERE outcome = 'adopted'")
    op.execute(
        "UPDATE image_reservations SET outcome = 'discarded' WHERE "  # noqa: S608 - constants
        + _in("outcome", tuple(a for a in ADDED if a != "adopted"))
    )
    op.create_check_constraint(
        CHECK, "image_reservations", "outcome IS NULL OR " + _in("outcome", OUTCOMES)
    )
    op.drop_column("image_reservations", "last_status")
    op.drop_column("image_reservations", "cancel_result")
    op.drop_column("image_jobs", "confirm_fallback")
    op.drop_column("image_jobs", "confirm_deadline_at")
    op.drop_column("image_jobs", "waiting_for_horizon")
