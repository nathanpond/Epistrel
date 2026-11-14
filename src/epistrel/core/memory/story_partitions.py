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
  transaction, idempotently, under a transaction advisory lock on the story id (and, from
  the ``CREATE``, the creations' global ``CREATE_LOCK``, so concurrent creations never pick the
  same generated index names):
  ``CREATE TABLE … (LIKE memories INCLUDING ALL) WITH (fillfactor = 70)`` (the checks, the
  generated ``search`` column, the defaults and the indexes; a partitioned parent carries no
  storage parameters, so the partition keeps 30 % of each page free itself), ``ATTACH
  PARTITION`` (which clones the row trigger and the ``operations`` foreign key), and the
  ``memories_no_truncate`` statement trigger, since the parent's does not fire on a direct
  ``TRUNCATE`` of a partition.
- ``story_partition``: what a creating path wraps the story's transaction in. It creates the
  partition in a short transaction of its own, before the story's (``CREATE TABLE … PARTITION``
  DDL inside a long fork or import transaction would hold its locks until that commit), with
  ``lock_timeout`` 5 s and up to ``ATTEMPTS`` attempts, and settles it (#876, below), on one
  pooled connection that it returns before the story's work begins, so a creation holds no more
  than one connection at a time (#875). A failed creation leaves the empty partition behind for
  the sweep.
- ``claim``: the first statement of the transaction that writes the story's row. It takes the
  story's transaction advisory lock until that commit, so a drop or sweep (#865) skips the
  partition while the story's row is being written, and it makes the partition again in that
  transaction in the rare case a sweep dropped it between ``story_partition`` and the claim
  (it had no story row then). Until #875, a session advisory lock on a connection of its own
  covered that gap, and every creation held two pooled connections.
- ``settle``: Postgres can leave another backend routing ``memories`` with a partition
  descriptor that lacks a just-attached partition. The backend builds the descriptor from a
  ``pg_inherits`` read made before the attach committed, and absorbs the attach's
  invalidation in the middle of the build (at its first ``pg_class`` lock of the transaction, for
  a partition it had not looked up yet). It then stores the stale descriptor with no
  invalidation left to clear it, so its later transactions fail to insert the story's memories
  (``no partition of relation "memories" found for row``, 23514), and its reads of them prune
  to nothing, until the next attach. Only a transaction that held a lock on ``memories`` when
  the attach committed can have made that read. So the creator waits, up to
  ``SETTLE_TIMEOUT_S``, for every such transaction to end, then invalidates ``memories``'
  relcache entry once more with a no-op ``ALTER TABLE ONLY memories ALTER COLUMN story_id SET
  STATISTICS`` (its current value; SHARE UPDATE EXCLUSIVE, as ``ATTACH`` takes). Each backend
  drops any stale descriptor at its next transaction, before the story's row exists.
- ``drop_partition`` (#865): drops a deleted story's partition, after the deletion commits (the
  ``memory_partition_drop`` job ``core.stories.delete_story`` enqueues through
  ``schedule_drop``). Detaching or dropping a partition inside a transaction would hold
  ACCESS EXCLUSIVE on ``memories`` until the commit and stall every story's retrieval, so it
  runs on an autocommit connection, with ``lock_timeout`` 5 s: ``ALTER TABLE memories DETACH
  PARTITION … CONCURRENTLY`` (``… FINALIZE`` when an earlier detach was interrupted), then
  ``DROP TABLE``. It holds the story's session advisory lock throughout and does nothing while
  the story has a ``stories`` row or another session holds that lock (a creation's ``claim``).
  The detach's second phase waits for every transaction already open in the database: one
  open past the lock timeout fails the attempt and leaves the detach pending, and the next
  attempt (or the sweep) finalizes it. ``memories`` holds one pending detach at a time, so a
  drop finalizes any orphan's pending detach first.
- ``sweep_orphans`` (#865): the safety net, run hourly and once at a worker's start (the
  ``memory_partition_sweep`` task) under a global advisory lock. It drops every partition
  whose bound (``pg_get_expr(relpartbound)``: a hashed name cannot be reversed) names no
  ``stories`` row, finalizing a detach-pending one, and every detached ``memories_p_*`` table
  a crash between the detach and the drop left behind (unless its name is a live story's).
  A story whose advisory lock is held (a creation's ``claim``) is skipped. A live story's
  partition is never dropped.

Logs carry identifiers only (invariant 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.testing import pauses

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
SWEEP_LOCK = (0x4D454D53, 0)
"""The sweep's global advisory lock ("MEMS", 0): one sweep runs at a time across processes."""
SETTLE_TIMEOUT_S = 5.0
"""How long a creation waits for the transactions that held a lock on ``memories`` at its
attach to end (#876); past it, the creation logs a warning and invalidates all the same."""
SETTLE_POLL_S = 0.02
CREATE_LOCK = (0x4D454D43, 0)
"""The creations' global transaction advisory lock ("MEMC", 0), taken before ``CREATE TABLE``:
Postgres names a partition's indexes after the partition's name cut to 63 bytes, so two stories
whose ids share a long prefix (ULIDs made in the same millisecond) get the same index names, and
two creations at once collided on ``pg_class``'s unique name index (#784). One at a time, each
creation sees the other's names and numbers its own."""

DROPPED, ABSENT, LIVE, BUSY = "dropped", "absent", "live", "busy"
"""``drop_partition``'s outcomes: the table was dropped; there was none; the story has a
``stories`` row (nothing is dropped); another session holds the story's advisory lock."""

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


PARTITIONS_PER_TRANSACTION = 50
"""How many partitions ``count_rows`` reads in one transaction."""


async def attached(conn: AsyncConnection, table: str = PARENT) -> list[str]:
    """The partitions attached to ``table`` (a catalog read: it locks none of them), by name;
    empty for a table that is not partitioned."""
    return [
        str(n)
        for n in await conn.scalars(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = to_regclass(:t) ORDER BY c.relname"
            ),
            {"t": _ident(table)},
        )
    ]


async def count_rows(db: AsyncEngine, table: str, where: str = "TRUE") -> int:
    """The rows of public ``table`` matching the fixed SQL predicate ``where``. A partitioned
    table is counted ``PARTITIONS_PER_TRANSACTION`` partitions to a transaction: one statement
    over ``memories`` locks every story's partition and its indexes (16 locks a story) until it
    commits, which exhausts the shared lock table on a database of a thousand stories."""
    async with db.connect() as conn:
        partitions = await attached(conn, table)
        if not partitions:
            return int(
                await conn.scalar(text(f"SELECT count(*) FROM {_ident(table)} WHERE {where}"))  # noqa: S608 - fixed SQL
                or 0
            )
    total = 0
    for start in range(0, len(partitions), PARTITIONS_PER_TRANSACTION):
        chunk = partitions[start : start + PARTITIONS_PER_TRANSACTION]
        counts = " + ".join(
            f"(SELECT count(*) FROM {_ident(name)} WHERE {where})"  # noqa: S608 - fixed SQL
            for name in chunk
        )
        async with db.connect() as conn:
            total += int(await conn.scalar(text(f"SELECT {counts}")) or 0)
    return total


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
    await _create_attached(conn, story_id)
    return True


async def _create_attached(conn: AsyncConnection, story_id: str) -> None:
    await conn.execute(select(func.pg_advisory_xact_lock(*CREATE_LOCK)))
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
    await pauses.pause(pauses.MEMORY_PARTITION_ATTACHED)


async def claim(conn: AsyncConnection, story_id: str) -> None:
    """The first statement of the transaction that writes ``story_id``'s row (see the module
    docstring): lock the story's key until that commit, and make the partition again in this
    transaction when a sweep dropped it after ``story_partition`` made it. The check reads the
    catalog (``pg_partition_root``), not the transaction's snapshot, which a REPEATABLE READ
    transaction (a fork's) took before the lock was granted."""
    await conn.execute(select(func.pg_advisory_xact_lock(*_lock_key(story_id))))
    attached = await conn.scalar(
        text(
            "SELECT to_regclass(:c) IS NOT NULL "
            "AND pg_partition_root(to_regclass(:c)) = CAST(:p AS regclass)"
        ),
        {"c": _ident(partition_name(story_id)), "p": PARENT},
    )
    if not attached:
        logger.warning("memories partition of %s: gone before its story; made again", story_id)
        await _create_attached(conn, story_id)


def _lock_timed_out(error: BaseException) -> bool:
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    return isinstance(error, DBAPIError) and sqlstate == LOCK_NOT_AVAILABLE


async def _in_own_transaction(
    conn: AsyncConnection, story_id: str, step: Callable[[AsyncConnection], Awaitable[bool]]
) -> bool:
    """``step`` in a transaction of its own with ``lock_timeout`` set, up to ``ATTEMPTS``
    attempts on a lock timeout; its answer."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                return await step(conn)
        except DBAPIError as exc:
            if not _lock_timed_out(exc) or attempt == ATTEMPTS:
                raise
            logger.warning("memories partition of %s: lock timeout (attempt %d)", story_id, attempt)
    raise AssertionError("unreachable")  # pragma: no cover - the last attempt raises


_LOCKERS = text(
    # The other transactions holding (or awaiting) a lock on ``memories``, by virtual
    # transaction id; ``pg_locks`` lists fast-path locks too.
    "SELECT DISTINCT l.virtualtransaction FROM pg_locks l "
    "WHERE l.locktype = 'relation' AND l.relation = CAST(:p AS regclass) "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND l.pid IS DISTINCT FROM pg_backend_pid()"
)


async def _lockers(conn: AsyncConnection) -> set[str]:
    async with conn.begin():
        return {str(v) for v in await conn.scalars(_LOCKERS, {"p": PARENT})}


async def _invalidate(conn: AsyncConnection) -> bool:
    """A no-op ``ALTER`` of ``memories`` (its ``story_id`` statistics target, set to what it
    is): every backend rebuilds ``memories``' relcache entry, partition descriptor included."""
    target = await conn.scalar(
        text(
            "SELECT attstattarget FROM pg_attribute "
            "WHERE attrelid = CAST(:p AS regclass) AND attname = 'story_id'"
        ),
        {"p": PARENT},
    )
    value = -1 if target is None else int(target)
    await conn.execute(
        text(f"ALTER TABLE ONLY {PARENT} ALTER COLUMN story_id SET STATISTICS {value}")
    )
    return True


async def settle(conn: AsyncConnection, story_id: str) -> None:
    """Settle ``story_id``'s just-committed partition (see the module docstring): wait for the
    transactions holding a lock on ``memories`` to end, then invalidate ``memories`` once more.
    ``conn`` is outside a transaction."""
    waiting = await _lockers(conn)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_TIMEOUT_S
    while waiting:
        if loop.time() >= deadline:
            logger.warning(
                "memories partition of %s: %d transaction(s) still open after %.0f s",
                story_id,
                len(waiting),
                SETTLE_TIMEOUT_S,
            )
            break
        await asyncio.sleep(SETTLE_POLL_S)
        waiting &= await _lockers(conn)
    await _in_own_transaction(conn, story_id, _invalidate)


async def _create_in_own_transaction(conn: AsyncConnection, story_id: str) -> None:
    async def create(c: AsyncConnection) -> bool:
        return await create_partition(c, story_id)

    if await _in_own_transaction(conn, story_id, create):
        await settle(conn, story_id)


@asynccontextmanager
async def story_partition(db: AsyncEngine | AsyncConnection, story_id: str) -> AsyncIterator[None]:
    """Create and settle ``story_id``'s partition before the story's transaction (see the
    module docstring); the story's transaction starts with ``claim``.

    ``db`` an engine: a connection of its own, returned to the pool before the block runs.
    ``db`` a connection outside a transaction: that connection. ``db`` a connection inside a
    transaction: the partition is created in that transaction (not settled)."""
    if isinstance(db, AsyncConnection) and db.in_transaction():
        await create_partition(db, story_id)
        yield
        return
    if isinstance(db, AsyncConnection):
        await _create_in_own_transaction(db, story_id)
    else:
        async with db.connect() as conn:
            await _create_in_own_transaction(conn, story_id)
    yield


# -- dropping (#865) --------------------------------------------------------------------------

DropScheduler = Callable[[AsyncConnection, str], Awaitable[None]]
"""``schedule(conn, story_id)``: enqueue the drop of ``story_id``'s partition in ``conn``'s
transaction (the Orchestrator's ``memory_partition_drop`` job), so it runs after the commit."""

_SCHEDULER: list[DropScheduler] = []


def set_drop_scheduler(scheduler: DropScheduler | None) -> None:
    """Make ``scheduler`` the one ``schedule_drop`` calls (``None``: nothing is scheduled, and
    the sweep drops the partition)."""
    _SCHEDULER.clear()
    if scheduler is not None:
        _SCHEDULER.append(scheduler)


async def schedule_drop(conn: AsyncConnection, story_id: str) -> bool:
    """Enqueue the drop of ``story_id``'s partition in ``conn``'s transaction; ``False`` when
    no scheduler is bound (the sweep drops it later)."""
    if not _SCHEDULER:
        return False
    await _SCHEDULER[0](conn, story_id)
    return True


@asynccontextmanager
async def _autocommit(db: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A pooled connection in autocommit (``DETACH … CONCURRENTLY`` runs outside a transaction
    block) with ``lock_timeout`` set; one that fails is invalidated, never pooled with a session
    advisory lock or the timeout still set."""
    async with db.connect() as pooled:
        conn = await pooled.execution_options(isolation_level="AUTOCOMMIT")
        try:
            await conn.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
            yield conn
            await conn.execute(text("RESET lock_timeout"))
        except BaseException:
            await conn.invalidate()
            raise


async def _attached(conn: AsyncConnection, table: str) -> bool | None:
    """``None``: ``table`` is not a partition of ``memories``; otherwise whether its detach is
    pending."""
    row = (
        await conn.execute(
            text(
                "SELECT i.inhdetachpending FROM pg_inherits i "
                "WHERE i.inhparent = CAST(:p AS regclass) AND i.inhrelid = to_regclass(:c)"
            ),
            {"p": PARENT, "c": _ident(table)},
        )
    ).first()
    return None if row is None else bool(row[0])


async def _drop(conn: AsyncConnection, story_id: str, table: str) -> str:
    """Drop ``table``, ``story_id``'s partition, on ``conn`` (autocommit) unless the story
    lives or its advisory lock is held elsewhere (see the module docstring)."""
    key = _lock_key(story_id)
    if not await conn.scalar(select(func.pg_try_advisory_lock(*key))):
        return BUSY
    try:
        live = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM stories WHERE id = :s)"), {"s": story_id.rstrip()}
        )
        if live:
            return LIVE
        pending = await _attached(conn, table)
        name = _ident(table)
        if pending is not None:
            mode = "FINALIZE" if pending else "CONCURRENTLY"
            await conn.execute(text(f"ALTER TABLE {PARENT} DETACH PARTITION {name} {mode}"))
            await pauses.pause(pauses.MEMORY_PARTITION_DETACHED)
        elif await conn.scalar(text("SELECT to_regclass(:c)"), {"c": name}) is None:
            return ABSENT
        await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        logger.info("memories partition of %s dropped", story_id)
        return DROPPED
    finally:
        await conn.execute(select(func.pg_advisory_unlock(*key)))


async def drop_partition(db: AsyncEngine, story_id: str) -> str:
    """Detach and drop the partition of ``story_id``, a deleted story (see the module
    docstring); one of ``DROPPED``, ``ABSENT``, ``LIVE`` or ``BUSY``. ``memories`` holds one
    pending detach at a time, so an orphan's detach an earlier run left pending is finalized and
    dropped first. A lock timeout raises (SQLSTATE 55P03), and the job retries."""
    async with _autocommit(db) as conn:
        for row in await _orphans(conn, pending_only=True):
            await _drop(conn, str(row.story_id), str(row.relname))
        return await _drop(conn, story_id, partition_name(story_id))


_ORPHANS = (
    # A partition's bound is ``FOR VALUES IN ('<story id>')`` (quotes doubled); the anti-join
    # compares as ``bpchar``, so it reads the ``stories`` key. A pending detach comes first.
    "SELECT o.relname, o.story_id, o.pending FROM (SELECT c.relname, "
    "i.inhdetachpending AS pending, replace(substring(pg_get_expr(c.relpartbound, c.oid) "
    "FROM '^FOR VALUES IN \\(''(.*)''\\)$'), '''''', '''') AS story_id "
    "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
    "WHERE i.inhparent = CAST(:p AS regclass){only}) o "
    "WHERE o.story_id IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM stories s WHERE s.id = CAST(o.story_id AS bpchar)) "
    "ORDER BY o.pending DESC, o.relname"
)


async def _orphans(conn: AsyncConnection, *, pending_only: bool = False) -> list[Any]:
    """The partitions whose bound names no ``stories`` row (``pending_only``: those whose
    detach is pending), pending ones first."""
    only = " AND i.inhdetachpending" if pending_only else ""
    return list((await conn.execute(text(_ORPHANS.format(only=only)), {"p": PARENT})).all())


_LEFTOVERS = text(
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'r' AND NOT c.relispartition AND n.nspname = current_schema() "
    "AND starts_with(c.relname, :prefix) ORDER BY c.relname"
)


async def _live_name(conn: AsyncConnection, table: str) -> bool:
    """Whether ``table`` is the partition name of a story that has a ``stories`` row."""
    suffix = table.removeprefix(PREFIX)
    found = await conn.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM stories WHERE lower(rtrim(id)) = :s "
            "OR left(encode(sha256(convert_to(rtrim(id), 'UTF8')), 'hex'), 16) = :s)"
        ),
        {"s": suffix},
    )
    return bool(found)


async def sweep_orphans(db: AsyncEngine) -> dict[str, int]:
    """Drop every orphan partition and detached leftover (see the module docstring); counts
    only. ``{"locked": 1}`` when another process is sweeping."""
    counts = {"dropped": 0, "leftovers": 0, "busy": 0, "failed": 0}
    async with _autocommit(db) as conn:
        if not await conn.scalar(select(func.pg_try_advisory_lock(*SWEEP_LOCK))):
            return {"locked": 1}
        try:
            for row in await _orphans(conn):
                try:
                    outcome = await _drop(conn, str(row.story_id), str(row.relname))
                except DBAPIError as exc:
                    # A lock timeout, or a pending detach this run could not finalize: the next
                    # run tries again.
                    logger.warning(
                        "memories sweep: %s not dropped: %s", row.relname, type(exc).__name__
                    )
                    counts["failed"] += 1
                    continue
                if outcome == DROPPED:
                    counts["dropped"] += 1
                elif outcome == BUSY:
                    counts["busy"] += 1
            for table in (await conn.scalars(_LEFTOVERS, {"prefix": PREFIX})).all():
                if await _live_name(conn, str(table)):
                    logger.warning("detached memories table %s names a live story", table)
                    continue
                await conn.execute(text(f"DROP TABLE IF EXISTS {_ident(str(table))}"))
                counts["leftovers"] += 1
        finally:
            await conn.execute(select(func.pg_advisory_unlock(*SWEEP_LOCK)))
    if counts["dropped"] or counts["leftovers"]:
        logger.info(
            "memories sweep: %d partition(s), %d leftover(s) dropped",
            counts["dropped"],
            counts["leftovers"],
        )
    return counts


__all__ = [
    "ABSENT",
    "ATTEMPTS",
    "BUSY",
    "CREATE_LOCK",
    "DROPPED",
    "FILLFACTOR",
    "LIVE",
    "LOCK_NAMESPACE",
    "PARTITIONS_PER_TRANSACTION",
    "PREFIX",
    "SETTLE_TIMEOUT_S",
    "SWEEP_LOCK",
    "DropScheduler",
    "attached",
    "claim",
    "count_rows",
    "create_partition",
    "drop_partition",
    "has_partition",
    "not_a_partition",
    "partition_name",
    "schedule_drop",
    "set_drop_scheduler",
    "settle",
    "story_partition",
    "sweep_orphans",
]
