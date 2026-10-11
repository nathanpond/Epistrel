"""The versions of a background job's inputs, and the one recheck its commit runs (FR-CONC-8,
FR-STORE-27; #70).

A job declares every input it reads as ``"<table>:<id>"`` and records the version it saw;
``input_versions`` is ``{"<table>:<id>": version}``. At commit, under the story lock,
``stale_inputs`` reads every declared input again and names the ones that are no longer active
or no longer the same version — the job's output is then dropped and the job re-enqueued.

Versions by table (a reader is registered per table; later stories add theirs):

- ``operations:<operation_id>`` → ``[membership, membership_tx]``: the operation must still be
  ``active`` and its membership must not have moved (a truncate and an untruncate both move
  ``membership_tx``, so a discard-and-restore is a change too);
- ``stories:<story_id>`` → ``timeline_revision``;
- ``story_texts:<story_id>`` → #47's hash of the pinned memories and custom instructions.

An input that no longer exists reads as ``None`` and is always stale.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.concurrency import hash_member
from epistrel.core.enums import Membership
from epistrel.core.tables import operations, stories, story_texts

Version = Any
"""A JSON value; compared for equality only."""

VersionReader = Callable[[AsyncConnection, str], Awaitable[Version | None]]
"""Reads the current version of the row with the given id (``None`` when it is gone)."""

ActiveCheck = Callable[[Version], bool]

_READERS: dict[str, tuple[VersionReader, ActiveCheck | None]] = {}


class UnknownInput(ValueError):
    """An input key names no registered table, or is not ``<table>:<id>``."""


def register_version_reader(
    table: str, reader: VersionReader, *, active: ActiveCheck | None = None
) -> None:
    """Register how to read the version of ``table``'s rows; ``active`` says whether a version
    is still active (an inactive input is stale even when unchanged)."""
    _READERS[table] = (reader, active)


def input_key(table: str, row_id: str) -> str:
    return f"{table}:{row_id}"


def _split(key: str) -> tuple[str, str]:
    table, sep, row_id = key.partition(":")
    if not sep or not row_id or table not in _READERS:
        raise UnknownInput(f"input {key!r} is not <table>:<id> of a registered table")
    return table, row_id


def check_key(key: str) -> None:
    """Raise ``UnknownInput`` unless ``key`` can be read back."""
    _split(key)


async def read_version(conn: AsyncConnection, key: str) -> Version | None:
    """The current version of input ``key`` (``None`` when the row is gone)."""
    table, row_id = _split(key)
    reader, _ = _READERS[table]
    return await reader(conn, row_id)


async def stale_inputs(conn: AsyncConnection, declared: Mapping[str, Version]) -> tuple[str, ...]:
    """The declared inputs that are gone, inactive or changed since they were read (sorted)."""
    stale: list[str] = []
    for key in sorted(declared):
        table, _ = _split(key)
        _, active = _READERS[table]
        current = await read_version(conn, key)
        if current is None or current != declared[key] or (active and not active(current)):
            stale.append(key)
    return tuple(stale)


# -- the M2 readers ---------------------------------------------------------------------------


async def _operation_version(conn: AsyncConnection, operation_id: str) -> Version | None:
    row = (
        await conn.execute(
            select(operations.c.membership, operations.c.membership_tx).where(
                operations.c.operation_id == operation_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    tx = row.membership_tx.isoformat() if row.membership_tx is not None else None
    return [str(row.membership), tx]


def _operation_active(version: Version) -> bool:
    return bool(version) and version[0] == Membership.ACTIVE.value


async def _story_version(conn: AsyncConnection, story_id: str) -> Version | None:
    found = await conn.scalar(select(stories.c.timeline_revision).where(stories.c.id == story_id))
    return int(found) if found is not None else None


async def _texts_version(conn: AsyncConnection, story_id: str) -> Version | None:
    row = (
        await conn.execute(
            select(story_texts.c.pinned, story_texts.c.custom_instructions).where(
                story_texts.c.story_id == story_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return hash_member([row.pinned, row.custom_instructions])


register_version_reader(operations.name, _operation_version, active=_operation_active)
register_version_reader(stories.name, _story_version)
register_version_reader(story_texts.name, _texts_version)


__all__ = [
    "UnknownInput",
    "Version",
    "VersionReader",
    "check_key",
    "input_key",
    "read_version",
    "register_version_reader",
    "stale_inputs",
]
