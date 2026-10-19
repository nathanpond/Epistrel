"""``/admin/evals``: claim detection's shadow report (FR-OBS-4, FR-VERIFY-13, NFR-QUAL-10; #148).

``GET /extraction-shadow`` answers what ``gated`` claim extraction missed over a window
(``from``/``to``, the last 30 days by default; one ``story_id`` or every story): the shadow
samples (``samples``, ``failed``), the claims their extractions made, the ones gating missed
(``missed``, ``missed_by_category``) and ``miss_rate``; and, separately, detection and checking
over every turn report in the window (``detected_total``, ``checked_total``). One page of the
samples follows (``limit`` 1 to 200, default 50; ``after`` is the last page's ``next``). Ids,
counts and codes only, never story text.

``GET /escalation-yield`` (#155; FR-OBS-2, NFR-PERF-5) answers escalation yield over a window
(``from`` inclusive, ``to`` exclusive, the last 30 days by default): per gate reason, per
predicate shape and per both, the escalations, the ones a live tightening skipped, the valid
model verdicts, the flips and the flip and consequence-weighted flip rates, then the overall
escalation rate with its 0.2 reference baseline (``is_gate`` false).

``/admin/verify/gate`` (#155; FR-OBS-4) holds per-shape gate tightenings: ``PUT`` proposes a
shape's threshold in ``shadow`` (the live gate unchanged; every escalation of the shape records
what the tightened gate would have decided) or puts it ``live`` (confirmed with
``ignore_warnings``), ``POST /gate/{shape}/promote`` makes a shadowed proposal live when its
window meets the rule (else 409 ``gate_not_promotable`` with the numbers), and
``DELETE /gate/{shape}`` removes it. A shape in a path is its key with ``|`` written ``~``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.errors import InvalidRequest
from epistrel.core.verify.shadow import PAGE_DEFAULT, PAGE_MAX, Sample, ShadowReport
from epistrel.core.verify.yield_queries import (
    GateRequest,
    GateWrite,
    YieldCluster,
    YieldReport,
    shape_of_path,
)

router = APIRouter(prefix="/evals")
gate_router = APIRouter(prefix="/verify")

ISO_DATETIME = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})$"
WHEN = "An ISO 8601 date-time with its offset (`2026-10-17T12:00:00Z`)."


def _when(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise InvalidRequest(
            f"{field} is an ISO 8601 date-time",
            {"field": f"query.{field}", "reason": "invalid_datetime"},
        ) from None


class ShadowSampleBody(BaseModel):
    """One shadow sample: a sampled turn's render or composed message."""

    model_config = ConfigDict(extra="forbid")

    id: str
    story_id: str
    operation_id: str = Field(description="The sampled turn's operation.")
    narrative_turn: int | None
    status: str = Field(description="`sampled`, or `failed` when its extraction call failed.")
    extraction_claims: int = Field(description="Every claim the shadow extraction made.")
    missed_count: int = Field(description="The claims no declared or detector claim covered.")
    missed_by_category: dict[str, int]
    created_at: datetime

    @classmethod
    def of(cls, sample: Sample) -> ShadowSampleBody:
        return cls(
            id=sample.id,
            story_id=sample.story_id,
            operation_id=sample.operation_id,
            narrative_turn=sample.narrative_turn,
            status=sample.status,
            extraction_claims=sample.extraction_claims,
            missed_count=sample.missed_count,
            missed_by_category=dict(sample.missed_by_category),
            created_at=sample.created_at,
        )


class ExtractionShadowBody(BaseModel):
    """What gating missed, and detection and checking counted apart."""

    model_config = ConfigDict(extra="forbid")

    window_from: datetime = Field(serialization_alias="from")
    window_to: datetime = Field(serialization_alias="to")
    story_id: str | None
    samples: int = Field(description="Sampled renders and composed messages.")
    failed: int = Field(description="Samples whose extraction call failed.")
    extraction_claims: int
    missed: int
    missed_by_category: dict[str, int]
    miss_rate: float | None = Field(
        description="Missed claims over every claim the sampled extractions made; `null` when "
        "they made none."
    )
    detected_total: int = Field(description="Claims detected in the window's turn reports.")
    checked_total: int = Field(description="Detected claims checked in those turn reports.")
    items: list[ShadowSampleBody]
    next: str | None = Field(description="Pass as `after` for the next page; `null` at the end.")

    @classmethod
    def of(cls, found: ShadowReport) -> ExtractionShadowBody:
        return cls(
            window_from=found.window_from,
            window_to=found.window_to,
            story_id=found.story_id,
            samples=found.samples,
            failed=found.failed,
            extraction_claims=found.extraction_claims,
            missed=found.missed,
            missed_by_category=dict(found.missed_by_category),
            miss_rate=found.miss_rate,
            detected_total=found.detected_total,
            checked_total=found.checked_total,
            items=[ShadowSampleBody.of(s) for s in found.items],
            next=found.next,
        )


@router.get(
    "/extraction-shadow",
    response_model=ExtractionShadowBody,
    response_model_by_alias=True,
    operation_id="extraction_shadow_report",
    summary="Read claim detection's shadow report",
)
async def extraction_shadow_report(
    ctx: ContextDep,
    engine: EngineDep,
    window_from: Annotated[
        str | None, Query(alias="from", pattern=ISO_DATETIME, description=WHEN)
    ] = None,
    window_to: Annotated[str | None, Query(alias="to", pattern=ISO_DATETIME, description=WHEN)] = (
        None
    ),
    story_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    after: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> ExtractionShadowBody:
    found = await engine.extraction_shadow_report(
        ctx,
        window_from=_when(window_from, "from"),
        window_to=_when(window_to, "to"),
        story_id=story_id,
        limit=limit,
        after=after,
    )
    return ExtractionShadowBody.of(found)


# -- escalation yield (#155) ------------------------------------------------------------------


class YieldClusterBody(BaseModel):
    """One cluster: a gate reason, a shape, or both."""

    model_config = ConfigDict(extra="forbid")

    gate_reason: str | None = None
    shape: str | None = None
    escalations: int = Field(description="Escalations the gate made (every verification pass).")
    skipped: int = Field(description="Claims a live tightening skipped (no model call).")
    valid: int = Field(description="Escalations with a valid model verdict.")
    flips: int = Field(description="Escalations whose model verdict was taken over `uncertain`.")
    flip_rate: float | None = Field(description="`flips / valid`; `null` when `valid` is 0.")
    weighted_flip_rate: float | None = Field(
        description="Each flip weighted by its consequence (`repair_kept` 1, `repair_discarded` "
        "0.25, `none` 0), over `valid`."
    )

    @classmethod
    def of(cls, found: YieldCluster) -> YieldClusterBody:
        return cls(
            gate_reason=found.gate_reason,
            shape=found.shape,
            escalations=found.escalations,
            skipped=found.skipped,
            valid=found.valid,
            flips=found.flips,
            flip_rate=found.flip_rate,
            weighted_flip_rate=found.weighted_flip_rate,
        )


class EscalationYieldBody(BaseModel):
    """Escalation yield over the window (see the module docstring)."""

    model_config = ConfigDict(extra="forbid")

    window_from: datetime = Field(serialization_alias="from")
    window_to: datetime = Field(serialization_alias="to")
    by_gate_reason: list[YieldClusterBody]
    by_shape: list[YieldClusterBody]
    by_gate_reason_shape: list[YieldClusterBody]
    totals: YieldClusterBody
    claims: int = Field(description="Claims verified by the window's turns.")
    escalated: int = Field(description="Of those, the claims escalated to the model.")
    escalation_rate: float | None = Field(
        description="`escalated / claims`; `null` when no claim was verified."
    )
    reference_baseline: float = Field(description="NFR-PERF-5's reference rate, 0.2.")
    is_gate: bool = Field(description="Always `false`: the baseline is a reference, not a gate.")

    @classmethod
    def of(cls, found: YieldReport) -> EscalationYieldBody:
        return cls(
            window_from=found.window_from,
            window_to=found.window_to,
            by_gate_reason=[YieldClusterBody.of(c) for c in found.by_gate_reason],
            by_shape=[YieldClusterBody.of(c) for c in found.by_shape],
            by_gate_reason_shape=[YieldClusterBody.of(c) for c in found.by_gate_reason_shape],
            totals=YieldClusterBody.of(found.totals),
            claims=found.claims,
            escalated=found.escalated,
            escalation_rate=found.escalation_rate,
            reference_baseline=found.reference_baseline,
            is_gate=found.is_gate,
        )


@router.get(
    "/escalation-yield",
    response_model=EscalationYieldBody,
    response_model_by_alias=True,
    operation_id="escalation_yield",
    summary="Read escalation yield",
)
async def escalation_yield(
    ctx: ContextDep,
    engine: EngineDep,
    window_from: Annotated[
        str | None, Query(alias="from", pattern=ISO_DATETIME, description=WHEN)
    ] = None,
    window_to: Annotated[str | None, Query(alias="to", pattern=ISO_DATETIME, description=WHEN)] = (
        None
    ),
) -> EscalationYieldBody:
    found = await engine.escalation_yield(
        ctx, window_from=_when(window_from, "from"), window_to=_when(window_to, "to")
    )
    return EscalationYieldBody.of(found)


# -- gate tightening (#155) -------------------------------------------------------------------


class GateBody(DeploymentMutationResponse):
    """A shape's gate threshold after the call (`null` values once removed)."""

    model_config = ConfigDict(extra="forbid")

    shape: str
    applied: bool = Field(
        description="`false` for a `live` write without `ignore_warnings`: nothing was written."
    )
    warnings: list[str] = Field(
        description="`unvalidated_tightening`: a `live` write skips the shadow window."
    )
    threshold: float | None = Field(description="The proposed threshold.")
    mode: str | None = Field(description="`shadow` (under evaluation) or `live`.")
    live_threshold: float | None = Field(description="The threshold the live gate applies.")
    window_start: datetime | None = Field(description="The shadow window's start.")
    proposed_at: datetime | None
    promoted_at: datetime | None

    @classmethod
    def of(cls, written: GateWrite) -> GateBody:
        return cls(
            config_revision=written.config_revision,
            shape=written.shape,
            applied=written.applied,
            warnings=list(written.warnings),
            threshold=written.threshold,
            mode=written.mode,
            live_threshold=written.live_threshold,
            window_start=written.window_start,
            proposed_at=written.proposed_at,
            promoted_at=written.promoted_at,
        )


ShapePath = Annotated[
    str,
    Path(
        min_length=7,
        max_length=170,
        pattern=r"^[a-z0-9_~]+$",
        description="The shape key with `|` written `~` (`core~functional~fluent~affirmed`).",
    ),
]


@gate_router.put(
    "/gate",
    response_model=GateBody,
    operation_id="set_verify_gate",
    summary="Propose or apply a gate tightening for one predicate shape",
)
async def set_verify_gate(
    body: GateRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> GateBody:
    written = await engine.set_verify_gate(ctx, body)
    return answered(GateBody.of(written), ctx, response, replayed=written.replayed)


@gate_router.post(
    "/gate/{shape}/promote",
    response_model=GateBody,
    operation_id="promote_verify_gate",
    summary="Promote a shadowed gate tightening",
)
async def promote_verify_gate(
    shape: ShapePath, response: Response, ctx: ContextDep, engine: EngineDep
) -> GateBody:
    written = await engine.promote_verify_gate(ctx, shape_of_path(shape))
    return answered(GateBody.of(written), ctx, response, replayed=written.replayed)


@gate_router.delete(
    "/gate/{shape}",
    response_model=GateBody,
    operation_id="remove_verify_gate",
    summary="Remove a gate tightening",
)
async def remove_verify_gate(
    shape: ShapePath, response: Response, ctx: ContextDep, engine: EngineDep
) -> GateBody:
    written = await engine.remove_verify_gate(ctx, shape_of_path(shape))
    return answered(GateBody.of(written), ctx, response, replayed=written.replayed)


__all__ = [
    "EscalationYieldBody",
    "ExtractionShadowBody",
    "GateBody",
    "ShadowSampleBody",
    "YieldClusterBody",
    "gate_router",
    "router",
]
