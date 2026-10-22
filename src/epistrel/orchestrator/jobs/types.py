"""The shapes a job handler works with: the claimed job, its context and its output (#70)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import input_versions
from epistrel.core.enums import OperationKind
from epistrel.core.events.operation import Operation


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A ``jobs`` row as a worker claimed it; ``attempt`` is the claim's fencing token."""

    id: str
    kind: str
    story_id: str | None
    payload: dict[str, Any]
    attempt: int
    failures: int
    due_at: datetime
    lease_until: datetime | None
    worker_id: str | None
    coalesce_key: str | None = None


@dataclass
class JobContext:
    """What a handler gets: the database, its claimed job, the validated payload, and
    ``declare_input`` to record the version of every input it reads (FR-CONC-8).

    Reads go through ``snapshot()``: one REPEATABLE READ, read-only transaction, so every input
    the handler declares is read from one consistent state. A handler that calls a model reads
    first, leaves the snapshot, and then calls (no transaction stays open across a model call).
    """

    db: AsyncEngine
    job: ClaimedJob
    payload: BaseModel
    input_versions: dict[str, Any] = field(default_factory=dict)

    @asynccontextmanager
    async def snapshot(self) -> AsyncIterator[AsyncConnection]:
        async with self.db.connect() as conn, conn.begin():
            await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            yield conn

    async def declare_input(self, conn: AsyncConnection, key: str) -> Any:
        """Read input ``key`` (``"<table>:<id>"``) now, record its version, and return it."""
        version = await input_versions.read_version(conn, key)
        self.input_versions[key] = version
        return version

    async def declare_inputs(self, conn: AsyncConnection, keys: Sequence[str]) -> None:
        """``declare_input`` for each of ``keys``, in their order, read in one statement per
        table (``input_versions.read_versions``, #452)."""
        for key, version in (await input_versions.read_versions(conn, keys)).items():
            self.input_versions[key] = version


Stage = Callable[[Operation], None]
"""Adds the job's output to its operation: events (``op.append``) and commit steps
(``op.add_commit_step``), which run under the story lock after the input recheck."""


@dataclass(frozen=True, slots=True)
class PositionedOutput:
    """One more operation a job commits with its own (#72): the same kind, positioned at
    ``turn``'s active variant (so its records follow that turn's membership), with its id fixed
    up front (another output may name it as a source) and its ``source_window``."""

    turn: int
    stage: Stage
    operation_id: str
    source_window: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class JobOutput:
    """A handler's output: ``stage`` writes it in the job's operation; ``result`` (JSON, ids and
    counts only, never story text) is stored on the job row. ``result_of`` builds the result
    inside the commit instead (e.g. counts a commit step produced). ``positioned`` are further
    operations committed in the same transaction, after the job's own and in order, under the
    same fencing and input recheck (#72: one per summary segment). ``source_window`` is the job
    operation's declared window (FR-STORE-27)."""

    stage: Stage | None = None
    result: dict[str, Any] | None = None
    result_of: Callable[[], dict[str, Any]] | None = None
    positioned: tuple[PositionedOutput, ...] = ()
    source_window: dict[str, Any] | None = None
    requeue_in: float | None = None
    """For a handler that records no operation: put the job back to ``queued``, due in this many
    seconds, instead of finishing it (#117's offboarding yields between batches and waits out a
    busy story). Not a failure: the attempt's token is spent, and the next claim takes a new
    one."""


Run = Callable[[JobContext], Coroutine[Any, Any, JobOutput]]


@dataclass(frozen=True, slots=True)
class JobHandler:
    """One job kind: its payload model, what it runs, and the kind of operation its output
    commits as (``maintenance`` or ``consolidation``; neither bumps the revision).

    ``max_attempts`` overrides the worker's ``job_max_attempts`` for this kind (recurring
    sweeps use 1, #71). ``records_operation=False`` is for housekeeping that writes no story
    state (the sweeps, the turn-queue safety net): the job finishes under its fencing token
    without an ``operations`` row, so a task running every 30 s does not grow the log."""

    kind: str
    payload: type[BaseModel]
    run: Run
    operation_kind: OperationKind = OperationKind.MAINTENANCE
    max_attempts: int | None = None
    records_operation: bool = True


__all__ = [
    "ClaimedJob",
    "JobContext",
    "JobHandler",
    "JobOutput",
    "PositionedOutput",
    "Run",
    "Stage",
]
