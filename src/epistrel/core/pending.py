"""A commit's epistemic writes, batched (#754; ADR-199). It lives outside ``core.epistemic`` so
the canon reads that run inside a batch can mark themselves (``check-epistemic-separation``).

Inside ``batching(conn)`` (the structured commit opens it around its testimony, lazy-resolution
and speech passes) the epistemic writers do not execute their inserts one by one: each is held
here, in order, and the whole set is written at the scope's end, one multi-row statement per
table in foreign-key order (``ORDER``): propositions, events, transitions, beliefs, the ``tx_to``
of a belief written and closed in the same commit, subject references, dependency edges and the
change index; then the deferred exposure updates and one ``belief_state``/``nested_belief_state``
refresh of every touched key. The rows are the ones the per-belief path writes, in the same order,
with the same ids (generated when the write is made, not at the flush), the same event ``seq``
(numbered here from one ``max`` read) and the same final column values.

**Reads see the pending rows.** The readers a commit makes between its writes (a partition, a
belief by id, the speech acts, the nested beliefs, a transition replay, the trust counts, an
antecedent probe, an event's intent) merge the pending rows themselves; their statements carry the
``AWARE`` execution option. Any other statement that touches a table with pending rows, any
statement whose tables are unknown (raw SQL) and any other write flush first, inside
SQLAlchemy's ``before_execute`` on the same connection: an unaware reader costs a flush, never a
stale answer, and an unaware writer never references an unwritten row. Such a flush also clears
the scope's read memo (``remembered``). A savepoint flushes first; a rollback
to one drops what was buffered after it (the flush at its start wrote everything older).

A belief row closed in the same commit is buffered already closed (``held_to``, state, corrected
by), so ``ux_beliefs_open`` never sees two open rows; its ``tx_to`` is set right after the insert
by one ``UPDATE … clock_timestamp()`` (``ck_beliefs_tx_order`` needs it after ``tx_from``). A
belief closed in place that was stored before the commit is updated at once, as before.

``disabled()`` turns batching off (the per-belief path; the equivalence test compares the two).
Log lines carry identifiers and counts only (invariant 3)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Hashable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import TableClause

log = logging.getLogger(__name__)

AWARE = "epistrel_pending"
"""The execution option of a statement whose caller merges the pending rows itself."""

ORDER: tuple[str, ...] = (
    "propositions",
    "events",
    "belief_transitions",
    "beliefs",
    "subject_references",
    "dependency_edges",
    "proposition_changes",
)
"""The flush order: each table after every table it references (the beliefs' provenance
trigger needs their transitions first, ``ADR-168``)."""

CHUNK = 1_000
"""The most rows one flushed statement carries."""

_CURRENT: ContextVar[Pending | None] = ContextVar("epistrel_pending_writes", default=None)
_DISABLED: ContextVar[bool] = ContextVar("epistrel_pending_disabled", default=False)

MISSING = object()
"""``remembered``'s answer for a read the scope does not hold."""


@dataclass(eq=False)
class Pending:
    """One commit's buffered epistemic writes (see the module docstring)."""

    sync: Connection
    inserts: dict[str, list[tuple[Any, dict[str, Any]]]] = field(default_factory=dict)
    """Per table, the ``(statement, row)`` pairs in write order."""
    beliefs: dict[str, Any] = field(default_factory=dict)
    """The pending beliefs by id, as the per-belief path would read them now."""
    belief_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Their insert rows (the same dicts as in ``inserts``: a patch changes what is written)."""
    closing: dict[str, None] = field(default_factory=dict)
    """Pending beliefs closed in this commit: the flush sets their ``tx_to``."""
    transitions: dict[str, Any] = field(default_factory=dict)
    """The pending transitions by id."""
    transition_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Their insert rows."""
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    """The pending events by id (their rows)."""
    propositions: dict[str, Any] = field(default_factory=dict)
    """The pending propositions by id (as a read of their row would answer them)."""
    projection: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    """The ``(holder, proposition)`` keys to refresh, by story."""
    exposures: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    """Deferred ``exposures`` updates: ``(statement, parameters)`` in order."""
    seq: dict[str, int] = field(default_factory=dict)
    """Each story's last event ``seq`` (read once, then counted here)."""
    memo: dict[Hashable, Any] = field(default_factory=dict)
    slices: dict[tuple[str, str], dict[str, Any] | None] = field(default_factory=dict)
    """Each holder's stored beliefs, read once (``partitions``' reads answer from them and the
    pending rows; ``None``: too many to hold)."""
    said: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    """A sliced belief's producing transition's source and outcome (a speech act's)."""
    flushing: bool = False
    flushes: int = 0
    guard_flushes: int = 0

    def empty(self) -> bool:
        return not (self.inserts or self.closing or self.projection or self.exposures)

    def tables(self) -> set[str]:
        """The tables a read must not see without the pending rows."""
        found = set(self.inserts)
        if self.closing:
            found.add("beliefs")
        if self.projection:
            found |= {"belief_state", "nested_belief_state"}
        if self.exposures:
            found.add("exposures")
        if self.seq:
            found.add("events")
        return found

    def defer(self, statement: Any, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Hold ``statement``'s ``rows`` (an insert) for the flush; answers the held dicts."""
        table = statement.table.name
        if table not in ORDER:
            raise ValueError(f"no batched writes to {table}")
        held = [dict(r) for r in rows]
        self.inserts.setdefault(table, []).extend((statement, r) for r in held)
        return held

    def drop(self) -> None:
        """Forget everything buffered (a rollback)."""
        self.inserts.clear()
        self.beliefs.clear()
        self.belief_rows.clear()
        self.closing.clear()
        self.transitions.clear()
        self.transition_rows.clear()
        self.events.clear()
        self.propositions.clear()
        self.projection.clear()
        self.exposures.clear()
        self.forget()

    def forget(self) -> None:
        """Drop what the scope remembers of the stored rows (an unaware write, a flush)."""
        self.seq.clear()
        self.memo.clear()
        self.slices.clear()
        self.said.clear()


def current(conn: AsyncConnection | Connection | None) -> Pending | None:
    """The open batch of ``conn``'s commit, or ``None`` (none open, another connection's, or
    batching disabled)."""
    found = _CURRENT.get()
    if found is None or conn is None:
        return None
    sync = conn.sync_connection if isinstance(conn, AsyncConnection) else conn
    return found if sync is found.sync and not found.flushing else None


def aware(statement: Any) -> Any:
    """``statement`` marked as merging the pending rows itself (the guard lets it through)."""
    return statement.execution_options(**{AWARE: True})


async def execute(conn: AsyncConnection, statement: Any, rows: Sequence[Mapping[str, Any]]) -> None:
    """``statement`` over ``rows`` now, or held when ``conn``'s commit batches."""
    found = current(conn)
    if found is None:
        await conn.execute(statement, [dict(r) for r in rows])
    elif rows:
        found.defer(statement, rows)


def remembered(conn: AsyncConnection, key: Hashable) -> Any:
    """A read the open batch remembers (``MISSING``: none, or no batch)."""
    found = current(conn)
    return MISSING if found is None else found.memo.get(key, MISSING)


def remember(conn: AsyncConnection, key: Hashable, value: Any) -> None:
    found = current(conn)
    if found is not None:
        found.memo[key] = value


def numeric3(value: Any) -> float | None:
    """``value`` as a ``NUMERIC(4, 3)`` column stores it (rounded half away from zero, as
    PostgreSQL rounds a numeric)."""
    if value is None:
        return None
    return float(Decimal(repr(float(value))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


async def flush(conn: AsyncConnection) -> None:
    """Write what ``conn``'s batch holds now (a reader that cannot merge it calls this)."""
    found = _CURRENT.get()
    if found is None or conn.sync_connection is not found.sync or found.empty():
        return
    await conn.run_sync(lambda sync: _flush(found, sync))


@contextmanager
def disabled() -> Iterator[None]:
    """Within the block, ``batching`` batches nothing (the per-belief path)."""
    token = _DISABLED.set(True)
    try:
        yield
    finally:
        _DISABLED.reset(token)


@asynccontextmanager
async def batching(conn: AsyncConnection) -> AsyncIterator[Pending | None]:
    """Batch ``conn``'s epistemic writes until the block ends (see the module docstring); a
    nested call joins the open batch. The block's exception drops the batch (its transaction is
    rolled back)."""
    if _DISABLED.get() or _CURRENT.get() is not None or conn.sync_connection is None:
        yield _CURRENT.get()
        return
    sync = conn.sync_connection
    found = Pending(sync)
    listeners: list[tuple[str, Callable[..., Any]]] = [
        ("before_execute", lambda *a: _guard(found, *a)),
        ("savepoint", lambda c, _name: _savepoint(found, c)),
        ("rollback_savepoint", lambda *_: found.drop()),
        ("rollback", lambda *_: found.drop()),
    ]
    for name, listener in listeners:
        event.listen(sync, name, listener)
    token = _CURRENT.set(found)
    try:
        yield found
        await conn.run_sync(lambda c: _flush(found, c))
    finally:
        _CURRENT.reset(token)
        for name, listener in listeners:
            event.remove(sync, name, listener)
    log.debug(
        "epistemic writes batched", extra={"flushes": found.flushes, "guarded": found.guard_flushes}
    )


# -- the guard ---------------------------------------------------------------------------------


def _tables(clause: Any) -> set[str] | None:
    """The tables ``clause`` reads or writes (``None``: unknown, raw SQL)."""
    if isinstance(clause, TextClause) or not hasattr(clause, "get_children"):
        return None
    found: set[str] = set()
    for element in visitors.iterate(clause):
        if isinstance(element, TableClause):
            found.add(element.name)
        elif isinstance(element, TextClause) and element is clause:
            return None
    return found


def _options(clause: Any, given: Mapping[str, Any] | None) -> bool:
    if given and given.get(AWARE):
        return True
    own = getattr(clause, "get_execution_options", None)
    return bool(own is not None and own().get(AWARE))


def _guard(
    found: Pending,
    conn: Connection,
    clause: Any,
    _multiparams: Any,
    _params: Any,
    options: Mapping[str, Any] | None,
) -> None:
    if found.flushing or _options(clause, options):
        return
    touched = _tables(clause)
    # An unaware write flushes first too: it may reference a pending row (a fact citing a
    # proposition this commit wrote), and it may change what the memo remembers.
    writes = touched is None or isinstance(clause, (Insert, Update, Delete))
    if writes or touched is None or touched & found.tables():
        if not found.empty():
            found.guard_flushes += 1
            log.debug("pending writes flushed", extra={"tables": sorted(touched or ())})
        _flush(found, conn)
        found.forget()


def _savepoint(found: Pending, conn: Connection) -> None:
    if found.flushing:
        return
    _flush(found, conn)
    found.forget()


# -- the flush ---------------------------------------------------------------------------------


def _flush(found: Pending, conn: Connection) -> None:
    if found.empty():
        return
    found.flushing = True
    try:
        for table in ORDER:
            held = found.inserts.pop(table, None)
            if not held:
                continue
            if table == "events":
                _number(found, conn, [row for _, row in held])
            for statement, rows in _groups(held):
                for start in range(0, len(rows), CHUNK):
                    conn.execute(statement, rows[start : start + CHUNK])
            if table == "beliefs" and found.closing:
                from epistrel.core.tables.beliefs import beliefs

                conn.execute(
                    update(beliefs)
                    .where(beliefs.c.id.in_(list(found.closing)))
                    .values(tx_to=func.clock_timestamp())
                )
                found.closing.clear()
        if found.closing:  # closed rows whose insert was flushed before
            from epistrel.core.tables.beliefs import beliefs

            conn.execute(
                update(beliefs)
                .where(beliefs.c.id.in_(list(found.closing)))
                .values(tx_to=func.clock_timestamp())
            )
            found.closing.clear()
        for statement, parameters in _exposure_groups(found.exposures):
            conn.execute(statement, parameters)
        found.exposures.clear()
        if found.projection:
            from epistrel.core.epistemic import partitions

            for story_id, keys in sorted(found.projection.items()):
                partitions.refresh_sync(conn, story_id, keys)
            found.projection.clear()
        found.beliefs.clear()
        found.belief_rows.clear()
        found.transitions.clear()
        found.transition_rows.clear()
        found.events.clear()
        found.propositions.clear()
        found.slices.clear()  # the flushed rows are stored now: a slice would miss them
        found.said.clear()
        found.flushes += 1
    finally:
        found.flushing = False


def _number(found: Pending, conn: Connection, rows: Sequence[dict[str, Any]]) -> None:
    """Each pending event's ``seq``: the story's next, in write order (the caller holds the
    story lock)."""
    from epistrel.core.tables import events

    for row in rows:
        story_id = str(row["story_id"])
        last = found.seq.get(story_id)
        if last is None:
            last = int(
                conn.execute(
                    select(func.coalesce(func.max(events.c.seq), 0)).where(
                        events.c.story_id == story_id
                    )
                ).scalar_one()
            )
        last += 1
        row["seq"] = last
        found.seq[story_id] = last


def _groups(held: Sequence[tuple[Any, dict[str, Any]]]) -> list[tuple[Any, list[dict[str, Any]]]]:
    """``held``'s rows by statement (in first-write order), each group's rows filled to one
    column set (a column a row leaves out takes its default), so each is one statement."""
    by: dict[int, tuple[Any, list[dict[str, Any]]]] = {}
    for statement, row in held:
        by.setdefault(id(statement), (statement, []))[1].append(row)
    out: list[tuple[Any, list[dict[str, Any]]]] = []
    for statement, rows in by.values():
        columns = list(dict.fromkeys(k for row in rows for k in row))
        if any(len(row) != len(columns) for row in rows):
            table = statement.table
            filled = []
            for row in rows:
                full = dict(row)
                for name in columns:
                    if name not in full:
                        full[name] = column_default(table.c[name])
                filled.append(full)
            rows = filled
        out.append((statement, rows))
    return out


def column_default(column: Any) -> Any:
    """What the database puts in ``column`` when an insert leaves it out."""
    default = column.server_default
    if default is None:
        if column.nullable:
            return None
        raise ValueError(f"{column} has no default")
    arg = getattr(default, "arg", None)
    if isinstance(arg, TextClause):
        found = arg.text.strip()
        if found.startswith("'") and found.endswith("'"):
            inner = found[1:-1]
            return [] if inner == "{}" else inner
        try:
            return int(found)
        except ValueError:
            return float(found)
    if arg is not None and str(arg).lower() in ("false", "true"):
        return str(arg).lower() == "true"
    raise ValueError(f"{column}'s default is not a literal")


def _exposure_groups(
    held: Sequence[tuple[Any, dict[str, Any]]],
) -> list[tuple[Any, list[dict[str, Any]]]]:
    by: dict[int, tuple[Any, list[dict[str, Any]]]] = {}
    for statement, parameters in held:
        by.setdefault(id(statement), (statement, []))[1].append(parameters)
    return list(by.values())


__all__ = [
    "AWARE",
    "MISSING",
    "ORDER",
    "Pending",
    "aware",
    "batching",
    "column_default",
    "current",
    "disabled",
    "execute",
    "flush",
    "numeric3",
    "remember",
    "remembered",
]
