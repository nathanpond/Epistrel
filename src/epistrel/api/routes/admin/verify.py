"""``/admin/evals``: claim detection's shadow report (FR-OBS-4, FR-VERIFY-13, NFR-QUAL-10; #148).

``GET /extraction-shadow`` answers what ``gated`` claim extraction missed over a window
(``from``/``to``, the last 30 days by default; one ``story_id`` or every story): the shadow
samples (``samples``, ``failed``), the claims their extractions made, the ones gating missed
(``missed``, ``missed_by_category``) and ``miss_rate``; and, separately, detection and checking
over every turn report in the window (``detected_total``, ``checked_total``). One page of the
samples follows (``limit`` 1 to 200, default 50; ``after`` is the last page's ``next``). Ids,
counts and codes only, never story text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.core.errors import InvalidRequest
from epistrel.core.verify.shadow import PAGE_DEFAULT, PAGE_MAX, Sample, ShadowReport

router = APIRouter(prefix="/evals")

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


__all__ = ["ExtractionShadowBody", "ShadowSampleBody", "router"]
