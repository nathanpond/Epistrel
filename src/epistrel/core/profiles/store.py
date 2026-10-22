"""Engine profiles and their versions: create, read, list, edit, copy, validate, delete and
purge (FR-PROF-1,3; FR-API-6; FR-AUTH-5; #106).

**Writes.** Each write is one transaction of row writes that also records one committed,
story-less ``control`` operation by the requesting principal (``core.control``), which binds the
call's ``Idempotency-Key``; a write that changes nothing records none. A profile's settings are
validated as a layer of a story's configuration (#113): the document on its own (strictly, every
name delivered) and over the deployment's ``profile_defaults`` as a whole (FR-PROF-4).

**A new version is a configuration change** (FR-PROF-6, FR-CONC-14; #113): every story that
follows the profile runs under it from its next turn. The edit takes one deployment
``config_revision`` and records, through ``record_config_change`` (deployment scope), one row
``engine_profile:<id>`` (the old and new version numbers) and one row
``engine_profile:<id>.<dotted path>`` per setting whose value over the defaults changed, each
with that setting's class (``ConfigClass``; the version row has the strongest of them). Other
profile writes (create, copy, delete, a rename) change no configuration a story runs under and
leave ``config_revision`` as it is.

- **Create** stores version 1 of the validated settings, owned by the request's ``owner`` or
  else the requester (never ``system``).
- **Edit** (``PUT``) replaces the name and the settings. A new version is made only when the
  settings differ from the latest version's; a name-only edit makes none. A deleted profile
  cannot be edited (422 ``profile_deleted``). An edit is two-stage (FR-PROF-7, #109;
  ``core.profiles.impact``): its impact report lists every story that follows the profile's
  latest version, and a change the report warns of (a capability gained or lost by those
  stories, an upgrade unavailable) applies only with ``ignore_warnings``; otherwise the answer is
  the report and nothing is written. Each following story's entry is the change it sees through
  its own overrides (#113), and an edit that would leave a following story's configuration
  unable to run is 422 ``invalid_profile`` (``details.stories``). A data-producing increase
  cannot apply to a following story with history (M3's interim rule until M7): a confirmed edit
  pins that story to the version it ran on (``stories.engine_profile_version_pin``), and every
  other following story moves to the new version.
- **Copy** makes a new profile, owned by the requester (or the named ``owner``), at version 1
  with the settings of the source's latest version (or of ``version``). A deleted profile may be
  copied; the copy is an ordinary, live profile (``preset`` is false).
- **Delete** is always soft: ``deleted_at`` is set, the profile leaves listings and can no
  longer be assigned (422 ``profile_deleted``), and every story that uses it keeps running on its
  last version. The answer warns of those stories, and of the scenarios that recommend the
  profile. The deployment's default cannot be deleted (409, ``reason: is_default``); deleting a
  deleted profile changes nothing. ``purge_deleted`` (#71's ``retention_sweep``) removes a
  deleted profile and its versions once no story uses it, recording its ownership ``deleted``
  event by ``system``.
- **A fork keeps its story's profile**, a deleted one included: a fork copies its story
  (FR-STORE-15, ``core.fork``), as it copies a retired rating (FR-RATE-1), so it is not a new
  assignment. The fork runs on the profile's last version, and the purge waits for it too.

**Presets are ordinary profiles** (FR-PROF-3): a preset is edited, copied and deleted like any
other; ``preset`` records provenance only.

**Locks.** Edits and deletes hold the profile row ``FOR NO KEY UPDATE``, which leaves free the
``FOR KEY SHARE`` a story insert's foreign key check takes. An edit that adds data-producing
capability also holds its following stories ``FOR NO KEY UPDATE``, in id order, from its report
to its commit (``impact.following_report``). An edit first takes the deployment row ``FOR
UPDATE``, as every deployment configuration write does (``config_changes.lock_deployment``): a
new version takes a configuration revision, and the defaults it was validated over cannot
change under it. A create and a delete read it ``FOR SHARE``, and a story's assignment or
overrides write too (``core.profiles.story``), so an edit waits for those and they for it. The
purge holds a profile ``FOR UPDATE``, so a story insert naming it waits and then fails its
foreign key, instead of naming a profile that is gone. Under
that lock the purge checks the profile's stories again: a fork reads its source in one snapshot
without locking it, so a fork that began before its source was deleted can still commit a story
on the profile after the purge found the profile unused. The re-check keeps that profile, where
deleting it would end the sweep with a foreign-key error.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints
from sqlalchemy import and_, delete, exists, func, insert, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, control, ownership
from epistrel.core.config_changes import bump_config_revision, lock_deployment, record_config_change
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.errors import ConfigConflict, InvalidRequest, ProfileDeleted, ProfileNotFound
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.ownership import Owner, RelationFilter
from epistrel.core.profiles import effective, guarantees, impact, schema
from epistrel.core.profiles.effective import Configuration
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.profiles.impact import Delta, ImpactReport, TwoStageRequest, TwoStageResult
from epistrel.core.profiles.schema import (
    SETTINGS_SCHEMA,
    ProfileSettings,
    Purposes,
    SettingsDocument,
)
from epistrel.core.profiles.validate import (
    ProfileValidation,
    canonical,
    refusal,
    validate_profile,
)
from epistrel.core.settings_schema import ConfigClass, flatten
from epistrel.core.tables import (
    deployment_state,
    engine_profile_versions,
    engine_profiles,
    scenario_catalog,
    scenario_versions,
    stories,
)
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.ownership import EntityType
from epistrel.core.trimmed import trim_as_sent
from epistrel.testing import pauses

NAME_MAX = 120
DEFAULT_PAGE = 50
MAX_PAGE = 200
VERSION_MAX = 2**31 - 1
"""The largest version a request may name (the column is INTEGER)."""
WARNINGS_MAX = 1_000
"""The most stories, and the most scenarios, a delete names; the counts say how many."""
PURGE_BATCH = 1_000
"""The most deleted profiles one ``retention_sweep`` run purges; the next run takes the rest."""

Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX),
    BeforeValidator(trim_as_sent(NAME_MAX)),
]


EXAMPLE_SETTINGS: dict[str, Any] = {
    "memory": {"rolling_summary": {"window_messages": 20}},
    "budgets": {"deadline_ms": 30_000},
    "models": {"generation": {"temperature": 0.7}},
}


# -- requests -------------------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    """``POST /engine-profiles``: the new profile's name, its settings (any delivered module;
    what it leaves out takes the default) and, optionally, its ``owner`` (an opaque principal,
    by default the requesting one, never ``system``; FR-AUTH-5). Ownership confers no
    permission (FR-AUTH-8)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"name": "Brisk Lite", "settings": EXAMPLE_SETTINGS}]},
    )

    name: Name
    settings: SettingsDocument = Field(default_factory=dict)
    owner: Owner | None = None


class ProfileUpdate(TwoStageRequest):
    """``PUT /engine-profiles/{id}``: the profile's name and settings, replaced in full. A new
    version is made only when the settings differ from the latest version's. ``ignore_warnings``
    (``ignoreWarnings``) confirms a change the impact report warns of (FR-PROF-7)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"name": "Brisk Lite", "settings": EXAMPLE_SETTINGS}]},
    )

    name: Name
    settings: SettingsDocument


class ProfileCopy(BaseModel):
    """``POST /engine-profiles/{id}/copy``: the copy's name and, optionally, its ``owner``
    (default: the requesting principal; never ``system``)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"name": "My Lite"}]})

    name: Name
    owner: Owner | None = None


class ProfileValidate(BaseModel):
    """``POST /engine-profiles/{id}/validate``: settings to validate (a dry run of an edit);
    without them, the profile's latest version is validated."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"settings": EXAMPLE_SETTINGS}]}
    )

    settings: SettingsDocument | None = None


# -- results ---------------------------------------------------------------------------------------


class EngineProfile(BaseModel):
    """An engine profile at one version: ``version`` is the one read (the latest unless a read
    names another), ``current_version`` the latest. ``settings`` is what that version sets; a
    setting it leaves out takes the default. ``preset`` is true for a profile shipped with
    Epistrel (owned by ``system``); a preset is edited, copied and deleted like any profile. A
    deleted profile (``deleted_at``) still reads and serves the stories that use it."""

    id: str
    name: str
    owner_principal: str
    preset: bool
    version: int
    current_version: int
    settings: SettingsDocument
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class ProfileVersion(BaseModel):
    """One version of a profile: its number, when it was made and by which principal."""

    version: int
    created_at: datetime
    created_by: str


class ProfileWarning(BaseModel):
    """What a delete warns of: a story that still runs under the profile (``profile_in_use``),
    or a scenario whose draft or latest version recommends it (``profile_recommended``)."""

    kind: Literal["profile_in_use", "profile_recommended"]
    story_id: str | None = None
    scenario_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileWrite:
    """A created, edited or copied profile (its latest version), and the deployment's
    ``config_revision`` (unchanged). ``changed`` is false for an edit that changed nothing."""

    profile: EngineProfile
    config_revision: int
    changed: bool = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class DeletedProfile:
    """A deleted profile: when it was deleted, and what still uses or recommends it (at most
    ``WARNINGS_MAX`` of each; ``stories_using`` and ``scenarios_recommending`` count them all).
    ``changed`` is false when it was deleted already."""

    id: str
    deleted_at: datetime
    warnings: tuple[ProfileWarning, ...]
    stories_using: int
    scenarios_recommending: int
    config_revision: int
    changed: bool = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class ProfilePage:
    """A page of profiles, newest first, and the cursor of the next page (``None``: the
    last)."""

    items: list[EngineProfile]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ProfileSchema:
    """What a profile may set here: the delivered schema, every delivered setting's tags, the
    delivered modules with their levels, and the model purposes ``models.purposes`` routes."""

    settings_schema: dict[str, Any]
    tags: dict[str, schema.SettingTags]
    delivered: dict[str, list[str]]
    purposes: list[str]


# -- rows ------------------------------------------------------------------------------------------

_HEAD = (
    engine_profiles.c.id,
    engine_profiles.c.name,
    engine_profiles.c.owner_principal,
    engine_profiles.c.preset,
    engine_profiles.c.current_version,
    engine_profiles.c.created_at,
    engine_profiles.c.updated_at,
    engine_profiles.c.deleted_at,
)

Lock = Literal["share", "no_key_update"] | None


def _not_found(profile_id: str, version: int | None = None) -> ProfileNotFound:
    if version is None:
        return ProfileNotFound(
            f"engine profile {profile_id} does not exist", {"profile_id": profile_id}
        )
    return ProfileNotFound(
        f"engine profile {profile_id} has no version {version}",
        {"profile_id": profile_id, "version": version},
    )


async def _head(conn: AsyncConnection, profile_id: str, *, lock: Lock = None) -> Any:
    query = select(*_HEAD).where(engine_profiles.c.id == profile_id)
    if lock == "share":
        query = query.with_for_update(read=True)
    elif lock == "no_key_update":
        query = query.with_for_update(key_share=True)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise _not_found(profile_id)
    return row


async def _version(conn: AsyncConnection, profile_id: str, version: int) -> Any:
    row = (
        await conn.execute(
            select(
                engine_profile_versions.c.version,
                engine_profile_versions.c.settings,
                engine_profile_versions.c.created_at,
                engine_profile_versions.c.created_by,
            ).where(
                engine_profile_versions.c.profile_id == profile_id,
                engine_profile_versions.c.version == version,
            )
        )
    ).one_or_none()
    if row is None:
        raise _not_found(profile_id, version)
    return row


def _view(head: Any, version: int, settings: Mapping[str, Any]) -> EngineProfile:
    return EngineProfile(
        id=str(head.id),
        name=head.name,
        owner_principal=head.owner_principal,
        preset=bool(head.preset),
        version=version,
        current_version=int(head.current_version),
        settings=dict(settings),
        created_at=head.created_at,
        updated_at=head.updated_at,
        deleted_at=head.deleted_at,
    )


async def _read(
    conn: AsyncConnection, profile_id: str, version: int | None = None
) -> EngineProfile:
    head = await _head(conn, profile_id)
    found = await _version(conn, profile_id, version or int(head.current_version))
    return _view(head, int(found.version), found.settings)


async def config_revision(conn: AsyncConnection) -> int:
    """The deployment's ``config_revision`` (profile writes leave it as it is)."""
    found = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(found or 0)


async def _insert_version(
    conn: AsyncConnection,
    profile_id: str,
    version: int,
    settings: Mapping[str, Any],
    principal: str,
    at: datetime,
) -> None:
    await conn.execute(
        insert(engine_profile_versions).values(
            profile_id=profile_id,
            version=version,
            settings=dict(settings),
            created_by=principal,
            created_at=at,
        )
    )


async def _insert_profile(
    conn: AsyncConnection,
    *,
    name: str,
    owner: str,
    settings: Mapping[str, Any],
    principal: str,
) -> str:
    profile_id = new_ulid()
    now = clock.now()
    await conn.execute(
        insert(engine_profiles).values(
            id=profile_id,
            name=name,
            owner_principal=owner,
            preset=False,
            current_version=1,
            created_at=now,
            updated_at=now,
        )
    )
    await _insert_version(conn, profile_id, 1, settings, principal, now)
    return profile_id


# -- the default ----------------------------------------------------------------------------------


async def default_profile(conn: AsyncConnection) -> str:
    """The deployment's default profile (a story created without one runs under it; FR-PROF-1),
    read ``FOR SHARE`` so it cannot change until the caller commits."""
    found = await conn.scalar(
        select(deployment_state.c.default_engine_profile_id)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .with_for_update(read=True)
    )
    return str(found)


async def require_assignable(
    conn: AsyncConnection, profile_id: str, *, field: str = "body.default_engine_profile_id"
) -> str:
    """``profile_id`` if it can be newly assigned: 422 ``profile_deleted`` when it is deleted or
    unknown. Holds the row ``FOR SHARE`` so it cannot be deleted before the caller commits."""
    row = (
        await conn.execute(
            select(engine_profiles.c.deleted_at)
            .where(engine_profiles.c.id == profile_id)
            .with_for_update(read=True)
        )
    ).one_or_none()
    if row is None or row.deleted_at is not None:
        reason = "unknown" if row is None else "deleted"
        raise ProfileDeleted(
            f"engine profile {profile_id} cannot be assigned ({reason})",
            {"profile_id": profile_id, "field": field, "reason": reason},
        )
    return profile_id


# -- writes ---------------------------------------------------------------------------------------


async def validated(
    conn: AsyncConnection,
    document: Mapping[str, Any],
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "settings"),
) -> tuple[dict[str, Any], ProfileSettings]:
    """A profile version's settings, normalised (what it stores), and what they resolve to over
    the deployment's ``profile_defaults`` (see the module docstring): 422 ``validation_error``
    for their shape, 422 ``invalid_profile`` for an undelivered name or a missing dependency."""
    settings = effective.check_document(document, purposes=purposes, field=field)
    defaults = await effective.profile_defaults(conn)
    whole = effective.resolve_layers(defaults, settings, purposes=purposes, field=field)
    return settings, whole


async def create(
    db: AsyncEngine, request: ProfileCreate, *, principal: str, purposes: Purposes | None = None
) -> ProfileWrite:
    """A new profile at version 1 (see the module docstring); 422 ``validation_error`` or
    ``invalid_profile`` for settings that cannot run, and nothing is written."""
    async with db.begin() as conn:
        await default_profile(conn)  # the deployment row FOR SHARE: the defaults hold
        settings, _ = await validated(conn, request.settings, purposes=purposes)
        profile_id = await _insert_profile(
            conn,
            name=request.name,
            owner=request.owner or principal,
            settings=settings,
            principal=principal,
        )
        await control.record_control_operation(conn, actor=principal)
        return ProfileWrite(await _read(conn, profile_id), await config_revision(conn))


async def update_profile(
    db: AsyncEngine,
    profile_id: str,
    request: ProfileUpdate,
    *,
    principal: str,
    facts: RuntimeFacts,
    purposes: Purposes | None = None,
) -> TwoStageResult[ProfileWrite]:
    """Replace the profile's name and settings, in two stages (see the module docstring): a new
    version when the settings differ. The impact report covers every story following the
    profile's latest version; ``facts`` are the model ids and template hashes their fingerprints
    need. 404 for an unknown profile, 422 ``profile_deleted`` for a deleted one, and 422
    ``invalid_profile`` for settings that cannot run, whatever ``ignore_warnings`` says."""
    async with db.begin() as conn:
        deployment = await lock_deployment(conn)  # first, as every configuration write
        head = await _head(conn, profile_id, lock="no_key_update")
        if head.deleted_at is not None:
            raise ProfileDeleted(
                f"engine profile {profile_id} is deleted and cannot be edited (copy it instead)",
                {"profile_id": profile_id, "field": "path.profile_id", "reason": "deleted"},
            )
        settings, after = await validated(conn, request.settings, purposes=purposes)
        defaults = deployment.profile_defaults
        current = int(head.current_version)
        latest = await _version(conn, profile_id, current)
        new_version = canonical(settings) != canonical(latest.settings)
        version = current + 1 if new_version else current
        before = effective.merged_settings(defaults, latest.settings)
        change = impact.delta(
            Configuration(profile_id, current, before),
            Configuration(profile_id, version, after),
            facts,
        )
        overridden = await _overridden_changes(
            conn,
            profile_id,
            defaults=defaults,
            versions=((current, latest.settings), (version, settings)),
            facts=facts,
            purposes=purposes,
        )
        following = await impact.following_report(conn, profile_id, change, overridden)

        async def report() -> ImpactReport:
            return following.report

        async def apply(_: ImpactReport) -> ProfileWrite:
            if not new_version and request.name == head.name:
                return ProfileWrite(
                    await _read(conn, profile_id), await config_revision(conn), changed=False
                )
            now = clock.now()
            values: dict[str, Any] = {"name": request.name, "updated_at": now}
            if new_version:
                await _insert_version(conn, profile_id, version, settings, principal, now)
                values["current_version"] = version
                await _pin(conn, profile_id, current, following.keep)
            await conn.execute(
                update(engine_profiles).where(engine_profiles.c.id == profile_id).values(**values)
            )
            if new_version:
                await _record_version(
                    conn,
                    deployment,
                    profile_id,
                    (current, before),
                    (version, after),
                    principal=principal,
                )
            else:
                await control.record_control_operation(conn, actor=principal)
            return ProfileWrite(await _read(conn, profile_id), await config_revision(conn))

        outcome = await impact.two_stage(report, apply, ignore_warnings=request.ignore_warnings)
        return dataclasses.replace(outcome, config_revision=await config_revision(conn))


async def _overridden_changes(
    conn: AsyncConnection,
    profile_id: str,
    *,
    defaults: Mapping[str, Any],
    versions: tuple[tuple[int, Mapping[str, Any]], tuple[int, Mapping[str, Any]]],
    facts: RuntimeFacts,
    purposes: Purposes | None,
) -> dict[str, Delta]:
    """The change each following story with overrides sees (its overrides over the profile's
    latest version, then over the new one); 422 ``invalid_profile`` (``details.stories``) when
    the new version would leave any of them unable to run."""
    (old_version, old_document), (new_version, new_document) = versions
    found = await effective.following_overrides(conn, profile_id)
    refused = effective.refusals(
        defaults,
        [(story, new_document, overrides) for story, overrides in found.items()],
        purposes=purposes,
        field=("body", "settings"),
    )
    if refused:
        raise effective.first_refusal(
            refused,
            kind="stories",
            message="the edit would leave stories that follow the profile unable to run with "
            "their overrides",
        )
    changes: dict[str, Delta] = {}
    for story, overrides in found.items():
        before = effective.merged_settings(defaults, old_document, overrides)
        after = effective.resolve_layers(defaults, new_document, overrides)
        changes[story] = impact.delta(
            Configuration(profile_id, old_version, before),
            Configuration(profile_id, new_version, after),
            facts,
        )
    return changes


def profile_setting(profile_id: str, path: str | None = None) -> str:
    """The ``config_changes`` setting of a profile's version (``engine_profile:<id>``) or of one
    of its settings (``engine_profile:<id>.<dotted path>``)."""
    return f"{PROFILE_FAMILY}:{profile_id}" + (f".{path}" if path else "")


PROFILE_FAMILY = "engine_profile"
"""The ``config_changes`` row family of profile versions (a deployment-scope filter prefix)."""


async def _record_version(
    conn: AsyncConnection,
    deployment: Any,
    profile_id: str,
    old: tuple[int, ProfileSettings],
    new: tuple[int, ProfileSettings],
    *,
    principal: str,
) -> None:
    """A new version as a configuration change (see the module docstring): one revision, the
    version row and one row per setting whose value over the defaults changed."""
    (old_version, before), (new_version, after) = old, new
    old_values = flatten(ProfileSettings, before.model_dump(mode="json"))
    new_values = flatten(ProfileSettings, after.model_dump(mode="json"))
    tags = schema.setting_tags()
    changed = sorted(path for path in new_values if new_values[path] != old_values.get(path))
    classes = {tags[path].change_class for path in changed}
    strongest = (
        ConfigClass.interpretive
        if ConfigClass.interpretive in classes
        else (ConfigClass.prospective)
    )
    revision = await bump_config_revision(conn, deployment)
    await record_config_change(
        conn,
        config_revision=revision,
        setting=profile_setting(profile_id),
        old=old_version,
        new=new_version,
        actor=principal,
        change_class=strongest,
    )
    for path in changed:
        await record_config_change(
            conn,
            config_revision=revision,
            setting=profile_setting(profile_id, path),
            old=old_values.get(path),
            new=new_values[path],
            actor=principal,
            change_class=tags[path].change_class,
        )


async def _pin(
    conn: AsyncConnection, profile_id: str, version: int, story_ids: frozenset[str]
) -> None:
    """Keep ``story_ids`` (following stories whose upgrade is unavailable) on ``version``."""
    if not story_ids:
        return
    await conn.execute(
        update(stories)
        .where(
            stories.c.id.in_(sorted(story_ids)),
            stories.c.engine_profile_id == profile_id,
            stories.c.engine_profile_version_pin.is_(None),
        )
        .values(engine_profile_version_pin=version)
    )


async def copy_profile(
    db: AsyncEngine,
    profile_id: str,
    request: ProfileCopy,
    *,
    principal: str,
    version: int | None = None,
) -> ProfileWrite:
    """A new profile at version 1 with the settings of ``profile_id``'s latest version (or of
    ``version``), owned by ``request.owner`` or the requester; a deleted profile may be copied.
    404 for an unknown profile or version."""
    async with db.begin() as conn:
        head = await _head(conn, profile_id, lock="share")
        source = await _version(conn, profile_id, version or int(head.current_version))
        copied = await _insert_profile(
            conn,
            name=request.name,
            owner=request.owner or principal,
            settings=source.settings,
            principal=principal,
        )
        await control.record_control_operation(conn, actor=principal)
        return ProfileWrite(await _read(conn, copied), await config_revision(conn))


async def _usage(conn: AsyncConnection, profile_id: str) -> tuple[list[ProfileWarning], int, int]:
    """The warnings of a delete: the stories that use the profile and the scenarios that
    recommend it, each list capped, and the two counts."""
    using = stories.c.engine_profile_id == profile_id
    story_ids = await conn.scalars(
        select(stories.c.id).where(using).order_by(stories.c.id).limit(WARNINGS_MAX)
    )
    story_count = int(
        await conn.scalar(select(func.count()).select_from(stories).where(using)) or 0
    )
    recommends = or_(
        scenario_catalog.c.draft["recommendation"]["engine_profile_id"].astext == profile_id,
        exists().where(
            scenario_versions.c.catalog_id == scenario_catalog.c.id,
            scenario_versions.c.version == scenario_catalog.c.current_version,
            scenario_versions.c.content["recommendation"]["engine_profile_id"].astext == profile_id,
        ),
    )
    scenario_ids = await conn.scalars(
        select(scenario_catalog.c.id)
        .where(recommends)
        .order_by(scenario_catalog.c.id)
        .limit(WARNINGS_MAX)
    )
    scenario_count = int(
        await conn.scalar(select(func.count()).select_from(scenario_catalog).where(recommends)) or 0
    )
    warnings = [ProfileWarning(kind="profile_in_use", story_id=str(s)) for s in story_ids]
    warnings += [
        ProfileWarning(kind="profile_recommended", scenario_id=str(s)) for s in scenario_ids
    ]
    return warnings, story_count, scenario_count


async def delete_profile(db: AsyncEngine, profile_id: str, *, principal: str) -> DeletedProfile:
    """Soft-delete the profile (see the module docstring): 404 for an unknown one, 409
    (``reason: is_default``) for the deployment's default; a deleted one is answered as it is.
    The answer warns of every story still using the profile and every scenario recommending
    it."""
    async with db.begin() as conn:
        default = await default_profile(conn)
        head = await _head(conn, profile_id, lock="no_key_update")
        if profile_id == default:
            raise ConfigConflict(
                f"engine profile {profile_id} is the deployment's default profile; make another "
                "profile the default first",
                {"id": profile_id, "reason": "is_default"},
            )
        warnings, story_count, scenario_count = await _usage(conn, profile_id)
        deleted_at: datetime | None = head.deleted_at
        changed = deleted_at is None
        if deleted_at is None:
            deleted_at = clock.now()
            await conn.execute(
                update(engine_profiles)
                .where(engine_profiles.c.id == profile_id)
                .values(deleted_at=deleted_at, updated_at=deleted_at)
            )
            await control.record_control_operation(conn, actor=principal)
        return DeletedProfile(
            id=profile_id,
            deleted_at=deleted_at,
            warnings=tuple(warnings),
            stories_using=story_count,
            scenarios_recommending=scenario_count,
            config_revision=await config_revision(conn),
            changed=changed,
        )


async def purge_deleted(db: AsyncEngine) -> int:
    """Purge deleted profiles that no story uses and that are not the default (at most
    ``PURGE_BATCH`` per run), each in its own transaction under its row lock: its relations go
    and a ``deleted`` ownership event by ``system`` records it (``ownership.drop_entity``), then
    the row and its versions. A profile that a story came to use after the candidates were read
    (a fork committing late; see the module docstring) is kept. Returns how many it purged."""
    unused = ~exists().where(stories.c.engine_profile_id == engine_profiles.c.id)
    default = (
        select(deployment_state.c.default_engine_profile_id)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .scalar_subquery()
    )
    async with db.connect() as conn:
        candidates = list(
            await conn.scalars(
                select(engine_profiles.c.id)
                .where(
                    engine_profiles.c.deleted_at.is_not(None),
                    unused,
                    engine_profiles.c.id != default,
                )
                .order_by(engine_profiles.c.deleted_at, engine_profiles.c.id)
                .limit(PURGE_BATCH)
            )
        )
    purged = 0
    for profile_id in candidates:
        await pauses.pause(pauses.PROFILE_PURGE_BEFORE_LOCK)
        async with db.begin() as conn:
            locked = await conn.scalar(
                select(engine_profiles.c.id)
                .where(
                    engine_profiles.c.id == profile_id,
                    engine_profiles.c.deleted_at.is_not(None),
                    engine_profiles.c.id != default,
                )
                .with_for_update(skip_locked=True)
            )
            if locked is None:
                continue
            if await conn.scalar(select(exists().where(stories.c.engine_profile_id == profile_id))):
                continue
            await ownership.drop_entity(
                conn, EntityType.ENGINE_PROFILE, profile_id, principal=SYSTEM_PRINCIPAL
            )
            await conn.execute(delete(engine_profiles).where(engine_profiles.c.id == profile_id))
            purged += 1
    return purged


# -- reads ----------------------------------------------------------------------------------------


async def get_profile(
    db: AsyncEngine, profile_id: str, *, version: int | None = None
) -> EngineProfile:
    """The profile at its latest version (or ``version``), deleted or not; 404 for an unknown
    profile or version."""
    async with db.connect() as conn:
        return await _read(conn, profile_id, version)


async def profile_guarantees(
    db: AsyncEngine, profile_id: str, *, version: int | None = None
) -> guarantees.GuaranteeTable:
    """The guarantee table (FR-VERIFY-15, FR-PROF-12; #132) of the profile at its latest version
    (or ``version``) over the deployment's ``profile_defaults``, as a story following it with no
    overrides runs; 404 for an unknown profile or version. A deleted profile still answers."""
    async with db.connect() as conn:
        found = await _read(conn, profile_id, version)
        defaults = await effective.profile_defaults(conn)
    settings = effective.merged_settings(defaults, found.settings)
    return guarantees.table(found.id, found.version, settings)


async def list_versions(db: AsyncEngine, profile_id: str) -> list[ProfileVersion]:
    """Every version of the profile, oldest first; 404 for an unknown profile."""
    async with db.connect() as conn:
        await _head(conn, profile_id)
        rows = await conn.execute(
            select(
                engine_profile_versions.c.version,
                engine_profile_versions.c.created_at,
                engine_profile_versions.c.created_by,
            )
            .where(engine_profile_versions.c.profile_id == profile_id)
            .order_by(engine_profile_versions.c.version)
        )
        return [
            ProfileVersion(version=int(r.version), created_at=r.created_at, created_by=r.created_by)
            for r in rows
        ]


def _encode_cursor(profile: EngineProfile) -> str:
    raw = json.dumps([profile.created_at.isoformat(), profile.id]).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created_at, profile_id = json.loads(raw)
        moment = datetime.fromisoformat(created_at)
        if not (isinstance(profile_id, str) and re.fullmatch(ULID_PATTERN, profile_id)):
            raise ValueError("not an id")
        if moment.tzinfo is None:
            raise ValueError("no time zone")
    except ValueError, TypeError, binascii.Error:
        raise InvalidRequest("not an engine-profile cursor", {"field": "query.cursor"}) from None
    return moment, profile_id


async def list_profiles(
    db: AsyncEngine,
    *,
    owner: str | None = None,
    relations: Sequence[RelationFilter] = (),
    include_deleted: bool = False,
    limit: int = DEFAULT_PAGE,
    cursor: str | None = None,
) -> ProfilePage:
    """A page of profiles at their latest version, newest first, presets included; deleted ones
    only with ``include_deleted``. ``owner`` and ``relations`` narrow the list only when given
    (FR-AUTH-8)."""
    if not 1 <= limit <= MAX_PAGE:
        raise InvalidRequest(f"limit must be 1 to {MAX_PAGE}", {"field": "query.limit"})
    where = ownership.list_filters(
        EntityType.ENGINE_PROFILE,
        engine_profiles.c.id,
        engine_profiles.c.owner_principal,
        owner=owner,
        relations=relations,
    )
    if not include_deleted:
        where.append(engine_profiles.c.deleted_at.is_(None))
    if cursor is not None:
        moment, after = _decode_cursor(cursor)
        where.append(
            tuple_(engine_profiles.c.created_at, engine_profiles.c.id) < tuple_(moment, after)
        )
    query = (
        select(*_HEAD, engine_profile_versions.c.settings)
        .select_from(
            engine_profiles.join(
                engine_profile_versions,
                and_(
                    engine_profile_versions.c.profile_id == engine_profiles.c.id,
                    engine_profile_versions.c.version == engine_profiles.c.current_version,
                ),
            )
        )
        .where(*where)
        .order_by(engine_profiles.c.created_at.desc(), engine_profiles.c.id.desc())
        .limit(limit + 1)
    )
    async with db.connect() as conn:
        rows = (await conn.execute(query)).all()
    items = [_view(row, int(row.current_version), row.settings) for row in rows[:limit]]
    following = _encode_cursor(items[-1]) if len(rows) > limit and items else None
    return ProfilePage(items=items, next_cursor=following)


async def validate_stored(
    db: AsyncEngine,
    profile_id: str,
    request: ProfileValidate | None = None,
    *,
    purposes: Purposes | None = None,
    guardrail_calls: tuple[int, int] = (0, 0),
) -> ProfileValidation:
    """Validate ``request.settings`` (a dry run), or the profile's latest version: the result
    when it is valid (with its ``warnings``, #156: ``guardrail_calls`` is the deployment's
    model-assisted input and output policies); 422 ``invalid_profile`` naming what is missing
    or undelivered, or ``validation_error`` for the settings' shape, when it is not. 404 for an
    unknown profile."""
    async with db.connect() as conn:
        head = await _head(conn, profile_id)
        if request is not None and request.settings is not None:
            document: Mapping[str, Any] = request.settings
            field: tuple[str, ...] = ("body", "settings")
        else:
            document = (await _version(conn, profile_id, int(head.current_version))).settings
            field = ("settings",)
        defaults = await effective.profile_defaults(conn)
    effective.check_document(document, purposes=purposes, field=field)
    result = validate_profile(
        effective.layered(defaults, document),
        purposes=purposes,
        field=field,
        guardrail_calls=guardrail_calls,
    )
    if not result.valid:
        raise refusal(result)
    return result


def profile_schema(purposes: Purposes | None = None) -> ProfileSchema:
    """The delivered schema, tags, modules and purposes (``GET /engine-profiles/schema``)."""
    return ProfileSchema(
        settings_schema=schema.delivered_schema(),
        tags=schema.delivered_tags(),
        delivered=schema.delivered_levels(),
        purposes=sorted(purposes.delivered) if purposes is not None else [],
    )


__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "NAME_MAX",
    "PROFILE_FAMILY",
    "SETTINGS_SCHEMA",
    "VERSION_MAX",
    "WARNINGS_MAX",
    "DeletedProfile",
    "EngineProfile",
    "ProfileCopy",
    "ProfileCreate",
    "ProfilePage",
    "ProfileSchema",
    "ProfileUpdate",
    "ProfileValidate",
    "ProfileVersion",
    "ProfileWarning",
    "ProfileWrite",
    "config_revision",
    "copy_profile",
    "create",
    "default_profile",
    "delete_profile",
    "get_profile",
    "list_profiles",
    "list_versions",
    "profile_guarantees",
    "profile_schema",
    "profile_setting",
    "purge_deleted",
    "require_assignable",
    "update_profile",
    "validate_stored",
    "validated",
]
