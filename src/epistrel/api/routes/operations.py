"""``GET /operations/{id}``: one operation through the history API (FR-CONC-6; #61).

The reference a retry gets after the idempotency result window points here. A thin handler
over ``Engine.get_operation``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep, OperationId
from epistrel.core.genesis.models import CreationReport
from epistrel.core.operation_view import OperationLabel, OperationView, Stage, StageStatus

router = APIRouter(prefix="/operations", tags=["operations"])

OPERATION_DESCRIPTION = (
    "An operation's kind, status (`in_flight`, `committed`, `abandoned` or `failed`), position "
    "(`narrative_turn`, `turn_variant`), the story's `timeline_revision` once it committed, the "
    "requesting principal, its timestamps, revalidation `attempts`, `config_revisions`, "
    "`origin` (`generated` or `ingested`) and, for an ingested batch, `batch_operation_ids`. "
    "A committed Lite turn reports `queued_ms` (its wait in the story's turn queue, null when "
    "it was not queued) and `first_call_offset_ms` (from reaching the front to its first model "
    "call). A story's `genesis` operation from a scenario carries its full `creation_report` "
    "(every entry, deferred item and inactive setting) to a read with `?viewpoint=omniscient` "
    "only; otherwise it is null."
    "\n\nA long-running operation names what it was for in `label` and lists its `stages`, "
    "each `{name, status, at}` with `status` one of `pending`, `running`, `done`, `failed`, "
    "`skipped` (or `partial`, for an erasure's summary regeneration that rewrote only some "
    "windows) and `at` null while pending: a story's `genesis` (`validated`, `snapshotted`, "
    "`skipped` for a blank story, `materialized`); a rating change's `rating_check` or an edit's "
    "`reactivation_check` (`checking`, then `applied`/`reactivated` or `refused`); an "
    "`offboarding` (one stage per entity type, in processing order); and an `erasure` "
    "(`redacted`, `ledger_written`, `regenerated`, `rebuilt`; a catalog erasure skips the last "
    "two). Every other operation has a null `label` and no stages."
    "\n\nAn operation that captured a read set reports `read_set`: `keys_count` (the query "
    "keys a structured turn's verdict, precondition and blocker lookups recorded, empty "
    "results included; 0 for a Lite turn), `retrieved_count` (rows read as generation context "
    "only) and, with `?viewpoint=omniscient` only, the `keys` themselves (`subject`, "
    "`predicate`, `object`, `holder`, `window`, `result_hash`); otherwise `keys` is null. "
    "`read_set` is null for an operation with no read set."
    "\n\nA committed structured turn reports `eval_hooks` to a read with "
    "`?viewpoint=omniscient` only (FR-OBS-2): `raw_render_event_ids`, the verification "
    "`verdicts`, the `repair_rungs`, `model_calls_by_purpose` and `latency_ms_by_stage`; "
    "otherwise it is null."
    "\n\nWithin the idempotency result window (default 24 hours) `result` holds the stored "
    "response (`status`, `body`) of the operation's `Idempotency-Key`. A result produced for an "
    "output viewpoint (a turn) is returned only when `?viewpoint=` names that viewpoint; another "
    "named viewpoint is 409 `idempotency_conflict`. After the window `result` is absent: the "
    "operation is the reference a retry receives. Unknown ids are 404."
)


class StoredResultBody(BaseModel):
    """The stored response of the operation's ``Idempotency-Key``: its status and body."""

    status: int
    body: Any


class StageBody(BaseModel):
    """One stage of a long-running operation: its name, where it is, and when it got there
    (null while pending)."""

    name: str
    status: StageStatus
    at: datetime | None

    @classmethod
    def of(cls, stage: Stage) -> StageBody:
        return cls(name=stage.name, status=stage.status, at=stage.at)


class QueryKeyBody(BaseModel):
    """One query key of a structured turn's read set: what the lookup asked (ids; `*` for a
    wildcard subject or object) and the hash of what it found."""

    subject: str | list[str]
    predicate: str
    object: str | dict[str, Any]
    holder: str
    window: list[int | None]
    result_hash: str


class ReadSetBody(BaseModel):
    """An operation's read set: counts, and the keys to an omniscient read only."""

    keys_count: int
    retrieved_count: int
    keys: list[QueryKeyBody] | None = None


class EvalHooksBody(BaseModel):
    """A structured turn's eval hooks (FR-OBS-2; #155): ids, codes and counts only."""

    raw_render_event_ids: list[str] = Field(
        description="The events holding the raw render: each released beat's own event, and a "
        "repaired beat's original `render_rejected` event."
    )
    verdicts: list[dict[str, Any]] = Field(
        description="The verification verdicts (`turn_report.verify` and `verify_follow_up`)."
    )
    repair_rungs: list[dict[str, Any]] = Field(
        description="Per repaired beat: `beat`, the `rung` accepted (4: the safe fallback) and "
        "whether a correction was `accepted`."
    )
    model_calls_by_purpose: dict[str, int]
    latency_ms_by_stage: dict[str, int] = Field(
        description="Wall time by stage: `plan`, `render`, `normalize`, `detection`, `verify`, "
        "`entailment`, `repair`, `composition`, `commit`."
    )


class OperationBody(BaseModel):
    """One operation: kind, status, position, revision, attribution, timestamps, attempts,
    configuration revisions, origin, a turn's queue wait and first-call delay, a genesis
    operation's creation report (omniscient reads only), a long-running operation's label and
    stages and, within the result window, its stored result."""

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
    result: StoredResultBody | None = None
    queued_ms: int | None = None
    first_call_offset_ms: int | None = None
    creation_report: CreationReport | None = None
    label: OperationLabel | None = None
    stages: list[StageBody] = []
    read_set: ReadSetBody | None = None
    eval_hooks: EvalHooksBody | None = None

    @classmethod
    def of(cls, view: OperationView) -> OperationBody:
        return cls(
            id=view.id,
            story_id=view.story_id,
            kind=view.kind,
            status=view.status,
            narrative_turn=view.narrative_turn,
            turn_variant=view.turn_variant,
            timeline_revision=view.timeline_revision,
            principal=view.principal,
            started_at=view.started_at,
            committed_at=view.committed_at,
            viewpoint=view.viewpoint,
            origin=view.origin,
            batch_operation_ids=view.batch_operation_ids,
            attempts=view.attempts,
            config_revisions=view.config_revisions,
            idempotency_key=view.idempotency_key,
            queued_ms=view.queued_ms,
            first_call_offset_ms=view.first_call_offset_ms,
            creation_report=None
            if view.creation_report is None
            else CreationReport.model_validate(view.creation_report),
            label=view.label,
            stages=[StageBody.of(stage) for stage in view.stages],
            read_set=None
            if view.read_set is None
            else ReadSetBody(
                keys_count=view.read_set.keys_count,
                retrieved_count=view.read_set.retrieved_count,
                keys=None
                if view.read_set.keys is None
                else [QueryKeyBody.model_validate(k) for k in view.read_set.keys],
            ),
            eval_hooks=None
            if view.eval_hooks is None
            else EvalHooksBody.model_validate(view.eval_hooks),
            result=(
                StoredResultBody(status=view.result.status, body=view.result.body)
                if view.result is not None
                else None
            ),
        )


@router.get(
    "/{operation_id}",
    response_model=OperationBody,
    response_model_exclude_none=False,
    operation_id="get_operation",
    summary="Read one operation (the reference of an idempotent retry)",
    description=OPERATION_DESCRIPTION,
)
async def get_operation(
    operation_id: OperationId, ctx: ContextDep, engine: EngineDep
) -> OperationBody:
    return OperationBody.of(await engine.get_operation(ctx, operation_id))
