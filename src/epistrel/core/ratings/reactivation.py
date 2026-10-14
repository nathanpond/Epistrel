"""Re-check turns against the current guardrails before any reactivation (FR-RATE-7; #120).

A stricter rating cannot be undone through the back door of rewinding, swapping or forking old
content. Three edits make turns active again: ``untruncate`` (#65; a reactivation, or the swap
it becomes over newer turns), ``swap`` (#66), and ``fork`` with a ``variant_chain`` that is not
the active chain (#67: the dormant continuation becomes the fork's active history). Each one's
outer entry (the ``Engine`` method) calls ``guard`` after the edit's own validation, with the
story locked:

- **Which turns.** ``activates`` plans the edit as it would run now and lists the committed
  operations it would make active (for a fork: the copied operations on the chain above where
  it parts from the active one, through ``at_turn``). Only ``turn`` operations are compared.
- **Unchecked.** A turn is unchecked when its ``checked_guardrails`` (#118's stamp: ``{rating_id,
  rating_version_id, global, rating, story, character}``, each ``{policy_id: version}``) lacks
  a policy of the story's current effective set (``stamp.live_policies``: the live global
  policies, the rating closure's, the story's and its characters'), compared by policy id and
  integer version. A stamp of ``NULL`` lacks every policy, whatever left the turn unrecorded.
  The rating's version id is not compared: an edit of its guidance alone checks nothing.
- **Nothing unchecked.** ``guard`` answers ``None`` and the edit runs as it always did, with no
  guardrail check at all.
- **Otherwise** the edit is held back. In one transaction: a ``rating_check_jobs`` row with
  ``kind = reactivation`` (from and target: the story's rating at its current version), its
  #70 ``rating_check`` job whose payload carries the edit (``Continuation``), a committed
  ``control`` operation that binds the request's ``Idempotency-Key`` and records
  ``story_paused`` (``cause: reactivation_check``), and the story ``paused``. The caller gets
  #118's ``RatingCheckStarted`` (REST 202 with ``Location``: the check).

The job (``orchestrator.jobs.handlers.rating_check``) waits for a turn in flight to commit,
then ``read_pending`` plans the edit again and reads the unchecked turns' player inputs and
rendered beats (and their story images, none in M3), which it evaluates against the current
policies (``change.evaluate_history``). ``settle``, under the story lock, runs the check again
when the timeline or the policy set moved since that read; otherwise it stamps every turn that
passed with the current set, so a later attempt never checks it again, even when another turn
failed. Any violation refuses the edit: the check is ``refused`` with every violation by
position and policy (never the text), the story resumes, and nothing else changes. When every
turn passed, ``apply`` runs the edit itself while the story is still paused
(``pause.holding``: only this job's own calls pass), as the request would have run it (same
principal, same membership hooks; the request's ``X-Epistrel-Expected-Revision`` was checked at
request time and is not checked again), and ``end`` resumes the story with the check
``reactivated`` and its ``outcome``: ``{operation, result}``, the answer the edit would have
given synchronously (a fork's new story included). An edit the timeline no longer allows ends
the check ``failed`` with its error class, the story resumed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, control, fork, pause, timeline
from epistrel.core.enums import Membership, OperationKind, OperationStatus, StoryStatus
from epistrel.core.errors import RevisionConflict, StoryArchived, StoryNotFound, StoryPaused
from epistrel.core.events.types import StoryPausedEvent, StoryResumedEvent
from epistrel.core.policies.model import Policy
from epistrel.core.ratings import change
from epistrel.core.ratings.stamp import STAMP_SCOPES, live_policies, stamp_of, story_rating
from epistrel.core.tables import operations, rating_check_jobs, stories
from epistrel.core.tables.rating_checks import RatingCheckKind, RatingCheckStatus
from epistrel.core.variants import (
    Chain,
    SwapRequest,
    fork_selection,
    list_chains,
    load_tree,
    plan_swap,
)


class Edit(StrEnum):
    """The edits that make turns active again."""

    UNTRUNCATE = "untruncate"
    SWAP = "swap"
    FORK = "fork"


class Continuation(BaseModel):
    """The edit a reactivation check holds back (the job's payload): which edit, of which
    story, its request body, and the principal who asked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Edit
    story_id: str
    args: dict[str, Any] = Field(default_factory=dict)
    principal: str


@dataclass(frozen=True, slots=True)
class PendingTurn:
    """A turn variant the edit would make active whose stamp lacks a current policy."""

    operation_id: str
    turn: int
    variant: int


StartJob = Callable[[AsyncConnection, Continuation], Awaitable[str]]
"""Enqueue the check's job (its payload carries the continuation) in the caller's transaction
and return its id (the Engine's, which owns the Orchestrator's job queue)."""


# -- the comparison ---------------------------------------------------------------------------


def held_policies(stamp: Mapping[str, Any] | None) -> dict[str, int]:
    """``{policy_id: version}`` of every policy a stamp records, every scope together."""
    held: dict[str, int] = {}
    if not stamp:
        return held
    for scope in STAMP_SCOPES:
        for policy_id, version in dict(stamp.get(scope.value) or {}).items():
            held[str(policy_id)] = int(version)
    return held


def missing(stamp: Mapping[str, Any] | None, current: Sequence[Policy]) -> tuple[Policy, ...]:
    """The policies of ``current`` that ``stamp`` does not record at their version (all of them
    for a ``NULL`` stamp)."""
    held = held_policies(stamp)
    return tuple(p for p in current if held.get(p.id) != p.version)


def policy_key(found: Sequence[Policy]) -> frozenset[tuple[str, int]]:
    return frozenset((p.id, p.version) for p in found)


async def current_set(
    conn: AsyncConnection, story_id: str
) -> tuple[str | None, str | None, tuple[Policy, ...]]:
    """``(rating id, its version id, the live policies)`` in effect for the story now."""
    rating_id, version_id = await story_rating(conn, story_id)
    return rating_id, version_id, await live_policies(conn, story_id, rating_id)


# -- which turns ------------------------------------------------------------------------------


async def activates(conn: AsyncConnection, continuation: Continuation) -> list[str]:
    """The committed operations ``continuation`` would make active, planned now (raising the
    edit's own 409/422 errors as it would)."""
    story_id = continuation.story_id
    match continuation.operation:
        case Edit.UNTRUNCATE:
            named = continuation.args.get("truncate_operation_id")
            return await timeline.untruncate_activates(conn, story_id, named)
        case Edit.SWAP:
            target = SwapRequest.model_validate(continuation.args).to_variant_chain
            planned = await plan_swap(conn, story_id, target)
            return [] if planned is None else list(planned.payload.activated_operation_ids)
        case Edit.FORK:
            request = fork.ForkRequest.model_validate(continuation.args)
            if request.variant_chain is None:
                return []
            selection, through = await fork_selection(
                conn, story_id, request.variant_chain, request.at_turn
            )
            if selection is None or selection.active or not selection.operation_ids:
                return []
            found = await conn.scalars(
                select(operations.c.operation_id)
                .where(
                    operations.c.operation_id.in_(list(selection.operation_ids)),
                    operations.c.membership != Membership.ACTIVE.value,
                    operations.c.narrative_turn <= through,
                )
                .order_by(operations.c.narrative_turn, operations.c.operation_id)
            )
            return list(found)
    raise AssertionError(continuation.operation)  # pragma: no cover - every edit is matched


async def unchecked(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str], current: Sequence[Policy]
) -> list[PendingTurn]:
    """The ``turn`` operations among ``operation_ids`` whose stamp lacks a policy of
    ``current``, by turn and variant."""
    if not operation_ids:
        return []
    found = await conn.execute(
        select(
            operations.c.operation_id,
            operations.c.narrative_turn,
            operations.c.turn_variant,
            operations.c.checked_guardrails,
        )
        .where(
            operations.c.story_id == story_id,
            operations.c.operation_id.in_(list(operation_ids)),
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        .order_by(operations.c.narrative_turn, operations.c.turn_variant)
    )
    return [
        PendingTurn(row.operation_id, int(row.narrative_turn), int(row.turn_variant or 1))
        for row in found
        if missing(row.checked_guardrails, current)
    ]


# -- the guard --------------------------------------------------------------------------------


async def guard(
    db: AsyncEngine,
    continuation: Continuation,
    *,
    start_job: StartJob,
    expected_revision: int | None = None,
) -> change.RatingCheckStarted | None:
    """``None`` when no turn ``continuation`` would make active lacks a current policy (the
    caller runs the edit); otherwise the started check (see the module docstring). 404, 409
    ``story_archived`` / ``story_paused`` / ``revision_conflict`` and the edit's own planning
    errors are raised first."""
    story_id = continuation.story_id
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(
                    stories.c.status,
                    stories.c.config_revision,
                    stories.c.timeline_revision,
                )
                .where(stories.c.id == story_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        if row.status == StoryStatus.ARCHIVED.value and continuation.operation is not Edit.FORK:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
            )
        await pause.refuse(conn, story_id, row.status)
        revision = int(row.timeline_revision)
        if expected_revision is not None and expected_revision != revision:
            raise RevisionConflict(
                f"expected timeline revision {expected_revision}, the story is at {revision}",
                {"current_revision": revision, "attempts": 0, "reason": "expected_revision"},
            )
        ids = await activates(conn, continuation)
        if not ids:
            return None
        rating_id, version_id, current = await current_set(conn, story_id)
        pending = await unchecked(conn, story_id, ids, current)
        if not pending:
            return None
        await _refuse_running(conn, story_id)
        job_id = await start_job(conn, continuation)
        operation_id = await control.record_control_operation(
            conn, actor=continuation.principal, story_id=story_id
        )
        await conn.execute(
            insert(rating_check_jobs).values(
                id=job_id,
                story_id=story_id,
                kind=RatingCheckKind.REACTIVATION.value,
                from_rating_id=rating_id,
                target_rating_id=rating_id,
                target_rating_version=version_id,
                status=RatingCheckStatus.CHECKING.value,
                progress={"done": 0, "total": 0},
                violations=[],
                started_by=continuation.principal,
                started_at=clock.now(),
                operation_id=operation_id,
            )
        )
        if row.status != StoryStatus.ARCHIVED.value:
            # An archived source (a fork) changes no truth meanwhile: it is not paused.
            await conn.execute(
                update(stories)
                .where(stories.c.id == story_id)
                .values(status=StoryStatus.PAUSED.value, paused_by=job_id)
            )
        await change.control_event(
            conn,
            story_id=story_id,
            operation_id=operation_id,
            type_="story_paused",
            payload=StoryPausedEvent(job_id=job_id, cause=pause.REACTIVATION_CHECK),
            actor=continuation.principal,
        )
    return change.RatingCheckStarted(
        job_id=job_id,
        operation_id=operation_id,
        timeline_revision=revision,
        config_revision=int(row.config_revision),
    )


async def _refuse_running(conn: AsyncConnection, story_id: str) -> None:
    """409 ``story_paused`` while another check of the story still runs (an archived source's
    reactivation check, which pauses nothing; one check per story at a time)."""
    running = (
        await conn.execute(
            select(rating_check_jobs.c.id, rating_check_jobs.c.kind, rating_check_jobs.c.progress)
            .where(
                rating_check_jobs.c.story_id == story_id,
                rating_check_jobs.c.status == RatingCheckStatus.CHECKING.value,
            )
            .limit(1)
        )
    ).one_or_none()
    if running is None:
        return
    progress = dict(running.progress or {})
    raise StoryPaused(
        f"story {story_id} has a check running; try again when it ends",
        {
            "story_id": story_id,
            "job_id": running.id,
            "cause": (
                pause.REACTIVATION_CHECK
                if running.kind == RatingCheckKind.REACTIVATION.value
                else pause.RATING_CHECK
            ),
            "progress": {
                "done": int(progress.get("done", 0)),
                "total": int(progress.get("total", 0)),
            },
        },
    )


# -- the job's steps --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pending:
    """What a run of the job read in one snapshot: the unchecked turns, their records, the
    current policies and the timeline revision it read them at."""

    turns: tuple[PendingTurn, ...]
    records: tuple[change.HistoryRecord, ...]
    policies: tuple[Policy, ...]
    revision: int


async def read_pending(conn: AsyncConnection, continuation: Continuation) -> Pending:
    """Plan the edit again and read its unchecked turns' records (in the caller's snapshot)."""
    story_id = continuation.story_id
    revision = await conn.scalar(
        select(stories.c.timeline_revision).where(stories.c.id == story_id)
    )
    if revision is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    ids = await activates(conn, continuation)
    _, _, current = await current_set(conn, story_id)
    turns = await unchecked(conn, story_id, ids, current)
    chosen = [t.operation_id for t in turns]
    records: list[change.HistoryRecord] = []
    if chosen:
        records = await change.event_records(conn, story_id, operations.c.operation_id.in_(chosen))
        records.extend(await change.story_image_records(conn, story_id, chosen))
    return Pending(tuple(turns), tuple(records), current, int(revision))


class Verdict(StrEnum):
    PASSED = "passed"
    """Every unchecked turn passed: apply the edit."""
    REFUSED = "refused"
    RESTART = "restart"
    """The timeline or the policy set moved since the read: run again."""
    GONE = "gone"


async def settle(
    db: AsyncEngine,
    check: change.Check,
    pending: Pending,
    result: change.HistoryResult,
    *,
    guard: change.Guard,
) -> Verdict:
    """Stamp the turns that passed, and refuse the edit when any failed (the story resumes),
    in one fenced transaction under the story lock; see the module docstring."""
    async with db.begin() as conn:
        await guard(conn)
        current = await change.load_check(conn, check.id)
        if current is None or current.status != RatingCheckStatus.CHECKING.value:
            return Verdict.GONE
        story = (
            await conn.execute(
                select(stories.c.timeline_revision)
                .where(stories.c.id == check.story_id)
                .with_for_update()
            )
        ).one_or_none()
        if story is None:
            return Verdict.GONE
        rating_id, version_id, found = await current_set(conn, check.story_id)
        if int(story.timeline_revision) != pending.revision or policy_key(found) != policy_key(
            pending.policies
        ):
            return Verdict.RESTART
        violated = {
            (int(v.position["turn"]), int(v.position["variant"]))
            for v in result.violations
            if "turn" in v.position
        }
        passing = [t.operation_id for t in pending.turns if (t.turn, t.variant) not in violated]
        if passing:
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id.in_(passing))
                .values(checked_guardrails=stamp_of(rating_id, version_id, found))
            )
        progress = {"done": result.checked, "total": result.checked}
        if result.passed:
            await conn.execute(
                update(rating_check_jobs)
                .where(rating_check_jobs.c.id == check.id)
                .values(progress=progress, checked_revision=pending.revision)
            )
            return Verdict.PASSED
        operation_id = await control.record_control_operation(
            conn, actor=check.started_by, story_id=check.story_id
        )
        await _resume(conn, check, operation_id, RatingCheckStatus.REFUSED.value)
        await conn.execute(
            update(rating_check_jobs)
            .where(rating_check_jobs.c.id == check.id)
            .values(
                status=RatingCheckStatus.REFUSED.value,
                progress=progress,
                violations=[v.as_json() for v in result.violations],
                checked_revision=pending.revision,
                finished_at=clock.now(),
            )
        )
        return Verdict.REFUSED


async def _resume(
    conn: AsyncConnection, check: change.Check, operation_id: str, outcome: str
) -> None:
    await conn.execute(
        update(stories)
        .where(stories.c.id == check.story_id)
        .values(status=StoryStatus.ACTIVE.value, paused_by=None)
    )
    await change.control_event(
        conn,
        story_id=check.story_id,
        operation_id=operation_id,
        type_="story_resumed",
        payload=StoryResumedEvent(job_id=check.id, outcome=outcome),
        actor=check.started_by,
    )


def _answer(done: Any) -> dict[str, Any]:
    found: dict[str, Any] = TypeAdapter(type(done)).dump_python(done, mode="json")
    found.pop("replayed", None)
    return found


async def apply(db: AsyncEngine, check: change.Check, continuation: Continuation) -> dict[str, Any]:
    """Run the held-back edit as the request would have (no guard: this is the job's own
    call), while the story stays paused by ``check``; ``{operation, result}``."""
    story_id = continuation.story_id
    principal = continuation.principal
    done: Any
    with pause.holding(check.id):
        match continuation.operation:
            case Edit.UNTRUNCATE:
                done = await timeline.untruncate(
                    db,
                    story_id,
                    timeline.UntruncateRequest.model_validate(continuation.args),
                    principal=principal,
                )
            case Edit.SWAP:
                done = await timeline.swap(
                    db,
                    story_id,
                    SwapRequest.model_validate(continuation.args),
                    principal=principal,
                )
            case Edit.FORK:
                done = await fork.fork(
                    db,
                    story_id,
                    fork.ForkRequest.model_validate(continuation.args),
                    principal=principal,
                )
    return {"operation": continuation.operation.value, "result": _answer(done)}


async def end(
    db: AsyncEngine,
    check: change.Check,
    *,
    outcome: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """``reactivated`` with ``outcome``, or ``failed`` with ``error``'s class; the story
    resumes (its own committed ``control`` operation records ``story_resumed``)."""
    status = RatingCheckStatus.FAILED if error is not None else RatingCheckStatus.REACTIVATED
    async with db.begin() as conn:
        current = await change.load_check(conn, check.id)
        if current is None or current.status != RatingCheckStatus.CHECKING.value:
            return
        paused_by = await conn.scalar(
            select(stories.c.paused_by).where(stories.c.id == check.story_id).with_for_update()
        )
        if paused_by == check.id:
            operation_id = await control.record_control_operation(
                conn, actor=check.started_by, story_id=check.story_id
            )
            await _resume(conn, check, operation_id, status.value)
        values: dict[str, Any] = {"status": status.value, "finished_at": clock.now()}
        if error is not None:
            kind = type(error).__name__
            values["error"] = {"class": kind, "message": f"the reactivation stopped ({kind})"}
        else:
            values["result"] = outcome
        await conn.execute(
            update(rating_check_jobs).where(rating_check_jobs.c.id == check.id).values(**values)
        )


# -- reads ------------------------------------------------------------------------------------


async def chains(db: AsyncEngine, story_id: str) -> list[Chain]:
    """The story's chains (``variants.list_chains``), each not active one with
    ``requires_check``: whether making it active would start a reactivation check (one of its
    turns lacks a current policy)."""
    found = await list_chains(db, story_id)
    async with db.connect() as conn:
        _, _, current = await current_set(conn, story_id)
        tree = await load_tree(conn, story_id)
        stamps = {
            row.operation_id: row.checked_guardrails
            for row in await conn.execute(
                select(operations.c.operation_id, operations.c.checked_guardrails).where(
                    operations.c.story_id == story_id,
                    operations.c.kind == OperationKind.TURN.value,
                    operations.c.status == OperationStatus.COMMITTED.value,
                    operations.c.membership != Membership.ACTIVE.value,
                )
            )
        }
    marked: list[Chain] = []
    for chain in found:
        needs = False
        if chain.membership is not Membership.ACTIVE:
            needs = any(
                missing(stamps.get(node.operation_id), current)
                for node in tree.segment_of(chain.turn, chain.variant)
            )
        marked.append(chain if not needs else _requiring(chain))
    return marked


def _requiring(chain: Chain) -> Chain:
    return replace(chain, requires_check=True)


__all__ = [
    "Continuation",
    "Edit",
    "Pending",
    "PendingTurn",
    "StartJob",
    "Verdict",
    "activates",
    "apply",
    "chains",
    "current_set",
    "end",
    "guard",
    "held_policies",
    "missing",
    "policy_key",
    "read_pending",
    "settle",
    "unchecked",
]
