"""Deployment-wide settings: ``GET/PUT /admin/settings`` (#48; #64 covers every setting).

The settings are ``DeploymentSettings`` (``core/settings_schema.py``): the two
``deployment_state`` columns (``default_content_rating_id``, ``minor_age_threshold``) and the
sections of its ``settings`` document — the Lite defaults (#57), the story-level text caps
(#60), the idempotency result window (#61), the default ``turn_queue_max`` (#63), the
structured-result settings (#53), the safe fallback (#68) and the default
``max_model_calls_per_turn`` (#69).

A write is a JSON merge over them (``PUT /admin/settings``; the library helpers below take a
section's values): the merged document is validated as a whole, and when anything changed the
write takes one configuration revision and records one ``config_changes`` row per changed leaf
setting, each with its own class (FR-CONC-15), through ``record_config_change``. A write that
changes nothing writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.config_changes import (
    DeploymentState,
    bump_config_revision,
    lock_deployment,
    raw_document,
    read_lite_defaults,
    read_settings,
    read_story_text_caps,
    record_config_change,
    section,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.idempotency import read_idempotency_settings
from epistrel.core.ratings import require_assignable
from epistrel.core.settings_schema import (
    DEPLOYMENT_COLUMNS,
    MINOR_AGE_THRESHOLD_MAX,
    MINOR_AGE_THRESHOLD_MIN,
    DeploymentSettings,
    GenerationSettings,
    IdempotencySettings,
    LiteDefaults,
    StoryTextCaps,
    flatten,
    merge_patch,
    setting_class,
)
from epistrel.core.tables import deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

__all__ = [
    "MINOR_AGE_THRESHOLD_MAX",
    "MINOR_AGE_THRESHOLD_MIN",
    "DeploymentState",
    "SettingsPatch",
    "apply_settings",
    "read_idempotency_settings",
    "read_lite_defaults",
    "read_settings",
    "read_story_text_caps",
    "update_idempotency_settings",
    "update_lite_defaults",
    "update_settings",
    "update_story_text_caps",
]


NULLABLE = frozenset({"max_model_calls_per_turn"})
"""Top-level settings whose ``null`` resets them to their default instead of being refused."""


class SettingsPatch(BaseModel):
    """A partial update (JSON merge) of ``DeploymentSettings``: only the fields present change,
    ``lite_generation_defaults`` merges key by key, and ``null`` is refused for a top-level
    field (except ``max_model_calls_per_turn``, whose ``null`` restores the formula). Bounds
    are ``DeploymentSettings``'s, checked on the merged settings (422 ``validation_error`` at
    ``body.<field>``)."""

    model_config = ConfigDict(extra="forbid")

    default_content_rating_id: str | None = None
    minor_age_threshold: int | None = None
    lite_generation_defaults: GenerationSettings | None = None
    lite_window_messages: int | None = None
    lite_summary_chunk: int | None = None
    lite_summary_max_tokens: int | None = None
    max_pinned_memories: int | None = None
    max_custom_instructions_chars: int | None = None
    idempotency_result_window: int | None = None
    turn_queue_max: int | None = None
    max_model_calls_per_turn: int | None = None
    structured_retry_budget: int | None = None
    uncertain_below: float | None = None
    default_safe_fallback: str | None = None
    audit_retention_days: int | None = None
    job_retention_days: int | None = None
    worker_heartbeat_timeout: float | None = None
    timer_lag_alert: float | None = None

    def changes(self) -> dict[str, Any]:
        present: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if value is None and name in NULLABLE:
                present[name] = None  # back to the default (the Lite formula, #69)
                continue
            if value is None:
                raise InvalidRequest(f"{name} cannot be null", {"field": f"body.{name}"})
            present[name] = (
                value.model_dump(mode="json", exclude_unset=True)
                if isinstance(value, BaseModel)
                else value
            )
        return present


def _validated(document: Mapping[str, Any]) -> DeploymentSettings:
    try:
        return DeploymentSettings.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise InvalidRequest(
            str(first["msg"]), {"field": f"body.{location}", "errors": exc.error_count()}
        ) from None


async def apply_settings(
    db: AsyncEngine, changes: Mapping[str, Any], *, principal: str
) -> DeploymentState:
    """Merge ``changes`` into the deployment's settings; see the module docstring. A default
    rating that is unknown or retired is 422 ``rating_retired``."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        before = current.as_settings()
        wanted = _validated(merge_patch(before.model_dump(mode="json"), changes))
        old = flatten(DeploymentSettings, before.model_dump(mode="json"))
        new = flatten(DeploymentSettings, wanted.model_dump(mode="json"))
        changed = sorted(key for key in new if new[key] != old.get(key))
        if not changed:
            return current
        if "default_content_rating_id" in changed:
            await require_assignable(
                conn, wanted.default_content_rating_id, field="default_content_rating_id"
            )
        revision = await bump_config_revision(conn, current)
        document = await raw_document(conn)
        await conn.execute(
            update(deployment_state)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .values(
                settings={**document, **wanted.document()},
                **{name: getattr(wanted, name) for name in DEPLOYMENT_COLUMNS},
            )
        )
        for key in changed:
            await record_config_change(
                conn,
                config_revision=revision,
                setting=key,
                old=old.get(key),
                new=new[key],
                actor=principal,
                change_class=setting_class(DeploymentSettings, key),
            )
        return await lock_deployment(conn)


async def update_settings(
    db: AsyncEngine, patch: SettingsPatch, *, principal: str
) -> DeploymentState:
    """``PUT /admin/settings``: apply a partial update (see ``SettingsPatch``)."""
    return await apply_settings(db, patch.changes(), principal=principal)


async def update_lite_defaults(
    db: AsyncEngine,
    *,
    principal: str,
    generation: GenerationSettings | None = None,
    window_messages: int | None = None,
) -> LiteDefaults:
    """Replace the deployment's Lite generation defaults and/or window size (FR-LITE-4, #57).
    A value equal to the current one writes nothing."""
    changes: dict[str, Any] = {}
    if generation is not None:
        changes["lite_generation_defaults"] = generation.model_dump(mode="json")
    if window_messages is not None:
        changes["lite_window_messages"] = LiteDefaults(
            lite_window_messages=window_messages
        ).lite_window_messages
    state = await apply_settings(db, changes, principal=principal)
    return section(LiteDefaults, state.settings)


async def update_story_text_caps(
    db: AsyncEngine,
    *,
    principal: str,
    max_pinned_memories: int | None = None,
    max_custom_instructions_chars: int | None = None,
) -> StoryTextCaps:
    """Replace the deployment's caps on pinned memories and custom instructions (#60).

    Bounded 1 to 500 and 1 to 20,000 (pydantic ``ValidationError`` otherwise)."""
    values = {
        k: v
        for k, v in {
            "max_pinned_memories": max_pinned_memories,
            "max_custom_instructions_chars": max_custom_instructions_chars,
        }.items()
        if v is not None
    }
    StoryTextCaps.model_validate(values)
    state = await apply_settings(db, values, principal=principal)
    return section(StoryTextCaps, state.settings)


async def update_idempotency_settings(
    db: AsyncEngine, *, principal: str, result_window_hours: int
) -> IdempotencySettings:
    """Set the deployment's idempotency result window (hours, 1 to 720; #61)."""
    wanted = IdempotencySettings(idempotency_result_window=result_window_hours)
    state = await apply_settings(db, wanted.model_dump(mode="json"), principal=principal)
    return section(IdempotencySettings, state.settings)
