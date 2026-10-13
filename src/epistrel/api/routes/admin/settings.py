"""``/admin/settings``: every deployment setting (#48, #64: ``DeploymentSettings``), and
``/admin/config-changes``: the deployment's configuration log (FR-CONC-14)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import ConfigDict

from epistrel.api.config_changes import ConfigChangesBody, DeploymentSettingFilter, IsoDateTime
from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.config_changes import ConfigScope
from epistrel.core.deployment import DeploymentState, SettingsPatch
from epistrel.core.settings_schema import ConfigClass, DeploymentSettings

router = APIRouter()


class SettingsBody(DeploymentMutationResponse, DeploymentSettings):
    """Every deployment setting, flat, with the deployment's ``config_revision``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    @classmethod
    def of(cls, state: DeploymentState) -> SettingsBody:
        return cls(
            config_revision=state.config_revision, **state.as_settings().model_dump(mode="json")
        )


@router.get(
    "/settings",
    response_model=SettingsBody,
    operation_id="deployment_settings",
    summary="Read every deployment setting",
)
async def read_settings(ctx: ContextDep, engine: EngineDep) -> SettingsBody:
    return SettingsBody.of(await engine.deployment_settings(ctx))


@router.put(
    "/settings",
    response_model=SettingsBody,
    operation_id="update_deployment_settings",
    summary="Change deployment settings",
    description=(
        "Change any of the deployment settings; omitted keys keep their values. Every change is "
        "recorded as a configuration change and moves `config_revision`; a write that changes "
        "nothing writes nothing. `media_moderation_webhook_url`, `public_base_url` and "
        "`max_characters_at_start_ceiling` accept `null` (unset: no hook, no base URL, no "
        "ceiling); no other key does. A list replaces the stored one. `profile_defaults` holds "
        "the engine profiles' defaults (FR-PROF-5): the bottom layer of every story's "
        "configuration, under its profile's version and its own overrides, seeded from the Lite "
        "preset. A write is laid over them (an object merges into an object; any other value "
        "replaces, `null` included, so `budgets.model_calls: null` is the formula `9 + G`), and "
        "must leave a document every profile in use and every story can run under (422 "
        "`validation_error` at the setting, or `invalid_profile` naming `details.profiles` or "
        "`details.stories`); each changed setting is recorded as `profile_defaults.<path>`. The "
        "engine dials that moved there (`lite_generation_defaults`, `lite_window_messages`, "
        "`lite_summary_max_tokens`, `max_model_calls_per_turn`) are 422 with `details.reason: "
        "superseded` and their replacement in `details.replacement`. `listing_status_values` "
        "lists the catalog's listing status values in order (a new entry takes the first; "
        "distinct, case-sensitive, 1 to 40 printable characters each); removing a value a "
        "catalog entry still holds is 409 `listing_status_in_use` (`details.value`, "
        "`details.count`). `validation_model_calls` (0 to 1000, default 0) is how many model "
        "calls one catalog validation (a publish, or a dry run) may spend on model-assisted "
        "input policies; those it does not reach are listed `unavailable` in the report. "
        "`require_primary_image` (default `false`) makes a primary image required to publish a "
        "catalog character (422 `publish_invalid`, `primary_image_required`); off, a character "
        "publishes with no image. `feedback_tags` lists the reason tags a message rating may "
        "carry (FR-FB-1's nine to start; distinct, 1 to 40 of `a-z 0-9 _` each, up to 50; may be "
        "empty); removing a tag a rating or its history still carries is 409 "
        "`feedback_tag_in_use` (`details.in_use`: each tag with its `current` and `history` "
        "counts). `feedback_text_max_chars` (1 to 20000, default 2000) caps a rating's free "
        "text, and `feedback_retention` keeps ratings for the story's life (`story_lifetime`, "
        "the default) or a number of days (1 to 3650)."
    ),
)
async def update_settings(
    body: SettingsPatch, response: Response, ctx: ContextDep, engine: EngineDep
) -> SettingsBody:
    updated = await engine.update_deployment_settings(ctx, body)
    return answered(SettingsBody.of(updated), ctx, response, replayed=updated.replayed)


@router.get(
    "/config-changes",
    response_model=ConfigChangesBody,
    operation_id="config_changes_deployment",
    summary="List the deployment's configuration changes",
)
async def config_changes(
    ctx: ContextDep,
    engine: EngineDep,
    setting: Annotated[list[DeploymentSettingFilter] | None, Query()] = None,
    change_class: Annotated[ConfigClass | None, Query(alias="class")] = None,
    since: IsoDateTime | None = None,
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConfigChangesBody:
    """The deployment's configuration changes, newest first; ``next`` pages on (``after``)."""
    page = await engine.config_changes(
        ctx,
        scope=ConfigScope.DEPLOYMENT,
        settings=setting or (),
        change_class=change_class,
        since=since,
        after=after,
        limit=limit,
    )
    return ConfigChangesBody.of(page)
