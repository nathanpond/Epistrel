"""The versions of a background job's inputs, and the one recheck its commit runs (FR-CONC-8,
FR-STORE-27; #70).

A job declares every input it reads as ``"<table>:<id>"`` and records the version it saw;
``input_versions`` is ``{"<table>:<id>": version}``. At commit, under the story lock,
``stale_inputs`` reads every declared input again and names the ones that are no longer active
or no longer the same version — the job's output is then dropped and the job re-enqueued.

Versions by table (a reader is registered per table; later stories add theirs):

- ``operations:<operation_id>`` → ``[membership, membership_tx, record_version]``: the
  operation must still be ``active`` and its membership must not have moved (a truncate and an
  untruncate both move ``membership_tx``, so a discard-and-restore is a change too), and
  ``record_version`` is the latest active record part's ``tx_from`` over the operation's events
  (an amendment that corrected one of them without discarding it, #236; ``None``: none);
- ``stories:<story_id>`` → ``timeline_revision``;
- ``story_texts:<story_id>`` → #47's hash of the pinned memories and custom instructions.

An input that no longer exists reads as ``None`` and is always stale.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.concurrency import hash_member
from epistrel.core.enums import Membership
from epistrel.core.tables import amendment_parts, events, operations, stories, story_texts

Version = Any
"""A JSON value; compared for equality only."""

VersionReader = Callable[[AsyncConnection, str], Awaitable[Version | None]]
"""Reads the current version of the row with the given id (``None`` when it is gone)."""

ActiveCheck = Callable[[Version], bool]

BatchReader = Callable[[AsyncConnection, Sequence[str]], Awaitable[Mapping[str, Version]]]
"""Reads the current versions of several rows of one table in one statement (a gone row is
absent from the answer); the same versions the table's ``VersionReader`` answers (#452)."""

_READERS: dict[str, tuple[VersionReader, ActiveCheck | None]] = {}
_BATCH: dict[str, BatchReader] = {}


class UnknownInput(ValueError):
    """An input key names no registered table, or is not ``<table>:<id>``."""


def register_version_reader(
    table: str,
    reader: VersionReader,
    *,
    active: ActiveCheck | None = None,
    batch: BatchReader | None = None,
) -> None:
    """Register how to read the version of ``table``'s rows; ``active`` says whether a version
    is still active (an inactive input is stale even when unchanged); ``batch`` reads many of
    them in one statement (``read_versions``)."""
    _READERS[table] = (reader, active)
    if batch is None:
        _BATCH.pop(table, None)
    else:
        _BATCH[table] = batch


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


async def read_versions(conn: AsyncConnection, keys: Iterable[str]) -> dict[str, Version | None]:
    """The current version of each input of ``keys``, in their order (``None`` for a gone
    row): one statement per table that has a batch reader, one per key otherwise (#452: a
    consolidation window declares and rechecks dozens of operations and memories)."""
    wanted = list(dict.fromkeys(keys))
    by_table: dict[str, list[str]] = {}
    for key in wanted:
        table, row_id = _split(key)
        by_table.setdefault(table, []).append(row_id)
    found: dict[str, Version | None] = {}
    for table, ids in by_table.items():
        batch = _BATCH.get(table)
        if batch is not None and len(ids) > 1:
            versions = await batch(conn, ids)
            for row_id in ids:
                found[input_key(table, row_id)] = versions.get(row_id)
        else:
            reader, _ = _READERS[table]
            for row_id in ids:
                found[input_key(table, row_id)] = await reader(conn, row_id)
    return {key: found[key] for key in wanted}


async def stale_inputs(conn: AsyncConnection, declared: Mapping[str, Version]) -> tuple[str, ...]:
    """The declared inputs that are gone, inactive or changed since they were read (sorted)."""
    stale: list[str] = []
    current_versions = await read_versions(conn, sorted(declared))
    for key in sorted(declared):
        table, _ = _split(key)
        _, active = _READERS[table]
        current = current_versions[key]
        if current is None or current != declared[key] or (active and not active(current)):
            stale.append(key)
    return tuple(stale)


# -- the M2 readers ---------------------------------------------------------------------------


async def record_versions(conn: AsyncConnection, ids: Sequence[str]) -> dict[str, str]:
    """Per operation of ``ids``, the latest active record part's ``tx_from`` over its events
    (an operation no amendment corrected is absent; #236)."""
    p, e = amendment_parts.c, events.c
    found = await conn.execute(
        select(e.operation_id, func.max(p.tx_from))
        .select_from(
            amendment_parts.join(events, and_(p.record_table == "events", p.record_id == e.id))
        )
        .where(e.operation_id.in_(list(ids)), p.kind == "record", p.status == "active")
        .group_by(e.operation_id)
    )
    return {str(row[0]): row[1].isoformat() for row in found if row[1] is not None}


async def record_version(conn: AsyncConnection, operation_id: str) -> str | None:
    """``record_versions`` for one operation (``None``: no amendment corrected its events)."""
    return (await record_versions(conn, [operation_id])).get(operation_id)


async def _operation_version(conn: AsyncConnection, operation_id: str) -> Version | None:
    return (await _operation_versions(conn, [operation_id])).get(operation_id)


async def _operation_versions(conn: AsyncConnection, ids: Sequence[str]) -> dict[str, Version]:
    o = operations.c
    rows = await conn.execute(
        select(o.operation_id, o.membership, o.membership_tx).where(o.operation_id.in_(list(ids)))
    )
    found = list(rows)
    corrected = await record_versions(conn, [str(row.operation_id) for row in found])
    return {
        str(row.operation_id): [
            str(row.membership),
            row.membership_tx.isoformat() if row.membership_tx is not None else None,
            corrected.get(str(row.operation_id)),
        ]
        for row in found
    }


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


register_version_reader(
    operations.name, _operation_version, active=_operation_active, batch=_operation_versions
)
register_version_reader(stories.name, _story_version)
register_version_reader(story_texts.name, _texts_version)


__all__ = [
    "BatchReader",
    "UnknownInput",
    "Version",
    "VersionReader",
    "check_key",
    "input_key",
    "read_version",
    "read_versions",
    "record_version",
    "record_versions",
    "register_version_reader",
    "stale_inputs",
]
