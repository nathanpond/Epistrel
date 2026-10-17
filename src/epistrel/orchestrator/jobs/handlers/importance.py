"""``importance_scoring``: score new memories' importance in the background (FR-MEM-7; #135).

**When it runs.** One coalesced job per story (``coalesce_key`` ``importance_scoring:<story>``),
enqueued inside the transaction that writes a batch of episodic memories
(``core.memory.records.MEMORY_HOOKS``: the turn's commit), so a turn never waits for a score.
The recurring task of the same name (daily) is the safety net: its run has no story and
enqueues the job of every story that still has unscored memories. Every job runs once
(``max_attempts`` 1); a failed run is retried by the next trigger.

**What a run does.** It does nothing, and calls nothing, unless the story's effective
configuration has the ``episodic`` module on (#129's ``module_on``). In one snapshot it reads
up to ``MAX_CALLS_PER_RUN`` (20) of the story's active hot memories without an importance,
oldest first, declaring each one's operation as an input (``operations:<id>``, FR-CONC-8).
Then, outside the snapshot, it calls the ``importance_scoring`` purpose once per memory through
the Model Access Layer (``{text, kind}`` → ``{importance: 0..1}``, a structured call with its
deterministic step: an empty text scores 0), under its own budget of ``MAX_CALLS_PER_RUN``
calls, never a turn's, routed by the story's effective configuration
(``models.purposes.importance_scoring``). An ``Uncertain`` answer leaves that memory unscored
for a later run.

**What it commits** (``queue.commit_job``): its ``consolidation`` operation and, in a commit
step, the scores (``records.set_importance``: only where no importance is set, so an
author-set value or an earlier score is never overwritten). When more unscored memories are
left than one run takes, it enqueues itself again in the same transaction. If a declared input
moved meanwhile (a truncation), nothing is written and the job is queued again. Results, logs
and metrics carry ids and counts only (invariant 3).

``bind(models)`` gives the job the process's Model Access Layer (``Engine.start_workers``);
without one bound the run fails (its class name is the job's result).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import OperationKind
from epistrel.core.events.operation import Operation
from epistrel.core.input_versions import input_key
from epistrel.core.memory import records
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.tables import operations, stories
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.recurring import FAILED as TASK_FAILED
from epistrel.orchestrator.jobs.recurring import OK as TASK_OK
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, record
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.models.access import ModelAccessLayer, routing
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import routes_of
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelError
from epistrel.orchestrator.models.registry import (
    IMPORTANCE_SCORING as PURPOSE,
)
from epistrel.orchestrator.models.registry import (
    ImportanceScore,
    ImportanceScoringInputs,
)
from epistrel.orchestrator.models.structured import Uncertain

logger = logging.getLogger(__name__)

IMPORTANCE = PURPOSE.name
"""The job kind, the recurring task and the model purpose share the name."""
INTERVAL = timedelta(days=1)
MAX_CALLS_PER_RUN = 20
BUILT_IN_PARAMS = ModelParams(temperature=0.0, max_tokens=50)
"""The scoring call's own params; the story's ``importance_scoring`` route's params win."""

_bound: list[ModelAccessLayer] = []


class NoModels(RuntimeError):
    """No Model Access Layer is bound in this process (``bind``)."""


def bind(models: ModelAccessLayer | None) -> None:
    """Make ``models`` the layer the job calls through; ``None`` unbinds."""
    _bound.clear()
    if models is not None:
        _bound.append(models)


def bound() -> ModelAccessLayer | None:
    return _bound[0] if _bound else None


def coalesce_key(story_id: str) -> str:
    """One queued scoring job per story."""
    return f"{IMPORTANCE}:{story_id}"


# -- scheduling ---------------------------------------------------------------------------------


async def schedule_in(conn: AsyncConnection, story_id: str) -> None:
    """Enqueue the story's scoring job in ``conn``'s transaction (coalesced)."""
    await queue.enqueue(
        conn, IMPORTANCE, NoPayload(), story_id=story_id, coalesce_key=coalesce_key(story_id)
    )


async def on_memories_written(
    conn: AsyncConnection, story_id: str, memory_ids: tuple[str, ...]
) -> None:
    """``core.memory.records``' hook: a batch of new memories is scored in the background."""
    if memory_ids:
        await schedule_in(conn, story_id)


# -- the run ------------------------------------------------------------------------------------


def _blank(text: str) -> Any:
    def deterministic() -> ImportanceScore | None:
        # FR-MODEL-5's deterministic step: a memory with no text left is trivia.
        return ImportanceScore(importance=0.0) if not text.strip() else None

    return deterministic


async def _fan_out(ctx: JobContext) -> JobOutput:
    """The recurring run: enqueue the job of every story with unscored memories."""
    async with ctx.db.connect() as conn:
        candidates = await records.stories_with_unscored(conn)
    scheduled = 0
    for story_id in candidates:
        async with ctx.db.begin() as conn:
            exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
            if exists is not None:
                await schedule_in(conn, story_id)
                scheduled += 1
    return JobOutput(result={"stories": len(candidates), "scheduled": scheduled})


async def _run_story(ctx: JobContext, story_id: str) -> JobOutput:
    async with ctx.snapshot() as conn:
        if not await records.episodic_on(conn, story_id):
            return JobOutput(result={"scored": 0, "calls": 0, "module": "off"})
        found = await records.unscored(conn, story_id, MAX_CALLS_PER_RUN + 1)
        batch = found[:MAX_CALLS_PER_RUN]
        for memory in batch:
            await ctx.declare_input(conn, input_key(operations.name, memory.operation_id))
        routes = routes_of((await effective_configuration(conn, story_id, validate=False)).settings)
    if not batch:
        return JobOutput(result={"scored": 0, "calls": 0})
    models = bound()
    if models is None:
        raise NoModels("no Model Access Layer is bound for the importance_scoring job")
    route = routes.get(IMPORTANCE)
    params = BUILT_IN_PARAMS.merged(route.params if route is not None else None)
    budget = TurnBudget(max_calls=MAX_CALLS_PER_RUN)
    scores: dict[str, float] = {}
    uncertain = 0
    for memory in batch:
        inputs = ImportanceScoringInputs(text=memory.text, kind=memory.kind.value)
        try:
            with routing(routes):
                answer = await models.structured_call(
                    IMPORTANCE,
                    ImportanceScore,
                    inputs,
                    params=params,
                    deterministic_first=_blank(memory.text),
                    budget=budget,
                )
        except BudgetExhausted:
            break
        except ModelError as exc:
            if not scores:
                raise
            logger.info("importance call failed: %s", type(exc).__name__)
            break
        if isinstance(answer, Uncertain):
            uncertain += 1
            continue
        scores[memory.id] = answer.importance
    more = len(found) > len(batch)
    written: dict[str, int] = {"scored": 0}

    def stage(op: Operation) -> None:
        async def step(conn: AsyncConnection) -> None:
            written["scored"] = await records.set_importance(conn, story_id, scores)
            if more:
                await schedule_in(conn, story_id)

        op.add_commit_step(step)

    return JobOutput(
        stage=stage,
        result={"calls": budget.used, "uncertain": uncertain, "more": more},
        result_of=lambda: dict(written),
    )


async def _run(ctx: JobContext) -> JobOutput:
    story_id = ctx.job.story_id
    if story_id is not None:
        return await _run_story(ctx, story_id)
    try:
        output = await _fan_out(ctx)
    except Exception as exc:
        await record(ctx, TASK_FAILED, type(exc).__name__)
        raise
    await record(ctx, TASK_OK)
    return output


IMPORTANCE_HANDLER = JobHandler(
    kind=IMPORTANCE,
    payload=NoPayload,
    run=_run,
    operation_kind=OperationKind.CONSOLIDATION,
    max_attempts=1,
)
IMPORTANCE_TASK = RecurringTask(IMPORTANCE, INTERVAL)

records.register_memory_hook(on_memories_written)


__all__ = [
    "IMPORTANCE",
    "IMPORTANCE_HANDLER",
    "IMPORTANCE_TASK",
    "INTERVAL",
    "MAX_CALLS_PER_RUN",
    "NoModels",
    "bind",
    "bound",
    "coalesce_key",
    "on_memories_written",
    "schedule_in",
]
