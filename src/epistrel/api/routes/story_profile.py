"""A story's engine configuration (FR-PROF-5,7,16; FR-API-6; #109; #113 adds the assign and
override routes beside them).

- ``POST /stories/{id}/impact-preview``: the impact report of assigning a profile
  (``profile_id``) and/or applying overrides (``overrides``) to the story, without changing
  anything (``mutating=False``: no ``Idempotency-Key``, no expected revision).
- ``GET /stories/{id}/effective-config``: the profile and version the story runs on, every
  delivered setting at its effective value, and the configuration's fingerprint, capabilities
  and quality status.

Every handler makes one ``Engine`` call.
"""

from __future__ import annotations

from fastapi import APIRouter

from epistrel.api.deps import ContextDep, EngineDep, StoryId, route_flags
from epistrel.api.routes.engine_profiles import PROFILES_TAG
from epistrel.core.profiles.effective import EffectiveConfig
from epistrel.core.profiles.impact import ImpactPreviewRequest, ImpactReport

router = APIRouter(tags=[PROFILES_TAG])

PREVIEW_DESCRIPTION = (
    "What assigning `profile_id` (its latest version) and/or applying `overrides` (settings, as "
    "a profile names them, that replace the profile's) would do to the story (FR-PROF-7), "
    "without changing anything: the capabilities it would gain and lose, whether a backfill "
    "would be needed (with its estimated model calls, tokens and duration), whether the upgrade "
    "is unavailable (a data-producing increase for a story with history: M3 has no backfill), "
    "the guarantees it would lose, the fingerprint before and after and the quality status "
    "after, and `warnings`: the reasons the change itself would need `ignore_warnings`. A "
    "request naming neither is 422. A profile that cannot be assigned is 422 `profile_deleted`; "
    "a result that cannot run is 422 `validation_error` at its field or `invalid_profile` "
    "(FR-PROF-4). Nothing is written: the call takes no `Idempotency-Key`."
)
EFFECTIVE_DESCRIPTION = (
    "The story's effective configuration (FR-PROF-5,16): the engine profile and the version it "
    "runs on (the profile's latest, or with `pinned` an earlier one it kept because a "
    "data-producing edit could not apply to its history), every delivered setting at its "
    "effective value, the configuration's `fingerprint` (`sha256:` over the profile version, "
    "each purpose's model and every quality-relevant setting, prompt templates included), the "
    "`capabilities` it enables and its `quality_status` (`uncertified` until a certification "
    "matches the fingerprint)."
)


@router.post(
    "/stories/{story_id}/impact-preview",
    response_model=ImpactReport,
    operation_id="impact_preview",
    summary="Preview the impact of a profile change",
    description=PREVIEW_DESCRIPTION,
    **route_flags(mutating=False),
)
async def impact_preview(
    story_id: StoryId, body: ImpactPreviewRequest, ctx: ContextDep, engine: EngineDep
) -> ImpactReport:
    return await engine.impact_preview(ctx, story_id, body)


@router.get(
    "/stories/{story_id}/effective-config",
    response_model=EffectiveConfig,
    operation_id="get_effective_config",
    summary="Read a story's effective configuration",
    description=EFFECTIVE_DESCRIPTION,
)
async def get_effective_config(
    story_id: StoryId, ctx: ContextDep, engine: EngineDep
) -> EffectiveConfig:
    return await engine.get_effective_config(ctx, story_id)
