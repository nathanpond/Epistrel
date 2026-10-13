"""Deployment-scoped configuration: ``deployment_state`` and the ``config_changes`` log.

``deployment_state`` has exactly one row (``id = 1``): the deployment's ``config_revision``
(FR-CONC-14), the default rating for a story created without one, ``minor_age_threshold``
(docs/02 §4.12), a ``settings`` document later stories extend (#64), and ``media_store``: the
blob store kind (``local`` or ``s3``) the deployment's media live in, recorded at the first
start-up and compared at every later one, so switching stores with media in place is refused
(#107).

``config_changes`` records every configuration write with its scope, the revision it produced,
the setting, the old and new values, the change class (FR-CONC-15: ``interpretive`` or
``prospective``) and the acting principal (docs/02, "Configuration revisions").
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

DEPLOYMENT_ROW_ID = 1
MEDIA_STORE_KINDS = ("local", "s3")
"""The blob store kinds ``EPISTREL_MEDIA_STORE`` names (#107)."""
MEDIA_STORE_CHECK = "media_store IN ('local', 's3')"

deployment_state = Table(
    "deployment_state",
    metadata,
    Column("id", SmallInteger, primary_key=True, server_default=text(str(DEPLOYMENT_ROW_ID))),
    Column("config_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("default_content_rating_id", ulid_type(), nullable=False),
    Column("minor_age_threshold", Integer, nullable=False, server_default=text("18")),
    Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("media_store", Text, nullable=True),
    CheckConstraint(f"id = {DEPLOYMENT_ROW_ID}", name="single_row"),
    CheckConstraint(MEDIA_STORE_CHECK, name="media_store"),
)

config_changes = Table(
    "config_changes",
    metadata,
    Column("change_id", ulid_type(), primary_key=True),
    Column("scope", Text, nullable=False),
    Column("scope_id", Text, nullable=True),
    Column("config_revision", BigInteger, nullable=False),
    Column("setting", Text, nullable=False),
    Column("old", JSONB, nullable=True),
    Column("new", JSONB, nullable=True),
    Column("class", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column(
        "at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    CheckConstraint("class IN ('interpretive', 'prospective')", name="change_class"),
    Index("ix_config_changes_scope_revision", "scope", "scope_id", "config_revision"),
)
