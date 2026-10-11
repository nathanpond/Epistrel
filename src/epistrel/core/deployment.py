"""Deployment-wide settings: ``GET/PUT /admin/settings`` (#48; #64 adds fields)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.config_changes import (
    DeploymentSettings,
    bump_config_revision,
    lock_deployment,
    read_settings,
    record_config_change,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

__all__ = ["DeploymentSettings", "SettingsPatch", "read_settings", "update_settings"]

MINOR_AGE_THRESHOLD_MIN = 0
MINOR_AGE_THRESHOLD_MAX = 120


class SettingsPatch(BaseModel):
    """A partial update (JSON merge): only the fields present change; ``null`` is refused."""

    model_config = ConfigDict(extra="forbid")

    default_content_rating_id: str | None = Field(default=None, min_length=1, max_length=64)
    minor_age_threshold: int | None = Field(
        default=None, ge=MINOR_AGE_THRESHOLD_MIN, le=MINOR_AGE_THRESHOLD_MAX
    )

    def changes(self) -> dict[str, Any]:
        present = {name: getattr(self, name) for name in self.model_fields_set}
        for name, value in present.items():
            if value is None:
                raise InvalidRequest(f"{name} cannot be null", {"field": f"body.{name}"})
        return present


async def update_settings(
    db: AsyncEngine, patch: SettingsPatch, *, principal: str
) -> DeploymentSettings:
    """Apply a partial settings update: one ``config_changes`` row per changed field (class
    interpretive) and one revision for the write; an update that changes nothing writes
    nothing. A default rating that is unknown or retired is 422 ``rating_retired``."""
    requested = patch.changes()
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        changed = {
            name: value for name, value in requested.items() if getattr(current, name) != value
        }
        if not changed:
            return current
        if "default_content_rating_id" in changed:
            await require_assignable(
                conn, changed["default_content_rating_id"], field="default_content_rating_id"
            )
        revision = await bump_config_revision(conn, current)
        await conn.execute(
            update(deployment_state)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .values(**changed)
        )
        for name, value in sorted(changed.items()):
            await record_config_change(
                conn,
                config_revision=revision,
                setting=name,
                old=getattr(current, name),
                new=value,
                actor=principal,
            )
        return await lock_deployment(conn)
