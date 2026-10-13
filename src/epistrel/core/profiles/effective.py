"""A story's effective engine configuration (FR-PROF-5,6,16; #109's resolution, which #113
extends).

**Resolution.** A story runs under its engine profile (``stories.engine_profile_id``) at one
version: the profile's latest (FR-PROF-6), unless the story keeps an earlier one
(``stories.engine_profile_version_pin``, set when a confirmed data-producing edit could not
apply to it: FR-PROF-7's ``upgrade_unavailable``, M3's interim rule until M7). That version's
settings are read over ``ProfileSettings``' defaults, as a stored version is validated. A
preview may lay overrides on top: a sparse settings document, validated strictly like a
profile's settings (so ``null`` only where a setting takes it), whose every setting replaces
the profile's. The result is validated as a whole, and an invalid one is refused (422
``invalid_profile``, FR-PROF-4) whatever the caller confirms. #113 adds the deployment's profile
defaults under the profile and the story's stored overrides above it.

**Stamp.** A configuration's fingerprint, capabilities and quality status come from
``fingerprint.stamp`` over the resolved profile, version and settings, with the model ids and
prompt templates the Orchestrator resolves (``RuntimeFacts``). The Lite turn stamps itself at
turn start (``orchestrator.lite.pipeline``) from the same resolution, so a respond response and
an effective-configuration read of the same configuration state the same fingerprint.

**The read** (``GET /stories/{id}/effective-config``) answers the profile and the version the
story runs on (``pinned``, ``deleted``), its delivered settings at their effective values, and
the stamp. Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.errors import ProfileDeleted, StoryNotFound
from epistrel.core.profiles import schema
from epistrel.core.profiles.fingerprint import QualityStatus, RuntimeFacts, Stamp, stamp
from epistrel.core.profiles.schema import ProfileSettings, Purposes, SettingsDocument
from epistrel.core.profiles.validate import parse_settings, require_valid
from epistrel.core.tables import engine_profile_versions, engine_profiles, stories

STORED = ("settings",)
"""Where a stored version's settings are reported, should one ever fail to parse."""


@dataclass(frozen=True, slots=True)
class Configuration:
    """One effective configuration: the profile and version it resolves to, and its settings
    (every setting the documents leave out at its default)."""

    profile_id: str
    version: int
    settings: ProfileSettings

    def stamp(self, facts: RuntimeFacts) -> Stamp:
        return stamp(
            profile_id=self.profile_id, version=self.version, settings=self.settings, facts=facts
        )


@dataclass(frozen=True, slots=True)
class StoryProfile:
    """The profile a story runs under: the version it runs on (its pin, else the latest),
    whether that is a pin, whether the profile is deleted, and that version's settings as
    stored."""

    story_id: str
    profile_id: str
    version: int
    latest_version: int
    pinned: bool
    deleted: bool
    document: Mapping[str, Any]

    def configuration(self) -> Configuration:
        return Configuration(
            self.profile_id, self.version, parse_settings(self.document, field=STORED)
        )


@dataclass(frozen=True, slots=True)
class AssignableProfile:
    """A profile that may be newly assigned: its latest version and that version's settings."""

    profile_id: str
    version: int
    document: Mapping[str, Any]


async def story_profile(conn: AsyncConnection, story_id: str) -> StoryProfile:
    """The profile ``story_id`` runs under, at the version it runs on; 404 for an unknown
    story."""
    s, p, v = stories, engine_profiles, engine_profile_versions
    runs_on = func.coalesce(s.c.engine_profile_version_pin, p.c.current_version)
    row = (
        await conn.execute(
            select(
                s.c.engine_profile_id,
                s.c.engine_profile_version_pin,
                p.c.current_version,
                p.c.deleted_at,
                v.c.version,
                v.c.settings,
            )
            .select_from(
                s.join(p, p.c.id == s.c.engine_profile_id).join(
                    v, and_(v.c.profile_id == p.c.id, v.c.version == runs_on)
                )
            )
            .where(s.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return StoryProfile(
        story_id=story_id,
        profile_id=str(row.engine_profile_id),
        version=int(row.version),
        latest_version=int(row.current_version),
        pinned=row.engine_profile_version_pin is not None,
        deleted=row.deleted_at is not None,
        document=dict(row.settings),
    )


async def assignable(
    conn: AsyncConnection, profile_id: str, *, field: str = "body.profile_id"
) -> AssignableProfile:
    """``profile_id`` at its latest version, when it may be newly assigned: 422
    ``profile_deleted`` (``reason: unknown`` or ``deleted``) at ``field`` otherwise, as
    ``store.require_assignable`` refuses a default."""
    row = (
        await conn.execute(
            select(
                engine_profiles.c.deleted_at,
                engine_profile_versions.c.version,
                engine_profile_versions.c.settings,
            )
            .select_from(
                engine_profiles.join(
                    engine_profile_versions,
                    and_(
                        engine_profile_versions.c.profile_id == engine_profiles.c.id,
                        engine_profile_versions.c.version == engine_profiles.c.current_version,
                    ),
                )
            )
            .where(engine_profiles.c.id == profile_id)
        )
    ).one_or_none()
    if row is None or row.deleted_at is not None:
        reason = "unknown" if row is None else "deleted"
        raise ProfileDeleted(
            f"engine profile {profile_id} cannot be assigned ({reason})",
            {"profile_id": profile_id, "field": field, "reason": reason},
        )
    return AssignableProfile(profile_id, int(row.version), dict(row.settings))


def resolve(
    document: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "overrides"),
) -> ProfileSettings:
    """A version's settings with ``overrides`` laid on top, over the defaults: ``overrides`` is
    checked as a settings document of its own (422 ``validation_error`` at ``field``), then the
    result as a whole (422 ``invalid_profile`` for an undelivered module or a missing dependency,
    FR-PROF-4)."""
    if overrides:
        parse_settings(overrides, field=field)
    merged = layered(document, overrides or {})
    return parse_settings(require_valid(merged, purposes=purposes, field=field), field=field)


def layered(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    """``top`` laid over ``base``: an object merges into an object, anything else replaces."""
    found = dict(base)
    for key, value in top.items():
        current = found.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            found[key] = layered(current, value)
        else:
            found[key] = value
    return found


# -- the read -------------------------------------------------------------------------------------


class ProfileRef(BaseModel):
    """The engine profile a story runs under and the version it runs on. ``pinned``: the story
    keeps an earlier version, because a data-producing edit could not apply to its history
    (``upgrade_unavailable``, M3's interim rule); ``false`` while it follows the latest.
    ``deleted``: the profile is deleted and still serves the story."""

    model_config = ConfigDict(frozen=True)

    id: str
    version: int
    pinned: bool
    deleted: bool


class EffectiveConfig(BaseModel):
    """A story's effective configuration (FR-PROF-5,16): the profile and version it runs on,
    every delivered setting at its effective value, and the configuration's fingerprint, the
    capabilities it enables and its quality status (``uncertified`` in M3)."""

    story_id: str
    profile: ProfileRef
    effective: SettingsDocument
    fingerprint: str
    capabilities: list[str]
    quality_status: QualityStatus


async def read(db: AsyncEngine, story_id: str, facts: RuntimeFacts) -> EffectiveConfig:
    """``GET /stories/{id}/effective-config`` (see the module docstring); 404 for an unknown
    story."""
    async with db.connect() as conn:
        found = await story_profile(conn, story_id)
    configuration = found.configuration()
    stamped = configuration.stamp(facts)
    return EffectiveConfig(
        story_id=story_id,
        profile=ProfileRef(
            id=found.profile_id, version=found.version, pinned=found.pinned, deleted=found.deleted
        ),
        effective=schema.delivered_values(configuration.settings),
        fingerprint=stamped.fingerprint,
        capabilities=list(stamped.capabilities),
        quality_status=stamped.quality_status,
    )


__all__ = [
    "AssignableProfile",
    "Configuration",
    "EffectiveConfig",
    "ProfileRef",
    "StoryProfile",
    "assignable",
    "layered",
    "read",
    "resolve",
    "story_profile",
]
