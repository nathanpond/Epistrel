"""Deployment-wide settings: ``GET/PUT /admin/settings`` (#48; #64 covers every setting).

The settings are ``DeploymentSettings`` (``core/settings_schema.py``): the
``deployment_state`` columns (``default_content_rating_id``, ``minor_age_threshold``, the
starting-cast limits ``max_characters_at_start_default``/``_ceiling``, #108, the default
engine profile ``default_engine_profile_id``, #106, and the catalog's
``listing_status_values``, #110: a write replaces the whole list, and removing a value a catalog
entry still holds is 409 ``listing_status_in_use``; the message-feedback reason tags
``feedback_tags``, #112: likewise, and removing a tag a rating or its history still carries is 409
``feedback_tag_in_use``; the engine profiles' defaults ``profile_defaults``, #113) and the
sections of its ``settings`` document — the Lite summary chunking (#57, #72), the story-level
text caps (#60), the idempotency result window (#61), the default ``turn_queue_max`` (#63), the
structured-result settings (#53), the safe fallback (#68), the media settings (#105), the
catalog validation's model-call budget ``validation_model_calls`` (#111) and its
``require_primary_image`` (#115), and message feedback's
text cap and retention (``feedback_text_max_chars``, ``feedback_retention``; #112) and its
export switch and aggregate window (``feedback_export``, ``feedback_regen_window_s``; #126),
and what a catalog erasure does with media others still reference (``erasure_shared_media``, #124),
and how fast memory importance decays (``memory_importance_half_life_turns``, #135),
and the embedding port's batches and HNSW index (``embedding_batch_max``,
``reembed_batch_size``, ``embedding_hnsw_m``, ``embedding_hnsw_ef_construction``; #138; and
``embedding_hnsw_ef_search``, #141), and the retrieval's arms (``retrieval_vector_k``,
``retrieval_lexical_k``, ``retrieval_rrf_k``, ``retrieval_rehydrate_max_rows``; #141).
``media_sweep_interval`` also moves its recurring task's ``recurring_tasks`` row
(``RECURRING_INTERVAL_SETTINGS``).

A write is a JSON merge over them (``PUT /admin/settings``; the library helpers below take a
section's values): the merged document is validated as a whole, and when anything changed the
write takes one configuration revision and records one ``config_changes`` row per changed leaf
setting, each with its own class (FR-CONC-15), through ``record_config_change``. A write that
changes nothing writes nothing.

**Profile defaults** (FR-PROF-5; #113). ``profile_defaults`` is the bottom layer of every story's
effective configuration (``core.profiles.effective``). A write lays its value over the stored
document (an object merges into an object; any other value, ``null`` included, replaces), and
the result must remain a complete document (every delivered setting named) that #106's validator
accepts (422 ``validation_error`` or ``invalid_profile``), under which every profile version in
use and every story's overrides can still run (422 ``invalid_profile`` naming them). Each changed
setting is one row, ``profile_defaults.<dotted path>``, with that setting's class. M2's engine
dials (``lite_generation_defaults``, ``lite_window_messages``, ``lite_summary_max_tokens``,
``max_model_calls_per_turn``) are refused (422): they live here now
(``SUPERSEDED_DEPLOYMENT_SETTINGS``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, feedback
from epistrel.core.catalog import listing
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
from epistrel.core.profiles import effective, schema
from epistrel.core.profiles import store as profiles
from epistrel.core.profiles.schema import ProfileSettings, Purposes, SettingsDocument
from epistrel.core.ratings import require_assignable
from epistrel.core.settings_schema import (
    DEPLOYMENT_COLUMNS,
    MINOR_AGE_THRESHOLD_MAX,
    MINOR_AGE_THRESHOLD_MIN,
    PROFILE_DEFAULTS,
    RECURRING_INTERVAL_SETTINGS,
    SUPERSEDED_DEPLOYMENT_SETTINGS,
    DeploymentSettings,
    ErasureSharedMedia,
    FeedbackRetention,
    IdempotencySettings,
    StoryTextCaps,
    flatten,
    merge_patch,
    setting_class,
)
from epistrel.core.tables import deployment_state, recurring_tasks
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
    "update_profile_defaults",
    "update_settings",
    "update_story_text_caps",
]


NULLABLE = frozenset(
    {
        "media_moderation_webhook_url",
        "public_base_url",
        "max_characters_at_start_ceiling",
    }
)
"""Top-level settings whose ``null`` resets them to their default instead of being refused: no
moderation hook or base URL (#105), and no starting-cast ceiling (#108)."""

FEEDBACK_TAGS = "feedback_tags"


class SettingsPatch(BaseModel):
    """A partial update (JSON merge) of ``DeploymentSettings``: only the fields present change,
    and ``null`` is refused for a top-level field (except ``media_moderation_webhook_url``,
    ``public_base_url`` and ``max_characters_at_start_ceiling``, whose ``null`` unsets them). A
    list (``media_allowed_types``, ``listing_status_values``, ``feedback_tags``) replaces the
    stored one. ``profile_defaults`` is laid over the stored defaults (an object merges into an
    object; any other value, ``null`` included, replaces). Bounds are ``DeploymentSettings``'s,
    checked on the merged settings (422 ``validation_error`` at ``body.<field>``); M2's engine
    dials are refused (``SUPERSEDED_DEPLOYMENT_SETTINGS``)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "turn_queue_max": 10,
                    "profile_defaults": {"memory": {"rolling_summary": {"window_messages": 40}}},
                }
            ]
        },
    )

    default_content_rating_id: str | None = None
    minor_age_threshold: StrictInt | None = None
    max_characters_at_start_default: StrictInt | None = None
    max_characters_at_start_ceiling: StrictInt | None = None
    default_engine_profile_id: str | None = None
    lite_summary_chunk: StrictInt | None = None
    max_pinned_memories: StrictInt | None = None
    max_custom_instructions_chars: StrictInt | None = None
    idempotency_result_window: StrictInt | None = None
    turn_queue_max: StrictInt | None = None
    structured_retry_budget: StrictInt | None = None
    uncertain_below: StrictFloat | None = None
    default_safe_fallback: str | None = None
    rating_check_model_calls_per_record: StrictInt | None = None
    audit_retention_days: StrictInt | None = None
    job_retention_days: StrictInt | None = None
    offboarding_report_retention_days: StrictInt | None = None
    rating_check_retention_days: StrictInt | None = None
    readset_change_retention_days: StrictInt | None = None
    worker_heartbeat_timeout: StrictFloat | None = None
    timer_lag_alert: StrictFloat | None = None
    media_allowed_types: list[str] | None = None
    media_max_bytes: StrictInt | None = None
    media_moderation_webhook_url: str | None = None
    media_moderation_timeout_ms: StrictInt | None = None
    media_sweep_interval: StrictInt | None = None
    public_base_url: str | None = None
    media_signed_url_ttl: StrictInt | None = None
    media_multipart_threshold: StrictInt | None = None
    media_upload_ttl: StrictInt | None = None
    listing_status_values: list[str] | None = None
    validation_model_calls: StrictInt | None = None
    require_primary_image: StrictBool | None = None
    feedback_tags: list[str] | None = None
    feedback_text_max_chars: StrictInt | None = None
    feedback_retention: FeedbackRetention | None = None
    feedback_export: StrictBool | None = None
    feedback_regen_window_s: StrictInt | None = None
    erasure_shared_media: ErasureSharedMedia | None = None
    memory_importance_half_life_turns: StrictInt | None = None
    embedding_batch_max: StrictInt | None = None
    reembed_batch_size: StrictInt | None = None
    embedding_hnsw_m: StrictInt | None = None
    embedding_hnsw_ef_construction: StrictInt | None = None
    embedding_hnsw_ef_search: StrictInt | None = None
    retrieval_vector_k: StrictInt | None = None
    retrieval_lexical_k: StrictInt | None = None
    retrieval_rrf_k: StrictInt | None = None
    retrieval_rehydrate_max_rows: StrictInt | None = None
    profile_defaults: SettingsDocument | None = None

    @model_validator(mode="before")
    @classmethod
    def _superseded(cls, data: Any) -> Any:
        """M2's engine dials are refused, naming the ``profile_defaults`` setting that replaced
        each (#113)."""
        if isinstance(data, Mapping):
            for name, path in SUPERSEDED_DEPLOYMENT_SETTINGS.items():
                if name in data:
                    raise InvalidRequest(
                        f"{name} moved to the engine profiles' defaults: set "
                        f"profile_defaults.{path}",
                        {
                            "field": f"body.{name}",
                            "reason": "superseded",
                            "replacement": f"{PROFILE_DEFAULTS}.{path}",
                        },
                    )
        return data

    def changes(self) -> dict[str, Any]:
        present: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if value is None and name in NULLABLE:
                present[name] = None  # back to the default (unset #105, #108)
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
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None


def _profile_defaults(
    stored: Mapping[str, Any], patch: Any, *, purposes: Purposes | None
) -> dict[str, Any]:
    """The defaults ``patch`` leaves (see the module docstring), normalised, when they are
    complete and valid on their own."""
    field = ("body", PROFILE_DEFAULTS)
    if not isinstance(patch, Mapping):
        raise InvalidRequest(f"{PROFILE_DEFAULTS} must be an object", {"field": ".".join(field)})
    merged = effective.check_document(
        effective.layered(stored, patch), purposes=purposes, field=field
    )
    missing = [path for path in schema.delivered_tags() if not effective.named(merged, path)]
    if missing:
        raise InvalidRequest(
            f"{PROFILE_DEFAULTS} must name every delivered setting",
            {"field": f"{'.'.join(field)}.{missing[0]}", "missing": missing},
        )
    effective.resolve_layers(merged, {}, purposes=purposes, field=field)
    return merged


def _dumped(defaults: Mapping[str, Any]) -> dict[str, Any]:
    return effective.merged_settings(defaults, {}).model_dump(mode="json")


async def _defaults_changes(
    conn: AsyncConnection,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    purposes: Purposes | None,
    revision: int,
    principal: str,
) -> None:
    """Record each setting the new defaults change (``profile_defaults.<path>``, its class)."""
    old_values = flatten(ProfileSettings, _dumped(before))
    new_values = flatten(ProfileSettings, _dumped(after))
    tags = schema.setting_tags()
    for path in sorted(p for p in new_values if new_values[p] != old_values.get(p)):
        await record_config_change(
            conn,
            config_revision=revision,
            setting=f"{PROFILE_DEFAULTS}.{path}",
            old=old_values.get(path),
            new=new_values[path],
            actor=principal,
            change_class=tags[path].change_class,
        )


async def apply_settings(
    db: AsyncEngine,
    changes: Mapping[str, Any],
    *,
    principal: str,
    purposes: Purposes | None = None,
) -> DeploymentState:
    """Merge ``changes`` into the deployment's settings; see the module docstring. A default
    rating that is unknown or retired is 422 ``rating_retired``; a default engine profile that is
    unknown or deleted is 422 ``profile_deleted``; removing a listing status value a catalog
    entry still holds is 409 ``listing_status_in_use``, and a feedback tag a rating or its
    history still carries 409 ``feedback_tag_in_use``. ``profile_defaults`` that are not
    complete, or under which something in use could not run, are 422 (``purposes``: the model
    purposes a profile may route, which the Engine passes)."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        before = current.as_settings()
        others = {key: value for key, value in changes.items() if key != PROFILE_DEFAULTS}
        merged = merge_patch(before.model_dump(mode="json"), others)
        if PROFILE_DEFAULTS in changes:
            # Laid over, never merge-patched: a ``null`` is a value here (#113).
            merged[PROFILE_DEFAULTS] = _profile_defaults(
                current.profile_defaults, changes[PROFILE_DEFAULTS], purposes=purposes
            )
        wanted = _validated(merged)
        old = flatten(DeploymentSettings, before.model_dump(mode="json"))
        new = flatten(DeploymentSettings, wanted.model_dump(mode="json"))
        changed = sorted(key for key in new if new[key] != old.get(key))
        if not changed:
            return current
        if "default_content_rating_id" in changed:
            await require_assignable(
                conn, wanted.default_content_rating_id, field="default_content_rating_id"
            )
        if "default_engine_profile_id" in changed:
            await profiles.require_assignable(conn, wanted.default_engine_profile_id)
        if listing.SETTING in changed:
            await listing.refuse_removal(
                conn, before.listing_status_values, wanted.listing_status_values
            )
        if FEEDBACK_TAGS in changed:
            await feedback.refuse_tag_removal(conn, before.feedback_tags, wanted.feedback_tags)
        if PROFILE_DEFAULTS in changed:
            await effective.runnable_under(conn, wanted.profile_defaults, purposes=purposes)
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
            if key == PROFILE_DEFAULTS:
                await _defaults_changes(
                    conn,
                    before.profile_defaults,
                    wanted.profile_defaults,
                    purposes=purposes,
                    revision=revision,
                    principal=principal,
                )
                continue
            await record_config_change(
                conn,
                config_revision=revision,
                setting=key,
                old=old.get(key),
                new=new[key],
                actor=principal,
                change_class=setting_class(DeploymentSettings, key),
            )
        await _move_recurring_intervals(conn, wanted, changed)
        return await lock_deployment(conn)


async def _move_recurring_intervals(
    conn: AsyncConnection, wanted: DeploymentSettings, changed: list[str]
) -> None:
    """Give each recurring task whose interval setting changed its new interval (#105:
    ``media_sweep_interval``): prospective, so the next run comes no later than one new interval
    after the last one. A task no worker has reconciled yet has no row; reconcile reads the
    setting when it creates it."""
    for setting, task in RECURRING_INTERVAL_SETTINGS.items():
        if setting not in changed:
            continue
        interval = timedelta(seconds=int(getattr(wanted, setting)))
        await conn.execute(
            update(recurring_tasks)
            .where(recurring_tasks.c.name == task)
            .values(
                interval=interval,
                next_run_at=func.least(
                    recurring_tasks.c.next_run_at,
                    func.coalesce(recurring_tasks.c.last_run_at, clock.now()) + interval,
                ),
            )
        )


async def update_settings(
    db: AsyncEngine, patch: SettingsPatch, *, principal: str, purposes: Purposes | None = None
) -> DeploymentState:
    """``PUT /admin/settings``: apply a partial update (see ``SettingsPatch``)."""
    return await apply_settings(db, patch.changes(), principal=principal, purposes=purposes)


async def update_profile_defaults(
    db: AsyncEngine,
    settings: Mapping[str, Any],
    *,
    principal: str,
    purposes: Purposes | None = None,
) -> dict[str, Any]:
    """Lay ``settings`` over the deployment's ``profile_defaults`` (FR-PROF-5; #113; see the
    module docstring) and return the defaults after. A value equal to the current one writes
    nothing."""
    state = await apply_settings(
        db, {PROFILE_DEFAULTS: dict(settings)}, principal=principal, purposes=purposes
    )
    return dict(state.profile_defaults)


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
