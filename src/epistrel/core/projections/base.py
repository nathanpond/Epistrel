"""The projection contract: in-memory rows, a pure ``apply``, and the registry.

A projection owns one table. Its ``apply(rows, event)`` is a pure function of the story's
projection rows and one event: it reads and changes nothing but ``rows``. The incremental path
(``Operation.commit``) and the fold (``rebuild``) both call it, so a rebuilt projection equals
the incrementally maintained one by construction (FR-STORE-4); the two paths differ only in how
rows are loaded and written.

A projection over stored state rather than the log (#137's structured projections, a cache over
canon) answers its rows from ``fold_state``: the fold uses them instead of replaying the log,
and its ``apply`` is a no-op for every event (its incremental path is canon's own write hook).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, Table, and_, insert, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import EventCategory, Membership, OperationKind, Provenance
from epistrel.core.events.registry import EventPayload

Row = dict[str, Any]
Key = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class LogEvent:
    """What a projection sees of one event: the row, its decoded payload, and its operation.

    ``kind`` and ``membership`` are the operation's. The incremental path builds these from the
    events being committed (membership ``active``); the fold reads them from the log joined with
    ``operations``, so a row's membership is its operation's current one.
    """

    id: str
    story_id: str
    seq: int
    operation_id: str
    kind: OperationKind
    membership: Membership
    category: EventCategory
    narrative_turn: int | None
    turn_variant: int | None
    beat: int | None
    character_id: str | None
    provenance: Provenance
    type: str
    payload: EventPayload
    redacted_at: datetime | None = None
    """When erasure rewrote the event in place (#124): the projections mark their row
    redacted. An event being committed never is."""
    step: int = 0
    """The event's declared causal step within its beat (#168, FR-STORE-31; 0: outside)."""
    slot_k: int | None = None
    """The inter-turn slot of an operation positioned in one (#193; ``None``: none)."""


class Rows:
    """One story's rows of one projection table, keyed by the table's primary key.

    ``keys_where`` answers the keys of the rows with a column's value through an index built on
    its first use and kept by ``put``, so a fold's per-event lookups do not scan every row
    (#887). A projection may change a row's dict or list value in place (``story_heads``' map,
    #887); ``snapshot`` copies those values, so the incremental path still sees the change.
    """

    def __init__(self, table: Table, rows: Iterable[Row] = ()) -> None:
        self.table = table
        self.key_columns: tuple[str, ...] = tuple(c.name for c in table.primary_key.columns)
        self._rows: dict[Key, Row] = {}
        self._indexes: dict[str, dict[Any, dict[Key, None]]] = {}
        for row in rows:
            self.put(row)

    def key(self, row: Row) -> Key:
        return tuple(row[name] for name in self.key_columns)

    def get(self, key: Key) -> Row | None:
        row = self._rows.get(key)
        return dict(row) if row is not None else None

    def put(self, row: Row) -> None:
        """Insert or replace the row with ``row``'s key."""
        key = self.key(row)
        if self._indexes:
            old = self._rows.get(key)
            for column, index in self._indexes.items():
                value = row[column]
                if old is not None:
                    if old[column] == value:
                        continue
                    _drop(index, old[column], key)
                index.setdefault(value, {})[key] = None
        self._rows[key] = dict(row)

    def keys_where(self, column: str, value: Any) -> list[Key]:
        """The keys of the rows whose ``column`` equals ``value``, in no particular order."""
        index = self._indexes.get(column)
        if index is None:
            index = {}
            for key, row in self._rows.items():
                index.setdefault(row[column], {})[key] = None
            self._indexes[column] = index
        return list(index.get(value, ()))

    def __iter__(self) -> Iterator[Row]:
        return (dict(row) for row in self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)

    def snapshot(self) -> dict[Key, Row]:
        return {key: _copied(row) for key, row in self._rows.items()}


def _drop(index: dict[Any, dict[Key, None]], value: Any, key: Key) -> None:
    keys = index.get(value)
    if keys is not None:
        keys.pop(key, None)
        if not keys:
            del index[value]


def _copied(row: Row) -> Row:
    """``row`` with its dict and list values copied (what ``apply`` may change in place)."""
    return {k: (v.copy() if isinstance(v, dict | list) else v) for k, v in row.items()}


class Projection(ABC):
    """A cache over the event log, maintained in the commit transaction and rebuildable."""

    table: ClassVar[Table]

    @property
    def name(self) -> str:
        return self.table.name

    @abstractmethod
    def apply(self, rows: Rows, event: LogEvent) -> None:
        """Fold one event into ``rows``. Pure: no I/O, nothing changed but ``rows``.

        An event type the projection does not know is a no-op.
        """

    @abstractmethod
    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        """The rows of ``story_id`` that applying ``events`` can read or change."""

    async def write(self, conn: AsyncConnection, rows: Rows, before: dict[Key, Row]) -> None:
        """Write what ``apply`` changed since ``before`` (``rows.snapshot()`` taken after
        ``load``): the default inserts the rows it added and updates the columns it changed."""
        await write_difference(conn, rows, before)

    async def fold_state(self, conn: AsyncConnection, story_id: str) -> Rows | None:
        """The story's rows folded from stored state, for a projection that is not a fold of
        the log; ``None`` (the default): fold the log through ``apply``."""
        del conn, story_id
        return None


async def write_difference(conn: AsyncConnection, rows: Rows, before: dict[Key, Row]) -> None:
    """Insert the rows ``apply`` added and update the ones it changed (``apply`` never removes)."""
    table = rows.table
    after = rows.snapshot()

    def where(key: Key) -> ColumnElement[bool]:
        pairs = zip(rows.key_columns, key, strict=True)
        return and_(*(table.c[name] == value for name, value in pairs))

    added = sorted(after.keys() - before.keys())
    if added:
        await conn.execute(insert(table), [after[key] for key in added])
    for key in before.keys() & after.keys():
        if before[key] != after[key]:
            changed = {k: v for k, v in after[key].items() if before[key].get(k) != v}
            await conn.execute(update(table).where(where(key)).values(**changed))


_PROJECTIONS: dict[str, Projection] = {}


def register_projection(projection: Projection) -> Projection:
    """Add ``projection`` to the set every commit maintains and every rebuild refolds."""
    if projection.name in _PROJECTIONS:
        raise ValueError(f"projection {projection.name!r} is already registered")
    _PROJECTIONS[projection.name] = projection
    return projection


def registered_projections() -> tuple[Projection, ...]:
    """Every registered projection, in registration order."""
    return tuple(_PROJECTIONS.values())
