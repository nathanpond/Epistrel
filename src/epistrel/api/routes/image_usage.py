"""Image usage and cost reports (FR-IMG-11, part D; #269).

- ``GET /admin/image-usage?from=&to=``: the deployment's usage and cost per local day.
- ``GET /stories/{story_id}/image-usage``: a story's, from its records by ``story_id``.
- ``GET /catalog/characters/{character_id}/image-usage`` and
  ``GET /catalog/scenarios/{scenario_id}/image-usage``: a catalog entry's, one route per kind.

Every report counts every provider attempt: billed outputs whatever became of them (kept,
discarded by a check, stale, unattached candidates, late or duplicate returns), attempts the
provider did not bill at ``zero``, and ``unknown`` costs counted apart, never summed as 0. Known
costs are totalled per currency, never mixed or converted. The reads need only the service
credential: ownership gates nothing. Thin handlers over ``Engine.get_image_usage``,
``get_story_image_usage`` and ``get_entry_image_usage``.
"""

from __future__ import annotations

import datetime as dt
import re
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from epistrel.api.deps import CharacterId, ContextDep, EngineDep, ScenarioId, StoryId
from epistrel.core.images import costs as image_costs

admin_router = APIRouter(prefix="/image-usage", tags=["admin"])
story_router = APIRouter(prefix="/stories", tags=["images"])
characters = APIRouter(prefix="/catalog/characters", tags=["images"])
scenarios = APIRouter(prefix="/catalog/scenarios", tags=["images"])

MONEY = (
    "Money is a decimal string at the stored precision (8 decimals), in the provider's currency."
)
COUNTING = (
    "Every provider attempt counts: billed outputs whatever became of them (kept, discarded by "
    "a check, stale, unattached candidates, late or duplicate returns), attempts the provider "
    "refused before generating at zero (`cost_zero_attempts`), and attempts whose cost the "
    "provider gave no figure for in `cost_unknown_attempts`, never added as 0. `costs` lists "
    "the known costs per currency, never mixed or converted. `ambiguous_sends` counts the "
    "attempts sent when a worker was lost (presumed, or corrected once by the actual "
    "result). " + MONEY + " The cost records hold no story content: they survive erasure, "
    "principal offboarding and the story's deletion, and go with the accounting retention "
    "(`image_accounting_retention_days`)."
)
RANGE = (
    "`from` and `to` are local dates in `deployment_time_zone`, both inclusive; by default the "
    f"last {image_costs.RANGE_DEFAULT_DAYS} days through today, at most "
    f"{image_costs.RANGE_MAX_DAYS} days; "
    "422 `invalid_range` when `from` is after `to` or the range is longer."
)


def _iso_date(value: object) -> object:
    """A query ``date`` is ``YYYY-MM-DD`` only: pydantic would also take a number of seconds
    since the epoch, which the schema does not promise (FR-API-4)."""
    if isinstance(value, date):
        return value
    if isinstance(value, str) and ISO_DATE.fullmatch(value):
        return value
    raise ValueError("must be a date, e.g. 2026-11-08")


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
IsoDate = Annotated[date, BeforeValidator(_iso_date)]
FromQuery = Annotated[
    IsoDate | None, Query(alias="from", description="The first local day (inclusive).")
]
ToQuery = Annotated[IsoDate | None, Query(description="The last local day (inclusive).")]


class CurrencyCost(BaseModel):
    """Known costs in one currency."""

    currency: str = Field(description="ISO 4217, upper case.")
    known_total: str = Field(description="The sum of the known costs (a decimal string).")
    known_attempts: int = Field(description="The attempts with a known cost in this currency.")

    @classmethod
    def of(cls, total: image_costs.CurrencyTotal) -> CurrencyCost:
        return cls(
            currency=total.currency,
            known_total=image_costs.money(total.known_total),
            known_attempts=total.known_attempts,
        )


class ImageUsageTotals(BaseModel):
    """Usage and cost totals (FR-IMG-11)."""

    outputs_used: int = Field(description="Outputs counted as used.")
    overage: int = Field(description="Outputs counted past their reservation.")
    extra_outputs: int = Field(
        description="Outputs beyond what was requested, discarded unchecked."
    )
    reserved: int = Field(description="Outputs reserved by attempts still held now.")
    ambiguous_sends: int = Field(description="Attempts whose send was ambiguous.")
    cost_unknown_attempts: int = Field(description="Attempts whose cost is unknown (not 0).")
    cost_zero_attempts: int = Field(description="Attempts the provider did not bill.")
    costs: list[CurrencyCost] = Field(description="Known costs per currency.")

    @staticmethod
    def fields_of(totals: image_costs.UsageTotals) -> dict[str, object]:
        return {
            "outputs_used": totals.outputs_used,
            "overage": totals.overage,
            "extra_outputs": totals.extra_outputs,
            "reserved": totals.reserved,
            "ambiguous_sends": totals.ambiguous_sends,
            "cost_unknown_attempts": totals.cost.unknown_attempts,
            "cost_zero_attempts": totals.cost.zero_attempts,
            "costs": [CurrencyCost.of(c) for c in totals.cost.costs],
        }

    @classmethod
    def of(cls, totals: image_costs.UsageTotals) -> ImageUsageTotals:
        return cls.model_validate(cls.fields_of(totals))


class ImageUsageDay(ImageUsageTotals):
    """One local day's totals."""

    date: dt.date

    @classmethod
    def of_day(cls, day: image_costs.UsageDay) -> ImageUsageDay:
        return cls.model_validate({"date": day.day, **cls.fields_of(day.totals)})


class _Ranged(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_date: date = Field(alias="from", serialization_alias="from")
    to: date
    retained_from: date = Field(
        description="The first day the accounting retention keeps; earlier days are left out."
    )


class DeploymentImageUsage(_Ranged):
    """``GET /admin/image-usage``: every retained day of the range, ascending, and the totals."""

    days: list[ImageUsageDay]
    totals: ImageUsageTotals

    @classmethod
    def of(cls, usage: image_costs.DeploymentUsage) -> DeploymentImageUsage:
        return cls(
            from_date=usage.from_date,
            to=usage.to_date,
            retained_from=usage.retained_from,
            days=[ImageUsageDay.of_day(d) for d in usage.days],
            totals=ImageUsageTotals.of(usage.totals),
        )


class StoryImageUsage(_Ranged):
    """``GET /stories/{story_id}/image-usage``: all-time totals and the range's active days.
    There are no story limits."""

    story_id: str
    all_time: ImageUsageTotals
    days: list[ImageUsageDay] = Field(description="Only the days with activity, ascending.")

    @classmethod
    def of(cls, story_id: str, usage: image_costs.ScopedUsage) -> StoryImageUsage:
        return cls(
            story_id=story_id,
            from_date=usage.from_date,
            to=usage.to_date,
            retained_from=usage.retained_from,
            all_time=ImageUsageTotals.of(usage.all_time),
            days=[ImageUsageDay.of_day(d) for d in usage.days],
        )


class EntryLimitsBody(BaseModel):
    """Today's entry limit (``image_limit_entry_day``, 0 = disabled) and its counter."""

    image_limit_entry_day: int
    today_used: int
    today_reserved: int
    today_remaining: int


class EntryImageUsage(_Ranged):
    """A catalog entry's usage: all-time totals, the range's active days and today's limit."""

    entry_kind: Literal["character", "scenario"]
    entry_id: str
    all_time: ImageUsageTotals
    days: list[ImageUsageDay] = Field(description="Only the days with activity, ascending.")
    limits: EntryLimitsBody

    @classmethod
    def of(
        cls, kind: Literal["character", "scenario"], entry_id: str, usage: image_costs.ScopedUsage
    ) -> EntryImageUsage:
        assert usage.limits is not None  # noqa: S101 - an entry report has them
        lim = usage.limits
        return cls(
            entry_kind=kind,
            entry_id=entry_id,
            from_date=usage.from_date,
            to=usage.to_date,
            retained_from=usage.retained_from,
            all_time=ImageUsageTotals.of(usage.all_time),
            days=[ImageUsageDay.of_day(d) for d in usage.days],
            limits=EntryLimitsBody(
                image_limit_entry_day=lim.image_limit_entry_day,
                today_used=lim.today_used,
                today_reserved=lim.today_reserved,
                today_remaining=lim.today_remaining,
            ),
        )


@admin_router.get(
    "",
    response_model=DeploymentImageUsage,
    operation_id="get_image_usage",
    summary="Read the deployment's image usage and cost per day",
    description=(
        "The deployment's image usage and cost per local day, every retained day of the range "
        "listed ascending (zeros included), and the range's `totals`. Usage, overage and "
        "reserved come from the deployment's daily counters; a deleted story's costs stay in. "
        + COUNTING
        + " "
        + RANGE
    ),
)
async def get_image_usage(
    ctx: ContextDep, engine: EngineDep, from_date: FromQuery = None, to: ToQuery = None
) -> DeploymentImageUsage:
    return DeploymentImageUsage.of(
        await engine.get_image_usage(ctx, from_date=from_date, to_date=to)
    )


@story_router.get(
    "/{story_id}/image-usage",
    response_model=StoryImageUsage,
    operation_id="get_story_image_usage",
    summary="Read a story's image usage and cost",
    description=(
        "A story's image usage and cost from its attempts' records by story (`all_time`), and "
        "the range's days with activity. There are no story limits: per-story admission is the "
        "consumer's. 404 once the story is deleted (its costs stay in "
        "`GET /admin/image-usage`). " + COUNTING + " " + RANGE
    ),
)
async def get_story_image_usage(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    from_date: FromQuery = None,
    to: ToQuery = None,
) -> StoryImageUsage:
    return StoryImageUsage.of(
        story_id,
        await engine.get_story_image_usage(ctx, story_id, from_date=from_date, to_date=to),
    )


ENTRY = (
    "A catalog {noun}'s image usage (its entry counters) and cost (its candidate jobs' "
    "attempts), all-time and for the range's days with activity, with today's entry limit "
    "(`image_limit_entry_day`; 0 disables it). 404 for an unknown or deleted {noun}. "
    + COUNTING
    + " "
    + RANGE
)


@characters.get(
    "/{character_id}/image-usage",
    response_model=EntryImageUsage,
    operation_id="get_entry_image_usage_character",
    summary="Read a catalog character's image usage and cost",
    description=ENTRY.format(noun="character"),
)
async def get_entry_image_usage_character(
    character_id: CharacterId,
    ctx: ContextDep,
    engine: EngineDep,
    from_date: FromQuery = None,
    to: ToQuery = None,
) -> EntryImageUsage:
    found = await engine.get_entry_image_usage(
        ctx, character_id=character_id, from_date=from_date, to_date=to
    )
    return EntryImageUsage.of("character", character_id, found)


@scenarios.get(
    "/{scenario_id}/image-usage",
    response_model=EntryImageUsage,
    operation_id="get_entry_image_usage_scenario",
    summary="Read a scenario's image usage and cost",
    description=ENTRY.format(noun="scenario"),
)
async def get_entry_image_usage_scenario(
    scenario_id: ScenarioId,
    ctx: ContextDep,
    engine: EngineDep,
    from_date: FromQuery = None,
    to: ToQuery = None,
) -> EntryImageUsage:
    found = await engine.get_entry_image_usage(
        ctx, scenario_id=scenario_id, from_date=from_date, to_date=to
    )
    return EntryImageUsage.of("scenario", scenario_id, found)


__all__ = [
    "CurrencyCost",
    "DeploymentImageUsage",
    "EntryImageUsage",
    "ImageUsageDay",
    "ImageUsageTotals",
    "StoryImageUsage",
    "admin_router",
    "characters",
    "scenarios",
    "story_router",
]
