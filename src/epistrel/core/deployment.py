"""Deployment-wide settings: ``GET/PUT /admin/settings`` (#48; #64 adds fields), and the Lite
defaults (#57) and story-level text caps (#60) under ``deployment_state.settings`` (library
writes until #64's settings surface)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.config_changes import (
    ChangeClass,
    DeploymentSettings,
    bump_config_revision,
    lite_defaults,
    lock_deployment,
    read_lite_defaults,
    read_settings,
    read_story_text_caps,
    record_config_change,
    story_text_caps,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.ratings import require_assignable
from epistrel.core.settings_schema import GenerationSettings, LiteDefaults, StoryTextCaps
from epistrel.core.tables import deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

__all__ = [
    "DeploymentSettings",
    "SettingsPatch",
    "read_lite_defaults",
    "read_settings",
    "read_story_text_caps",
    "update_lite_defaults",
    "update_settings",
    "update_story_text_caps",
]

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


async def update_lite_defaults(
    db: AsyncEngine,
    *,
    principal: str,
    generation: GenerationSettings | None = None,
    window_messages: int | None = None,
) -> LiteDefaults:
    """Replace the deployment's Lite generation defaults and/or window size (FR-LITE-4).

    Recorded like every deployment write: one revision, one prospective ``config_changes`` row
    per changed key. A value equal to the current one writes nothing."""
    async with db.begin() as conn:
        current_settings = await lock_deployment(conn)
        document = dict(
            await conn.scalar(
                select(deployment_state.c.settings).where(
                    deployment_state.c.id == DEPLOYMENT_ROW_ID
                )
            )
            or {}
        )
        current = lite_defaults(document)
        wanted = LiteDefaults(
            lite_generation_defaults=generation or current.lite_generation_defaults,
            lite_window_messages=window_messages or current.lite_window_messages,
        )
        old = current.model_dump(mode="json")
        new = wanted.model_dump(mode="json")
        changed = sorted(key for key in new if old[key] != new[key])
        if not changed:
            return current
        revision = await bump_config_revision(conn, current_settings)
        await conn.execute(
            update(deployment_state)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .values(settings={**document, **new})
        )
        for key in changed:
            await record_config_change(
                conn,
                config_revision=revision,
                setting=key,
                old=old[key],
                new=new[key],
                actor=principal,
                change_class=ChangeClass.PROSPECTIVE,
            )
        return wanted


async def update_story_text_caps(
    db: AsyncEngine,
    *,
    principal: str,
    max_pinned_memories: int | None = None,
    max_custom_instructions_chars: int | None = None,
) -> StoryTextCaps:
    """Replace the deployment's caps on pinned memories and custom instructions (#60).

    Bounded 1 to 500 and 1 to 20,000 (0 is refused). Recorded like every deployment write: one
    revision, one prospective ``config_changes`` row per changed key; a value equal to the
    current one writes nothing."""
    async with db.begin() as conn:
        current_settings = await lock_deployment(conn)
        document = dict(
            await conn.scalar(
                select(deployment_state.c.settings).where(
                    deployment_state.c.id == DEPLOYMENT_ROW_ID
                )
            )
            or {}
        )
        current = story_text_caps(document)
        wanted = StoryTextCaps(
            max_pinned_memories=current.max_pinned_memories
            if max_pinned_memories is None
            else max_pinned_memories,
            max_custom_instructions_chars=current.max_custom_instructions_chars
            if max_custom_instructions_chars is None
            else max_custom_instructions_chars,
        )
        old = current.model_dump(mode="json")
        new = wanted.model_dump(mode="json")
        changed = sorted(key for key in new if old[key] != new[key])
        if not changed:
            return current
        revision = await bump_config_revision(conn, current_settings)
        await conn.execute(
            update(deployment_state)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .values(settings={**document, **new})
        )
        for key in changed:
            await record_config_change(
                conn,
                config_revision=revision,
                setting=key,
                old=old[key],
                new=new[key],
                actor=principal,
                change_class=ChangeClass.PROSPECTIVE,
            )
        return wanted
