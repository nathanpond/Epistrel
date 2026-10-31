"""``external_providers`` and ``story_provider_bindings``: external-data providers and each
story's ids at them (FR-EXT-1,6,7; FR-STATE-2; #204, migration 0086).

- **``external_providers``** — one row per provider the operator registered, deployment-wide
  (``/admin/external-providers``): its ``name``, ``trust_tier`` (``authoritative`` or
  ``advisory``; #207 reads it), ``transport`` (``http``: a declarative manifest the Orchestrator
  executes by template substitution; ``in_process``: callables a library consumer registers per
  process), the ``manifest`` (the tracked values it owns and, over ``http``, each value's read
  and write request templates; secret *references* only, never a secret), whether it is
  ``write_capable`` and, if so, its ``confirmation`` mode, whether it ``dedupes_dispatch`` and
  ``returns_resulting_values``; its ``unavailability`` policy (FR-EXT-7) with its
  ``freshness_minutes`` window, ``confirm_wait_max_seconds`` (FR-EXT-15's wait budget), the
  deployment ``config_revision`` of its last change and when it was created and last updated.
  Every write is a deployment configuration change (``external_provider:<name>[.<field>]``).
  Not story-scoped and holds no story content.
- **``story_provider_bindings``** — one row per (story, provider): the story's ``external_ids``
  (the game's ids for this story's player, substituted into every call as ``{binding.<key>}``)
  and when it was bound. Story-scoped: story deletion empties it and a fork copies it (the fork
  reads the same player's values).

``state_definitions.provider_id`` holds a provider's ``name`` with no foreign key: deleting a
provider a story's State names is refused (409 ``provider_in_use``).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

TRUST_TIERS = ("authoritative", "advisory")
TRANSPORTS = ("http", "in_process")
CONFIRMATIONS = ("synchronous", "asynchronous")
UNAVAILABILITY = ("fail_turn", "last_known", "unknown")
DEFAULT_CONFIRM_WAIT_MAX_SECONDS = 10


def _one_of(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


external_providers = Table(
    "external_providers",
    metadata,
    Column("name", Text, nullable=False),
    Column("trust_tier", Text, nullable=False),
    Column("transport", Text, nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("write_capable", Boolean, nullable=False, server_default=text("false")),
    Column("confirmation", Text, nullable=True),
    Column("dedupes_dispatch", Boolean, nullable=True),
    Column("returns_resulting_values", Boolean, nullable=True),
    Column("unavailability", Text, nullable=False, server_default=text("'last_known'")),
    Column("freshness_minutes", Integer, nullable=True),
    Column(
        "confirm_wait_max_seconds",
        Integer,
        nullable=False,
        server_default=text(str(DEFAULT_CONFIRM_WAIT_MAX_SECONDS)),
    ),
    Column("config_revision", BigInteger, nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column(
        "updated_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    PrimaryKeyConstraint("name"),
    CheckConstraint("name ~ '^[a-z][a-z0-9_.-]{0,63}$'", name="name"),
    CheckConstraint(_one_of("trust_tier", TRUST_TIERS), name="trust_tier"),
    CheckConstraint(_one_of("transport", TRANSPORTS), name="transport"),
    CheckConstraint(
        f"confirmation IS NULL OR {_one_of('confirmation', CONFIRMATIONS)}", name="confirmation"
    ),
    CheckConstraint(_one_of("unavailability", UNAVAILABILITY), name="unavailability"),
    CheckConstraint("jsonb_typeof(manifest) = 'object'", name="manifest"),
    CheckConstraint("freshness_minutes IS NULL OR freshness_minutes > 0", name="freshness_minutes"),
    CheckConstraint("confirm_wait_max_seconds > 0", name="confirm_wait_max_seconds"),
)

story_provider_bindings = Table(
    "story_provider_bindings",
    metadata,
    Column("story_id", ulid_type(), ForeignKey("stories.id"), nullable=False),
    Column("provider_name", Text, nullable=False),
    Column("external_ids", JSONB, nullable=False),
    Column(
        "bound_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    PrimaryKeyConstraint("story_id", "provider_name"),
    CheckConstraint("jsonb_typeof(external_ids) = 'object'", name="external_ids"),
)
