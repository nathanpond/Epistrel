"""One ``memories`` partition per story (ADR-187; #784, migration 0151).

``memories`` is LIST-partitioned by ``story_id`` with no default partition, so a story's
memories live in a table of their own (``memories_p_<story>``) with its own indexes, the HNSW
graph included: a story's vector search walks only its own vectors. A memory written for a
story with no partition fails (SQLSTATE 23514): that is a bug, never a user error, so every
path that creates a story creates its partition first.

- ``partition_name``: the partition's table name, ``memories_p_`` + the lowercased,
  right-trimmed story id when it is 1..26 characters of ``[0-9a-z]`` (every ULID), otherwise
  ``memories_p_`` + the first 16 hex digits of its SHA-256 (tests pass arbitrary ids).
- ``create_partition``: creates and attaches the story's partition in the caller's
  transaction, idempotently, under a transaction advisory lock on the story id:
  ``CREATE TABLE … (LIKE memories INCLUDING ALL) WITH (fillfactor = 70)`` (the checks, the
  generated ``search`` column, the defaults and the indexes; a partitioned parent carries no
  storage parameters, so the partition keeps 30 % of each page free itself), ``ATTACH
  PARTITION`` (which clones the row trigger and the ``operations`` foreign key), and the
  ``memories_no_truncate`` statement trigger, since the parent's does not fire on a direct
  ``TRUNCATE`` of a partition.
- ``story_partition``: what a creating path wraps the story's transaction in. It creates the
  partition in a short transaction of its own, before the story's (``CREATE TABLE … PARTITION``
  DDL inside a long fork or import transaction would hold its locks until that commit), with
  ``lock_timeout`` 5 s and up to ``ATTEMPTS`` attempts, and holds a session advisory lock on
  the story id until the story's transaction is done, so a sweep of empty partitions with no
  story row (#865) never drops one a creation is about to fill. A failed creation leaves the
  empty partition behind for that sweep.

Logs carry identifiers only (invariant 3).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger(__name__)

PARENT = "memories"
PREFIX = "memories_p_"
"""Every partition's name starts with it (``core.schema.include_object`` ignores them)."""
FILLFACTOR = 70
"""The partitions' ``fillfactor`` (migration 0067's, #452): a retrieval's touch stays a
heap-only update."""
LOCK_NAMESPACE = 0x4D454D50
"""The advisory locks' first key ("MEMP"); the second is ``hashtext(story_id)``."""
LOCK_TIMEOUT = "5s"
"""How long one attempt waits for a lock (``ATTACH`` takes ``memories``' SHARE UPDATE
EXCLUSIVE and ``operations``' SHARE ROW EXCLUSIVE briefly)."""
ATTEMPTS = 3
LOCK_NOT_AVAILABLE = "55P03"

_PLAIN = re.compile(r"[0-9a-z]{1,26}")


def partition_name(story_id: str) -> str:
    """The table name of ``story_id``'s partition (see the module docstring)."""
    trimmed = story_id.rstrip().lower()
    if _PLAIN.fullmatch(trimmed):
        return PREFIX + trimmed
    return PREFIX + hashlib.sha256(story_id.rstrip().encode()).hexdigest()[:16]


def not_a_partition(table: str = "table_name", schema: str = "table_schema") -> str:
    """A SQL predicate over an ``information_schema`` row (its ``table`` and ``schema`` column
    expressions): the table is not a partition. A catalog scan that enumerates tables reads
    ``memories`` through its parent, never once per story's partition."""
    return (
        "NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class pc JOIN pg_catalog.pg_namespace pn "  # noqa: S608 - a fixed catalog predicate
        f"ON pn.oid = pc.relnamespace WHERE pc.relispartition AND pc.relname = {table} "
        f"AND pn.nspname = {schema})"
    )


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _lock_key(story_id: str) -> tuple[object, object]:
    return LOCK_NAMESPACE, func.hashtext(story_id.rstrip())


async def has_partition(conn: AsyncConnection, story_id: str) -> bool:
    """Whether ``story_id``'s partition exists and is attached to ``memories``."""
    found = await conn.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhparent = CAST(:p AS regclass) "
            "AND i.inhrelid = to_regclass(:c))"
        ),
        {"p": PARENT, "c": _ident(partition_name(story_id))},
    )
    return bool(found)


async def create_partition(conn: AsyncConnection, story_id: str) -> bool:
    """Create and attach ``story_id``'s partition in ``conn``'s transaction unless it exists;
    answer whether it was created. The caller commits."""
    await conn.execute(select(func.pg_advisory_xact_lock(*_lock_key(story_id))))
    if await has_partition(conn, story_id):
        return False
    name = _ident(partition_name(story_id))
    await conn.execute(
        text(f"CREATE TABLE {name} (LIKE {PARENT} INCLUDING ALL) WITH (fillfactor = {FILLFACTOR})")
    )
    await conn.execute(
        text(
            f"ALTER TABLE {PARENT} ATTACH PARTITION {name} "
            f"FOR VALUES IN ({_literal(story_id.rstrip())})"
        )
    )
    await conn.execute(
        text(
            f"CREATE TRIGGER memories_no_truncate BEFORE TRUNCATE ON {name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_memories_guard()"
        )
    )
    return True


def _lock_timed_out(error: BaseException) -> bool:
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    return isinstance(error, DBAPIError) and sqlstate == LOCK_NOT_AVAILABLE


async def _create_in_own_transaction(conn: AsyncConnection, story_id: str) -> None:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                await create_partition(conn, story_id)
            return
        except DBAPIError as exc:
            if not _lock_timed_out(exc) or attempt == ATTEMPTS:
                raise
            logger.warning("memories partition of %s: lock timeout (attempt %d)", story_id, attempt)


@asynccontextmanager
async def story_partition(db: AsyncEngine | AsyncConnection, story_id: str) -> AsyncIterator[None]:
    """Create ``story_id``'s partition before the story's transaction (see the module
    docstring) and hold the creator's session advisory lock until the block ends.

    ``db`` an engine: a connection of its own holds the lock. ``db`` a connection outside a
    transaction: that connection does. ``db`` a connection inside a transaction: the partition
    is created in that transaction, whose advisory lock is held to its commit."""
    if isinstance(db, AsyncConnection) and db.in_transaction():
        await create_partition(db, story_id)
        yield
        return
    if isinstance(db, AsyncConnection):
        async with _held(db, story_id):
            yield
        return
    async with db.connect() as conn, _held(conn, story_id):
        yield


@asynccontextmanager
async def _held(conn: AsyncConnection, story_id: str) -> AsyncIterator[None]:
    await conn.execute(select(func.pg_advisory_lock(*_lock_key(story_id))))
    await conn.commit()
    try:
        await _create_in_own_transaction(conn, story_id)
        yield
    finally:
        try:
            if conn.in_transaction():
                await conn.rollback()
            await conn.execute(select(func.pg_advisory_unlock(*_lock_key(story_id))))
            await conn.commit()
        except Exception:
            # A connection that may still hold the session lock never goes back to the pool.
            await conn.invalidate()


__all__ = [
    "ATTEMPTS",
    "FILLFACTOR",
    "LOCK_NAMESPACE",
    "PREFIX",
    "create_partition",
    "has_partition",
    "not_a_partition",
    "partition_name",
    "story_partition",
]
