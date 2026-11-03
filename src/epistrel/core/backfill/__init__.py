"""Backfill: re-deriving a story's structured data from its stored transcript (FR-PROF-8,9;
ADR-086; #228).

**When.** A confirmed ``PUT /stories/{id}/engine-profile`` or ``PATCH
/stories/{id}/profile-overrides`` that adds data-producing capability to a story with history
(``impact.with_history``) holds the change and starts a backfill (``start``) instead of
applying it, unless every gained capability already covers the story's turns
(``coverage.gaps``: then it applies at once). ``POST /stories/{id}/backfill`` starts a
**gap fill** of the capabilities the story already has on.

**Start** (one transaction): a committed ``control`` operation with the ``story_paused`` event
(``cause: backfill``), the ``story_backfills`` row (the configuration it came from and the one
it applies), the story paused (``paused_by`` = the backfill), and its ``backfill`` job (the
caller's ``start_job``). While it runs, every truth-changing call is 409 ``story_paused`` with the
backfill's progress (``core.pause``), and reads are served; the effective configuration is still
the previous one, with ``pending_backfill``.

**The run** (``orchestrator.jobs.handlers.backfill``) extracts chunk by chunk, each chunk's
result stored as its checkpoint, then reconciles turn by turn in narrative order
(``core.backfill.reconcile``), writing through the live writers under the backfill operation
with provenance ``backfill``. Its rows stay ``dormant`` until **completion** (``complete``),
which in one transaction makes them ``active``, records the covered range, applies the held
configuration (an upgrade's), ends the backfill and resumes the story.

**Cancel** (``cancel``) discards the operation's rows (never deletes them: invariant 6), drops
the chunks, ends the backfill ``cancelled`` and resumes the story on its configuration.
A failed run ends the same way, ``failed``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, control, pause
from epistrel.core.backfill import coverage
from epistrel.core.backfill.conflicts import Resumed
from epistrel.core.enums import Membership, StoryStatus
from epistrel.core.errors import (
    BackfillInProgress,
    BackfillNotCancellable,
    BackfillNotFound,
    InvalidRequest,
    NothingToBackfill,
)
from epistrel.core.events.types import StoryPausedEvent, StoryResumedEvent
from epistrel.core.ids import new_ulid
from epistrel.core.membership import flip_membership
from epistrel.core.profiles import estimates
from epistrel.core.tables import (
    backfill_chunks,
    backfill_conflicts,
    operations,
    stories,
    story_backfills,
    transcript_messages,
)
from epistrel.core.tables.backfill import ACTIVE_STATUSES
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

CHUNK_TURNS = 30
"""Whole turns per extraction chunk (``backfill_chunk_turns``' default)."""
PARALLELISM = 4
"""Chunks extracted at once (``backfill_parallelism``' default)."""
BACKFILL_KIND = "backfill"
"""The job kind (#70)."""
TERMINAL = ("completed", "cancelled", "failed")

Kind = Literal["upgrade", "gap_fill", "resummarize"]


class Progress(BaseModel):
    """Where a backfill is: its phase, the chunks extracted of its total, the last turn its
    reconciliation committed (``null`` before), and the range's last turn."""

    phase: str
    chunks_done: int
    chunks_total: int
    reconciled_through_turn: int | None
    last_turn: int


class TurnRange(BaseModel):
    """A range of turns, both ends included."""

    from_turn: int
    to_turn: int


class ConflictCounts(BaseModel):
    """A backfill's contradictions by status (#230 opens and resolves them)."""

    open: int = 0
    resolved: int = 0
    uncertain: int = 0


class Backfill(BaseModel):
    """``GET /stories/{id}/backfill``: the story's latest backfill (see the module docstring)."""

    id: str
    status: str
    kind: str
    capabilities: list[str]
    range: TurnRange
    progress: Progress
    conflicts: ConflictCounts
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    report: dict[str, Any] = Field(default_factory=dict)
    conflict_list: list[Any] = Field(
        default_factory=list,
        description="#230: the backfill's conflicts with their statements (the open ones, or "
        "every one with `?conflicts=all`), each open one with its `recommended` answer.",
    )


class BackfillRequest(BaseModel):
    """``POST /stories/{id}/backfill``: a gap fill of the capabilities the story has on (or of
    ``capabilities``, a subset of them). Without ``ignore_warnings`` it answers the estimate
    and changes nothing."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"ignore_warnings": True}]}
    )

    ignore_warnings: StrictBool = False
    capabilities: list[StrictStr] | None = Field(default=None, min_length=1, max_length=50)


class Estimate(BaseModel):
    """What a backfill would take: model calls, tokens and duration (#109's formula)."""

    model_calls: int
    tokens: int
    duration_s: float


class Preview(BaseModel):
    """A gap fill's preview: whether one is needed, its estimate, range and capabilities."""

    needed: bool
    estimate: Estimate
    range: TurnRange
    capabilities: list[str]


class Queued(BaseModel):
    """#229: a profile edit's queued backfill of one story: the story, the backfill, its job, its
    status and its 1-based place in the deployment's queue."""

    story_id: str
    id: str
    job_id: str
    status: str
    queue_position: int | None = None


@dataclass(frozen=True, slots=True)
class Started:
    """A started backfill: its id, job and status (REST 202), with the story's revisions."""

    id: str
    job_id: str
    status: str
    timeline_revision: int = 0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class GapFill:
    """``POST /stories/{id}/backfill``'s answer: the preview (``applied`` false) or the
    started backfill."""

    applied: bool
    preview: Preview | None = None
    backfill: Started | None = None
    timeline_revision: int = 0
    replayed: bool = False
    resumed: Resumed | None = None
    """#230: on a backfill awaiting conflicts, the resume's answer."""


@dataclass(frozen=True, slots=True)
class BackfillCancelled:
    """``DELETE /stories/{id}/backfill``'s answer: the backfill's final state."""

    backfill: Backfill
    timeline_revision: int = 0
    replayed: bool = False


StartJob = Callable[[AsyncConnection, str, str], Awaitable[str]]
"""``start_job(conn, backfill_id, story_id) -> job_id``: enqueue the backfill's job (the
Orchestrator's)."""


# -- reads ---------------------------------------------------------------------------------------


async def _conflict_counts(conn: AsyncConnection, backfill_id: str) -> ConflictCounts:
    t = backfill_conflicts
    rows = await conn.execute(
        select(t.c.status, func.count()).where(t.c.backfill_id == backfill_id).group_by(t.c.status)
    )
    return ConflictCounts(**{str(status): int(n) for status, n in rows})


async def view(conn: AsyncConnection, row: Any) -> Backfill:
    return Backfill(
        id=str(row.id),
        status=str(row.status),
        kind=str(row.kind),
        capabilities=list(row.capabilities),
        range=TurnRange(from_turn=int(row.range_from_turn), to_turn=int(row.range_to_turn)),
        progress=Progress(**pause.backfill_progress(row)),
        conflicts=await _conflict_counts(conn, str(row.id)),
        requested_by=str(row.requested_by),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
        report=dict(row.report or {}),
    )


async def latest_row(conn: AsyncConnection, story_id: str) -> Any:
    t = story_backfills
    return (
        await conn.execute(
            select(t).where(t.c.story_id == story_id).order_by(t.c.id.desc()).limit(1)
        )
    ).one_or_none()


async def running(conn: AsyncConnection, story_id: str) -> Any:
    """The story's non-terminal backfill, if any."""
    t = story_backfills
    return (
        await conn.execute(
            select(t).where(t.c.story_id == story_id, t.c.status.in_(ACTIVE_STATUSES))
        )
    ).one_or_none()


async def get(
    db: AsyncEngine,
    story_id: str,
    *,
    conflicts: Literal["open", "all"] = "open",
    limit: int = 50,
) -> Backfill:
    """The story's latest backfill with its conflicts (#230: the ``open`` ones or ``all``); 404
    ``backfill_not_found`` when it never had one."""
    from epistrel.core.backfill import conflicts as backfill_conflicts_module

    async with db.connect() as conn:
        row = await latest_row(conn, story_id)
        if row is None:
            raise BackfillNotFound("the story has no backfill", {"story_id": story_id})
        found = await view(conn, row)
        listed = await backfill_conflicts_module.listing(conn, str(row.id), conflicts, limit)
        return found.model_copy(update={"conflict_list": listed})


async def refuse_running(conn: AsyncConnection, story_id: str, *, queued_ok: bool = False) -> None:
    """409 ``backfill_in_progress`` while a backfill of the story runs (``queued_ok``: not for a
    profile edit's backfill still queued, which the caller's change supersedes, #229)."""
    found = await running(conn, story_id)
    if found is not None and not (queued_ok and await waiting(conn, found)):
        raise BackfillInProgress(
            "a backfill of the story is running",
            {"backfill_id": str(found.id), "status": str(found.status)},
        )


# -- the estimate --------------------------------------------------------------------------------


async def estimate(
    conn: AsyncConnection, story_id: str, spans: Sequence[tuple[int, int]], modules: int
) -> Estimate:
    """#109's formula over the active transcript in ``spans``: one call per chunk per gained
    module, plus a salience and a time-estimation call per chunk; tokens are characters over
    four, the calls one after another."""
    t = transcript_messages
    if not spans or modules <= 0:
        return Estimate(model_calls=0, tokens=0, duration_s=0.0)
    c = t.c
    messages = characters = 0
    for low, high in spans:
        found = (
            await conn.execute(
                select(func.count(), func.coalesce(func.sum(func.length(c.text)), 0)).where(
                    c.story_id == story_id,
                    c.membership == Membership.ACTIVE.value,  # the transcript projection's own
                    c.narrative_turn.between(low, high),
                )
            )
        ).one()
        messages += int(found[0])
        characters += int(found[1])
    calls, tokens, duration = estimates.backfill_cost(messages, characters, modules + 2)
    return Estimate(model_calls=calls, tokens=tokens, duration_s=duration)


def span_of(gaps: Mapping[str, Sequence[tuple[int, int]]]) -> tuple[int, int] | None:
    """The one range covering every capability's gaps (first to last turn)."""
    lows = [low for spans in gaps.values() for low, _ in spans]
    highs = [high for spans in gaps.values() for _, high in spans]
    return (min(lows), max(highs)) if lows else None


def chunk_ranges(low: int, high: int, size: int = CHUNK_TURNS) -> list[tuple[int, int]]:
    """``[low, high]`` as chunks of ``size`` whole turns."""
    return [(start, min(start + size - 1, high)) for start in range(low, high + 1, size)]


# -- start ---------------------------------------------------------------------------------------


async def start(
    conn: AsyncConnection,
    story_id: str,
    *,
    kind: Kind,
    capabilities: Sequence[str],
    span: tuple[int, int],
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    principal: str,
    start_job: StartJob,
) -> Started:
    """Start a backfill in ``conn``'s transaction (see the module docstring); the caller holds
    the story's row lock. A queued profile-edit backfill of the story not yet claimed is
    superseded (#229)."""
    await supersede_queued(conn, story_id)
    await refuse_running(conn, story_id)
    backfill_id = new_ulid()
    low, high = span
    chunks = chunk_ranges(low, high)
    await conn.execute(
        insert(story_backfills).values(
            id=backfill_id,
            story_id=story_id,
            kind=kind,
            source=dict(source),
            target=dict(target),
            capabilities=list(capabilities),
            range_from_turn=low,
            range_to_turn=high,
            chunks_total=len(chunks),
            requested_by=principal,
        )
    )
    await _chunks(conn, backfill_id, story_id, span)
    job_id = await start_job(conn, backfill_id, story_id)
    await conn.execute(
        update(story_backfills).where(story_backfills.c.id == backfill_id).values(job_id=job_id)
    )
    operation_id = await control.record_control_operation(conn, actor=principal, story_id=story_id)
    await conn.execute(
        update(stories)
        .where(stories.c.id == story_id)
        .values(status=StoryStatus.PAUSED.value, paused_by=backfill_id)
    )
    from epistrel.core.ratings.change import control_event  # here: change imports profiles

    await control_event(
        conn,
        story_id=story_id,
        operation_id=operation_id,
        type_="story_paused",
        payload=StoryPausedEvent(job_id=job_id, cause=pause.BACKFILL),
        actor=principal,
    )
    revision = await conn.scalar(
        select(stories.c.timeline_revision).where(stories.c.id == story_id)
    )
    log.info("backfill started", backfill_id=backfill_id, chunks=len(chunks), kind=kind)
    return Started(backfill_id, job_id, "queued", int(revision or 0))


# -- the gap fill ---------------------------------------------------------------------------------


async def gap_fill(
    db: AsyncEngine,
    story_id: str,
    request: BackfillRequest,
    *,
    principal: str,
    start_job: StartJob,
    complete_conflicts: Any = None,
) -> GapFill:
    """``POST /stories/{id}/backfill`` (see the module docstring): the preview without
    ``ignore_warnings``, else the started backfill; 409 ``nothing_to_backfill`` when no
    capability the story has on has a gap, 409 ``backfill_in_progress`` while one runs, 422
    ``validation_error`` for a capability it does not have on."""
    from epistrel.core.profiles import effective

    async with db.begin() as conn:
        status = await conn.scalar(
            select(stories.c.status).where(stories.c.id == story_id).with_for_update()
        )
        if status is None:
            from epistrel.core.errors import StoryNotFound

            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        awaiting = await running(conn, story_id)
        if (
            awaiting is not None
            and str(awaiting.status) == "paused_for_conflicts"
            and complete_conflicts is not None
        ):
            # #230: the resume of a backfill waiting for its conflicts.
            from epistrel.core.backfill import conflicts as backfill_conflicts_module

            locked = await _lock(conn, str(awaiting.id))
            resumed = await backfill_conflicts_module.resume(
                conn,
                locked,
                ignore_warnings=request.ignore_warnings,
                principal=principal,
                complete=complete_conflicts,
            )
            revision = int(
                await conn.scalar(
                    select(stories.c.timeline_revision).where(stories.c.id == story_id)
                )
                or 0
            )
            return GapFill(applied=resumed.applied, resumed=resumed, timeline_revision=revision)
        await refuse_running(conn, story_id)
        current = await effective.effective_configuration(conn, story_id, validate=False)
        enabled = coverage.capabilities_of(current.settings)
        wanted = enabled
        if request.capabilities is not None:
            unknown = sorted(set(request.capabilities) - set(enabled))
            if unknown:
                raise InvalidRequest(
                    "a gap fill names capabilities the story's configuration has on",
                    {"field": "body.capabilities", "not_enabled": unknown},
                )
            wanted = {c: enabled[c] for c in request.capabilities}
        missing = await coverage.gaps_of(conn, story_id, wanted)
        span = span_of(missing)
        if span is None:
            raise NothingToBackfill(
                "every capability the story has on covers its turns", {"story_id": story_id}
            )
        revision = int(
            await conn.scalar(select(stories.c.timeline_revision).where(stories.c.id == story_id))
            or 0
        )
        found = await estimate(conn, story_id, [span], len(missing))
        preview = Preview(
            needed=True,
            estimate=found,
            range=TurnRange(from_turn=span[0], to_turn=span[1]),
            capabilities=sorted(missing),
        )
        if not request.ignore_warnings:
            return GapFill(applied=False, preview=preview, timeline_revision=revision)
        started = await start(
            conn,
            story_id,
            kind="gap_fill",
            capabilities=sorted(missing),
            span=span,
            source={
                "profile_id": current.profile_id,
                "profile_version": current.version,
                "overrides_revision": current.overrides_revision,
            },
            target={},
            principal=principal,
            start_job=start_job,
        )
        return GapFill(applied=False, preview=preview, backfill=started, timeline_revision=revision)


# -- ending --------------------------------------------------------------------------------------


async def _lock(conn: AsyncConnection, backfill_id: str) -> Any:
    return (
        await conn.execute(
            select(story_backfills).where(story_backfills.c.id == backfill_id).with_for_update()
        )
    ).one_or_none()


async def _resume(conn: AsyncConnection, row: Any, outcome: str) -> None:
    """Resume the story the backfill paused (when it still is), with ``story_resumed``."""
    story_id = str(row.story_id)
    paused_by = await conn.scalar(
        select(stories.c.paused_by).where(stories.c.id == story_id).with_for_update()
    )
    if paused_by != row.id:
        return
    await conn.execute(
        update(stories)
        .where(stories.c.id == story_id)
        .values(status=StoryStatus.ACTIVE.value, paused_by=None)
    )
    operation_id = await control.record_control_operation(
        conn, actor=str(row.requested_by), story_id=story_id
    )
    from epistrel.core.ratings.change import control_event

    await control_event(
        conn,
        story_id=story_id,
        operation_id=operation_id,
        type_="story_resumed",
        payload=StoryResumedEvent(job_id=str(row.job_id or row.id), outcome=outcome),
        actor=str(row.requested_by),
    )


async def discard(conn: AsyncConnection, row: Any, *, status: str, error: str | None) -> None:
    """End ``row`` without its results: its operation's rows (and the operation) discarded, its
    chunks dropped, the story resumed on its configuration."""
    if row.operation_id is not None:
        from epistrel.core.genesis import deferred

        await flip_membership(
            conn, [str(row.operation_id)], Membership.DISCARDED, story_id=str(row.story_id)
        )
        await deferred.undo(conn, str(row.story_id), str(row.operation_id))  # #231
        await conn.execute(
            update(operations)
            .where(operations.c.operation_id == row.operation_id)
            .values(
                membership=Membership.DISCARDED.value, membership_tx=func.transaction_timestamp()
            )
        )
    await conn.execute(delete(backfill_chunks).where(backfill_chunks.c.backfill_id == row.id))
    await conn.execute(
        update(story_backfills)
        .where(story_backfills.c.id == row.id)
        .values(status=status, error=error, finished_at=clock.now())
    )
    await _resume(conn, row, status)


async def cancel(db: AsyncEngine, story_id: str) -> Backfill:
    """Cancel the story's running backfill (see the module docstring): 404
    ``backfill_not_found`` without one, 409 ``backfill_not_cancellable`` when it has ended."""
    async with db.begin() as conn:
        latest = await latest_row(conn, story_id)
        if latest is None:
            raise BackfillNotFound("the story has no backfill", {"story_id": story_id})
        row = await _lock(conn, str(latest.id))
        if row is None or row.status not in ACTIVE_STATUSES:
            raise BackfillNotCancellable(
                "the backfill has ended",
                {"backfill_id": str(latest.id), "status": str(latest.status)},
            )
        await discard(conn, row, status="cancelled", error=None)
        log.info("backfill cancelled", backfill_id=str(row.id))
        found = await latest_row(conn, story_id)
        return await view(conn, found)


ApplyTarget = Callable[[AsyncConnection, str, Mapping[str, Any], str], Awaitable[None]]
"""``apply(conn, story_id, target, principal)``: apply a held configuration (#109's write)."""


async def complete(
    conn: AsyncConnection,
    row: Any,
    *,
    apply_target: ApplyTarget,
    after_complete: Callable[[AsyncConnection, str], Awaitable[None]] | None = None,
) -> None:
    """The completion transaction (see the module docstring); ``after_complete(conn,
    story_id)`` enqueues the follow-up jobs (consolidation, importance, embedding)."""
    from epistrel.core.profiles import effective  # here: profiles imports this package

    story_id = str(row.story_id)
    if row.operation_id is not None:
        await flip_membership(conn, [str(row.operation_id)], Membership.ACTIVE, story_id=story_id)
    if row.kind == "upgrade" and row.target:
        with pause.holding(str(row.id)):
            await apply_target(conn, story_id, dict(row.target), str(row.requested_by))
    current = await effective.effective_configuration(conn, story_id, validate=False)
    levels = coverage.capabilities_of(current.settings)
    for capability in row.capabilities:
        await coverage.add(
            conn,
            story_id,
            capability,
            int(row.range_from_turn),
            int(row.range_to_turn),
            source=coverage.BACKFILL,
            level=levels.get(capability),
            operation_id=None if row.operation_id is None else str(row.operation_id),
        )
    await conn.execute(
        update(story_backfills)
        .where(story_backfills.c.id == row.id)
        .values(status="completed", finished_at=clock.now())
    )
    await _resume(conn, row, "completed")
    if after_complete is not None:
        await after_complete(conn, story_id)
    log.info("backfill completed", backfill_id=str(row.id))


# -- the profile-edit queue (#229) ---------------------------------------------------------------

SINGLE_STORY = "single_story"
PROFILE_EDIT = "profile_edit"
QUEUE_LOCK = "backfill_queue"
"""The advisory lock a claim takes (``pg_advisory_xact_lock(hashtext(...))``)."""

ClaimOutcome = Literal["claimed", "wait", "ended"]


async def _chunks(
    conn: AsyncConnection, backfill_id: str, story_id: str, span: tuple[int, int]
) -> int:
    chunks = chunk_ranges(*span)
    await conn.execute(
        insert(backfill_chunks),
        [
            {
                "backfill_id": backfill_id,
                "story_id": story_id,
                "chunk_index": index,
                "from_turn": first,
                "to_turn": last,
            }
            for index, (first, last) in enumerate(chunks)
        ],
    )
    return len(chunks)


async def enqueue(
    conn: AsyncConnection,
    story_id: str,
    *,
    capabilities: Sequence[str],
    span: tuple[int, int],
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    principal: str,
    start_job: StartJob,
) -> Started:
    """Queue a profile edit's backfill of one story (#229) in ``conn``'s transaction: the
    ``story_backfills`` row (``queued``, ``origin = profile_edit``) and its job, the story **not**
    paused (the handler pauses it when it claims the row, ``claim``). A queued row of the story
    not yet claimed is superseded (``supersede_queued``)."""
    await supersede_queued(conn, story_id)
    await refuse_running(conn, story_id)
    backfill_id = new_ulid()
    low, high = span
    await conn.execute(
        insert(story_backfills).values(
            id=backfill_id,
            story_id=story_id,
            kind="upgrade",
            origin=PROFILE_EDIT,
            source=dict(source),
            target=dict(target),
            capabilities=list(capabilities),
            range_from_turn=low,
            range_to_turn=high,
            chunks_total=0,
            requested_by=principal,
        )
    )
    total = await _chunks(conn, backfill_id, story_id, span)
    job_id = await start_job(conn, backfill_id, story_id)
    await conn.execute(
        update(story_backfills)
        .where(story_backfills.c.id == backfill_id)
        .values(job_id=job_id, chunks_total=total)
    )
    log.info("backfill queued", backfill_id=backfill_id, chunks=total)
    return Started(backfill_id, job_id, "queued")


async def waiting(conn: AsyncConnection, row: Any) -> bool:
    """Whether ``row`` is a profile edit's backfill not yet claimed (its story not paused by
    it)."""
    if str(row.kind) == "resummarize":
        return True  # #232: a summary rebuild never pauses its story and is superseded
    if str(row.origin) != PROFILE_EDIT or str(row.status) != "queued":
        return False
    paused_by = await conn.scalar(select(stories.c.paused_by).where(stories.c.id == row.story_id))
    return bool(paused_by != row.id)


async def supersede_queued(conn: AsyncConnection, story_id: str) -> bool:
    """Cancel the story's queued profile-edit backfill not yet claimed (a later change of the
    story's configuration supersedes it, #229); whether there was one. The story keeps its
    pin."""
    found = await running(conn, story_id)
    if found is None or not await waiting(conn, found):
        return False
    await conn.execute(delete(backfill_chunks).where(backfill_chunks.c.backfill_id == found.id))
    await conn.execute(
        update(story_backfills)
        .where(story_backfills.c.id == found.id)
        .values(status="cancelled", error="superseded", finished_at=clock.now())
    )
    log.info("queued backfill superseded", backfill_id=str(found.id), kind=str(found.kind))
    return True


def _unclaimed_before(seq: int) -> Any:
    t = story_backfills.c
    return (
        t.origin == PROFILE_EDIT,
        t.status == "queued",
        t.seq < seq,
        stories.c.paused_by.is_distinct_from(t.id),
    )


def _joined() -> Any:
    return story_backfills.join(stories, stories.c.id == story_backfills.c.story_id)


async def queue_position(conn: AsyncConnection, row: Any) -> int | None:
    """``row``'s 1-based place among the queued profile-edit backfills not yet claimed."""
    if not await waiting(conn, row):
        return None
    ahead = await conn.scalar(
        select(func.count()).select_from(_joined()).where(*_unclaimed_before(int(row.seq)))
    )
    return int(ahead or 0) + 1


async def _pause(conn: AsyncConnection, row: Any) -> None:
    """Pause the story for its claimed backfill (``start``'s pause)."""
    story_id = str(row.story_id)
    principal = str(row.requested_by)
    operation_id = await control.record_control_operation(conn, actor=principal, story_id=story_id)
    await conn.execute(
        update(stories)
        .where(stories.c.id == story_id)
        .values(status=StoryStatus.PAUSED.value, paused_by=row.id)
    )
    from epistrel.core.ratings.change import control_event

    await control_event(
        conn,
        story_id=story_id,
        operation_id=operation_id,
        type_="story_paused",
        payload=StoryPausedEvent(job_id=str(row.job_id or row.id), cause=pause.BACKFILL),
        actor=principal,
    )


Replan = Callable[[AsyncConnection, Any], Awaitable[tuple[list[str], tuple[int, int] | None]]]
"""``replan(conn, row) -> (capabilities, span)``: the gained capabilities and their range now
(``None``: nothing left to derive)."""


async def claim(
    conn: AsyncConnection,
    backfill_id: str,
    *,
    concurrency: int,
    replan: Replan,
    apply_target: ApplyTarget,
) -> ClaimOutcome:
    """Claim a queued profile-edit backfill (#229) in ``conn``'s transaction, under the queue's
    advisory lock: ``wait`` while ``concurrency`` backfills already run, an earlier queued one
    is unclaimed or the story is paused otherwise; else the range re-planned (``replan``: turns
    committed since, or a newer target) and the story paused (``claimed``), or, with nothing
    left to derive, the held configuration applied (the pin cleared) and the row ``completed``
    (``ended``)."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtext(QUEUE_LOCK))))
    row = await _lock(conn, backfill_id)
    if row is None or str(row.status) in TERMINAL:
        return "ended"
    if not await waiting(conn, row):
        return "claimed"
    t = story_backfills.c
    earlier = await conn.scalar(
        select(func.count()).select_from(_joined()).where(*_unclaimed_before(int(row.seq)))
    )
    running_now = await conn.scalar(
        select(func.count())
        .select_from(_joined())
        .where(
            t.status.in_(ACTIVE_STATUSES),
            t.status != "paused_for_conflicts",  # #230: waiting for the author frees its slot
            stories.c.paused_by == t.id,
        )
    )
    status = await conn.scalar(
        select(stories.c.status).where(stories.c.id == row.story_id).with_for_update()
    )
    if earlier or int(running_now or 0) >= concurrency or status != StoryStatus.ACTIVE.value:
        return "wait"
    capabilities, span = await replan(conn, row)
    await conn.execute(delete(backfill_chunks).where(backfill_chunks.c.backfill_id == row.id))
    if span is None:
        with pause.holding(str(row.id)):
            await apply_target(conn, str(row.story_id), dict(row.target), str(row.requested_by))
        await conn.execute(
            update(story_backfills)
            .where(story_backfills.c.id == row.id)
            .values(status="completed", finished_at=clock.now(), report={"no_op": True})
        )
        log.info("queued backfill ended: nothing to derive", backfill_id=str(row.id))
        return "ended"
    total = await _chunks(conn, str(row.id), str(row.story_id), span)
    await conn.execute(
        update(story_backfills)
        .where(story_backfills.c.id == row.id)
        .values(
            capabilities=list(capabilities),
            range_from_turn=span[0],
            range_to_turn=span[1],
            chunks_total=total,
        )
    )
    await _pause(conn, row)
    log.info("queued backfill claimed", backfill_id=str(row.id), chunks=total)
    return "claimed"


__all__ = [
    "BACKFILL_KIND",
    "CHUNK_TURNS",
    "PARALLELISM",
    "PROFILE_EDIT",
    "SINGLE_STORY",
    "TERMINAL",
    "Backfill",
    "BackfillCancelled",
    "BackfillRequest",
    "Estimate",
    "GapFill",
    "Preview",
    "Progress",
    "Queued",
    "Started",
    "cancel",
    "chunk_ranges",
    "claim",
    "complete",
    "discard",
    "enqueue",
    "estimate",
    "gap_fill",
    "get",
    "latest_row",
    "queue_position",
    "refuse_running",
    "running",
    "span_of",
    "start",
    "supersede_queued",
    "view",
    "waiting",
]
