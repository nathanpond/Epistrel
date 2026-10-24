"""``consolidation``: compress old episodic detail into semantic facts and reflections, in the
background (FR-MEM-2,3,6,9,10, FR-STORE-13,24,26,27, FR-CONC-8; #143).
``core.memory.consolidation`` holds the deterministic half (windows, scopes, inputs, coverage,
the modality rule, the cascade); this module calls the model and commits.

**When it runs.** A job is one window of one scope at one level (payload ``{from_turn, to_turn,
characters, level, explicit}``; ``coalesce_key`` ``consolidation:<story>:<scope>:<level>:<from>-
<to>``, with ``:explicit`` for a requested one):

- after an episodic write (``records.EPISODIC_HOOKS``, in the turn's commit): when the oldest
  uncovered window is due, by ``memory.consolidation.window_memories`` or ``cadence_turns``
  (both triggers; whichever fires first);
- after a truncation, untruncation, swap or fork (the post-commit membership hook): whatever
  is uncovered in the story as it now stands, so a window a truncation cut is consolidated again
  over the turns that survived, and a swap's restored chain is caught up;
- after a level-1 window commits: the next due window (catch-up) and, every ``LEVEL2_EVERY``
  level-1 windows, a level-2 window over their outputs;
- on request: ``POST /admin/stories/{id}/consolidate`` (``Engine.consolidate``, ``explicit``)
  and ``consolidate_window`` (in process, #157's evals); #147's overflow episodes call
  ``schedule_overflow`` (due at once, ahead of the queue).

Nothing is enqueued, and a job does nothing and calls nothing, when the story's effective
configuration has the ``consolidation`` module off (#129).

**What a run does.** In one snapshot it clamps the window to the head and reads what covers it.
A window already consolidated with the same inputs (the replay key) is a no-op with **no model
call** unless the request is explicit (FR-MEM-10: replay uses the stored result; a fork copies
it); a window whose inputs changed, or an explicit request, is rerun, and the outputs it
replaces (an explicit request's: those of the windows inside the one asked for) become
``superseded``. A window an earlier one partly covers starts after it. It plans
the window (``ScopeMismatch`` refuses mixed scopes before any call), declares every input
(``memories:<id>``, ``operations:<id>``), then, outside the snapshot, calls the
``consolidation`` purpose through the Model Access Layer once per ``CHUNK_RECORDS`` inputs
(``structured_call``: parse, validate, retry), under its own budget of ``MAX_CALLS_PER_RUN``
(10) calls, routed by the story's ``models.purposes.consolidation``. The engine recomputes each
statement's modality, speaker and visibility (``derive``) and checks each text against the
story's deterministic output policies: a violation re-summarises that chunk once, then the
violating statements are dropped (each rejected text an audit record on the job's operation). A
spent budget, a failed call or an uncertain answer ends the window at the last whole turn done.

**What it commits** (``queue.commit_job``): the job's own ``consolidation`` operation and one
operation positioned at the window's ``to_turn``, with the window's ``source_window`` and input
versions, a ``consolidation_output`` event and, in its commit step, the outputs
(``records.write_consolidated``: provenance ``system_consolidated``, model, model version
(``None``: the adapter reports none), prompt hash) and the inputs' ``covered_by``. If an input
was discarded or changed meanwhile (a truncation, an erasure) or another run covered the window
first, nothing is written and the job is queued again; its rerun plans the window against the
timeline as it then stands. Results, logs and metrics carry ids and counts only (invariant 3).

``bind(models)`` gives the job the process's Model Access Layer (``Engine.start_workers``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, timeline
from epistrel.core.enums import Membership, MemoryKind, OperationKind, Provenance
from epistrel.core.epistemic import transitions
from epistrel.core.errors import ScopeMismatch
from epistrel.core.events.operation import Operation
from epistrel.core.events.types import (
    ConsolidationOutput,
    GuardrailOutputRejected,
    RejectedViolation,
)
from epistrel.core.ids import new_ulid
from epistrel.core.input_versions import input_key
from epistrel.core.memory import consolidation as core
from epistrel.core.memory import records
from epistrel.core.memory.consolidation import Covered, Derived, Input, Plan, Scope, Window
from epistrel.core.policies.model import ModelAssistedRule, Policy
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.tables import operations
from epistrel.core.tables.memories import memories
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import (
    ClaimedJob,
    JobContext,
    JobHandler,
    JobOutput,
    PositionedOutput,
)
from epistrel.orchestrator.lite.prompt import prompt_hash
from epistrel.orchestrator.models import metrics as model_metrics
from epistrel.orchestrator.models.access import NO_ROUTES, ModelAccessLayer, routing
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import routes_of
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelError
from epistrel.orchestrator.models.registry import CONSOLIDATION as PURPOSE
from epistrel.orchestrator.models.registry import (
    ConsolidationInputs,
    ConsolidationRecord,
    ConsolidationResult,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.policies.output_gate import deterministic_check, output_policies
from epistrel.orchestrator.policies.store import guard_context

logger = logging.getLogger(__name__)

CONSOLIDATION = PURPOSE.name
"""The job kind and the model purpose share the name."""
MAX_CALLS_PER_RUN = 10
ATTEMPTS_PER_CHUNK = 2
"""A chunk whose statements break a deterministic output policy is consolidated once more."""
BUILT_IN_PARAMS = ModelParams(temperature=0.2, max_tokens=1_500)
"""The call's own params; the story's ``consolidation`` route's params win over them."""
PRIORITY_LEAD = timedelta(hours=1)
"""How far ahead of the queue an overflow episode's job is due (#147)."""

_bound: list[ModelAccessLayer] = []
_override: ContextVar[ModelAccessLayer | None] = ContextVar("consolidation_models", default=None)


class NoModels(RuntimeError):
    """No Model Access Layer is bound in this process (``bind``)."""


def bind(models: ModelAccessLayer | None) -> None:
    """Make ``models`` the layer the job calls through; ``None`` unbinds."""
    _bound.clear()
    if models is not None:
        _bound.append(models)


def bound() -> ModelAccessLayer | None:
    found = _override.get()
    if found is not None:
        return found
    return _bound[0] if _bound else None


class ConsolidationPayload(BaseModel):
    """One window of one scope at one level; ``explicit`` when a caller asked for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_turn: int = Field(ge=1)
    to_turn: int = Field(ge=1)
    characters: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=50)
    level: Literal[1, 2] = 1
    explicit: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> ConsolidationPayload:
        if self.to_turn < self.from_turn:
            raise ValueError("to_turn is at or after from_turn")
        return self

    @property
    def scope(self) -> Scope:
        return Scope(characters=self.characters)

    @property
    def window(self) -> Window:
        return Window(self.from_turn, self.to_turn)


def coalesce_key(story_id: str, payload: ConsolidationPayload) -> str:
    """One queued job per story, scope, level and window (a requested one apart)."""
    key = (
        f"{CONSOLIDATION}:{story_id}:{payload.scope.key}:{payload.level}:"
        f"{payload.from_turn}-{payload.to_turn}"
    )
    return f"{key}:explicit" if payload.explicit else key


# -- scheduling ---------------------------------------------------------------------------------


async def enqueue_window(
    conn: AsyncConnection,
    story_id: str,
    window: Window,
    scope: Scope | None = None,
    *,
    level: int = 1,
    explicit: bool = False,
    due_at: datetime | None = None,
    coalesce: bool = True,
) -> str:
    """Enqueue the consolidation of ``window`` in ``conn``'s transaction; answers the job id."""
    payload = ConsolidationPayload(
        from_turn=window.from_turn,
        to_turn=window.to_turn,
        characters=(scope or Scope()).characters,
        level=1 if level == 1 else 2,
        explicit=explicit,
    )
    return await queue.enqueue(
        conn,
        CONSOLIDATION,
        payload,
        story_id=story_id,
        coalesce_key=coalesce_key(story_id, payload) if coalesce else None,
        due_at=due_at,
    )


async def schedule_in(
    conn: AsyncConnection,
    story_id: str,
    *,
    force: bool = False,
    dials: core.ConsolidationSettings | None = None,
    floor: int = 0,
    due_at: datetime | None = None,
    head: int | None = None,
) -> str | None:
    """Enqueue the story's oldest due window of the shared scope (see the module docstring);
    answers the job id, or ``None`` when nothing is due or the module is off."""
    dials = dials or await core.settings_of(conn, story_id)
    if not dials.enabled:
        return None
    window = await core.next_window(
        conn, story_id, Scope(), dials, floor=floor, force=force, head=head
    )
    if window is None:
        return None
    return await enqueue_window(conn, story_id, window, due_at=due_at)


async def schedule_overflow(conn: AsyncConnection, story_id: str) -> str | None:
    """#147's overflow episode: whatever is uncovered, due ahead of the queue (FR-MEM-8)."""
    return await schedule_in(conn, story_id, force=True, due_at=clock.now() - PRIORITY_LEAD)


async def on_episodic_written(
    conn: AsyncConnection, story_id: str, memory_ids: tuple[str, ...]
) -> None:
    """``core.memory.records``' episodic hook: the commit may have made a window due (the
    turn being committed is the head already)."""
    if memory_ids:
        written = await conn.scalar(
            select(func.max(memories.c.narrative_turn)).where(
                memories.c.story_id == story_id, memories.c.id.in_(list(memory_ids))
            )
        )
        await schedule_in(conn, story_id, head=None if written is None else int(written))


async def on_membership_change(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
    """After truncate, untruncate, swap or fork (post-commit): consolidate what the timeline as
    it now stands leaves uncovered (a truncation's surviving window, a swap's restored chain,
    a fork's boundary)."""
    if change.operation_ids:
        await schedule_in(conn, change.story_id, force=True)


async def resolve_on_reactivation(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
    """In the edit's transaction, after the derived rows followed their operations: a
    reactivated original window wins over an overlapping rerun (FR-MEM-10)."""
    if change.new is Membership.ACTIVE and change.operation_ids:
        await core.resolve_reactivated(conn, change.story_id, change.operation_ids)


# -- the run ----------------------------------------------------------------------------------


@dataclass
class _Run:
    models: ModelAccessLayer
    policies: tuple[Policy, ...]
    names: dict[str, str]
    level: int
    scope: Scope
    budget: TurnBudget
    routes: Any = NO_ROUTES
    rejected: list[GuardrailOutputRejected] = field(default_factory=list)
    derived: Derived = field(default_factory=Derived)
    made: list[tuple[core.Output, str | None, str]] = field(default_factory=list)
    """Each kept output with the model and prompt hash that made it."""
    seen: set[str] = field(default_factory=set)
    uncertain: int = 0


def _noop(reason: str, **counts: Any) -> JobOutput:
    return JobOutput(result={"calls": 0, "outputs": 0, "noop": reason, **counts})


def _visibility_label(i: Input) -> str:
    v = i.visibility
    return v.scope if v.scope != "characters" else "private to " + ", ".join(v.characters or ())


def _records(chunk: Sequence[Input], names: dict[str, str]) -> list[ConsolidationRecord]:
    return [
        ConsolidationRecord(
            index=n,
            text=i.text,
            kind=i.kind,
            holder=names.get(i.holder, i.holder) if i.holder else "shared",
            visibility=_visibility_label(i),
            position=f"turn {i.narrative_turn}",
            speaker=names.get(i.speaker, i.speaker) if i.speaker else None,
        )
        for n, i in enumerate(chunk)
    ]


def _scope_label(scope: Scope, names: dict[str, str]) -> str:
    if scope.characters is None:
        return "shared (what every character may know)"
    return "private to " + ", ".join(names.get(c, c) for c in scope.characters)


def _proposed(answer: ConsolidationResult) -> list[core.Proposed]:
    found = [
        core.Proposed(MemoryKind.SEMANTIC, s.text, s.modality, tuple(s.supports))
        for s in answer.facts
    ]
    found.extend(
        core.Proposed(MemoryKind.REFLECTION, s.text, s.modality, tuple(s.supports))
        for s in answer.reflections
    )
    return found


def _rejected(text_: str, attempt: int, check: Any) -> GuardrailOutputRejected:
    return GuardrailOutputRejected(
        text=text_,
        reason="output_policy",
        attempt=attempt,
        policy_ids=tuple(v.policy_id for v in check.violations),
        violations=tuple(
            RejectedViolation(policy_id=v.policy_id, name=v.name, message=v.message, kind=v.kind)
            for v in check.violations
        ),
    )


async def _chunk(run: _Run, chunk: Sequence[Input], window: Window) -> bool:
    """Consolidate one chunk; ``False`` when its answer was uncertain (nothing kept)."""
    inputs = ConsolidationInputs(
        records=_records(chunk, run.names),
        scope=_scope_label(run.scope, run.names),
        level=run.level,
        covers=window.label,
    )
    route = run.routes.get(CONSOLIDATION)
    params = BUILT_IN_PARAMS.merged(route.params if route is not None else None)
    for attempt in range(ATTEMPTS_PER_CHUNK):
        _, _, messages = run.models.prepare(CONSOLIDATION, inputs)
        outer = model_metrics.CALL_LOG.get()
        with routing(run.routes), model_metrics.recording() as log:
            answer = await run.models.structured_call(
                CONSOLIDATION,
                ConsolidationResult,
                inputs,
                params=params,
                deterministic_first=lambda: None if chunk else ConsolidationResult(),
                budget=run.budget,
            )
        if outer is not None:
            outer.calls.extend(log.calls)
        model = next((c.model for c in reversed(log.calls) if c.outcome == "ok"), None)
        if isinstance(answer, Uncertain):
            run.uncertain += 1
            return False
        seen = set(run.seen)
        derived = core.derive(_proposed(answer), chunk, run.names, level=run.level, seen=seen)
        checks = [(o, deterministic_check(o.text, run.policies)) for o in derived.outputs]
        broken = [(o, c) for o, c in checks if not c.passed]
        for output, check in broken:
            run.rejected.append(_rejected(output.text, attempt, check))
        if broken and attempt + 1 < ATTEMPTS_PER_CHUNK:
            continue
        kept = [o for o, c in checks if c.passed]
        run.seen.update(o.text.casefold() for o in kept)
        run.derived.unsupported += derived.unsupported
        run.derived.downgraded += derived.downgraded
        run.derived.duplicates += derived.duplicates
        run.derived.speculative += derived.speculative
        hashed = prompt_hash(messages)
        run.made.extend((o, model, hashed) for o in kept)
        return True
    return True  # pragma: no cover - the loop returns on its last attempt


def _done_through(chunks: Sequence[Sequence[Input]], done: int, window: Window) -> int:
    """The last whole turn the first ``done`` chunks consolidated (``from_turn - 1``: none)."""
    if done == 0:
        return window.from_turn - 1
    last = chunks[done - 1][-1].narrative_turn
    if done < len(chunks) and chunks[done][0].narrative_turn == last:
        return last - 1
    return last if done < len(chunks) else window.to_turn


def _trimmed(planned: Plan, through: int) -> Plan:
    kept = tuple(i for i in planned.inputs if i.narrative_turn <= through)
    return replace(
        planned,
        window=Window(planned.window.from_turn, through),
        inputs=kept,
        inputs_hash=core.inputs_hash(kept, planned.scope, planned.level),
    )


async def _consolidate(run: _Run, planned: Plan) -> tuple[Plan, int]:
    """Every chunk of ``planned`` in order, until one cannot be done; the plan as far as it got
    and the chunks done."""
    chunks = planned.chunks()
    done = 0
    for chunk in chunks:
        try:
            ok = await _chunk(run, chunk, planned.window)
        except BudgetExhausted:
            break
        except ModelError as exc:
            if done == 0:
                raise
            logger.info("consolidation call failed: %s", type(exc).__name__)
            break
        if not ok:
            break
        done += 1
    if done == len(chunks):
        return planned, done
    through = _done_through(chunks, done, planned.window)
    if through < planned.window.from_turn:
        return planned, 0
    cut = _trimmed(planned, through)
    kept = {i.key for i in cut.inputs}
    run.made = [(o, m, h) for o, m, h in run.made if all(s.key in kept for s in o.supports)]
    return cut, done


@dataclass(frozen=True, slots=True)
class _Read:
    """What the snapshot read."""

    planned: Plan
    replaced: tuple[Covered, ...]
    seen: tuple[Covered, ...]
    superseded: tuple[str, ...]
    names: dict[str, str]
    routes: Any
    dials: core.ConsolidationSettings


async def _read(ctx: JobContext, story_id: str, payload: ConsolidationPayload) -> _Read | JobOutput:
    scope, level = payload.scope, payload.level
    async with ctx.snapshot() as conn:
        effective = await effective_configuration(conn, story_id, validate=False)
        dials = core.ConsolidationSettings.of(effective.settings)
        if not dials.enabled:
            return _noop("module_off")
        head = await core.head_turn(conn, story_id)
        window = Window(payload.from_turn, min(payload.to_turn, head))
        if window.to_turn < window.from_turn:
            return _noop("beyond_head")
        covering = await core.coverage(conn, story_id, scope.key, level, window)
        inside = [
            c
            for c in covering
            if window.from_turn <= c.window.from_turn and c.window.to_turn <= window.to_turn
        ]
        replaced: list[Covered] = inside if payload.explicit else []
        exact = [c for c in covering if c.window == window]
        if not payload.explicit and covering and not exact:
            start = max(c.window.to_turn for c in covering) + 1
            if start > window.to_turn:
                return _noop("covered")
            window = Window(start, window.to_turn)
        try:
            planned = await core.plan(conn, story_id, window, scope, level)
        except ScopeMismatch:
            return _noop("scope_mismatch")
        if planned is None:
            return _noop("no_inputs")
        if not payload.explicit and exact:
            if all(c.inputs_hash == planned.inputs_hash for c in exact):
                return _noop("replayed")
            replaced = exact
        keys: list[str] = []
        for i in planned.inputs:
            keys += [i.key, input_key(operations.name, i.operation_id)]
        keys += [input_key(operations.name, c.operation_id) for c in replaced]
        await ctx.declare_inputs(conn, keys)
        superseded = await core.outputs_of(conn, story_id, [c.operation_id for c in replaced])
        names = await core.character_names(conn, story_id)
    return _Read(
        planned=planned,
        replaced=tuple(replaced),
        seen=tuple(covering),
        superseded=tuple(superseded),
        names=names,
        routes=routes_of(effective.settings),
        dials=dials,
    )


async def _run(ctx: JobContext) -> JobOutput:
    story_id = ctx.job.story_id
    if story_id is None:
        raise ValueError("a consolidation job belongs to a story")
    payload = ctx.payload
    assert isinstance(payload, ConsolidationPayload)  # noqa: S101 - the handler's model
    read = await _read(ctx, story_id, payload)
    if isinstance(read, JobOutput):
        return read
    models = bound()
    if models is None:
        raise NoModels("no Model Access Layer is bound for the consolidation job")
    guarded = await guard_context(ctx.db, story_id)
    run = _Run(
        models=models,
        policies=tuple(
            p for p in output_policies(guarded) if not isinstance(p.rule, ModelAssistedRule)
        ),
        names=read.names,
        level=payload.level,
        scope=payload.scope,
        budget=TurnBudget(max_calls=MAX_CALLS_PER_RUN),
        routes=read.routes,
    )
    planned, done = await _consolidate(run, read.planned)
    if done == 0:
        return JobOutput(
            result={"calls": run.budget.used, "outputs": 0, "uncertain": run.uncertain}
        )
    return _output(story_id, payload, read, run, planned, ctx.input_versions)


def _output(
    story_id: str,
    payload: ConsolidationPayload,
    read: _Read,
    run: _Run,
    planned: Plan,
    declared: Mapping[str, Any],
) -> JobOutput:
    rows = [
        core.output_row(
            output, planned, memory_id=new_ulid(), model=model, model_version=None, prompt_hash=h
        )
        for output, model, h in run.made
    ]
    memory_ids = tuple(str(r["id"]) for r in rows)
    covered = [i.record_id for i in planned.inputs if i.is_memory]
    positioned_id = new_ulid()
    window, scope, level = planned.window, planned.scope, planned.level
    rejected = list(run.rejected)
    seen = {c.operation_id for c in read.seen}
    replaced = [c.operation_id for c in read.replaced]
    dials = read.dials

    def stage(op: Operation) -> None:
        for record_ in rejected:
            op.append("guardrail_output_rejected", record_, provenance=Provenance.ENGINE)

        async def first_to_cover(conn: AsyncConnection) -> None:
            # Another run covered this window since the snapshot: drop, and rerun (a no-op).
            for current in await core.coverage(conn, story_id, scope.key, level, window):
                if current.operation_id not in seen:
                    raise queue.StaleInputs((input_key(operations.name, current.operation_id),))

        op.add_commit_step(first_to_cover)

    def positioned(op: Operation) -> None:
        op.append(
            "consolidation_output",
            ConsolidationOutput(
                memory_ids=memory_ids,
                superseded_ids=read.superseded,
                from_turn=window.from_turn,
                to_turn=window.to_turn,
                scope=scope.key,
                level=level,
            ),
            provenance=Provenance.ENGINE,
        )

        async def write(conn: AsyncConnection) -> None:
            if replaced:
                await core.supersede_outputs(conn, story_id, replaced)
            await records.write_consolidated(conn, op, rows, covered)
            if level == 1 and scope.characters is not None:
                # FR-BELIEF-10: the scope's beliefs from the window decay (#160's forgetting
                # transition; nothing when the story keeps no partitions).
                await transitions.forget_covered(
                    conn,
                    op,
                    holders=scope.characters,
                    from_turn=window.from_turn,
                    to_turn=window.to_turn,
                )
            if level == 1:
                folded = await core.next_level2(conn, story_id, scope, extra=window)
                if folded is not None:
                    await enqueue_window(conn, story_id, folded, scope, level=2)
                if scope.characters is None:
                    await schedule_in(conn, story_id, dials=dials, floor=window.to_turn)

        op.add_commit_step(write)

    job_window = {
        "consolidation": "job",
        **window.document(),
        "scope": scope.key,
        "level": level,
    }
    return JobOutput(
        stage=stage,
        positioned=(
            PositionedOutput(
                turn=window.to_turn,
                stage=positioned,
                operation_id=positioned_id,
                source_window=core.source_window(
                    window,
                    scope,
                    level,
                    planned.inputs_hash,
                    _versions(planned, declared),
                    explicit=payload.explicit,
                ),
            ),
        ),
        source_window=job_window,
        result={
            "calls": run.budget.used,
            "outputs": len(rows),
            "inputs": len(planned.inputs),
            "from_turn": window.from_turn,
            "to_turn": window.to_turn,
            "level": level,
            "unsupported": run.derived.unsupported,
            "downgraded": run.derived.downgraded,
            "duplicates": run.derived.duplicates,
            "speculative": run.derived.speculative,
            "rejected": len(rejected),
            "superseded": len(read.superseded),
        },
    )


def _versions(planned: Plan, declared: Mapping[str, Any]) -> dict[str, Any]:
    """The versions the run declared for ``planned``'s inputs (FR-STORE-27)."""
    keys = {i.key for i in planned.inputs} | {
        input_key(operations.name, i.operation_id) for i in planned.inputs
    }
    return {k: v for k, v in sorted(declared.items()) if k in keys}


CONSOLIDATION_HANDLER = JobHandler(
    kind=CONSOLIDATION,
    payload=ConsolidationPayload,
    run=_run,
    operation_kind=OperationKind.CONSOLIDATION,
    max_attempts=1,
)


@dataclass(frozen=True, slots=True)
class ConsolidationQueued:
    """A requested consolidation handed to a worker (``Engine.consolidate``, REST 202)."""

    job_id: str
    timeline_revision: int = 0
    """The story's revision when it was queued (a consolidation never moves it)."""
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- in process ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsolidationRun:
    """One ``consolidate_window`` call: its job, how its commit went, and its result (ids and
    counts)."""

    job_id: str
    outcome: queue.CommitOutcome | str
    result: dict[str, Any]


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'consolidate_window', updated_at = :now "
    "WHERE id = :j AND status = 'queued' RETURNING *"
)
LEASE_S = 300.0


async def consolidate_window(
    db: AsyncEngine,
    story_id: str,
    window: Window,
    scope: Scope | None = None,
    *,
    level: int = 1,
    explicit: bool = False,
    models: ModelAccessLayer | None = None,
) -> ConsolidationRun:
    """Consolidate ``window`` of ``scope`` now, in this process (#157's evals and the tests):
    the job a worker would run, enqueued uncoalesced, claimed at once and committed with the
    same recheck. ``models`` is the layer to call through (the bound one when ``None``)."""
    async with db.begin() as conn:
        job_id = await enqueue_window(
            conn, story_id, window, scope, level=level, explicit=explicit, coalesce=False
        )
    now = clock.now()
    async with db.begin() as conn:
        row = (
            await conn.execute(
                _CLAIM, {"j": job_id, "now": now, "lease": now + timedelta(seconds=LEASE_S)}
            )
        ).one()
    job = ClaimedJob(
        id=row.id,
        kind=row.kind,
        story_id=row.story_id,
        payload=dict(row.payload or {}),
        attempt=int(row.attempt),
        failures=int(row.failures),
        due_at=row.due_at,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        coalesce_key=row.coalesce_key,
    )
    payload = ConsolidationPayload.model_validate(job.payload)
    ctx = JobContext(db=db, job=job, payload=payload)
    token = _override.set(models) if models is not None else None
    try:
        output = await CONSOLIDATION_HANDLER.run(ctx)
    except Exception as exc:
        await queue.fail_job(db, job, type(exc).__name__, max_attempts=1, terminal=True)
        raise
    finally:
        if token is not None:
            _override.reset(token)
    outcome = await queue.commit_job(db, job, CONSOLIDATION_HANDLER, output, ctx.input_versions)
    result = dict(output.result or {})
    return ConsolidationRun(job_id=job_id, outcome=outcome, result=result)


records.register_episodic_hook(on_episodic_written)
timeline.on_membership_change(on_membership_change, phase=timeline.HookPhase.POST_COMMIT)
timeline.on_membership_change(resolve_on_reactivation)

__all__ = [
    "CONSOLIDATION",
    "CONSOLIDATION_HANDLER",
    "MAX_CALLS_PER_RUN",
    "ConsolidationPayload",
    "ConsolidationQueued",
    "ConsolidationRun",
    "NoModels",
    "bind",
    "bound",
    "coalesce_key",
    "consolidate_window",
    "enqueue_window",
    "on_episodic_written",
    "on_membership_change",
    "resolve_on_reactivation",
    "schedule_in",
    "schedule_overflow",
]
