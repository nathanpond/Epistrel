"""A story's effective engine configuration (FR-PROF-5,6,16; #109's resolution, #113's layers and
overrides).

**Layers (FR-PROF-5).** A story's effective configuration is three settings documents laid one
over the other (``layered``: an object merges into an object, any other value replaces):

1. the deployment's ``profile_defaults`` (``deployment_state.profile_defaults``, a complete
   document seeded from the Lite preset, edited through ``/admin/settings``);
2. the story's engine profile (``stories.engine_profile_id``) at the version it runs on: the
   profile's latest (FR-PROF-6), unless the story keeps an earlier one
   (``stories.engine_profile_version_pin``, set when a confirmed data-producing edit could not
   apply to it: FR-PROF-7's ``upgrade_unavailable``, M3's interim rule until M7). A stored
   version may be sparse;
3. the story's own overrides (``story_profile_overrides``): a sparse settings document with a
   ``revision`` (0 while the story has none). They persist across the profile's versions and a
   reassignment.

The merged document is validated as a whole by #106's validator (``validate.require_valid``: its
shape strictly, every name delivered, every dependency met) and parsed into ``ProfileSettings``;
a combination that cannot run is 422 ``invalid_profile``, whatever a caller confirms. So a
behavioral edit of a profile reaches every story that follows it at the story's next turn start,
with nothing to re-derive, while each story keeps its overrides.

**One query, and a cache.** ``effective_configuration`` reads the three documents and their
identities in one query: the profile and version, the overrides' revision and the deployment's
``config_revision`` (which every ``profile_defaults`` write and every new profile version
moves). The parsed, validated settings are cached per process by story, and the cached entry
is used only while those four identities are unchanged, so resolution costs one query. The
cache is off until the Engine turns it on (``Settings.effective_config_cache``); tests run
without it.

**Who reads it.** The Lite turn captures its configuration once, at turn start, and runs under
it to the end (prospective, FR-CONC-15): the generation settings, the budgets, the verbatim
window, and whether the rolling summary and the pinned memories are on
(``orchestrator.lite.pipeline``, ``core.turns``, ``core.turn_gate``). So do the summary job
(``core.summary``), the pinned-memory cap (``core.story_texts``), the effective budget a story's
settings report, and every impact report (``core.profiles.impact``).

**Stamp.** A configuration's fingerprint, capabilities and quality status come from
``fingerprint.stamp`` over the resolved profile, version and settings, with the model ids and
prompt templates the Orchestrator resolves (``RuntimeFacts``).

**The read** (``GET /stories/{id}/effective-config``) answers the deployment's defaults (every
delivered setting), the profile and the version the story runs on (``pinned``, ``deleted``),
the story's overrides with their revision, its delivered settings at their effective values, and
the stamp. Nothing here writes but ``store_overrides``, which ``core.profiles.story`` calls.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.errors import InvalidProfile, ProfileDeleted, StoryNotFound
from epistrel.core.profiles import schema
from epistrel.core.profiles.fingerprint import QualityStatus, RuntimeFacts, Stamp, stamp
from epistrel.core.profiles.schema import ProfileSettings, Purposes, SettingsDocument
from epistrel.core.profiles.validate import (
    ProfileValidation,
    normalised,
    parse_settings,
    refusal,
    require_valid,
    undelivered_settings,
)
from epistrel.core.tables import (
    deployment_state,
    engine_profile_versions,
    engine_profiles,
    stories,
    story_profile_overrides,
)
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

STORED = ("settings",)
"""Where a stored combination is reported, should one ever fail to validate."""
CACHE_MAX = 10_000
"""The most stories whose resolved configuration one process keeps."""

Source = Literal["story", "profile", "deployment"]
"""The layer that names a setting: the story's overrides, its profile's version, or the
deployment's defaults."""


@dataclass(frozen=True, slots=True)
class Configuration:
    """One effective configuration: the profile and version it resolves to, and its settings
    (every setting the documents leave out at the model's default)."""

    profile_id: str
    version: int
    settings: ProfileSettings

    def stamp(self, facts: RuntimeFacts) -> Stamp:
        return stamp(
            profile_id=self.profile_id, version=self.version, settings=self.settings, facts=facts
        )


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


def named(document: Mapping[str, Any], path: str) -> bool:
    """Whether ``document`` names the setting at a dotted path."""
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return True


def resolve_layers(
    defaults: Mapping[str, Any],
    document: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = STORED,
) -> ProfileSettings:
    """The three layers as one configuration, validated as a whole (see the module docstring):
    422 ``validation_error`` at ``field`` for its shape, 422 ``invalid_profile`` for an
    undelivered name or a missing dependency (FR-PROF-4)."""
    merged = layered(layered(defaults, document), overrides or {})
    return parse_settings(require_valid(merged, purposes=purposes, field=field), field=field)


def merged_settings(
    defaults: Mapping[str, Any],
    document: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    *,
    field: tuple[str, ...] = STORED,
) -> ProfileSettings:
    """The three layers as one configuration, parsed but not validated: a configuration that
    runs now, which a validation rule added since may refuse (a write's starting point, an impact
    report's "before"). 422 ``validation_error`` only for a shape no stored layer has."""
    return parse_settings(layered(layered(defaults, document), overrides or {}), field=field)


def check_document(
    document: Mapping[str, Any],
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "settings"),
) -> dict[str, Any]:
    """One layer's settings document (a profile version's, or a story's overrides), normalised
    (the settings it names, as stored): its shape is checked strictly (422 ``validation_error``
    at ``field``) and every name it uses must be delivered (422 ``invalid_profile``). Its
    dependencies are checked on the whole configuration (``resolve_layers``), never on one layer
    alone."""
    settings = parse_settings(document, field=field)
    undelivered = undelivered_settings(document, settings, purposes=purposes, field=field)
    if undelivered:
        raise refusal(
            ProfileValidation(valid=False, missing_dependencies=[], undelivered=undelivered)
        )
    return normalised(settings)


# -- the cache ----------------------------------------------------------------------------------

Key = tuple[str, int, int, int]
"""``(profile_id, version, overrides_revision, deployment_revision)``."""


class ResolutionCache:
    """Each story's resolved settings, used while the identities they were resolved from hold
    (see the module docstring). Bounded: the story read longest ago goes first."""

    def __init__(self, max_entries: int = CACHE_MAX) -> None:
        self.enabled = False
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[Key, ProfileSettings]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, story_id: str, key: Key) -> ProfileSettings | None:
        if not self.enabled:
            return None
        with self._lock:
            found = self._entries.get(story_id)
            if found is None or found[0] != key:
                self.misses += 1
                return None
            self._entries.move_to_end(story_id)
            self.hits += 1
            return found[1]

    def put(self, story_id: str, key: Key, settings: ProfileSettings) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._entries[story_id] = (key, settings)
            self._entries.move_to_end(story_id)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = self.misses = 0


CACHE = ResolutionCache()
"""This process's resolution cache (off until ``configure_cache`` turns it on)."""


def configure_cache(enabled: bool) -> None:
    """Turn the resolution cache on or off (the Engine does, from its settings); either way it
    starts empty."""
    CACHE.clear()
    CACHE.enabled = enabled


# -- a story's configuration --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoryProfile:
    """The configuration a story runs under: its profile, the version it runs on (its pin, else
    the latest), whether that is a pin and whether the profile is deleted; the three documents
    and their identities; and the settings they resolve to, validated as a whole."""

    story_id: str
    profile_id: str
    version: int
    latest_version: int
    pinned: bool
    deleted: bool
    defaults: Mapping[str, Any]
    """The deployment's ``profile_defaults``."""
    document: Mapping[str, Any]
    """The profile version's settings, as stored (possibly sparse)."""
    overrides: Mapping[str, Any]
    """The story's overrides, as stored (``{}`` when it has none)."""
    overrides_revision: int
    """0 while the story has no overrides row."""
    deployment_revision: int
    config_revision: int
    """The story's own ``config_revision``."""
    settings: ProfileSettings = field(repr=False)

    def configuration(self) -> Configuration:
        return Configuration(self.profile_id, self.version, self.settings)

    def source_of(self, path: str) -> Source | None:
        """The layer whose document names the setting at ``path`` (the topmost); ``None`` when
        none does and the model's default stands."""
        if named(self.overrides, path):
            return "story"
        if named(self.document, path):
            return "profile"
        if named(self.defaults, path):
            return "deployment"
        return None

    def with_profile(
        self, profile_id: str, version: int, document: Mapping[str, Any], settings: ProfileSettings
    ) -> StoryProfile:
        """The same story on another profile version (its overrides kept), already resolved."""
        return StoryProfile(
            story_id=self.story_id,
            profile_id=profile_id,
            version=version,
            latest_version=version,
            pinned=False,
            deleted=False,
            defaults=self.defaults,
            document=document,
            overrides=self.overrides,
            overrides_revision=self.overrides_revision,
            deployment_revision=self.deployment_revision,
            config_revision=self.config_revision,
            settings=settings,
        )


async def effective_configuration(
    conn: AsyncConnection,
    story_id: str,
    *,
    purposes: Purposes | None = None,
    validate: bool = True,
) -> StoryProfile:
    """``story_id``'s effective configuration (see the module docstring), read in one query on
    ``conn`` (its snapshot when the caller holds one); 404 for an unknown story, 422
    ``invalid_profile`` for a stored combination that cannot run. ``validate=False`` reads it
    as it stands, valid or not: a write that may change it starts from it (a validation rule
    added since must not keep a story from being fixed)."""
    s, p, v, o, d = (
        stories,
        engine_profiles,
        engine_profile_versions,
        story_profile_overrides,
        deployment_state,
    )
    runs_on = func.coalesce(s.c.engine_profile_version_pin, p.c.current_version)
    row = (
        await conn.execute(
            select(
                s.c.engine_profile_id,
                s.c.engine_profile_version_pin,
                s.c.config_revision,
                p.c.current_version,
                p.c.deleted_at,
                v.c.version,
                v.c.settings,
                o.c.settings.label("overrides"),
                o.c.revision.label("overrides_revision"),
                d.c.profile_defaults,
                d.c.config_revision.label("deployment_revision"),
            )
            .select_from(
                s.join(p, p.c.id == s.c.engine_profile_id)
                .join(v, and_(v.c.profile_id == p.c.id, v.c.version == runs_on))
                .outerjoin(o, o.c.story_id == s.c.id)
                .join(d, d.c.id == DEPLOYMENT_ROW_ID)
            )
            .where(s.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    profile_id, version = str(row.engine_profile_id), int(row.version)
    overrides = dict(row.overrides or {})
    overrides_revision = int(row.overrides_revision or 0)
    deployment_revision = int(row.deployment_revision)
    key: Key = (profile_id, version, overrides_revision, deployment_revision)
    settings = CACHE.get(story_id, key)
    if settings is None and not validate:
        settings = merged_settings(row.profile_defaults, row.settings, overrides)
    elif settings is None:
        settings = resolve_layers(
            row.profile_defaults, row.settings, overrides, purposes=purposes, field=STORED
        )
        CACHE.put(story_id, key, settings)
    return StoryProfile(
        story_id=story_id,
        profile_id=profile_id,
        version=version,
        latest_version=int(row.current_version),
        pinned=row.engine_profile_version_pin is not None,
        deleted=row.deleted_at is not None,
        defaults=dict(row.profile_defaults),
        document=dict(row.settings),
        overrides=overrides,
        overrides_revision=overrides_revision,
        deployment_revision=deployment_revision,
        config_revision=int(row.config_revision),
        settings=settings,
    )


story_profile = effective_configuration
"""#109's name for ``effective_configuration``."""


@dataclass(frozen=True, slots=True)
class AssignableProfile:
    """A profile that may be newly assigned: its latest version and that version's settings."""

    profile_id: str
    version: int
    document: Mapping[str, Any]


async def assignable(
    conn: AsyncConnection, profile_id: str, *, field: str = "body.profile_id", lock: bool = False
) -> AssignableProfile:
    """``profile_id`` at its latest version, when it may be newly assigned: 422
    ``profile_deleted`` (``reason: unknown`` or ``deleted``) at ``field`` otherwise, as
    ``store.require_assignable`` refuses a default. ``lock``: hold the profile ``FOR SHARE``
    until the caller commits, so it is not deleted or edited under an assignment."""
    query = (
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
    if lock:
        query = query.with_for_update(read=True, of=engine_profiles)
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.deleted_at is not None:
        reason = "unknown" if row is None else "deleted"
        raise ProfileDeleted(
            f"engine profile {profile_id} cannot be assigned ({reason})",
            {"profile_id": profile_id, "field": field, "reason": reason},
        )
    return AssignableProfile(profile_id, int(row.version), dict(row.settings))


def resolve(
    current: StoryProfile,
    document: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "overrides"),
) -> ProfileSettings:
    """A preview's configuration (#109): the deployment's defaults, ``document`` (a profile
    version's settings), the story's own overrides, and ``overrides`` laid on top of those (a
    sparse settings document, checked on its own first: 422 ``validation_error`` at ``field``),
    validated as a whole (422 ``invalid_profile``, FR-PROF-4)."""
    stacked = dict(current.overrides)
    if overrides:
        parse_settings(overrides, field=field)
        stacked = layered(stacked, overrides)
    return resolve_layers(current.defaults, document, stacked, purposes=purposes, field=field)


async def store_overrides(
    conn: AsyncConnection, story_id: str, settings: Mapping[str, Any], revision: int
) -> None:
    """Write ``story_id``'s overrides at ``revision`` (the caller holds the story's lock)."""
    statement = pg_insert(story_profile_overrides).values(
        story_id=story_id, settings=dict(settings), revision=revision
    )
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=[story_profile_overrides.c.story_id],
            set_={"settings": statement.excluded.settings, "revision": revision},
        )
    )


async def profile_defaults(conn: AsyncConnection) -> dict[str, Any]:
    """The deployment's ``profile_defaults`` document (in the caller's snapshot or lock)."""
    found = await conn.scalar(
        select(deployment_state.c.profile_defaults).where(
            deployment_state.c.id == DEPLOYMENT_ROW_ID
        )
    )
    return dict(found or {})


# -- combinations a change must leave runnable --------------------------------------------------

INVALID_MAX = 1_000
"""The most stories (and profiles) a refusal names; ``details.count`` counts them all."""


async def following_overrides(conn: AsyncConnection, profile_id: str) -> dict[str, dict[str, Any]]:
    """Each story that follows ``profile_id``'s latest version (unpinned) and overrides some of
    its dials, with its overrides, in story id order."""
    rows = await conn.execute(
        select(stories.c.id, story_profile_overrides.c.settings)
        .join(story_profile_overrides, story_profile_overrides.c.story_id == stories.c.id)
        .where(
            stories.c.engine_profile_id == profile_id,
            stories.c.engine_profile_version_pin.is_(None),
        )
        .order_by(stories.c.id)
    )
    return {str(r.id): dict(r.settings) for r in rows if r.settings}


@dataclass(frozen=True, slots=True)
class Refused:
    """A combination that cannot run, and the refusal it gives."""

    name: str
    """A story id, or ``<profile id>@<version>``."""
    error: InvalidProfile


def first_refusal(
    refused: list[Refused], *, kind: str, message: str, extra: Mapping[str, Any] | None = None
) -> InvalidProfile:
    """One 422 ``invalid_profile`` for every combination in ``refused`` (``kind``: ``stories``
    or ``profiles``): the first one's findings, every name (at most ``INVALID_MAX``) and the
    count."""
    first = refused[0].error
    return InvalidProfile(
        message,
        {
            **first.details,
            **(extra or {}),
            kind: [r.name for r in refused[:INVALID_MAX]],
            "count": len(refused),
        },
    )


def refusals(
    defaults: Mapping[str, Any],
    candidates: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    *,
    purposes: Purposes | None,
    field: tuple[str, ...],
) -> list[Refused]:
    """Every ``(name, document, overrides)`` among ``candidates`` that cannot run over
    ``defaults``."""
    found: list[Refused] = []
    for name, document, overrides in candidates:
        try:
            resolve_layers(defaults, document, overrides, purposes=purposes, field=field)
        except InvalidProfile as exc:
            found.append(Refused(name, exc))
    return found


async def runnable_under(
    conn: AsyncConnection,
    defaults: Mapping[str, Any],
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "profile_defaults"),
) -> None:
    """422 ``invalid_profile`` unless every configuration that runs, or may be assigned, can run
    over ``defaults`` (a ``profile_defaults`` write): every live profile's latest version, every
    version a story runs on (a pin, a deleted profile's last), and every story's overrides over
    the version it runs on. ``details.profiles`` (``<id>@<version>``) and ``details.stories``
    name what could not run."""
    p, v = engine_profiles, engine_profile_versions
    runs_on = func.coalesce(stories.c.engine_profile_version_pin, p.c.current_version)
    used = (
        select(p.c.id, v.c.version, v.c.settings)
        .select_from(
            stories.join(p, p.c.id == stories.c.engine_profile_id).join(
                v, and_(v.c.profile_id == p.c.id, v.c.version == runs_on)
            )
        )
        .distinct()
    )
    live = select(p.c.id, v.c.version, v.c.settings).select_from(
        p.join(v, and_(v.c.profile_id == p.c.id, v.c.version == p.c.current_version))
    )
    versions = {
        (str(r.id), int(r.version)): dict(r.settings)
        for r in (await conn.execute(live.where(p.c.deleted_at.is_(None)).union(used))).all()
    }
    refused_profiles = refusals(
        defaults,
        [(f"{pid}@{ver}", doc, {}) for (pid, ver), doc in sorted(versions.items())],
        purposes=purposes,
        field=field,
    )
    if refused_profiles:
        raise first_refusal(
            refused_profiles,
            kind="profiles",
            message="profile versions in use could not run under these defaults",
        )
    o = story_profile_overrides
    overridden = await conn.execute(
        select(stories.c.id, v.c.settings, o.c.settings.label("overrides"))
        .select_from(
            stories.join(o, o.c.story_id == stories.c.id)
            .join(p, p.c.id == stories.c.engine_profile_id)
            .join(v, and_(v.c.profile_id == p.c.id, v.c.version == runs_on))
        )
        .order_by(stories.c.id)
    )
    refused_stories = refusals(
        defaults,
        [(str(r.id), dict(r.settings), dict(r.overrides)) for r in overridden.all()],
        purposes=purposes,
        field=field,
    )
    if refused_stories:
        raise first_refusal(
            refused_stories,
            kind="stories",
            message="stories could not run under these defaults with their overrides",
        )


# -- the read -------------------------------------------------------------------------------------


class ProfileVersionRef(BaseModel):
    """An engine profile and one of its versions: the configuration a respond response ran
    under (C-16, FR-PROF-6), or a story's (``GET /stories/{id}``)."""

    model_config = ConfigDict(frozen=True)

    id: str
    version: int


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


class OverridesRef(BaseModel):
    """A story's overrides of its profile's dials (FR-PROF-5): the settings it names, as a
    profile names them, and their ``revision`` (0 while it has none; one more at each change)."""

    model_config = ConfigDict(frozen=True)

    settings: SettingsDocument
    revision: int


class EffectiveConfig(BaseModel):
    """A story's effective configuration (FR-PROF-5,16): the deployment's defaults, the profile
    and the version it runs on, its own overrides, every delivered setting at its effective
    value (the defaults, then the profile's version, then the overrides), and the
    configuration's fingerprint, the capabilities it enables and its quality status
    (``uncertified`` in M3)."""

    story_id: str
    deployment_defaults: SettingsDocument
    profile: ProfileRef
    overrides: OverridesRef
    effective: SettingsDocument
    fingerprint: str
    capabilities: list[str]
    quality_status: QualityStatus


def view(found: StoryProfile, facts: RuntimeFacts) -> EffectiveConfig:
    """``found`` as the read answers it."""
    stamped = found.configuration().stamp(facts)
    return EffectiveConfig(
        story_id=found.story_id,
        deployment_defaults=schema.delivered_values(
            parse_settings(found.defaults, field=("profile_defaults",))
        ),
        profile=ProfileRef(
            id=found.profile_id, version=found.version, pinned=found.pinned, deleted=found.deleted
        ),
        overrides=OverridesRef(settings=dict(found.overrides), revision=found.overrides_revision),
        effective=schema.delivered_values(found.settings),
        fingerprint=stamped.fingerprint,
        capabilities=list(stamped.capabilities),
        quality_status=stamped.quality_status,
    )


async def read(db: AsyncEngine, story_id: str, facts: RuntimeFacts) -> EffectiveConfig:
    """``GET /stories/{id}/effective-config`` (see the module docstring); 404 for an unknown
    story."""
    async with db.connect() as conn:
        found = await effective_configuration(conn, story_id)
    return view(found, facts)


__all__ = [
    "CACHE",
    "CACHE_MAX",
    "INVALID_MAX",
    "AssignableProfile",
    "Configuration",
    "EffectiveConfig",
    "OverridesRef",
    "ProfileRef",
    "ProfileVersionRef",
    "Refused",
    "ResolutionCache",
    "Source",
    "StoryProfile",
    "assignable",
    "check_document",
    "configure_cache",
    "effective_configuration",
    "first_refusal",
    "following_overrides",
    "layered",
    "merged_settings",
    "named",
    "profile_defaults",
    "read",
    "refusals",
    "resolve",
    "resolve_layers",
    "runnable_under",
    "store_overrides",
    "story_profile",
    "view",
]
