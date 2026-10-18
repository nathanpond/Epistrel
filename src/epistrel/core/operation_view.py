"""One operation as the history API returns it: ``GET /operations/{id}`` (FR-CONC-6; #61).

The view answers "what happened to my request": the operation's kind, status, position (turn
and variant), the story's revision once it committed, who asked, when, the viewpoint its result
was produced for, its revalidation attempts (#47), its origin and, for an ingested batch, every
operation the batch committed together (#58). A committed Lite turn also reports its wait in its
story's queue and its delay to the first model call (``queued_ms``, ``first_call_offset_ms``,
from its turn report; #76). A story's genesis from a scenario keeps its full creation report
(#122) on its ``genesis`` operation, returned only to a read naming the ``omniscient``
viewpoint. Within the result window the stored result of
the operation's ``Idempotency-Key`` is included — for a viewpoint-bound result (a turn) only
when the read names that same viewpoint; another named viewpoint is 409
``idempotency_conflict``, and a read naming none gets the view without the result.

**Long-running operations (FR-EXT-16's status surface, M3 subset; #128).** The view names what
an operation was for (``label``) and its ``stages`` ``[{name, status, at}]``, each ``status``
one of ``pending``, ``running``, ``done``, ``failed`` or ``skipped`` and ``at`` when the stage
reached it (null while pending); an erasure's summary regeneration can also be ``partial``:

- ``genesis`` (label ``genesis``): ``validated``, ``snapshotted`` (``skipped`` for a blank
  story) and ``materialized``, all at the commit, since a story's creation is one transaction.
- a rating change's ``control`` operation (label ``rating_check``) or a reactivation check's
  (``reactivation_check``): ``checking``, then ``applied`` (``reactivated``) or ``refused``,
  from the check's ``rating_check_jobs`` row (#118, #120).
- an offboarding's ``control`` operation (label ``offboarding``): one stage per entity type
  in the job's processing order, from its ``offboarding_jobs`` row (#117: its progress index
  and current entry; ``at`` is the row's last update).
- an erasure (label ``erasure``): ``redacted``, ``ledger_written``, ``regenerated`` and
  ``rebuilt`` from ``operations.stage_log`` (#124). A catalog erasure touches no story, so its
  ``regenerated`` and ``rebuilt`` are ``skipped``; a story erasure's later stages are
  ``pending`` until the work after its commit records them.

Any other operation has no label and ``stages: []``.

**The read set (#136).** An operation that captured one reports ``read_set``: ``keys_count`` (the
query keys of its structured lookups, FR-CONC-11; 0 for a Lite turn), ``retrieved_count`` (rows
read as generation context only) and, to a read naming the ``omniscient`` viewpoint only, the
keys themselves (``{subject, predicate, object, holder, window, result_hash}``: ids and hashes);
``null`` for an operation with no read set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind, OperationStatus
from epistrel.core.errors import EpistrelError, IdempotencyConflict
from epistrel.core.idempotency import StoredResult, stored_result
from epistrel.core.readset import read_set_counts, stored_keys
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import offboarding_jobs, operations, rating_check_jobs
from epistrel.core.tables.rating_checks import RatingCheckKind, RatingCheckStatus


class StageStatus(StrEnum):
    """Where one stage of a long-running operation is."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"
    """Some of the stage's work was done and the rest degraded (an erasure's summary
    regeneration that rewrote some windows only, #124)."""


@dataclass(frozen=True, slots=True)
class Stage:
    """One stage of a long-running operation: its name, status and when it got there."""

    name: str
    status: StageStatus
    at: datetime | None


class OperationLabel(StrEnum):
    """What a long-running operation was for (its ``kind`` alone does not say)."""

    GENESIS = "genesis"
    RATING_CHECK = "rating_check"
    REACTIVATION_CHECK = "reactivation_check"
    OFFBOARDING = "offboarding"
    ERASURE = "erasure"


GENESIS_STAGES = ("validated", "snapshotted", "materialized")
ERASURE_STAGES = ("redacted", "ledger_written", "regenerated", "rebuilt")
STORY_ONLY_ERASURE_STAGES = frozenset({"regenerated", "rebuilt"})


class OperationNotFound(EpistrelError):
    """No operation has the id a call names."""

    code = "not_found"
    http_status = 404


@dataclass(frozen=True, slots=True)
class ReadSetView:
    """An operation's read set as the view reports it (see the module docstring)."""

    keys_count: int
    retrieved_count: int
    keys: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class OperationView:
    id: str
    story_id: str | None
    kind: str
    status: str
    narrative_turn: int | None
    turn_variant: int | None
    timeline_revision: int | None
    principal: str
    started_at: datetime
    committed_at: datetime | None
    viewpoint: str | None
    origin: str
    batch_operation_ids: list[str] | None
    attempts: list[dict[str, Any]]
    config_revisions: dict[str, Any] | None
    idempotency_key: str | None
    result: StoredResult | None = None
    queued_ms: int | None = None
    """A committed turn's wait in its story's queue (``None``: not queued, or not a turn; #76)."""
    first_call_offset_ms: int | None = None
    """A committed turn's delay from reaching the front to its first model call (#76)."""
    creation_report: dict[str, Any] | None = None
    """A story's ``genesis`` operation from a scenario: its full creation report (#122),
    returned only to a read naming the ``omniscient`` viewpoint."""
    label: OperationLabel | None = None
    """What a long-running operation was for (see the module docstring), else ``None``."""
    stages: list[Stage] = field(default_factory=list)
    """A long-running operation's stages, in order (see the module docstring)."""
    read_set: ReadSetView | None = None
    """The operation's read set (#136), ``None`` when it captured none."""


INGESTED = "ingested"


async def read_operation(
    db: AsyncEngine, operation_id: str, *, viewpoint: str | None = None
) -> OperationView:
    """The view of ``operation_id``; see the module docstring."""
    async with db.connect() as conn:
        row = (
            await conn.execute(select(operations).where(operations.c.operation_id == operation_id))
        ).one_or_none()
        if row is None:
            raise OperationNotFound(
                f"operation {operation_id} does not exist", {"operation_id": operation_id}
            )
        batch: list[str] | None = None
        if row.origin == INGESTED and row.committed_at is not None:
            # A batch commits in one transaction: its operations share ``committed_at``.
            batch = list(
                await conn.scalars(
                    select(operations.c.operation_id)
                    .where(
                        operations.c.story_id == row.story_id,
                        operations.c.origin == INGESTED,
                        operations.c.committed_at == row.committed_at,
                    )
                    .order_by(operations.c.narrative_turn, operations.c.operation_id)
                )
            )
        result = await stored_result(conn, operation_id)
        label, stages = await _stages(conn, row)
    if result is not None and row.viewpoint is not None and viewpoint != row.viewpoint:
        if viewpoint is not None:
            raise IdempotencyConflict(
                "the operation's result was produced for another output viewpoint",
                {"reason": "viewpoint"},
            )
        result = None
    return OperationView(
        id=row.operation_id,
        story_id=row.story_id,
        kind=str(row.kind),
        status=str(row.status),
        narrative_turn=row.narrative_turn,
        turn_variant=row.turn_variant,
        timeline_revision=row.timeline_revision,
        principal=row.principal,
        started_at=row.started_at,
        committed_at=row.committed_at,
        viewpoint=row.viewpoint,
        origin=row.origin,
        batch_operation_ids=batch,
        attempts=list(row.attempts or []),
        config_revisions=row.config_revisions,
        idempotency_key=row.idempotency_key,
        result=result,
        queued_ms=_report_ms(row.turn_report, "queued_ms"),
        first_call_offset_ms=_report_ms(row.turn_report, "first_call_offset_ms"),
        creation_report=_creation_report(row, viewpoint),
        label=label,
        stages=stages,
        read_set=_read_set(row.read_set, viewpoint),
    )


def _omniscient(viewpoint: str | None) -> bool:
    return viewpoint is not None and viewpoint.strip().casefold() == OMNISCIENT


def _read_set(document: Any, viewpoint: str | None) -> ReadSetView | None:
    """Counts for every read; the keys for an omniscient one only (#136)."""
    if document is None:
        return None
    counts = read_set_counts(document)
    return ReadSetView(
        keys_count=counts["keys_count"],
        retrieved_count=counts["retrieved_count"],
        keys=[k.view() for k in stored_keys(document)] if _omniscient(viewpoint) else None,
    )


async def _stages(conn: AsyncConnection, row: Any) -> tuple[OperationLabel | None, list[Stage]]:
    """The label and stages of ``row``'s operation (see the module docstring)."""
    if str(row.kind) == OperationKind.GENESIS.value:
        return OperationLabel.GENESIS, _genesis_stages(row)
    if str(row.kind) != OperationKind.CONTROL.value:
        return None, []
    check = (
        await conn.execute(
            select(rating_check_jobs).where(rating_check_jobs.c.operation_id == row.operation_id)
        )
    ).first()
    if check is not None:
        return _check_stages(check)
    offboarding = (
        await conn.execute(
            select(offboarding_jobs).where(offboarding_jobs.c.operation_id == row.operation_id)
        )
    ).first()
    if offboarding is not None:
        return OperationLabel.OFFBOARDING, _offboarding_stages(offboarding)
    logged = [s for s in (row.stage_log or []) if isinstance(s, dict)]
    if any(s.get("stage") == ERASURE_STAGES[0] for s in logged):
        return OperationLabel.ERASURE, _erasure_stages(row, logged)
    return None, []


def _genesis_stages(row: Any) -> list[Stage]:
    """A story's creation is one transaction: every stage ends at its commit."""
    status = _terminal(str(row.status))
    from_scenario = isinstance(row.turn_report, dict) and "creation_report" in row.turn_report
    return [
        Stage(
            name,
            StageStatus.SKIPPED if name == "snapshotted" and not from_scenario else status,
            row.committed_at,
        )
        for name in GENESIS_STAGES
    ]


def _terminal(status: str) -> StageStatus:
    """A stage status for work that ended with its operation (``committed`` is done)."""
    if status == OperationStatus.COMMITTED.value:
        return StageStatus.DONE
    if status == OperationStatus.IN_FLIGHT.value:
        return StageStatus.RUNNING
    return StageStatus.FAILED


def _check_stages(check: Any) -> tuple[OperationLabel, list[Stage]]:
    """A rating or reactivation check: ``checking``, then its outcome (#118, #120)."""
    reactivation = str(check.kind) == RatingCheckKind.REACTIVATION.value
    label = OperationLabel.REACTIVATION_CHECK if reactivation else OperationLabel.RATING_CHECK
    passed = (
        RatingCheckStatus.REACTIVATED.value if reactivation else RatingCheckStatus.APPLIED.value
    )
    status = str(check.status)
    checking = Stage("checking", StageStatus.RUNNING, check.started_at)
    outcome = Stage(passed, StageStatus.PENDING, None)
    if status == RatingCheckStatus.FAILED.value:
        checking = Stage("checking", StageStatus.FAILED, check.finished_at)
        outcome = Stage(passed, StageStatus.SKIPPED, check.finished_at)
    elif status != RatingCheckStatus.CHECKING.value:
        checking = Stage("checking", StageStatus.DONE, check.finished_at)
        outcome = Stage(status, StageStatus.DONE, check.finished_at)
    return label, [checking, outcome]


def _offboarding_stages(job: Any) -> list[Stage]:
    """One stage per entity type, in the snapshot's processing order (#117)."""
    snapshot = [e for e in (job.snapshot or []) if isinstance(e, dict)]
    progress = job.progress if isinstance(job.progress, dict) else {}
    done = int(progress.get("index") or 0)
    status = str(job.status)
    order: dict[str, list[int]] = {}
    for index, entry in enumerate(snapshot):
        order.setdefault(str(entry.get("type")), []).append(index)
    stages: list[Stage] = []
    for name, indexes in order.items():
        if indexes[-1] < done:
            stages.append(Stage(name, StageStatus.DONE, job.updated_at))
        elif status == "failed":
            failed = indexes[0] <= done
            stages.append(
                Stage(
                    name,
                    StageStatus.FAILED if failed else StageStatus.SKIPPED,
                    job.finished_at or job.updated_at,
                )
            )
        elif indexes[0] <= done and status == "running":
            stages.append(Stage(name, StageStatus.RUNNING, job.updated_at))
        else:
            stages.append(Stage(name, StageStatus.PENDING, None))
    return stages


def _erasure_stages(row: Any, logged: list[dict[str, Any]]) -> list[Stage]:
    """An erasure's recorded stages, in their canonical order (#124)."""
    recorded = {str(s.get("stage")): s for s in logged}
    catalog = row.story_id is None
    stages: list[Stage] = []
    for name in ERASURE_STAGES:
        entry = recorded.get(name)
        if entry is not None:
            stages.append(Stage(name, _recorded(entry.get("status")), _at(entry, row)))
        elif catalog and name in STORY_ONLY_ERASURE_STAGES:
            stages.append(Stage(name, StageStatus.SKIPPED, row.committed_at))
        elif name == "ledger_written":
            # A reapply (#125) writes no new ledger entry: the ledger already holds it.
            stages.append(Stage(name, StageStatus.SKIPPED, row.committed_at))
        else:
            stages.append(Stage(name, StageStatus.PENDING, None))
    return stages


def _recorded(value: Any) -> StageStatus:
    """A recorded stage's status (an unknown value reads as ``failed``)."""
    try:
        return StageStatus(str(value))
    except ValueError:
        return StageStatus.FAILED


def _at(entry: dict[str, Any], row: Any) -> datetime | None:
    """A recorded stage's time; the commit's for an entry written before stages had one."""
    value = entry.get("at")
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    committed: datetime | None = row.committed_at
    return committed


def _creation_report(row: Any, viewpoint: str | None) -> dict[str, Any] | None:
    """A genesis operation's persisted creation report (#122), for an omniscient read only."""
    if str(row.kind) != OperationKind.GENESIS.value or not isinstance(row.turn_report, dict):
        return None
    if viewpoint is None or viewpoint.strip().casefold() != OMNISCIENT:
        return None
    found = row.turn_report.get("creation_report")
    return found if isinstance(found, dict) else None


def _report_ms(report: Any, name: str) -> int | None:
    """An integer field of the turn report (``operations.turn_report``), if recorded."""
    value = report.get(name) if isinstance(report, dict) else None
    return value if isinstance(value, int) else None


__all__ = [
    "ERASURE_STAGES",
    "GENESIS_STAGES",
    "OperationLabel",
    "OperationNotFound",
    "OperationView",
    "Stage",
    "StageStatus",
    "read_operation",
]
