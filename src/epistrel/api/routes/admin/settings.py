"""``/admin/settings``: the deployment-state settings (FR-RATE-1's default rating,
``minor_age_threshold``); a ``PUT`` is a partial update recorded in ``config_changes``."""

from __future__ import annotations

from fastapi import APIRouter, Response

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.deployment import DeploymentSettings, SettingsPatch

router = APIRouter(prefix="/settings")


class SettingsBody(DeploymentMutationResponse):
    default_content_rating_id: str
    minor_age_threshold: int

    @classmethod
    def of(cls, settings: DeploymentSettings) -> SettingsBody:
        return cls(
            config_revision=settings.config_revision,
            default_content_rating_id=settings.default_content_rating_id,
            minor_age_threshold=settings.minor_age_threshold,
        )


@router.get("", response_model=SettingsBody)
async def read_settings(ctx: ContextDep, engine: EngineDep) -> SettingsBody:
    return SettingsBody.of(await engine.deployment_settings(ctx))


@router.put("", response_model=SettingsBody)
async def update_settings(
    body: SettingsPatch, response: Response, ctx: ContextDep, engine: EngineDep
) -> SettingsBody:
    updated = await engine.update_deployment_settings(ctx, body)
    return answered(SettingsBody.of(updated), ctx, response, replayed=updated.replayed)
