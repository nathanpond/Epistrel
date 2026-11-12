"""memories: LIST-partitioned by story, one partition per story (#784, ADR-187)

``memories`` had one global HNSW index: a story's vector search walked a graph shared by every
story and filtered to its own rows, so thousands of other stories' identical vectors could
starve the iterative scan and return none of the story's vectors at any
``hnsw.max_scan_tuples``. Each story now has a partition of its own (``memories_p_<story>``,
``core.memory.story_partitions``) with its own indexes, the HNSW graph included.

Upgrade (offline: run it with the engine stopped; it takes time proportional to the number of
memories):

1. The old table becomes ``memories_old``; its primary key and indexes are renamed ``*_old``.
2. A new ``memories`` is created ``PARTITION BY LIST (story_id)`` with the old table's columns
   in their order and types (``embedding`` stays ``vector(d)`` where the deployment is pinned),
   its checks, defaults and generated ``search`` column, the primary key ``(id, story_id)``
   (Postgres cannot enforce a partitioned table's uniqueness without the partition key; memory
   ids stay globally unique by ULID generation), the old table's indexes under their own names
   and definitions (``ix_memories_embedding_hnsw`` exactly where ``core.embeddings.retype``
   built it, with the parameters it was built with), the ``operations`` foreign key and the
   two triggers. No default partition. A partitioned parent carries no storage parameters, so
   each partition carries ``fillfactor = 70`` (0067).
3. Every story (and any story id the old memories name) gets its partition, filled from the
   old table and attached, in batches of ``BATCH`` stories, each batch one transaction of its
   own (inside ``autocommit_block``), so the lock table never holds thousands of partitions'
   locks at once. A batch skips the stories that already have a partition, so a run that
   crashed part-way resumes where it stopped.
4. ``memories_old`` is dropped once every story id it holds has a partition.

Downgrade rebuilds the unpartitioned table: a flat copy (``fillfactor = 70``) is filled from
the partitions in batches, each partition dropped once copied (resumable the same way); then
the parent is dropped and the copy takes its name, primary key ``(id)``, indexes (the HNSW one
included where it existed), foreign key and triggers.

Revision ID: 0151
Revises: 0150
Create Date: 2026-11-11

"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import Connection, text

from epistrel.core.memory.story_partitions import FILLFACTOR, partition_name

revision: str = "0151"
down_revision: str | None = "0150"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "memories"
OLD = "memories_old"
FLAT = "memories_flat"
PK = "pk_memories"
BATCH = 50
"""Stories moved per transaction."""

NO_TRUNCATE = (
    "CREATE TRIGGER memories_no_truncate BEFORE TRUNCATE ON {table} "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_memories_guard()"
)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _relkind(bind: Connection, name: str) -> str | None:
    found = bind.scalar(
        text("SELECT c.relkind FROM pg_class c WHERE c.oid = to_regclass(:n)"), {"n": name}
    )
    return None if found is None else str(found)


def _columns(bind: Connection, table: str) -> str:
    """The stored (not generated) columns of ``table``, in order, quoted and comma-joined."""
    names = bind.scalars(
        text(
            "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:t AS regclass) "
            "AND attnum > 0 AND NOT attisdropped AND attgenerated = '' ORDER BY attnum"
        ),
        {"t": table},
    )
    return ", ".join(_ident(str(n)) for n in names)


def _definitions(bind: Connection, table: str) -> tuple[list[str], list[str], list[str]]:
    """``table``'s non-primary-key index definitions (as plain ``ON memories``), its foreign
    keys (``ALTER TABLE … ADD CONSTRAINT``) and its triggers, as SQL to run against a table of
    the same name."""
    indexes = [
        re.sub(r" ON ONLY ", " ON ", str(d))
        for d in bind.scalars(
            text(
                "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "WHERE i.indrelid = CAST(:t AS regclass) AND NOT i.indisprimary "
                "ORDER BY i.indexrelid"
            ),
            {"t": table},
        )
    ]
    foreign = [
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {_ident(str(r.conname))} {r.definition}"
        for r in bind.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conrelid = CAST(:t AS regclass) AND contype = 'f' ORDER BY conname"
            ),
            {"t": table},
        )
    ]
    triggers = [
        str(d)
        for d in bind.scalars(
            text(
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal AND tgparentid = 0 "
                "ORDER BY tgname"
            ),
            {"t": table},
        )
    ]
    return indexes, foreign, triggers


def _index_names(bind: Connection, table: str) -> list[str]:
    return [
        str(n)
        for n in bind.scalars(
            text(
                "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = CAST(:t AS regclass) AND NOT i.indisprimary"
            ),
            {"t": table},
        )
    ]


def _partitions(bind: Connection) -> list[str]:
    return [
        str(n)
        for n in bind.scalars(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = CAST(:t AS regclass) ORDER BY c.relname"
            ),
            {"t": TABLE},
        )
    ]


# -- upgrade ----------------------------------------------------------------------------------


def _partitioned_parent(bind: Connection) -> None:
    """Steps 1 and 2 (see the module docstring), in the migration's transaction."""
    indexes, foreign, triggers = _definitions(bind, TABLE)
    names = _index_names(bind, TABLE)
    op.execute(f"ALTER TABLE {TABLE} RENAME TO {OLD}")
    op.execute(f"ALTER TABLE {OLD} RENAME CONSTRAINT {PK} TO {PK}_old")
    for name in names:
        op.execute(f"ALTER INDEX {_ident(name)} RENAME TO {_ident(name + '_old')}")
    op.execute(
        f"CREATE TABLE {TABLE} (LIKE {OLD} INCLUDING ALL EXCLUDING INDEXES) "
        "PARTITION BY LIST (story_id)"
    )
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {PK} PRIMARY KEY (id, story_id)")
    for statement in (*indexes, *foreign, *triggers):
        op.execute(statement)


def _story_ids(bind: Connection, has_old: bool) -> list[str]:
    query = "SELECT id FROM stories"
    if has_old:
        query += f" UNION SELECT DISTINCT story_id FROM {OLD}"  # noqa: S608
    return sorted(str(s).rstrip() for s in bind.scalars(text(query)))


def _move(bind: Connection, story_id: str, columns: str, has_old: bool) -> None:
    """One story's partition, filled from the old table and attached."""
    name = _ident(partition_name(story_id))
    bind.exec_driver_sql(
        f"CREATE TABLE {name} (LIKE {TABLE} INCLUDING ALL EXCLUDING INDEXES) "
        f"WITH (fillfactor = {FILLFACTOR})"
    )
    if has_old:
        bind.exec_driver_sql(
            f"INSERT INTO {name} ({columns}) SELECT {columns} FROM {OLD} "  # noqa: S608
            f"WHERE story_id = {_literal(story_id)}"
        )
    bind.exec_driver_sql(
        f"ALTER TABLE {TABLE} ATTACH PARTITION {name} FOR VALUES IN ({_literal(story_id)})"
    )
    bind.exec_driver_sql(NO_TRUNCATE.format(table=name))


def _batches(bind: Connection) -> None:
    """Step 3: every story's partition, ``BATCH`` stories per transaction."""
    has_old = _relkind(bind, OLD) is not None
    columns = _columns(bind, TABLE)
    existing = set(_partitions(bind))
    pending = [s for s in _story_ids(bind, has_old) if partition_name(s) not in existing]
    for start in range(0, len(pending), BATCH):
        bind.exec_driver_sql("BEGIN")
        try:
            for story_id in pending[start : start + BATCH]:
                _move(bind, story_id, columns, has_old)
        except BaseException:
            bind.exec_driver_sql("ROLLBACK")
            raise
        bind.exec_driver_sql("COMMIT")


def _drop_old(bind: Connection) -> None:
    """Step 4, in the migration's closing transaction."""
    if _relkind(bind, OLD) is None:
        return
    existing = set(_partitions(bind))
    missing = [s for s in _story_ids(bind, True) if partition_name(s) not in existing]
    if missing:
        raise RuntimeError(f"{len(missing)} stories have no memories partition; not dropping")
    op.execute(f"DROP TABLE {OLD}")


def upgrade() -> None:
    bind = op.get_bind()
    if _relkind(bind, TABLE) == "r":
        _partitioned_parent(bind)
    with op.get_context().autocommit_block():
        _batches(op.get_bind())
    _drop_old(op.get_bind())


# -- downgrade --------------------------------------------------------------------------------


def _unpartition(bind: Connection) -> None:
    """Copy every partition into the flat table and drop it, ``BATCH`` per transaction."""
    columns = _columns(bind, FLAT)
    partitions = _partitions(bind)
    for start in range(0, len(partitions), BATCH):
        bind.exec_driver_sql("BEGIN")
        try:
            for name in partitions[start : start + BATCH]:
                bind.exec_driver_sql(
                    f"INSERT INTO {FLAT} ({columns}) "  # noqa: S608
                    f"SELECT {columns} FROM {_ident(name)}"
                )
                bind.exec_driver_sql(f"DROP TABLE {_ident(name)}")
        except BaseException:
            bind.exec_driver_sql("ROLLBACK")
            raise
        bind.exec_driver_sql("COMMIT")


def downgrade() -> None:
    bind = op.get_bind()
    if _relkind(bind, TABLE) != "p":
        return
    if _relkind(bind, FLAT) is None:
        op.execute(
            f"CREATE TABLE {FLAT} (LIKE {TABLE} INCLUDING ALL EXCLUDING INDEXES) "
            f"WITH (fillfactor = {FILLFACTOR})"
        )
    with op.get_context().autocommit_block():
        _unpartition(op.get_bind())
    bind = op.get_bind()
    indexes, foreign, triggers = _definitions(bind, TABLE)
    op.execute(f"DROP TABLE {TABLE}")
    op.execute(f"ALTER TABLE {FLAT} RENAME TO {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {PK} PRIMARY KEY (id)")
    for statement in (*indexes, *foreign, *triggers):
        op.execute(statement)
