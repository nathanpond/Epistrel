"""``rating_check``: a tightening's history check (FR-RATE-7; #118). ``core.ratings.change``
holds the rules; this handler drives them.

The job's id is its ``rating_check_jobs`` row's. A run:

1. ends at once when the row is gone (the story was deleted) or no longer ``checking``;
2. waits, while a turn that began before the pause still holds the story's turn slot, for it
   to commit (``JobOutput.requeue_in``, up to ``WAIT_LIMIT_S`` from the request);
3. reads the story's whole active history, the target rating's effective policies (live
   global, the target closure's, the story's and its characters'; age-conditioned ones only
   when the cast has a minor) and the cached verdicts, in one snapshot;
4. evaluates them (``change.evaluate_history``): deterministic policies first, then each
   model-assisted one through the ``guardrail_check`` purpose, one call at a time under its own
   budget (never a turn's), at most ``rating_check_model_calls_per_record`` per record;
5. checkpoints progress and the new verdicts every ``CHECKPOINT`` records, and yields
   (requeues) after ``YIELD_AFTER_S`` well before the worker's runtime cap: the next run
   answers what was checked from the cache;
6. applies or refuses the change and resumes the story (``change.finish``), or runs again when
   the history or the target rating moved since it was read.

Each of its transactions is fenced by its claim (``queue.hold``). A model that cannot answer
fails the attempt, retried with #70's backoff while the story stays paused; the last failed
attempt (``MAX_ATTEMPTS``) resumes the story on its old rating and marks the check ``failed``.
A last attempt the framework ends without this handler seeing it (the runtime cap cancels it,
or the commit fails) leaves the check ``checking``, and ``change.reconcile`` ends it the same
way (at the next read of the check, the next rating change, the worker's recovery pass or the
retention sweep). A checkpoint that finds the check gone (the story was deleted) or ended ends
the run.
It records no operation of its own: the change, the pause and the resume are recorded by
``core.ratings.change``.

A **reactivation** check (#120, ``rating_check_jobs.kind = reactivation``) runs the same job
kind with the held-back edit in its payload (``then``): after step 2 it plans the edit again and
reads only the turns it would make active whose recorded guardrail set lacks a policy now in
effect (``reactivation.read_pending``), evaluates them against the current policies, and
``reactivation.settle`` stamps those that passed and refuses the edit if any failed (or runs
again when the timeline or the policies moved). When all passed, it applies the edit itself
while the story is still paused (``reactivation.apply``) and ends the check ``reactivated``
with the edit's answer; an edit that is no longer possible ends it ``failed``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.config_changes import read_guardrail_settings, read_settings
from epistrel.core.errors import EpistrelError
from epistrel.core.policies.model import ModelAssistedRule, Policy, Target
from epistrel.core.ratings import change, reactivation
from epistrel.core.ratings.stamp import live_policies
from epistrel.core.settings_schema import DEFAULT_TURN_DEADLINE_S
from epistrel.core.tables import content_ratings
from epistrel.core.tables.rating_checks import RatingCheckKind, RatingCheckStatus
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext, JobHandler, JobOutput
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.errors import BudgetExhausted
from epistrel.orchestrator.policies.check import check_text
from epistrel.orchestrator.policies.store import GuardContext, read_cast

RATING_CHECK_KIND = change.RATING_CHECK_KIND
MAX_ATTEMPTS = 8
WAIT_S = 0.5
"""How long the job waits between looks at a turn still in flight."""
WAIT_LIMIT_S = DEFAULT_TURN_DEADLINE_S * 2
"""How long after the request the job waits for that turn before it checks anyway (the turn's
own lease expires well before)."""
CHECKPOINT = 25
"""Records between progress checkpoints."""
YIELD_AFTER_S = 240.0
"""A run yields after this long (the worker caps a run at 600 s)."""
CALL_DEADLINE_S = 60.0
"""The deadline of one model-assisted check."""

_bound: list[ModelAccessLayer] = []


class NoModels(RuntimeError):
    """No Model Access Layer is bound in this process (``bind``), and a model-assisted policy
    applies."""


def bind(models: ModelAccessLayer | None) -> None:
    """Make ``models`` the layer the job calls through; ``None`` unbinds."""
    _bound.clear()
    if models is not None:
        _bound.append(models)


def bound() -> ModelAccessLayer | None:
    return _bound[0] if _bound else None


class RatingCheckPayload(BaseModel):
    """A tightening's job carries nothing: its id is its ``rating_check_jobs`` row's. A
    reactivation check's (#120) carries ``then``: the edit it holds back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    then: reactivation.Continuation | None = None


class _Fence:
    def __init__(self, job: ClaimedJob) -> None:
        self.job = job

    async def __call__(self, conn: AsyncConnection) -> None:
        await queue.hold(conn, self.job)


class _Yield(Exception):
    """The run has worked long enough: requeue, and resume from the cache."""


class _Gone(Exception):
    """A checkpoint found the check gone (its story was deleted) or no longer ``checking``."""


class ModelPolicyCheck:
    """``change.ModelCheck`` over the Model Access Layer: one ``guardrail_check`` call under a
    budget of its own (one call, ``CALL_DEADLINE_S``), never a turn's."""

    def __init__(self, models: ModelAccessLayer, context: GuardContext) -> None:
        self.models = models
        self.context = context

    async def __call__(self, text: str, policy: Policy, target: Target) -> bool | None:
        budget = TurnBudget(max_calls=1, deadline_s=CALL_DEADLINE_S)
        try:
            outcome = await check_text(
                self.models,
                text,
                [policy],
                target,
                budget=budget,
                cast=self.context.cast,
                threshold=self.context.threshold,
            )
        except BudgetExhausted:
            return None
        if outcome.decided_by == "uncertain":
            return None
        return outcome.violated


async def _work(ctx: JobContext, guard: _Fence) -> JobOutput:
    check = await change.load_check(ctx.db, ctx.job.id)
    if check is None or check.status != RatingCheckStatus.CHECKING.value:
        return JobOutput(result={"status": check.status if check else "gone"})
    if await change.turn_in_flight(ctx.db, check.story_id):
        waited = (clock.now() - check.started_at).total_seconds()
        if waited < WAIT_LIMIT_S:
            return JobOutput(requeue_in=WAIT_S, result={"waiting": "turn_in_flight"})
    if check.kind == RatingCheckKind.REACTIVATION.value:
        return await _reactivate(ctx, guard, check)
    async with ctx.snapshot() as conn:
        history = await change.read_history(conn, check.story_id)
        version = str(
            await conn.scalar(
                select(content_ratings.c.current_version_id).where(
                    content_ratings.c.id == check.target_rating_id
                )
            )
        )
        found = await live_policies(conn, check.story_id, check.target_rating_id)
        cast = await read_cast(conn, check.story_id)
        threshold = (await read_settings(conn)).minor_age_threshold
        cache = await change.load_cache(conn, check.story_id, [r.key for r in history.records])
    context = GuardContext(policies=found, cast=cast, threshold=threshold)
    active = context.active
    models = bound()
    if models is None and any(isinstance(p.rule, ModelAssistedRule) for p in active):
        raise NoModels("no Model Access Layer is bound for the rating_check job")
    settings = await read_guardrail_settings(ctx.db)
    buffered: dict[change.CacheKey, str] = {}
    reached = [0]
    started = time.monotonic()

    async def progress(done: int, total: int, verdicts: Mapping[change.CacheKey, str]) -> None:
        buffered.update(verdicts)
        reached[0] = done
        if done % CHECKPOINT == 0 or done == total:
            saved = await change.save_progress(
                ctx.db, check, done, total, buffered, guard, revision=history.revision
            )
            buffered.clear()
            if not saved:
                raise _Gone
        if done < total and time.monotonic() - started > YIELD_AFTER_S:
            raise _Yield

    try:
        result = await change.evaluate_history(
            active,
            history.records,
            model_check=None if models is None else ModelPolicyCheck(models, context),
            calls_per_record=settings.rating_check_model_calls_per_record,
            cache=cache,
            progress=progress,
        )
    except _Yield:
        if buffered:
            total = len(history.records)
            await change.save_progress(ctx.db, check, reached[0], total, buffered, guard)
        return JobOutput(requeue_in=0.0, result={"yielded": True})
    if not history.records and not await change.save_progress(
        ctx.db, check, 0, 0, {}, guard, revision=history.revision
    ):
        raise _Gone
    outcome = await change.finish(
        ctx.db,
        check,
        result,
        found,
        evaluated_version=version,
        evaluated_revision=history.revision,
        guard=guard,
    )
    if outcome is change.Outcome.RESTART:
        return JobOutput(requeue_in=0.0, result={"status": outcome.value})
    summary: dict[str, Any] = {"status": outcome.value, "violations": len(result.violations)}
    summary["checked"], summary["cached"] = result.checked, result.cached
    return JobOutput(result=summary)


async def _reactivate(ctx: JobContext, guard: _Fence, check: change.Check) -> JobOutput:
    """A reactivation check (#120): evaluate the unchecked turns the held-back edit would make
    active against the current policies, then apply the edit or refuse it
    (``core.ratings.reactivation``)."""
    payload = ctx.payload
    assert isinstance(payload, RatingCheckPayload)  # noqa: S101 - the kind's payload model
    then = payload.then
    if then is None:
        await reactivation.end(ctx.db, check, error=MissingContinuation("no edit to apply"))
        return JobOutput(result={"status": RatingCheckStatus.FAILED.value})
    try:
        async with ctx.snapshot() as conn:
            pending = await reactivation.read_pending(conn, then)
            cast = await read_cast(conn, check.story_id)
            threshold = (await read_settings(conn)).minor_age_threshold
            cache = await change.load_cache(conn, check.story_id, [r.key for r in pending.records])
    except EpistrelError as exc:
        # The edit is no longer possible as asked (e.g. a turn in flight committed over it).
        await reactivation.end(ctx.db, check, error=exc)
        return JobOutput(result={"status": RatingCheckStatus.FAILED.value})
    context = GuardContext(policies=pending.policies, cast=cast, threshold=threshold)
    active = context.active
    models = bound()
    if models is None and any(isinstance(p.rule, ModelAssistedRule) for p in active):
        raise NoModels("no Model Access Layer is bound for the rating_check job")
    settings = await read_guardrail_settings(ctx.db)
    buffered: dict[change.CacheKey, str] = {}

    async def progress(done: int, total: int, verdicts: Mapping[change.CacheKey, str]) -> None:
        buffered.update(verdicts)
        if done % CHECKPOINT == 0 or done == total:
            saved = await change.save_progress(ctx.db, check, done, total, buffered, guard)
            buffered.clear()
            if not saved:
                raise _Gone

    result = await change.evaluate_history(
        active,
        pending.records,
        model_check=None if models is None else ModelPolicyCheck(models, context),
        calls_per_record=settings.rating_check_model_calls_per_record,
        cache=cache,
        progress=progress,
    )
    verdict = await reactivation.settle(ctx.db, check, pending, result, guard=guard)
    if verdict is reactivation.Verdict.RESTART:
        return JobOutput(requeue_in=0.0, result={"status": verdict.value})
    if verdict is not reactivation.Verdict.PASSED:
        return JobOutput(result={"status": verdict.value, "violations": len(result.violations)})
    try:
        outcome = await reactivation.apply(ctx.db, check, then)
    except EpistrelError as exc:
        await reactivation.end(ctx.db, check, error=exc)
        return JobOutput(result={"status": RatingCheckStatus.FAILED.value})
    await reactivation.end(ctx.db, check, outcome=outcome)
    return JobOutput(
        result={"status": RatingCheckStatus.REACTIVATED.value, "checked": result.checked}
    )


class MissingContinuation(RuntimeError):
    """A reactivation check's job carries no edit (it cannot happen through the Engine)."""


async def _run(ctx: JobContext) -> JobOutput:
    guard = _Fence(ctx.job)
    try:
        return await _work(ctx, guard)
    except queue.Fenced:
        # Another run holds the job now and finishes the check from the row and the cache.
        return JobOutput()
    except _Gone:
        return JobOutput(result={"status": "gone"})
    except Exception as exc:
        if ctx.job.failures + 1 >= MAX_ATTEMPTS:
            await change.fail(ctx.db, ctx.job.id, exc)
        raise


RATING_CHECK_HANDLER = JobHandler(
    kind=RATING_CHECK_KIND,
    payload=RatingCheckPayload,
    run=_run,
    max_attempts=MAX_ATTEMPTS,
    records_operation=False,
)

__all__ = [
    "CHECKPOINT",
    "MAX_ATTEMPTS",
    "RATING_CHECK_HANDLER",
    "RATING_CHECK_KIND",
    "ModelPolicyCheck",
    "NoModels",
    "RatingCheckPayload",
    "bind",
    "bound",
]
