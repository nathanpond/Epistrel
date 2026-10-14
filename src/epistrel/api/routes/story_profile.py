"""A story's engine configuration (FR-PROF-1,5,7,16; FR-API-6; #109, #113).

- ``POST /stories/{id}/impact-preview``: the impact report of assigning a profile
  (``profile_id``) and/or changing the story's overrides (``overrides``, a JSON merge patch as
  ``PATCH .../profile-overrides`` takes) to the story, without changing anything
  (``mutating=False``: no ``Idempotency-Key``, no expected revision).
- ``GET /stories/{id}/effective-config``: the deployment's defaults, the profile and version the
  story runs on, its overrides, every delivered setting at its effective value, and the
  configuration's fingerprint, capabilities and quality status.
- ``PUT /stories/{id}/engine-profile`` (#113): run the story under another profile.
- ``PATCH /stories/{id}/profile-overrides`` (#113): change the story's overrides of its
  profile's dials (a JSON merge patch).

The two writes are two-stage (#109): the answer is ``{applied, impact, result}``, and a change
its impact report warns of applies only with ``ignore_warnings``. Each is a story-scoped
configuration change: it moves the story's ``config_revision`` (answered beside the unchanged
``timeline_revision``). A confirmed change whose upgrade is unavailable (M3's interim rule) is
answered applied with the story's configuration as it was, and writes nothing. Every handler
makes one ``Engine`` call.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from epistrel.api.deps import ContextDep, EngineDep, StoryId, route_flags
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.engine_profiles import PROFILES_TAG
from epistrel.core.profiles.effective import EffectiveConfig
from epistrel.core.profiles.impact import ImpactPreviewRequest, ImpactReport, TwoStageResult
from epistrel.core.profiles.story import ProfileAssignment, ProfileOverridesPatch

router = APIRouter(tags=[PROFILES_TAG])

PREVIEW_DESCRIPTION = (
    "What assigning `profile_id` (its latest version) and/or applying `overrides` would do to "
    "the story (FR-PROF-7), without changing anything. `overrides` is a JSON merge patch over "
    "the story's overrides, exactly as `PATCH /stories/{id}/profile-overrides` takes its "
    "`settings` (`null` removes an override), so the report is the one that write would answer; "
    "an assigned profile keeps the story's overrides, as an assignment does. The report states "
    "the capabilities the change would gain and lose, whether a "
    "backfill would be needed (with its estimated model calls, tokens and duration), whether the "
    "upgrade is unavailable (a data-producing increase for a story with history, a committed "
    "turn still on its timeline: M3 has no backfill), the guarantees it would lose, the "
    "fingerprint before and after with the capabilities enabled and the quality status after "
    "(FR-PROF-16), and `warnings`: the reasons the change itself would need `ignore_warnings`. A "
    "request naming neither is 422. A profile that cannot be assigned is 422 `profile_deleted`; "
    "a patch or a result that cannot run is 422 `validation_error` at its field or "
    "`invalid_profile` (FR-PROF-4), as the write would be. Nothing is written: the call takes no "
    "`Idempotency-Key`."
)
EFFECTIVE_DESCRIPTION = (
    "The story's effective configuration (FR-PROF-5,16): `deployment_defaults` (every delivered "
    "setting at the deployment's default, `profile_defaults` in `/admin/settings`), the engine "
    "profile and the version it runs on (the profile's latest, or with `pinned` an earlier one "
    "it kept because a data-producing edit could not apply to its history), its `overrides` "
    "(the settings it overrides, with their `revision`), `effective`: every delivered setting at "
    "its effective value (the defaults, then the profile's version, then the overrides, "
    "validated as a whole), the configuration's `fingerprint` (`sha256:` over the profile "
    "version, each purpose's model and every quality-relevant setting, prompt templates "
    "included), the `capabilities` it enables and its `quality_status` (`uncertified` until a "
    "certification matches the fingerprint). A turn captures this configuration when it starts "
    "and runs under it to the end."
)
TWO_STAGE = (
    " Two stages (FR-PROF-7): the impact report (`impact`, one story) is always answered; a "
    "change it warns of (a capability gained or lost, an upgrade unavailable) is applied only "
    "with `ignore_warnings` (`ignoreWarnings`), and otherwise nothing is written (`applied: "
    "false`, `result: null`) and the `Idempotency-Key` is not kept, so the confirmation may "
    "reuse it. A configuration that cannot run is refused all the same (422 `invalid_profile`). "
    "Applied, `result` is the story's effective configuration after the write. The write is a "
    "story-scoped configuration change: it moves the story's `config_revision` and is listed in "
    "`GET /stories/{id}/config-changes`; the story's next turn runs under it, and a turn "
    "already running finishes under the configuration it started with. A confirmed change "
    "whose upgrade is unavailable (`upgrade_unavailable`: it adds data-producing capability to "
    "a story with history, and M3 has no backfill) keeps the story's configuration: `applied: "
    "true`, `result` is the configuration as it was, nothing is written and `config_revision` "
    "does not move. An archived story is 409 `story_archived` when the call would change "
    "something; a call that changes nothing (the story's own profile, a patch that changes no "
    "setting) answers 200 and writes nothing."
)
ASSIGN_DESCRIPTION = (
    "Run the story under another engine profile, at the profile's latest version, from its next "
    "turn (FR-PROF-1,7). The story keeps its overrides; when they cannot run over the new "
    "profile the call is 422 `invalid_profile` with `details.overrides`. A profile that is "
    "deleted or unknown is 422 `profile_deleted` (`reason`). Naming the story's current profile "
    "changes nothing (200): the story keeps the version it runs on. Recorded as the setting "
    "`engine_profile_id`." + TWO_STAGE
)
OVERRIDES_DESCRIPTION = (
    "Change the story's overrides of its profile's dials (FR-PROF-5) with a JSON merge patch "
    "(RFC 7396) in `settings`: a key present sets that dial (an object merges into an object), "
    "and `null` removes the override, so the dial follows the profile again. The overrides "
    "persist across the profile's versions. A `null` must name a setting or group of the "
    "published schema (422 `validation_error` at its field; `invalid_profile` for one this "
    "deployment has not delivered). The overrides that result must be a settings document "
    "naming only delivered settings (422 `validation_error` at `body.settings.<path>`, or "
    "`invalid_profile`), and the whole configuration must run (422 `invalid_profile`); nothing "
    "is stored otherwise. They are stored without empty objects. `expected_revision` "
    "(optional) is the overrides' revision the patch was made against: another is 409 "
    "`revision_conflict` (`details.reason: overrides_revision`). A patch that changes no setting "
    "(one that removes an override the story does not have, or names an empty group) writes "
    "nothing: neither revision moves. `POST /stories/{id}/impact-preview` with the same patch "
    "in `overrides` answers this call's report without writing. Recorded as one setting per "
    "dial, `profile_overrides.<path>`." + TWO_STAGE
)


class StoryProfileWriteBody(MutationResponse):
    """An assignment's or an overrides write's two stages (FR-PROF-7): ``applied``; the
    ``impact`` report on the story; and ``result``, the story's effective configuration once the
    change applied (``null`` when the report held it back and nothing was written). The
    story's ``config_revision`` (moved by a write that changed something) and its unchanged
    ``timeline_revision``."""

    applied: bool
    impact: ImpactReport
    result: EffectiveConfig | None
    config_revision: int

    @classmethod
    def of(cls, write: TwoStageResult[EffectiveConfig]) -> StoryProfileWriteBody:
        assert write.config_revision is not None  # noqa: S101 - a story configuration result
        assert write.timeline_revision is not None  # noqa: S101
        return cls(
            applied=write.applied,
            impact=write.impact,
            result=write.result,
            config_revision=write.config_revision,
            timeline_revision=write.timeline_revision,
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


@router.put(
    "/stories/{story_id}/engine-profile",
    response_model=StoryProfileWriteBody,
    operation_id="assign_engine_profile",
    summary="Assign a story's engine profile",
    description=ASSIGN_DESCRIPTION,
)
async def assign_engine_profile(
    story_id: StoryId,
    body: ProfileAssignment,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StoryProfileWriteBody:
    write = await engine.assign_engine_profile(ctx, story_id, body)
    return answered(StoryProfileWriteBody.of(write), ctx, response, replayed=write.replayed)


@router.patch(
    "/stories/{story_id}/profile-overrides",
    response_model=StoryProfileWriteBody,
    operation_id="set_profile_overrides",
    summary="Change a story's overrides of its profile",
    description=OVERRIDES_DESCRIPTION,
)
async def set_profile_overrides(
    story_id: StoryId,
    body: ProfileOverridesPatch,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StoryProfileWriteBody:
    write = await engine.set_profile_overrides(ctx, story_id, body)
    return answered(StoryProfileWriteBody.of(write), ctx, response, replayed=write.replayed)
