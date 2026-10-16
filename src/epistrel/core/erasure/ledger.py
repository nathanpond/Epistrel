"""The erasure ledger outside the database, its in-database copy, and reconciliation
(FR-ADMIN-7; #125).

**The file is the authority.** ``<EPISTREL_ERASURE_LEDGER_DIR>/ledger.jsonl`` holds one JSON line
per erasure, appended and never rewritten: ``{erasure_id, subject: {kind, id}, scope, story_id,
at, principal, operation_id, counts, scan_term_hashes, ledger_hash_prev, ledger_hash}``
(identifiers, counts and keyed hashes, never a name). ``at`` is ISO 8601 UTC with microseconds.
``ledger_hash`` is the SHA-256 of the line's canonical JSON without it (sorted keys, compact
separators), and ``ledger_hash_prev`` is the previous line's ``ledger_hash`` (64 zeros for the
first), so a changed, removed or reordered line breaks the chain. The directory lives on its own
volume, outside the database and the backups it governs; the ``erasure_ledger`` table holds the
same rows, with the same hashes, so every ``pg_dump`` carries a copy.

**Writing.** An erasure (#124) appends its line inside its own transaction, before the commit,
under the ``LEDGER_LOCK_KEY`` advisory transaction lock (held to the commit, so the chain's order
is the commit order across processes) and an ``fcntl.flock`` on the file (for the write itself);
the line is fsynced, then the row inserted. A file that cannot be written refuses the erasure
(503 ``ledger_unavailable``); a line whose transaction then fails is reapplied by the next
reconcile, which only repeats an erasure an operator asked for.

**Reconciling** compares the two, in two passes:

1. ``mark`` (at start-up, before traffic; and on an Engine's first story call): a line the table
   lacks (a restored backup that predates the erasure) is inserted with ``applied_at`` null
   (pending), and its story becomes ``blocked_by_erasure`` (its status kept in
   ``status_before_block``). A row the file lacks (a lost or new ledger volume) is appended to the
   file, chained after its last line, in ``at`` order. A row present in both whose fields differ
   is ``LedgerMismatch``: the ledger was tampered with, and the process refuses to serve. A
   pending entry whose story no longer exists is settled ``story_missing``. Rows a fork copied
   (``copied_from``, #127) take part in neither direction. With nothing to do it writes nothing.
2. ``reapply`` (in the background, under the ``RECONCILE_LOCK_KEY`` session advisory lock that
   ``serve`` and ``worker`` processes share): each pending entry, the catalog scope first, then
   in ``at`` order, is applied again by #124's operation with its original ``erasure_id``,
   principal ``system`` and no new ledger line, then settled (``applied_at``, ``applied_note =
   reapplied``); a story whose last pending entry is settled gets its status back. An entry whose
   subject no longer exists is settled ``subject_missing``. A failure leaves the entry pending
   and its story blocked, counts an attempt, and is retried by ``erasure_reconcile_retry`` every
   five minutes, with no terminal state.

**A blocked story** is refused, reads and writes alike, with 503 ``erasure_pending``
(``Retry-After: 30``): by ``check_story`` (every Engine method naming the story), by
``Operation`` (``refuse_blocked``; the reapply's own operations pass, inside ``reapplying``),
and by the job claim, which leaves the story's jobs queued. ``GET /stories`` lists it with its
status, and ``DELETE /stories/{id}`` deletes it (its entries
are settled ``story_deleted``).
A pending **catalog-scope** entry blocks no story; what it reaches in the catalog is refused
or left out of lists until it is reapplied (``pending_catalog``; ``core.erasure.catalog_block``,
#346, ADR-157).

The run's state (``in_progress``, ``last_run_at``, ``last_error``, ``attempts``) is
``deployment_state.erasure_reconcile_state``, written by reconcile runs only. Log lines carry
counts and identifiers only (invariant 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import ErasurePending, LedgerUnavailable
from epistrel.core.tables import deployment_state, erasure_ledger, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.observability.logging import get_logger
from epistrel.testing import pauses

log = get_logger(__name__)

LEDGER_FILE = "ledger.jsonl"
GENESIS_HASH = "0" * 64
LEDGER_LOCK_KEY = 0x45_5241_5345  # "ERASE": serialises ledger appends to the commit order
RECONCILE_LOCK_KEY = 0x45_5243_4E43  # "ERCNC": one reapply at a time across processes
REAPPLY_BUDGET_S = 600.0
"""The start-up reapply's budget; entries still pending after it are retried by the job."""
STORY, CATALOG = "story", "catalog"
REAPPLIED, STORY_MISSING, SUBJECT_MISSING, STORY_DELETED = (
    "reapplied",
    "story_missing",
    "subject_missing",
    "story_deleted",
)
BLOCKED = StoryStatus.BLOCKED_BY_ERASURE.value


class LedgerCorrupt(Exception):
    """The ledger file has a malformed line or a broken hash chain: the process refuses to
    serve (exit 78). ``reason`` is ``malformed`` or ``chain_broken``; ``line`` is 1-based."""

    def __init__(self, reason: str, line: int) -> None:
        super().__init__(f"the erasure ledger file is {reason.replace('_', ' ')} at line {line}")
        self.reason = reason
        self.line = line


class LedgerMismatch(Exception):
    """A row in both the file and the table differs between them: the ledger was tampered
    with, and the process refuses to serve."""

    def __init__(self, erasure_id: str) -> None:
        super().__init__(
            f"erasure ledger entry {erasure_id} differs between the ledger file and the database"
        )
        self.erasure_id = erasure_id


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def canonical(document: Mapping[str, Any]) -> bytes:
    """Sorted keys, compact separators, UTF-8: the bytes a line's hash is taken over."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True, slots=True)
class Entry:
    """One erasure, as the file and the table both hold it."""

    erasure_id: str
    at: datetime
    subject_kind: str
    subject_id: str
    scope: str
    story_id: str | None
    principal: str
    operation_id: str | None
    counts: Mapping[str, Any]
    scan_term_hashes: tuple[str, ...]
    ledger_hash_prev: str | None = None
    ledger_hash: str | None = None

    def body(self) -> dict[str, Any]:
        """The line without its own hash."""
        return {
            "erasure_id": self.erasure_id,
            "subject": {"kind": self.subject_kind, "id": self.subject_id},
            "scope": self.scope,
            "story_id": self.story_id,
            "at": _iso(self.at),
            "principal": self.principal,
            "operation_id": self.operation_id,
            "counts": dict(self.counts),
            "scan_term_hashes": list(self.scan_term_hashes),
            "ledger_hash_prev": self.ledger_hash_prev,
        }

    def line(self) -> dict[str, Any]:
        return {**self.body(), "ledger_hash": self.ledger_hash}

    def chained(self, previous: str) -> Entry:
        """This entry chained after ``previous`` (a line's ``ledger_hash``)."""
        linked = replace(self, ledger_hash_prev=previous)
        return replace(linked, ledger_hash=hashlib.sha256(canonical(linked.body())).hexdigest())

    def same(self, other: Entry) -> bool:
        """Whether two copies agree: every field, and the hashes when both are chained."""
        if (
            self.erasure_id,
            self.at,
            self.subject_kind,
            self.subject_id,
            self.scope,
            self.story_id,
            self.principal,
            self.operation_id,
            json.loads(canonical(self.counts)),
            tuple(self.scan_term_hashes),
        ) != (
            other.erasure_id,
            other.at,
            other.subject_kind,
            other.subject_id,
            other.scope,
            other.story_id,
            other.principal,
            other.operation_id,
            json.loads(canonical(other.counts)),
            tuple(other.scan_term_hashes),
        ):
            return False
        if self.ledger_hash is None or other.ledger_hash is None:
            return True
        return (self.ledger_hash_prev, self.ledger_hash) == (
            other.ledger_hash_prev,
            other.ledger_hash,
        )

    @classmethod
    def from_line(cls, document: Mapping[str, Any]) -> Entry:
        """Parse one line (``ValueError``/``KeyError``/``TypeError`` on a malformed one)."""
        subject = document["subject"]
        at = datetime.fromisoformat(str(document["at"]))
        if at.tzinfo is None:
            raise ValueError("at has no timezone")
        counts = document["counts"]
        hashes = document["scan_term_hashes"]
        if not isinstance(counts, dict) or not isinstance(hashes, list):
            raise TypeError("counts must be an object and scan_term_hashes a list")
        return cls(
            erasure_id=_text(document["erasure_id"]),
            at=at,
            subject_kind=_text(subject["kind"]),
            subject_id=_text(subject["id"]),
            scope=_text(document["scope"]),
            story_id=_optional(document["story_id"]),
            principal=_text(document["principal"]),
            operation_id=_optional(document["operation_id"]),
            counts=counts,
            scan_term_hashes=tuple(_text(h) for h in hashes),
            ledger_hash_prev=_text(document["ledger_hash_prev"]),
            ledger_hash=_text(document["ledger_hash"]),
        )

    @classmethod
    def from_row(cls, row: Any) -> Entry:
        return cls(
            erasure_id=row.id,
            at=row.at,
            subject_kind=row.subject_kind,
            subject_id=row.subject_id,
            scope=row.scope,
            story_id=row.story_ref,
            principal=row.principal,
            operation_id=row.operation_id,
            counts=dict(row.counts or {}),
            scan_term_hashes=tuple(row.scan_term_hashes or ()),
            ledger_hash_prev=row.ledger_hash_prev,
            ledger_hash=row.ledger_hash,
        )

    def row_values(self) -> dict[str, Any]:
        """The table's columns for this entry (``applied_at`` is the caller's)."""
        return {
            "id": self.erasure_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "scope": self.scope,
            "story_ref": self.story_id,
            "principal": self.principal,
            "operation_id": self.operation_id,
            "counts": dict(self.counts),
            "scan_term_hashes": list(self.scan_term_hashes),
            "ledger_hash_prev": self.ledger_hash_prev,
            "ledger_hash": self.ledger_hash,
            "at": self.at,
        }


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected a non-empty string")
    return value


def _optional(value: Any) -> str | None:
    return None if value is None else _text(value)


def parse(content: str) -> list[Entry]:
    """Every line of a ledger file's ``content``, its chain checked (``LedgerCorrupt``)."""
    entries: list[Entry] = []
    previous = GENESIS_HASH
    for number, raw in enumerate(content.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise TypeError("a line must be a JSON object")
            entry = Entry.from_line(document)
        except (ValueError, KeyError, TypeError) as exc:
            raise LedgerCorrupt("malformed", number) from exc
        if entry.ledger_hash_prev != previous or entry.chained(previous) != entry:
            raise LedgerCorrupt("chain_broken", number)
        entries.append(entry)
        previous = entry.ledger_hash or GENESIS_HASH
    return entries


class LedgerFile:
    """``<directory>/ledger.jsonl``: read, checked and appended to under ``fcntl.flock``."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / LEDGER_FILE

    def prepare(self) -> None:
        """Create the directory and the file and prove them writable (``LedgerUnavailable``
        with ``reason: unwritable`` otherwise; ``epistrel serve`` exits 78)."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8"):
                pass
            if not os.access(self.path, os.W_OK):
                raise PermissionError(str(self.path))
        except OSError as exc:
            raise LedgerUnavailable(
                f"the erasure ledger directory {self.directory} cannot be written "
                f"(EPISTREL_ERASURE_LEDGER_DIR): {type(exc).__name__}",
                {"reason": "unwritable"},
            ) from None

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> list[Entry]:
        """Every entry, the chain checked (``LedgerCorrupt``); ``[]`` when there is no file."""
        if not self.exists():
            return []
        return parse(self.path.read_text(encoding="utf-8"))

    def append(self, entries: Sequence[Entry]) -> list[Entry]:
        """Chain ``entries`` after the file's last line and append them, fsynced, under an
        exclusive ``flock``; returns them chained. ``LedgerCorrupt`` when the file is broken."""
        if not entries:
            return []
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = parse(handle.read().decode("utf-8"))
                previous = existing[-1].ledger_hash if existing else GENESIS_HASH
                chained: list[Entry] = []
                for entry in entries:
                    linked = entry.chained(previous or GENESIS_HASH)
                    chained.append(linked)
                    previous = linked.ledger_hash
                handle.seek(0, os.SEEK_END)
                handle.write(b"".join(canonical(e.line()) + b"\n" for e in chained))
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return chained


def ledger_problem(ledger: LedgerFile) -> str | None:
    """The start-up check of the file alone (``epistrel serve``'s pre-flight, exit 78, and the
    app's start-up): the directory can be written, every line parses and the chain holds.
    ``None`` when the process may start."""
    try:
        ledger.prepare()
        ledger.read()
    except LedgerUnavailable as exc:
        return f"error: EPISTREL_ERASURE_LEDGER_DIR: {exc}"
    except LedgerCorrupt as exc:
        return f"error: EPISTREL_ERASURE_LEDGER_DIR: {exc}; restore the file from a copy"
    except OSError as exc:
        return (
            "error: EPISTREL_ERASURE_LEDGER_DIR: the erasure ledger file cannot be read "
            f"({type(exc).__name__})"
        )
    return None


# -- writing one erasure (#124's transaction) -------------------------------------------------


async def write(
    conn: AsyncConnection, ledger: LedgerFile | None, entry: Entry, *, applied_at: datetime
) -> Entry:
    """Record ``entry``: the file line first (when ``ledger`` is given), then the row, both in
    ``conn``'s transaction under ``LEDGER_LOCK_KEY``. 503 ``ledger_unavailable`` when the file
    cannot be appended to; the transaction then rolls back with nothing written."""
    if ledger is not None:
        await conn.execute(select(func.pg_advisory_xact_lock(LEDGER_LOCK_KEY)))
        try:
            (entry,) = await asyncio.to_thread(ledger.append, [entry])
        except LedgerCorrupt as exc:
            raise LedgerUnavailable(
                f"the erasure ledger file is broken ({exc.reason}, line {exc.line}); the "
                "erasure was refused",
                {"reason": exc.reason},
            ) from None
        except OSError as exc:
            raise LedgerUnavailable(
                f"the erasure ledger file could not be written: {type(exc).__name__}; the "
                "erasure was refused",
                {"reason": "unwritable"},
            ) from None
    await conn.execute(insert(erasure_ledger).values(**entry.row_values(), applied_at=applied_at))
    return entry


# -- the block ----------------------------------------------------------------------------------

_REAPPLYING: ContextVar[str | None] = ContextVar("epistrel_erasure_reapplying", default=None)


@contextlib.contextmanager
def reapplying(story_id: str | None) -> Iterator[None]:
    """The block lets the reapply's own operations on ``story_id`` through (its summary
    regeneration and projection rebuild) inside this block."""
    token = _REAPPLYING.set(story_id)
    try:
        yield
    finally:
        _REAPPLYING.reset(token)


def _pending_rows() -> Any:
    return and_(erasure_ledger.c.applied_at.is_(None), erasure_ledger.c.copied_from.is_(None))


async def pending_error(conn: AsyncConnection, story_id: str) -> ErasurePending:
    """The 503 for a blocked story: its first pending erasure and how many it has."""
    found = (
        await conn.execute(
            select(erasure_ledger.c.id)
            .where(_pending_rows(), erasure_ledger.c.story_ref == story_id)
            .order_by(erasure_ledger.c.at, erasure_ledger.c.id)
        )
    ).all()
    return ErasurePending(
        f"story {story_id} is not served until the erasure ledger has been reapplied to it",
        {
            "story_id": story_id,
            "erasure_id": found[0].id if found else None,
            "pending_count": len(found),
        },
    )


async def refuse_blocked(conn: AsyncConnection, story_id: str, status: str | None) -> None:
    """503 ``erasure_pending`` when the story is blocked, unless this is its reapply."""
    if status == BLOCKED and _REAPPLYING.get() != story_id:
        raise await pending_error(conn, story_id)


async def refuse_catalog_pending(conn: AsyncConnection) -> None:
    """503 ``erasure_pending`` while a catalog-scope erasure is pending (#378): it blocks no
    story, but what it removes may still be anywhere the catalog reaches (an undelivered
    feedback export). ``story_id`` is null in the details."""
    found = (
        await conn.execute(
            select(erasure_ledger.c.id)
            .where(_pending_rows(), erasure_ledger.c.scope == CATALOG)
            .order_by(erasure_ledger.c.at, erasure_ledger.c.id)
        )
    ).all()
    if found:
        raise ErasurePending(
            "this is not served until the erasure ledger's catalog erasures have been reapplied",
            {"story_id": None, "erasure_id": found[0].id, "pending_count": len(found)},
        )


async def pending_catalog(conn: AsyncConnection) -> list[Pending]:
    """The pending catalog-scope entries, in ``at`` order: what ``core.erasure.catalog_block``
    refuses the catalog reads they reach for until each is reapplied (#346, ADR-157)."""
    return [item for item in await _pending(conn) if item.scope == CATALOG]


async def check_story(db: AsyncEngine, story_id: str) -> None:
    """The Engine's check before any read or write naming ``story_id``. An unknown story
    passes (the call itself answers 404)."""
    async with db.connect() as conn:
        status = await conn.scalar(select(stories.c.status).where(stories.c.id == story_id))
        await refuse_blocked(conn, story_id, status)


async def settle_deleted(conn: AsyncConnection, story_id: str) -> int:
    """A deleted story's pending entries are settled ``story_deleted`` (in the deletion's
    transaction); returns how many."""
    result = await conn.execute(
        update(erasure_ledger)
        .where(_pending_rows(), erasure_ledger.c.story_ref == story_id)
        .values(applied_at=clock.now(), applied_note=STORY_DELETED)
    )
    return int(result.rowcount or 0)


# -- reconciling --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pending:
    """One entry the database has not applied yet."""

    erasure_id: str
    scope: str
    story_id: str | None
    subject_kind: str
    subject_id: str
    at: datetime
    confirmed: tuple[str, ...] = ()
    """The candidate ids the erasure confirmed (its counts' ``confirmed_candidates``,
    ADR-154): the reapply erases them too."""


@dataclass(slots=True)
class MarkReport:
    """What a ``mark`` pass did: lines inserted as pending, rows appended to the file, stories
    blocked, entries settled because their story is gone, and what is pending now."""

    inserted: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    story_missing: list[str] = field(default_factory=list)
    pending: list[Pending] = field(default_factory=list)


def _pending_of(row: Any) -> Pending:
    counts = row.counts if isinstance(row.counts, Mapping) else {}
    confirmed = counts.get("confirmed_candidates")
    return Pending(
        row.id,
        row.scope,
        row.story_ref,
        row.subject_kind,
        row.subject_id,
        row.at,
        tuple(c for c in confirmed if isinstance(c, str)) if isinstance(confirmed, list) else (),
    )


async def _pending(conn: AsyncConnection) -> list[Pending]:
    """Every pending entry, catalog scope first, then in ``at`` order."""
    found = await conn.execute(
        select(erasure_ledger)
        .where(_pending_rows())
        .order_by(
            (erasure_ledger.c.scope != CATALOG).asc(), erasure_ledger.c.at, erasure_ledger.c.id
        )
    )
    return [_pending_of(row) for row in found]


async def mark(db: AsyncEngine, ledger: LedgerFile) -> MarkReport:
    """The comparison pass (see the module docstring). ``LedgerCorrupt`` for a broken file,
    ``LedgerMismatch`` for a row the two copies disagree on."""
    report = MarkReport()
    lines = await asyncio.to_thread(ledger.read)
    async with db.begin() as conn:
        any_row = await conn.scalar(select(erasure_ledger.c.id).limit(1))
        if not lines and any_row is None:
            return report
        # Both copies are read under the append lock: an erasure committing meanwhile has
        # either written both or neither.
        await conn.execute(select(func.pg_advisory_xact_lock(LEDGER_LOCK_KEY)))
        lines = await asyncio.to_thread(ledger.read)
        rows = {
            row.id: row
            for row in await conn.execute(
                select(erasure_ledger).where(erasure_ledger.c.copied_from.is_(None))
            )
        }
        for entry in lines:
            row = rows.pop(entry.erasure_id, None)
            if row is None:
                await conn.execute(insert(erasure_ledger).values(**entry.row_values()))
                report.inserted.append(entry.erasure_id)
                continue
            stored = Entry.from_row(row)
            if not stored.same(entry):
                raise LedgerMismatch(entry.erasure_id)
            if stored.ledger_hash is None:
                await _set_hashes(conn, entry)
        if rows:
            missing = sorted(rows.values(), key=lambda r: (r.at, r.id))
            appended = await asyncio.to_thread(
                ledger.append, [Entry.from_row(row) for row in missing]
            )
            for entry in appended:
                await _set_hashes(conn, entry)
                report.appended.append(entry.erasure_id)
        report.pending = await _pending(conn)
        for item in report.pending:
            if item.story_id is None:
                continue
            status = await conn.scalar(
                select(stories.c.status).where(stories.c.id == item.story_id).with_for_update()
            )
            if status is None:
                await _settle(conn, item.erasure_id, STORY_MISSING)
                report.story_missing.append(item.erasure_id)
                continue
            if status != BLOCKED:
                await conn.execute(
                    update(stories)
                    .where(stories.c.id == item.story_id)
                    .values(status=BLOCKED, status_before_block=status)
                )
                report.blocked.append(item.story_id)
        report.pending = [p for p in report.pending if p.erasure_id not in report.story_missing]
    if report.inserted or report.appended or report.blocked or report.story_missing:
        log.info(
            "erasure ledger compared",
            inserted=len(report.inserted),
            appended=len(report.appended),
            blocked=len(report.blocked),
            story_missing=len(report.story_missing),
            pending=len(report.pending),
            erasure_ids=report.inserted + report.appended,
            story_ids=report.blocked,
        )
    return report


async def _set_hashes(conn: AsyncConnection, entry: Entry) -> None:
    await conn.execute(
        update(erasure_ledger)
        .where(erasure_ledger.c.id == entry.erasure_id)
        .values(ledger_hash_prev=entry.ledger_hash_prev, ledger_hash=entry.ledger_hash)
    )


async def _settle(conn: AsyncConnection, erasure_id: str, note: str) -> None:
    await conn.execute(
        update(erasure_ledger)
        .where(erasure_ledger.c.id == erasure_id, erasure_ledger.c.applied_at.is_(None))
        .values(applied_at=clock.now(), applied_note=note)
    )


async def _unblock(conn: AsyncConnection, story_id: str) -> bool:
    """Give the story its status back once nothing of it is pending; whether it was."""
    still = await conn.scalar(
        select(func.count())
        .select_from(erasure_ledger)
        .where(_pending_rows(), erasure_ledger.c.story_ref == story_id)
    )
    if still:
        return False
    result = await conn.execute(
        update(stories)
        .where(stories.c.id == story_id, stories.c.status == BLOCKED)
        .values(status=stories.c.status_before_block, status_before_block=None)
    )
    return bool(result.rowcount)


ReapplyOne = Callable[[Pending], Awaitable[str]]
"""Apply one pending entry again; returns its note (``reapplied``, ``subject_missing`` or
``story_missing``)."""


@dataclass(slots=True)
class ReapplyReport:
    """What a ``reapply`` run did. ``ran`` is false when another process held the run."""

    ran: bool = False
    settled: dict[str, str] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    unblocked: list[str] = field(default_factory=list)


async def _state(conn: AsyncConnection) -> dict[str, Any]:
    found = await conn.scalar(
        select(deployment_state.c.erasure_reconcile_state).where(
            deployment_state.c.id == DEPLOYMENT_ROW_ID
        )
    )
    return dict(found or {})


async def _save_state(db: AsyncEngine, state: Mapping[str, Any]) -> None:
    async with db.begin() as conn:
        await conn.execute(
            update(deployment_state)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .values(erasure_reconcile_state=dict(state))
        )


async def reapply(db: AsyncEngine, run_one: ReapplyOne) -> ReapplyReport:
    """The reapply pass (see the module docstring), one process at a time."""
    report = ReapplyReport()
    async with db.connect() as holder:
        got = await holder.scalar(select(func.pg_try_advisory_lock(RECONCILE_LOCK_KEY)))
        await holder.commit()
        if not got:
            return report
        report.ran = True
        try:
            async with db.connect() as conn:
                state = await _state(conn)
                pending = await _pending(conn)
            if not pending and not state.get("in_progress") and not state.get("attempts"):
                return report  # nothing to do: nothing is written
            attempts: dict[str, int] = dict(state.get("attempts") or {})
            state = {**state, "in_progress": True, "last_error": None}
            await _save_state(db, {**state, "attempts": attempts})
            last_error: str | None = None
            try:
                for item in pending:
                    await pauses.pause(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY)
                    try:
                        with reapplying(item.story_id):
                            note = await run_one(item)
                    except Exception as exc:  # stays pending, story blocked; retried later
                        attempts[item.erasure_id] = attempts.get(item.erasure_id, 0) + 1
                        last_error = type(exc).__name__
                        report.failed.append(item.erasure_id)
                        log.warning(
                            "erasure reapply failed",
                            erasure_id=item.erasure_id,
                            story_id=item.story_id,
                            exc_type=last_error,
                            attempt=attempts[item.erasure_id],
                        )
                        continue
                    async with db.begin() as conn:
                        await _settle(conn, item.erasure_id, note)
                        if item.story_id is not None and await _unblock(conn, item.story_id):
                            report.unblocked.append(item.story_id)
                    attempts.pop(item.erasure_id, None)
                    report.settled[item.erasure_id] = note
            finally:
                await _save_state(
                    db,
                    {
                        **state,
                        "in_progress": False,
                        "last_run_at": _iso(clock.now()),
                        "last_error": last_error,
                        "attempts": attempts,
                    },
                )
        finally:
            await holder.execute(select(func.pg_advisory_unlock(RECONCILE_LOCK_KEY)))
            await holder.commit()
    return report


def log_summary(marked: MarkReport, report: ReapplyReport) -> None:
    """A reconcile run's one INFO line: counts and identifiers only (invariant 3)."""
    log.info(
        "erasure ledger reconciled",
        ran=report.ran,
        inserted=len(marked.inserted),
        appended=len(marked.appended),
        blocked=len(marked.blocked),
        settled=len(report.settled),
        failed=len(report.failed),
        unblocked=len(report.unblocked),
        erasure_ids=sorted({*marked.inserted, *report.settled, *report.failed}),
        story_ids=sorted({*marked.blocked, *report.unblocked}),
    )


@dataclass(frozen=True, slots=True)
class ReconcileRun:
    """``POST /admin/erasure/reconcile``: the run's id, whether it ran (another process may
    hold it), how many entries it settled and failed, and how many are still pending."""

    run_id: str
    ran: bool
    settled: int
    failed: int
    pending_count: int


# -- status ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingItem:
    erasure_id: str
    story_id: str | None


@dataclass(frozen=True, slots=True)
class ReconcileStatus:
    """``GET /admin/erasure/status``: what is pending, what is applied, and the last run."""

    pending: tuple[PendingItem, ...]
    pending_count: int
    applied_count: int
    in_progress: bool
    last_run_at: datetime | None
    last_error: str | None
    attempts: Mapping[str, int]


RUN_HELD = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND classid::bigint = :high AND objid::bigint = :low AND objsubid = 1)"
)
"""Whether some session holds the reapply's advisory lock (a one-key lock: ``objsubid = 1``)."""


async def pending_count(conn: AsyncConnection) -> int:
    return int(
        await conn.scalar(select(func.count()).select_from(erasure_ledger).where(_pending_rows()))
        or 0
    )


async def status(db: AsyncEngine) -> ReconcileStatus:
    async with db.connect() as conn:
        pending = await _pending(conn)
        applied = int(
            await conn.scalar(
                select(func.count())
                .select_from(erasure_ledger)
                .where(erasure_ledger.c.applied_at.is_not(None))
            )
            or 0
        )
        state = await _state(conn)
        held = await conn.scalar(
            RUN_HELD, {"high": RECONCILE_LOCK_KEY >> 32, "low": RECONCILE_LOCK_KEY & 0xFFFFFFFF}
        )
    last = state.get("last_run_at")
    return ReconcileStatus(
        pending=tuple(PendingItem(p.erasure_id, p.story_id) for p in pending),
        pending_count=len(pending),
        applied_count=applied,
        in_progress=bool(state.get("in_progress")) and bool(held),
        last_run_at=datetime.fromisoformat(last) if isinstance(last, str) else None,
        last_error=state.get("last_error"),
        attempts=dict(state.get("attempts") or {}),
    )


__all__ = [
    "BLOCKED",
    "CATALOG",
    "GENESIS_HASH",
    "LEDGER_FILE",
    "LEDGER_LOCK_KEY",
    "REAPPLIED",
    "REAPPLY_BUDGET_S",
    "RECONCILE_LOCK_KEY",
    "STORY",
    "STORY_DELETED",
    "STORY_MISSING",
    "SUBJECT_MISSING",
    "Entry",
    "LedgerCorrupt",
    "LedgerFile",
    "LedgerMismatch",
    "MarkReport",
    "Pending",
    "PendingItem",
    "ReapplyOne",
    "ReapplyReport",
    "ReconcileRun",
    "ReconcileStatus",
    "canonical",
    "check_story",
    "ledger_problem",
    "log_summary",
    "mark",
    "parse",
    "pending_catalog",
    "pending_count",
    "pending_error",
    "reapply",
    "reapplying",
    "refuse_blocked",
    "refuse_catalog_pending",
    "settle_deleted",
    "status",
    "write",
]
