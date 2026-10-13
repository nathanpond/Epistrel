"""``lite_summary``: the Lite rolling summary's background job (FR-LITE-2, FR-STORE-26,27,
FR-CONC-8, FR-LITE-7; #72).

**When it runs.** One coalesced job per story (``coalesce_key`` ``lite_summary:<story>``),
enqueued after a turn commits and after truncate, untruncate, swap and fork (``timeline``'s
post-commit membership hook), whenever ``core.summary`` finds work: a whole chunk older than the
trailing ``window_messages`` verbatim rows, or a summary over ``compress_threshold_tokens``
(the story's effective configuration's ``memory.rolling_summary``, #113; nothing with the
rolling summary off).
The recurring task of the same name (every 10 minutes) is the safety net: its run has no story
and enqueues the job of every story with work. Every ``lite_summary`` job runs once
(``max_attempts`` 1); a failed run is retried by the next trigger.

**What a run does.** In one snapshot it reads the summary state and, for each compressible chunk,
its transcript rows, declaring every source turn's operation and every included segment's
operation as an input (``operations:<id>``, FR-STORE-27). Then, outside the snapshot, it calls
the ``lite_summary`` purpose through the Model Access Layer, under its own budget of
``MAX_CALLS_PER_RUN`` (20) calls, never a turn's (FR-LITE-7):

- **compress**: each chunk, oldest first, becomes one level-0 segment (the segment just before
  is context only). Each text passes the story's **deterministic** output policies before it is
  kept; a violating text is summarised once more, and a second violation writes no segment, so
  the chunk's rows stay verbatim and later chunks wait (coverage stays contiguous). Each
  rejected text is a ``guardrail_output_rejected`` audit event on the job's operation.
- **merge**: while the summary's estimated tokens exceed the cap, the two oldest adjacent
  segments of the lowest level merge into one segment of the next level (``core.summary``),
  checked the same way.

**What it commits** (``queue.commit_job``, one transaction): the job's own ``consolidation``
operation (its audit events, ``source_window`` ``{from_turn, to_turn}``) and one
``consolidation`` operation per new segment, positioned at the segment's ``covers_to`` so it
follows that turn's membership, with its own source window. A merged segment names its children
as sources (they become ``superseded`` in the projection). If any declared input was discarded
or moved meanwhile, or another run already wrote one of the segments, nothing is written and the
job is queued again, due now (FR-CONC-8). Results, logs and metrics carry ids and counts only
(invariant 3).

``bind(models)`` gives the job the process's Model Access Layer (``Engine.start_workers``);
without one bound the run fails (its class name is the job's result).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import summary, summary_port, timeline
from epistrel.core.enums import Membership, OperationKind, Provenance
from epistrel.core.events.operation import Operation
from epistrel.core.events.types import (
    GuardrailOutputRejected,
    LiteSummarySegment,
    RejectedViolation,
    SummarySource,
)
from epistrel.core.ids import new_ulid
from epistrel.core.input_versions import input_key
from epistrel.core.policies.model import ModelAssistedRule, Policy
from epistrel.core.summary import Chunk, Segment
from epistrel.core.tables import characters, lite_summary_segments, operations, stories
from epistrel.core.tables import transcript_messages as transcript
from epistrel.core.turns import transcript_rows
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.recurring import FAILED as TASK_FAILED
from epistrel.orchestrator.jobs.recurring import OK as TASK_OK
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, record
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput, PositionedOutput
from epistrel.orchestrator.lite.prompt import prompt_hash, transcript_line
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelError
from epistrel.orchestrator.models.registry import LiteSummaryInputs
from epistrel.orchestrator.policies.output_gate import deterministic_check, output_policies
from epistrel.orchestrator.policies.store import guard_context

logger = logging.getLogger(__name__)

LITE_SUMMARY = "lite_summary"
INTERVAL = timedelta(minutes=10)
ATTEMPTS_PER_TEXT = 2
"""A text that breaks a deterministic output policy is summarised once more, then dropped."""
SUMMARY_TABLE = lite_summary_segments.name

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
    """One queued summary job per story."""
    return f"{LITE_SUMMARY}:{story_id}"


# -- scheduling ---------------------------------------------------------------------------------


async def schedule_in(conn: AsyncConnection, story_id: str) -> bool:
    """Enqueue the story's summary job in ``conn``'s transaction when it has work."""
    if not await summary.needs_work(conn, story_id):
        return False
    await queue.enqueue(
        conn, LITE_SUMMARY, NoPayload(), story_id=story_id, coalesce_key=coalesce_key(story_id)
    )
    return True


async def schedule(db: AsyncEngine, story_id: str) -> bool:
    """The summary port's scheduler (``core.summary_port``): after a turn commits."""
    async with db.begin() as conn:
        return await schedule_in(conn, story_id)


async def on_membership_change(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
    """After truncate, untruncate, swap or fork (post-commit): the summary's coverage may have
    changed (a segment discarded, restored, or copied up to the fork's boundary)."""
    if change.operation_ids:
        await schedule_in(conn, change.story_id)


# -- the run ------------------------------------------------------------------------------------


@dataclass
class _Pending:
    """A segment this run will write."""

    operation_id: str
    covers_from: int
    covers_to: int
    level: int
    text: str
    sources: tuple[SummarySource, ...]
    model: str | None
    prompt_hash: str | None

    def segment(self) -> Segment:
        return Segment(
            segment_id=self.operation_id,
            operation_id=self.operation_id,
            covers_from=self.covers_from,
            covers_to=self.covers_to,
            level=self.level,
            text=self.text,
            sources=tuple(s.operation_id for s in self.sources),
        )


@dataclass
class _Run:
    """What a run read and what it made."""

    models: ModelAccessLayer
    policies: tuple[Policy, ...]
    names: list[str]
    budget: TurnBudget
    cap: int
    """The summary's token cap (``compress_threshold_tokens``) the run merges down to."""
    pending: list[_Pending] = field(default_factory=list)
    rejected: list[GuardrailOutputRejected] = field(default_factory=list)
    calls_failed: bool = False


@dataclass(frozen=True, slots=True)
class _Text:
    text: str
    model: str | None
    prompt_hash: str


def _covers(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def _source(operation_id: str) -> SummarySource:
    return SummarySource(operation_id=operation_id, membership=Membership.ACTIVE.value)


async def _story_names(conn: AsyncConnection, story_id: str) -> dict[str, str]:
    found = await conn.execute(
        select(characters.c.id, characters.c.name)
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    return {r.id: r.name for r in found}


async def _write(run: _Run, inputs: LiteSummaryInputs) -> _Text | None:
    """One summary text that passes the deterministic output policies, or ``None`` after
    ``ATTEMPTS_PER_TEXT`` violating ones (each kept as an audit record)."""
    params = ModelParams(temperature=summary.TEMPERATURE, max_tokens=summary.RESPONSE_MAX_TOKENS)
    for attempt in range(ATTEMPTS_PER_TEXT):
        _, _, messages = run.models.prepare(LITE_SUMMARY, inputs)
        result = await run.models.call(LITE_SUMMARY, inputs, params, budget=run.budget)
        text = result.text.strip()
        check = deterministic_check(text, run.policies)
        if check.passed:
            return _Text(text, result.model, prompt_hash(messages))
        run.rejected.append(
            GuardrailOutputRejected(
                text=text,
                reason="output_policy",
                attempt=attempt,
                policy_ids=tuple(v.policy_id for v in check.violations),
                violations=tuple(
                    RejectedViolation(
                        policy_id=v.policy_id, name=v.name, message=v.message, kind=v.kind
                    )
                    for v in check.violations
                ),
            )
        )
    return None


async def _compress(
    run: _Run, chunk: Chunk, lines: Sequence[Any], prior: Segment | None
) -> _Pending | None:
    inputs = LiteSummaryInputs(
        mode="compress",
        messages=list(lines),
        prior_segments=[prior.label] if prior is not None else [],
        character_names=run.names,
        covers=_covers(chunk.covers_from, chunk.covers_to),
        target_tokens=summary.TARGET_TOKENS,
    )
    written = await _write(run, inputs)
    if written is None:
        return None
    return _Pending(
        operation_id=new_ulid(),
        covers_from=chunk.covers_from,
        covers_to=chunk.covers_to,
        level=0,
        text=written.text,
        sources=tuple(_source(t.operation_id) for t in chunk.turns),
        model=written.model,
        prompt_hash=written.prompt_hash,
    )


async def _merge(
    run: _Run, first: Segment, second: Segment, prior: Segment | None
) -> _Pending | None:
    inputs = LiteSummaryInputs(
        mode="merge",
        merge_segments=[first.label, second.label],
        prior_segments=[prior.label] if prior is not None else [],
        character_names=run.names,
        covers=_covers(first.covers_from, second.covers_to),
        target_tokens=summary.TARGET_TOKENS,
    )
    written = await _write(run, inputs)
    if written is None:
        return None
    return _Pending(
        operation_id=new_ulid(),
        covers_from=first.covers_from,
        covers_to=second.covers_to,
        level=max(first.level, second.level) + 1,
        text=written.text,
        sources=(_source(first.operation_id), _source(second.operation_id)),
        model=written.model,
        prompt_hash=written.prompt_hash,
    )


async def _calling[T](run: _Run, call: Any) -> T | None:
    """Run one summary call; a spent budget or a model failure stops the run's calls (what was
    made so far is still committed)."""
    try:
        result: T | None = await call
        return result
    except BudgetExhausted:
        run.calls_failed = True
        return None
    except ModelError as exc:
        if not run.pending:
            raise
        logger.info("lite summary call failed: %s", type(exc).__name__)
        run.calls_failed = True
        return None


async def _summarise(
    run: _Run, chunks: Sequence[tuple[Chunk, Sequence[Any]]], segments: Sequence[Segment]
) -> None:
    effective = list(segments)
    for chunk, lines in chunks:
        made: _Pending | None = await _calling(
            run, _compress(run, chunk, lines, effective[-1] if effective else None)
        )
        if made is None:
            break  # a blocked or unaffordable chunk stops the later ones: coverage is contiguous
        run.pending.append(made)
        effective.append(made.segment())
    while (
        not run.calls_failed and len(effective) > 1 and sum(s.tokens for s in effective) > run.cap
    ):
        index = summary.merge_pair(effective)
        assert index is not None  # noqa: S101 - two or more segments
        first, second = effective[index], effective[index + 1]
        merged: _Pending | None = await _calling(
            run, _merge(run, first, second, effective[index - 1] if index > 0 else None)
        )
        if merged is None:
            break
        run.pending.append(merged)
        effective[index : index + 2] = [merged.segment()]


async def _fan_out(ctx: JobContext) -> JobOutput:
    """The recurring run: enqueue the job of every story whose summary has work."""
    async with ctx.db.connect() as conn:
        candidates = list(
            await conn.scalars(
                select(transcript.c.story_id)
                .where(transcript.c.membership == Membership.ACTIVE.value)
                .group_by(transcript.c.story_id)
                .having(func.count() > 2)
                .order_by(transcript.c.story_id)
            )
        )
    scheduled = 0
    for story_id in candidates:
        async with ctx.db.begin() as conn:
            exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
            if exists is not None and await schedule_in(conn, story_id):
                scheduled += 1
    return JobOutput(result={"stories": len(candidates), "scheduled": scheduled})


async def _run_story(ctx: JobContext, story_id: str) -> JobOutput:
    models = bound()
    if models is None:
        raise NoModels("no Model Access Layer is bound for the lite_summary job")
    async with ctx.snapshot() as conn:
        state = await summary.summary_state(conn, story_id)
        if not state.needs_work:
            return JobOutput(result={"segments": 0, "calls": 0})
        for segment in state.segments:
            await ctx.declare_input(conn, input_key(operations.name, segment.operation_id))
        names = await _story_names(conn, story_id)
        chunks: list[tuple[Chunk, list[Any]]] = []
        for chunk in state.chunks[: summary.MAX_CALLS_PER_RUN]:
            for turn in chunk.turns:
                await ctx.declare_input(conn, input_key(operations.name, turn.operation_id))
            rows = await transcript_rows(
                conn, story_id, after_turn=chunk.covers_from - 1, through_turn=chunk.covers_to
            )
            chunks.append((chunk, [transcript_line(row, names) for row in rows]))
    context = await guard_context(ctx.db, story_id)
    deterministic = tuple(
        p for p in output_policies(context) if not isinstance(p.rule, ModelAssistedRule)
    )
    run = _Run(
        models=models,
        policies=deterministic,
        names=list(names.values()),
        budget=TurnBudget(max_calls=summary.MAX_CALLS_PER_RUN),
        cap=state.settings.max_tokens,
    )
    await _summarise(run, chunks, state.segments)
    return _output(story_id, run, chunks)


def _output(story_id: str, run: _Run, chunks: Sequence[tuple[Chunk, Sequence[Any]]]) -> JobOutput:
    pending = list(run.pending)
    rejected = list(run.rejected)

    def stage(op: Operation) -> None:
        for record_ in rejected:
            op.append("guardrail_output_rejected", record_, provenance=Provenance.ENGINE)
        if pending:
            op.add_commit_step(_not_written_yet(story_id, pending))

    def segment_stage(made: _Pending) -> Any:
        def stage_one(op: Operation) -> None:
            op.append(
                "lite_summary_segment",
                LiteSummarySegment(
                    text=made.text,
                    covers_from=made.covers_from,
                    covers_to=made.covers_to,
                    level=made.level,
                    source_versions=made.sources,
                    model=made.model,
                    prompt_hash=made.prompt_hash,
                ),
                provenance=Provenance.ENGINE,
            )

        return stage_one

    window = (
        {
            "from_turn": min(p.covers_from for p in pending),
            "to_turn": max(p.covers_to for p in pending),
        }
        if pending
        else None
    )
    return JobOutput(
        stage=stage,
        positioned=tuple(
            PositionedOutput(
                turn=made.covers_to,
                stage=segment_stage(made),
                operation_id=made.operation_id,
                source_window={"from_turn": made.covers_from, "to_turn": made.covers_to},
            )
            for made in pending
        ),
        source_window=window,
        result={
            "segments": len(pending),
            "merged": sum(1 for p in pending if p.level > 0),
            "chunks": len(chunks),
            "rejected": len(rejected),
            "calls": run.budget.used,
        },
    )


def _not_written_yet(story_id: str, pending: Sequence[_Pending]) -> Any:
    """A commit step: another run already wrote one of these segments (same turns and level,
    still in the summary) → drop this output and run again."""

    async def step(conn: AsyncConnection) -> None:
        t = lite_summary_segments
        for made in pending:
            taken = await conn.scalar(
                select(t.c.segment_id).where(
                    t.c.story_id == story_id,
                    t.c.covers_from == made.covers_from,
                    t.c.covers_to == made.covers_to,
                    t.c.level == made.level,
                    t.c.membership.in_((Membership.ACTIVE.value, Membership.SUPERSEDED.value)),
                )
            )
            if taken is not None:
                raise queue.StaleInputs((input_key(SUMMARY_TABLE, str(taken)),))

    return step


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


LITE_SUMMARY_HANDLER = JobHandler(
    kind=LITE_SUMMARY,
    payload=NoPayload,
    run=_run,
    operation_kind=OperationKind.CONSOLIDATION,
    max_attempts=1,
)
LITE_SUMMARY_TASK = RecurringTask(LITE_SUMMARY, INTERVAL)

summary_port.set_scheduler(schedule)
timeline.on_membership_change(on_membership_change, phase=timeline.HookPhase.POST_COMMIT)

__all__ = [
    "INTERVAL",
    "LITE_SUMMARY",
    "LITE_SUMMARY_HANDLER",
    "LITE_SUMMARY_TASK",
    "NoModels",
    "bind",
    "bound",
    "coalesce_key",
    "on_membership_change",
    "schedule",
    "schedule_in",
]
