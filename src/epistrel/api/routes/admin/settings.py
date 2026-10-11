"""``/admin/settings``: every deployment setting (#48, #64: ``DeploymentSettings``), and
``/admin/config-changes``: the deployment's configuration log (FR-CONC-14)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import ConfigDict

from epistrel.api.config_changes import ConfigChangesBody
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


@router.get("/settings", response_model=SettingsBody)
async def read_settings(ctx: ContextDep, engine: EngineDep) -> SettingsBody:
    return SettingsBody.of(await engine.deployment_settings(ctx))


@router.put("/settings", response_model=SettingsBody)
async def update_settings(
    body: SettingsPatch, response: Response, ctx: ContextDep, engine: EngineDep
) -> SettingsBody:
    updated = await engine.update_deployment_settings(ctx, body)
    return answered(SettingsBody.of(updated), ctx, response, replayed=updated.replayed)


@router.get("/config-changes", response_model=ConfigChangesBody)
async def config_changes(
    ctx: ContextDep,
    engine: EngineDep,
    setting: Annotated[list[str] | None, Query()] = None,
    change_class: Annotated[ConfigClass | None, Query(alias="class")] = None,
    since: datetime | None = None,
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
