"""``model_config``: per-purpose model configuration (FR-MODEL-1,3).

One row per configured purpose plus the ``default`` row every unconfigured field falls back to.
``api_key_ciphertext`` holds the provider key encrypted with ``EPISTREL_CONFIG_KEY`` (Fernet);
plaintext is never stored. The Orchestrator's Model Access Layer is the only reader and writer;
the table is declared here so the schema has one ``metadata``.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, Column, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata

DEFAULT_PURPOSE_KEY = "default"

model_config = Table(
    "model_config",
    metadata,
    Column("purpose", Text, primary_key=True),
    Column("base_url", Text, nullable=True),
    Column("model", Text, nullable=True),
    Column("api_key_ciphertext", Text, nullable=True),
    Column("params", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column(
        "updated_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("updated_by", Text, nullable=False, server_default=text("'system'")),
)
