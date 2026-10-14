"""The projection contract: in-memory rows, a pure ``apply``, and the registry.

A projection owns one table. Its ``apply(rows, event)`` is a pure function of the story's
projection rows and one event: it reads and changes nothing but ``rows``. The incremental path
(``Operation.commit``) and the fold (``rebuild``) both call it, so a rebuilt projection equals
the incrementally maintained one by construction (FR-STORE-4); the two paths differ only in how
rows are loaded and written.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import Table
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


class Rows:
    """One story's rows of one projection table, keyed by the table's primary key."""

    def __init__(self, table: Table, rows: Iterable[Row] = ()) -> None:
        self.table = table
        self.key_columns: tuple[str, ...] = tuple(c.name for c in table.primary_key.columns)
        self._rows: dict[Key, Row] = {}
        for row in rows:
            self.put(row)

    def key(self, row: Row) -> Key:
        return tuple(row[name] for name in self.key_columns)

    def get(self, key: Key) -> Row | None:
        row = self._rows.get(key)
        return dict(row) if row is not None else None

    def put(self, row: Row) -> None:
        """Insert or replace the row with ``row``'s key."""
        self._rows[self.key(row)] = dict(row)

    def __iter__(self) -> Iterator[Row]:
        return (dict(row) for row in self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)

    def snapshot(self) -> dict[Key, Row]:
        return {key: dict(row) for key, row in self._rows.items()}


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
