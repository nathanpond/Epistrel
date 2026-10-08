"""Canonical SQLAlchemy metadata for the Epistrel schema.

Every table the engine owns is declared against ``metadata`` so Alembic's autogenerate
comparison has one source of truth. The naming convention gives every constraint a
deterministic name, which keeps migrations reproducible across databases.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, MetaData, Table, Text

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ALEMBIC_VERSION_TABLE = "alembic_version"

metadata = MetaData(naming_convention=NAMING_CONVENTION)

schema_meta = Table(
    "schema_meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)
"""Key/value bookkeeping written by migrations; ``engine_version`` is the package version."""


def include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Alembic ``include_object`` hook: ignore Alembic's own bookkeeping table."""
    return not (type_ == "table" and name == ALEMBIC_VERSION_TABLE)


AUTOGENERATE_OPTS: dict[str, Any] = {
    "compare_type": True,
    "compare_server_default": True,
    "include_object": include_object,
}
"""Comparison options shared by ``migrations/env.py`` and the drift tests."""
