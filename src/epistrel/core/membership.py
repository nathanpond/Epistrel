"""Timeline membership of derived records (FR-STORE-9, FR-STORE-13, FR-STORE-29, FR-BITEMP-5;
docs/02 §4.4, §4.6; #130).

Everything the structured pipeline derives from events (facts, memories, utterances, thunks,
the promissory ledger and its resolutions, and in M5 beliefs) is a **derived record**: it
carries the operation that produced it (``operation_id``), the events it came from
(``source_event_ids``), its narrative position (``narrative_turn``, ``turn_variant``; ``NULL``
for position-less consolidation or backfill rows) and a timeline membership
(``membership``, ``membership_tx``) that follows its operation's.

- **The registry.** A derived table registers once, with ``register_derived(DerivedTable(...))``
  when its module is imported, and is then covered by everything below and by the schema test
  (``tests/integration/test_membership_registry.py``). Its columns come from
  ``derived_columns()`` and its indexes from ``derived_indexes()``, in the table and in its
  migration. A registered table is also a story table (``core.stories.STORY_TABLES``), so
  story deletion enumerates it and a fork copies it.
- **The flip.** ``flip_membership(conn, operation_ids, to)`` moves every registered row whose
  operation column names one of ``operation_ids`` to ``to``, stamping ``membership_tx`` with
  the transaction's time (the same stamp the operations get). ``core.timeline`` runs it first
  among its in-transaction membership hooks, so a truncate, an untruncate and a swap flip the
  derived rows of their operations in the edit's own transaction; ``core.events.operation``
  calls it for the two changes no hook sees (a regenerate retiring the active variant, a turn
  written over a rewound tail). Rows already at ``to`` keep their stamp (stacked truncations),
  ``superseded`` rows are never touched, and valid-time and narrative position are never
  written: membership is not valid-time (FR-BITEMP-5). ``flip_by_sources`` is the second entry
  point, for rows that follow their source events rather than an operation (#143's reflections).
- **The reads.** Views and retrieval read derived rows only through ``active_only(table)`` (or
  ``active_only_sql(alias)`` in raw SQL) and the coordinate helpers ``position_of``,
  ``valid_time_of`` and ``tx_time_of``, which keep the three coordinates apart.
  ``tools.gates.membership`` (``check-membership``) refuses a raw ``membership`` filter outside
  this module and the M2/M3 modules that filter operations and projections.

``lite_summary_segments`` and ``transcript_messages`` keep their own M2 membership handling and
are not registered; ``core.projections`` caches are rebuildable and are not derived records.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    TIMESTAMP,
    Column,
    ColumnElement,
    ForeignKey,
    FromClause,
    Index,
    Integer,
    SmallInteger,
    Table,
    and_,
    any_,
    bindparam,
    false,
    func,
    insert,
    literal,
    or_,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership
from epistrel.core.tables.base import pg_enum, ulid_type

if TYPE_CHECKING:
    from epistrel.core.timeline import MembershipChange

DERIVED_COLUMNS: tuple[str, ...] = (
    "operation_id",
    "source_event_ids",
    "narrative_turn",
    "turn_variant",
    "membership",
    "membership_tx",
)
"""The columns every derived table carries, from ``derived_columns()``."""

FLIP_TARGETS: frozenset[Membership] = frozenset(
    {Membership.ACTIVE, Membership.DISCARDED, Membership.DORMANT}
)
"""What a flip may set; ``superseded`` belongs to the table that supersedes its own rows."""

Follower = Callable[[AsyncConnection, str | None, tuple[str, ...], Membership], Awaitable[None]]
"""Run right after a flip of ``operation_ids`` to a membership, in its transaction (#154's commit
boundary): ``(conn, story_id, operation_ids, to)``."""
FOLLOWERS: list[Follower] = []
"""What runs after every operation-membership flip of derived rows: the timeline edits' (through
``follow_membership_change``) and the two ``core.events.operation`` makes itself."""


def register_follower(follower: Follower) -> Follower:
    """Register ``follower`` (once) to run after every flip of an operation's derived rows."""
    if follower not in FOLLOWERS:
        FOLLOWERS.append(follower)
    return follower


async def follow(
    conn: AsyncConnection,
    operation_ids: Iterable[str],
    to: Membership,
    *,
    story_id: str | None = None,
) -> int:
    """Flip the derived rows of ``operation_ids`` to ``to`` (``flip_membership``), then run the
    registered followers in the same transaction. Returns the rows the flip changed."""
    ids = tuple(_ids(operation_ids))
    changed = await flip_membership(conn, ids, to, story_id=story_id)
    if ids:
        for follower in list(FOLLOWERS):
            await follower(conn, story_id, ids, Membership(to))
    return changed


_EXCLUDED: ContextVar[tuple[str, ...]] = ContextVar("epistrel_membership_excluded", default=())
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


@contextmanager
def excluding(operation_ids: Iterable[str]) -> Iterator[None]:
    """Within the block (this task and the tasks it starts), every ``active_only``,
    ``active_operation`` and ``active_only_sql`` read also leaves out the rows of
    ``operation_ids``: a structured regenerate reads the story as it stood before the variant it
    replaces (#154), which its own commit then retires."""
    ids = tuple(_ids(operation_ids))
    for i in ids:
        if not _ULID.match(i):
            raise ValueError(f"{i!r} is not an operation id")
    token = _EXCLUDED.set(tuple(sorted({*_EXCLUDED.get(), *ids})))
    try:
        yield
    finally:
        _EXCLUDED.reset(token)


def excluded_operations() -> tuple[str, ...]:
    """The operations ``excluding`` leaves out of reads here (none outside it)."""
    return _EXCLUDED.get()


def _not_excluded(column: ColumnElement[Any]) -> ColumnElement[bool] | None:
    ids = _EXCLUDED.get()
    if not ids:
        return None
    return column.not_in([literal(i) for i in ids])


@dataclass(frozen=True, slots=True)
class DerivedTable:
    """A registered derived table and where its coordinates live.

    ``operation_columns`` are the columns naming an operation whose membership the row follows
    (``operation_id``; a thunk adds the operation that resolved it). ``position_columns`` are
    the narrative position; ``valid_time`` and ``tx_time`` the ``(from, to)`` columns of the
    other two coordinates when the table has them (``None`` otherwise: memories have neither).
    """

    table: Table
    operation_columns: tuple[str, ...] = ("operation_id",)
    position_columns: tuple[str, str] = ("narrative_turn", "turn_variant")
    valid_time: tuple[str, str] | None = None
    tx_time: tuple[str, str] | None = None

    @property
    def name(self) -> str:
        return self.table.name

    def __post_init__(self) -> None:
        named = [
            *DERIVED_COLUMNS,
            *self.operation_columns,
            *self.position_columns,
            *(self.valid_time or ()),
            *(self.tx_time or ()),
        ]
        missing = [c for c in named if c not in self.table.c]
        if missing:
            raise ValueError(f"derived table {self.table.name} has no column(s) {missing}")
        if "story_id" not in self.table.c:
            raise ValueError(f"derived table {self.table.name} has no story_id column")


DERIVED_TABLES: dict[str, DerivedTable] = {}
"""Every registered derived table, by table name, in registration order."""


def register_derived(entry: DerivedTable) -> DerivedTable:
    """Register ``entry`` (once; registering the same table again with another entry fails)."""
    known = DERIVED_TABLES.get(entry.name)
    if known is not None and known != entry:
        raise ValueError(f"derived table {entry.name} is already registered differently")
    DERIVED_TABLES[entry.name] = entry
    return entry


def derived_entry(table: FromClause | str) -> DerivedTable:
    """The registry entry of ``table`` (a table, an alias of one, or a name)."""
    name = table if isinstance(table, str) else _base_table(table).name
    entry = DERIVED_TABLES.get(name)
    if entry is None:
        raise KeyError(f"{name} is not a registered derived table")
    return entry


def _base_table(table: FromClause) -> Table:
    found: Any = table
    while not isinstance(found, Table):
        found = getattr(found, "element", None)
        if found is None:
            raise TypeError(f"{table!r} is not a table or an alias of one")
    return found


# -- schema -----------------------------------------------------------------------------------


def derived_columns() -> list[Column[Any]]:
    """Fresh copies of the derived-record columns, for a table and for its migration.

    ``operation_id`` is the producing operation (a ULID with a foreign key to ``operations``);
    ``source_event_ids`` the events it came from; the narrative position is nullable for
    position-less rows; ``membership`` reuses the operations' enum, ``active`` by default;
    ``membership_tx`` has no default, so a row is written through ``insert_derived()``.
    """
    return [
        Column(
            "operation_id",
            ulid_type(),
            ForeignKey("operations.operation_id"),
            nullable=False,
        ),
        Column(
            "source_event_ids",
            ARRAY(ulid_type()),
            nullable=False,
            server_default=text("'{}'"),
        ),
        Column("narrative_turn", Integer, nullable=True),
        Column("turn_variant", SmallInteger, nullable=True),
        Column(
            "membership",
            pg_enum(Membership, "membership"),
            nullable=False,
            server_default=text("'active'"),
        ),
        Column("membership_tx", TIMESTAMP(timezone=True), nullable=False),
    ]


def derived_index_names(table: str) -> tuple[str, str]:
    """The two standard index names of derived table ``table``."""
    return (f"ix_{table}_story_operation", f"ix_{table}_story_membership")


def derived_indexes(table: str) -> list[Index]:
    """The standard indexes of derived table ``table``: ``(story_id, operation_id)`` for the
    flip and ``(story_id, membership)`` for ``active_only`` reads. Pass them to ``Table(...)``;
    a migration creates them with ``op.create_index`` under the same names."""
    by_operation, by_membership = derived_index_names(table)
    return [
        Index(by_operation, "story_id", "operation_id"),
        Index(by_membership, "story_id", "membership"),
    ]


DERIVED_INDEX_COLUMNS: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("story_id", "operation_id"),
    ("story_id", "membership"),
)
"""The columns of ``derived_indexes()``'s two indexes, in order."""


# -- writes -----------------------------------------------------------------------------------


async def insert_derived(
    conn: AsyncConnection, table: Table, rows: Sequence[Mapping[str, Any]]
) -> int:
    """Insert derived rows into registered ``table``: each ``active``, ``membership_tx`` the
    transaction's time. Each row names its ``operation_id`` (and its position and source
    events when it has them). Returns the number of rows written."""
    entry = derived_entry(table)
    if not rows:
        return 0
    for row in rows:
        if "membership" in row or "membership_tx" in row:
            raise ValueError("insert_derived sets membership itself; a row cannot name it")
        if not row.get("operation_id"):
            raise ValueError("a derived row names the operation that produced it")
    statement = insert(entry.table).values(
        membership=Membership.ACTIVE.value, membership_tx=func.transaction_timestamp()
    )
    await conn.execute(statement, [dict(row) for row in rows])
    return len(rows)


def _ids(values: Iterable[str]) -> list[str]:
    return sorted({str(v) for v in values})


def _target(to: Membership) -> Membership:
    to = Membership(to)
    if to not in FLIP_TARGETS:
        raise ValueError(f"a flip cannot set membership {to.value}")
    return to


async def _flip_where(
    conn: AsyncConnection,
    entry: DerivedTable,
    matches: ColumnElement[bool],
    to: Membership,
    story_id: str | None,
) -> int:
    t = entry.table
    where = [
        matches,
        t.c.membership.not_in((to.value, Membership.SUPERSEDED.value)),
    ]
    if story_id is not None:
        where.insert(0, t.c.story_id == story_id)
    result = await conn.execute(
        update(t)
        .where(and_(*where))
        .values(membership=to.value, membership_tx=func.transaction_timestamp())
    )
    return int(result.rowcount)


async def flip_membership(
    conn: AsyncConnection,
    operation_ids: Iterable[str],
    to: Membership,
    *,
    story_id: str | None = None,
) -> int:
    """Move every registered derived row of ``operation_ids`` to membership ``to`` on
    ``conn``'s transaction; see the module docstring. ``story_id`` (when known) narrows each
    update to the ``(story_id, operation_id)`` index. Returns the number of rows changed."""
    target = _target(to)
    ids = _ids(operation_ids)
    if not ids or not DERIVED_TABLES:
        return 0
    param = bindparam("flip_operation_ids", ids, type_=ARRAY(ulid_type()))
    changed = 0
    for entry in list(DERIVED_TABLES.values()):
        t = entry.table
        matches = or_(false(), *(t.c[col] == any_(param) for col in entry.operation_columns))
        changed += await _flip_where(conn, entry, matches, target, story_id)
    return changed


async def flip_by_sources(
    conn: AsyncConnection,
    event_ids: Iterable[str],
    to: Membership = Membership.DISCARDED,
    *,
    story_id: str | None = None,
) -> int:
    """Move every registered derived row with a source event among ``event_ids`` to ``to``
    (#143: a reflection whose sources are discarded goes with them). Returns the rows changed."""
    target = _target(to)
    ids = _ids(event_ids)
    if not ids or not DERIVED_TABLES:
        return 0
    param = bindparam("flip_event_ids", ids, type_=ARRAY(ulid_type()))
    changed = 0
    for entry in list(DERIVED_TABLES.values()):
        matches = entry.table.c.source_event_ids.overlap(param)
        changed += await _flip_where(conn, entry, matches, target, story_id)
    return changed


async def supersede(
    conn: AsyncConnection, table: Table, row_ids: Iterable[str], *, story_id: str
) -> int:
    """Mark the ``active`` rows ``row_ids`` of registered derived ``table`` ``superseded`` (a
    newer derivation replaced them: #143's explicit rerun, or a reactivated original winning over
    a rerun), stamping ``membership_tx``. A superseded row is never flipped again (the flips
    skip it) and never read as active. Returns the rows changed."""
    entry = derived_entry(table)
    ids = _ids(row_ids)
    if not ids:
        return 0
    t = entry.table
    result = await conn.execute(
        update(t)
        .where(
            t.c.story_id == story_id,
            t.c.id.in_(ids),
            t.c.membership == Membership.ACTIVE.value,
        )
        .values(membership=Membership.SUPERSEDED.value, membership_tx=func.transaction_timestamp())
    )
    return int(result.rowcount)


async def follow_membership_change(conn: AsyncConnection, change: MembershipChange) -> None:
    """``core.timeline``'s first in-transaction membership hook: the derived rows of the
    changed operations take their new membership in the edit's transaction, then the followers
    run (#154's commit boundary)."""
    await follow(conn, change.operation_ids, change.new, story_id=change.story_id)


# -- reads ------------------------------------------------------------------------------------


def active_only(table: FromClause) -> ColumnElement[bool]:
    """The predicate every view and retrieval query puts on registered derived ``table`` (or an
    alias of it): ``membership = 'active'``. Position bounds are the caller's."""
    derived_entry(table)
    active = table.c.membership == Membership.ACTIVE.value
    kept = _not_excluded(table.c.operation_id)
    return active if kept is None else and_(active, kept)


def active_operation(table: FromClause) -> ColumnElement[bool]:
    """The predicate on ``operations`` (or an alias of it) for an operation on the active
    timeline, for derived-record reads that join their sources' operations (#135's working
    memory and view through N)."""
    if _base_table(table).name != "operations":
        raise TypeError(f"{table!r} is not the operations table")
    active = table.c.membership == Membership.ACTIVE.value
    kept = _not_excluded(table.c.operation_id)
    return active if kept is None else and_(active, kept)


def active_as_of(table: FromClause, record_time: Any) -> ColumnElement[bool]:
    """Whether a row of registered derived ``table`` was in the timeline at record-time
    ``record_time`` (#133's as-recorded reads): active now, or moved out of it only after
    ``record_time`` (``membership_tx`` later). A row that left and came back keeps only its last
    change, so it reads as active throughout; the control events hold the full history."""
    return or_(active_only(table), table.c.membership_tx > record_time)


_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def active_only_predicate() -> str:
    """``active_only`` as an unqualified SQL predicate, for the ``WHERE`` of a partial index over
    a registered derived table (#147's hot-set indexes on ``memories``)."""
    return f"membership = '{Membership.ACTIVE.value}'"


def active_only_sql(alias: str) -> str:
    """``active_only`` as a SQL fragment for raw queries (#141's pgvector search), over the
    derived row aliased ``alias``."""
    if not _ALIAS.match(alias):
        raise ValueError(f"{alias!r} is not a SQL alias")
    found = f"{alias}.membership = '{Membership.ACTIVE.value}'"
    ids = _EXCLUDED.get()
    if ids:  # ULIDs only (``excluding`` checks), so they are safe to inline
        found += f" AND {alias}.operation_id NOT IN ({', '.join(repr(i) for i in ids)})"
    return found


def position_of(table: FromClause) -> tuple[ColumnElement[Any], ColumnElement[Any]]:
    """The narrative position columns of derived ``table``: ``(turn, variant)``."""
    turn, variant = derived_entry(table).position_columns
    return table.c[turn], table.c[variant]


def valid_time_of(table: FromClause) -> tuple[ColumnElement[Any], ColumnElement[Any]] | None:
    """The valid-time ``(from, to)`` columns of derived ``table``, or ``None`` when it has none."""
    found = derived_entry(table).valid_time
    return None if found is None else (table.c[found[0]], table.c[found[1]])


def tx_time_of(table: FromClause) -> tuple[ColumnElement[Any], ColumnElement[Any]] | None:
    """The transaction-time ``(from, to)`` columns of derived ``table``, or ``None``."""
    found = derived_entry(table).tx_time
    return None if found is None else (table.c[found[0]], table.c[found[1]])


__all__ = [
    "DERIVED_COLUMNS",
    "DERIVED_INDEX_COLUMNS",
    "DERIVED_TABLES",
    "FLIP_TARGETS",
    "FOLLOWERS",
    "DerivedTable",
    "Follower",
    "active_as_of",
    "active_only",
    "active_only_predicate",
    "active_only_sql",
    "active_operation",
    "derived_columns",
    "derived_entry",
    "derived_index_names",
    "derived_indexes",
    "excluded_operations",
    "excluding",
    "flip_by_sources",
    "flip_membership",
    "follow",
    "follow_membership_change",
    "insert_derived",
    "position_of",
    "register_derived",
    "register_follower",
    "supersede",
    "tx_time_of",
    "valid_time_of",
]
