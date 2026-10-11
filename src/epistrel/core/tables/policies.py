"""The policy store's table (FR-GUARD-1,8; NFR-SEC-1).

One row per policy, edited in place: every edit increments ``version`` and records a
``config_changes`` row with the old and new content. Retiring a policy sets ``retired_at``,
and a retired policy drops out of every effective set. ``story_id`` is set for story and
character scopes so that story deletion (#50) and fork (#67) find them. ``scope_id`` has no
constraint: the API checks that it exists.

The only code that writes this table is ``epistrel.orchestrator.policies.admin`` (import-linter
contract ``policy-store-written-only-by-admin``). Deterministic constraints back that up:
``effect`` can only be ``deny``, and ``applies_to`` must be a non-empty subset of the four
targets.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type

policies = Table(
    "policies",
    metadata,
    Column("id", Text, primary_key=True),
    Column("scope", Text, nullable=False),
    Column("scope_id", Text, nullable=True),
    Column("story_id", ulid_type(), nullable=True),
    Column("name", Text, nullable=False),
    Column("applies_to", ARRAY(Text), nullable=False),
    Column("kind", Text, nullable=False),
    Column("effect", Text, nullable=False, server_default=text("'deny'")),
    Column("rule", JSONB, nullable=False),
    Column("age_condition", Text, nullable=True),
    Column("message", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("updated_by", Text, nullable=False),
    Column("version", Integer, nullable=False, server_default=text("1")),
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
    Column("retired_at", TIMESTAMP(timezone=True), nullable=True),
    Column("retired_by", Text, nullable=True),
    Column("idempotency_key", Text, nullable=True),
    CheckConstraint("scope IN ('global', 'rating', 'story', 'character')", name="scope"),
    CheckConstraint("(scope = 'global') = (scope_id IS NULL)", name="scope_id"),
    CheckConstraint("(scope IN ('story', 'character')) = (story_id IS NOT NULL)", name="story_id"),
    CheckConstraint("kind IN ('deterministic', 'model_assisted')", name="kind"),
    CheckConstraint("effect = 'deny'", name="effect"),
    CheckConstraint(
        "cardinality(applies_to) > 0 AND applies_to <@ "
        "ARRAY['text_input', 'text_output', 'image_prompt', 'image_output']::text[]",
        name="applies_to",
    ),
)

# Names are unique among live policies of one scope, ignoring case.
Index(
    "uq_policies_live_name",
    policies.c.scope,
    func.coalesce(policies.c.scope_id, ""),
    func.lower(policies.c.name),
    unique=True,
    postgresql_where=text("retired_at IS NULL"),
)
Index("ix_policies_story_id", policies.c.story_id)
Index(
    "uq_policies_idempotency_key",
    policies.c.idempotency_key,
    unique=True,
    postgresql_where=text("idempotency_key IS NOT NULL"),
)
