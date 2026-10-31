"""External-data providers: ``external_providers``, ``story_provider_bindings`` and
``state_definitions.external_value_name`` (#204)

FR-EXT-1,6,7, FR-STATE-2 (docs/02 §4.8): ``core.tables.providers`` describes the tables.

- ``external_providers`` is created: one row per registered provider (deployment-wide), keyed by
  its ``name``, with its trust tier, transport, manifest, write capability and policies.
- ``story_provider_bindings`` is created: a story's external ids at one provider, keyed by
  ``(story_id, provider_name)``.
- ``state_definitions`` gains ``external_value_name`` (the provider's value an externally owned
  State reads), set exactly when ``owner = 'external'``. No State is externally owned before
  this migration (nothing could register a provider), so the check holds for every row.

Downgrade drops the column, its check and both tables.

Revision ID: 0086
Revises: 0085
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    op.create_table(
        "external_providers",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("trust_tier", sa.Text(), nullable=False),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("write_capable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confirmation", sa.Text(), nullable=True),
        sa.Column("dedupes_dispatch", sa.Boolean(), nullable=True),
        sa.Column("returns_resulting_values", sa.Boolean(), nullable=True),
        sa.Column(
            "unavailability", sa.Text(), server_default=sa.text("'last_known'"), nullable=False
        ),
        sa.Column("freshness_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "confirm_wait_max_seconds", sa.Integer(), server_default=sa.text("10"), nullable=False
        ),
        sa.Column("config_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("name ~ '^[a-z][a-z0-9_.-]{0,63}$'", name="ck_external_providers_name"),
        sa.CheckConstraint(
            "trust_tier IN ('authoritative', 'advisory')", name="ck_external_providers_trust_tier"
        ),
        sa.CheckConstraint(
            "transport IN ('http', 'in_process')", name="ck_external_providers_transport"
        ),
        sa.CheckConstraint(
            "confirmation IS NULL OR confirmation IN ('synchronous', 'asynchronous')",
            name="ck_external_providers_confirmation",
        ),
        sa.CheckConstraint(
            "unavailability IN ('fail_turn', 'last_known', 'unknown')",
            name="ck_external_providers_unavailability",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(manifest) = 'object'", name="ck_external_providers_manifest"
        ),
        sa.CheckConstraint(
            "freshness_minutes IS NULL OR freshness_minutes > 0",
            name="ck_external_providers_freshness_minutes",
        ),
        sa.CheckConstraint(
            "confirm_wait_max_seconds > 0", name="ck_external_providers_confirm_wait_max_seconds"
        ),
        sa.PrimaryKeyConstraint("name", name="pk_external_providers"),
    )

    op.create_table(
        "story_provider_bindings",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("external_ids", postgresql.JSONB(), nullable=False),
        sa.Column(
            "bound_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(external_ids) = 'object'",
            name="ck_story_provider_bindings_external_ids",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.id"], name="fk_story_provider_bindings_story_id_stories"
        ),
        sa.PrimaryKeyConstraint("story_id", "provider_name", name="pk_story_provider_bindings"),
    )

    op.add_column("state_definitions", sa.Column("external_value_name", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_state_definitions_external_value_name",
        "state_definitions",
        "(owner = 'external') = (external_value_name IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_state_definitions_external_value_name", "state_definitions", type_="check"
    )
    op.drop_column("state_definitions", "external_value_name")
    op.drop_table("story_provider_bindings")
    op.drop_table("external_providers")
