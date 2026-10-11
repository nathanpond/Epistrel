"""The shared ``MetaData`` every Epistrel table is declared against, and column helpers."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CHAR, Enum, MetaData

from epistrel.core.ids import ULID_LENGTH

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def ulid_type() -> CHAR:
    """The column type of every ULID identifier."""
    return CHAR(ULID_LENGTH)


def pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """A native Postgres enum over ``enum_cls``'s values (created by the migration, not here)."""
    return Enum(
        *[member.value for member in enum_cls],
        name=name,
        native_enum=True,
        create_constraint=False,
        metadata=metadata,
    )
