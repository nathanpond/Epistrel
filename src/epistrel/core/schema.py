"""Canonical SQLAlchemy metadata for the Epistrel schema.

Every table the engine owns is declared against ``metadata`` so Alembic's autogenerate
comparison has one source of truth; the event store's tables live in ``core.tables``. The
naming convention gives every constraint a deterministic name, which keeps migrations
reproducible across databases.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Table, Text

from epistrel.core import tables
from epistrel.core.tables import NAMING_CONVENTION, metadata
from epistrel.core.tables.catalog import CATALOG_SCHEMA

__all__ = ["AUTOGENERATE_OPTS", "NAMING_CONVENTION", "metadata", "schema_meta", "tables"]

ALEMBIC_VERSION_TABLE = "alembic_version"

schema_meta = Table(
    "schema_meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)
"""Key/value bookkeeping written by migrations; ``engine_version`` is the package version."""


OWNED_SCHEMAS = frozenset({None, CATALOG_SCHEMA})
"""The schemas the engine's tables live in: the default schema (``None``) and ``catalog``
(#103). Autogenerate compares exactly these."""


RUNTIME_INDEXES = frozenset({"ix_memories_embedding_hnsw"})
"""Indexes the engine creates at run time, not in a migration: the HNSW index over
``memories.embedding``, built when the first embedding pins the dimension (#138,
``core.embeddings.retype``)."""
RUNTIME_TYPED_COLUMNS = frozenset({("memories", "embedding")})
"""Columns whose type the engine sets at run time: ``memories.embedding`` is the untyped
``vector`` until the first embedding retypes it to ``vector(d)`` (#138)."""


def include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Alembic ``include_object`` hook: ignore Alembic's own bookkeeping table and the indexes
    the engine creates at run time (``RUNTIME_INDEXES``)."""
    if type_ == "index" and name in RUNTIME_INDEXES:
        return False
    return not (type_ == "table" and name == ALEMBIC_VERSION_TABLE)


def compare_type(
    context: Any,
    inspected_column: Any,
    metadata_column: Any,
    inspected_type: Any,
    metadata_type: Any,
) -> bool | None:
    """Alembic ``compare_type`` hook: a column retyped at run time (``RUNTIME_TYPED_COLUMNS``)
    is never a difference; every other column uses Alembic's own comparison (``None``)."""
    table = getattr(metadata_column, "table", None)
    if table is not None and (table.name, metadata_column.name) in RUNTIME_TYPED_COLUMNS:
        return False
    return None


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Alembic ``include_name`` hook: compare only the schemas the engine owns."""
    if type_ == "schema":
        return name in OWNED_SCHEMAS
    return True


AUTOGENERATE_OPTS: dict[str, Any] = {
    "compare_type": compare_type,
    "compare_server_default": True,
    "include_schemas": True,
    "include_name": include_name,
    "include_object": include_object,
}
"""Comparison options shared by ``migrations/env.py`` and the drift tests."""
