"""``/engine-profiles``: engine profiles, named and versioned sets of capability modules and
dials (FR-PROF-1..4, FR-API-6; #106).

- ``POST /engine-profiles``: a new profile at version 1, owned by the requester or the named
  ``owner``; 201. Its settings may name any delivered module (``GET /engine-profiles/schema``);
  what they leave out takes the default.
- ``GET /engine-profiles``: profiles at their latest version, newest first, presets included,
  a page at a time; ``owner`` and ``relation`` narrow the list only when asked, and deleted
  profiles are listed only with ``include_deleted``.
- ``GET /engine-profiles/schema``: the delivered schema, every delivered setting's three tags,
  the delivered modules with their levels, and the model purposes a profile may route.
- ``GET /engine-profiles/{id}``: the profile at its latest version (``?version=n``: that one).
- ``PUT /engine-profiles/{id}``: the name and settings, replaced; a new version only when the
  settings differ. Two-stage (FR-PROF-7, #109): the answer is ``{applied, impact, result}``,
  and a change its impact report warns of applies only with ``ignore_warnings``.
- ``POST /engine-profiles/{id}/copy``: a new profile with the settings of the latest version (or
  ``?version=n``); 201.
- ``POST /engine-profiles/{id}/validate``: validates the body's settings (a dry run), or the
  latest version when there is no body or it holds no settings; it changes nothing
  (``mutating=False``: no ``Idempotency-Key``).
- ``GET /engine-profiles/{id}/versions``: every version, oldest first.
- ``DELETE /engine-profiles/{id}``: a soft delete answering the stories still using the profile
  and the scenarios recommending it; the ``retention_sweep`` purges it once no story uses it. A
  fork of a story on a deleted profile runs under it too (a fork copies its story).
- ``PUT /engine-profiles/{id}/owner``, ``POST``/``GET …/relations`` and ``DELETE
  …/relations/{relation}/{principal}``: the profile's owner and relations (``routes.ownership``,
  #104). Nothing gates on them (FR-AUTH-8).

Every handler makes one ``Engine`` call. Writes are deployment-scoped: idempotent under
``Idempotency-Key``, answering the deployment's ``config_revision`` (which they leave as it
is). Presets are ordinary profiles (FR-PROF-3): the only refusal is deleting the deployment's
default (409, ``reason: is_default``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from epistrel.api.deps import ContextDep, EngineDep, ProfileId, route_flags
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.api.routes.ownership import mount_ownership_routes
from epistrel.api.routes.stories import (
    OWNER_FILTER_SCHEMA,
    RELATION_FILTERS_SCHEMA,
    RelationFilterParam,
)
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.profiles.impact import ImpactReport, TwoStageResult
from epistrel.core.profiles.schema import Quality, SettingTags, Tag
from epistrel.core.profiles.store import (
    DEFAULT_PAGE,
    MAX_PAGE,
    VERSION_MAX,
    DeletedProfile,
    EngineProfile,
    ProfileCopy,
    ProfileCreate,
    ProfilePage,
    ProfileSchema,
    ProfileUpdate,
    ProfileValidate,
    ProfileVersion,
    ProfileWarning,
    ProfileWrite,
)
from epistrel.core.profiles.validate import ProfileValidation
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.tables.ownership import EntityType

PROFILES_TAG = "profiles"

router = APIRouter(prefix="/engine-profiles", tags=[PROFILES_TAG])

VersionQuery = Annotated[
    int | None,
    Query(ge=1, le=VERSION_MAX, description="A version's number (default: the latest)."),
]


class ProfileBody(EngineProfile, DeploymentMutationResponse):
    """The profile after a create, an edit or a copy, at its latest version, with the
    deployment's ``config_revision`` (unchanged)."""

    model_config = ConfigDict(frozen=False)

    @classmethod
    def of(cls, write: ProfileWrite) -> ProfileBody:
        return cls(**write.profile.model_dump(), config_revision=write.config_revision)


class ProfileEditBody(DeploymentMutationResponse):
    """An edit's two stages (FR-PROF-7): ``applied``; the ``impact`` report over every story
    following the profile; and ``result``, the profile at its latest version once the edit
    applied (``null`` when the report held it back and nothing was written). ``config_revision``
    is the deployment's (unchanged)."""

    applied: bool
    impact: ImpactReport
    result: EngineProfile | None

    @classmethod
    def of(cls, edit: TwoStageResult[ProfileWrite]) -> ProfileEditBody:
        assert edit.config_revision is not None  # noqa: S101 - a deployment-scoped result
        return cls(
            applied=edit.applied,
            impact=edit.impact,
            result=edit.result.profile if edit.result is not None else None,
            config_revision=edit.config_revision,
        )


class ProfileListBody(BaseModel):
    """A page of profiles at their latest version, newest first; ``next_cursor`` fetches the
    next page (``null`` on the last)."""

    items: list[EngineProfile]
    next_cursor: str | None

    @classmethod
    def of(cls, page: ProfilePage) -> ProfileListBody:
        return cls(items=page.items, next_cursor=page.next_cursor)


class ProfileVersionsBody(BaseModel):
    """Every version of a profile, oldest first."""

    items: list[ProfileVersion]


class ProfileDeletedBody(DeploymentMutationResponse):
    """A deleted profile: when it was deleted, and the warnings: every story that still runs
    under it (``profile_in_use``) and every scenario that recommends it
    (``profile_recommended``), at most 1,000 of each; the two counts name them all. Those
    stories keep running on its last version until it is purged, once no story uses it."""

    id: str
    deleted_at: datetime
    warnings: list[ProfileWarning]
    stories_using: int
    scenarios_recommending: int

    @classmethod
    def of(cls, deleted: DeletedProfile) -> ProfileDeletedBody:
        return cls(
            id=deleted.id,
            deleted_at=deleted.deleted_at,
            warnings=list(deleted.warnings),
            stories_using=deleted.stories_using,
            scenarios_recommending=deleted.scenarios_recommending,
            config_revision=deleted.config_revision,
        )


class SettingTagsBody(BaseModel):
    """One setting's three tags: ``behavioral`` or ``data_producing`` (FR-PROF-6),
    ``interpretive`` or ``prospective`` (FR-CONC-15), and quality ``relevant`` or ``irrelevant``
    (FR-PROF-16)."""

    model_config = ConfigDict(populate_by_name=True)

    tag: Tag
    change_class: ConfigClass = Field(alias="class", serialization_alias="class")
    quality: Quality

    @classmethod
    def of(cls, tags: SettingTags) -> SettingTagsBody:
        return cls(tag=tags.tag, change_class=tags.change_class, quality=tags.quality)


class ProfileSchemaBody(BaseModel):
    """What a profile may set on this deployment: ``schema`` is the JSON Schema of the
    delivered modules and levels (a setting it leaves out is refused, 422 ``invalid_profile``),
    ``tags`` each delivered setting's tags by dotted path, ``delivered`` each delivered module
    with its levels, and ``purposes`` the model purposes ``models.purposes`` may route."""

    model_config = ConfigDict(populate_by_name=True)

    settings_schema: dict[str, Any] = Field(alias="schema", serialization_alias="schema")
    tags: dict[str, SettingTagsBody]
    delivered: dict[str, list[str]]
    purposes: list[str]

    @classmethod
    def of(cls, found: ProfileSchema) -> ProfileSchemaBody:
        return cls(
            settings_schema=found.settings_schema,
            tags={path: SettingTagsBody.of(tags) for path, tags in found.tags.items()},
            delivered=found.delivered,
            purposes=found.purposes,
        )


_SETTINGS_RULES = (
    "The settings may name any delivered module and level (`GET /engine-profiles/schema`); "
    "what they leave out takes the default. A shape error is 422 `validation_error` at the "
    "field; a module or level this deployment has not delivered, or a module whose "
    "dependencies are off (FR-PROF-4), is 422 `invalid_profile` naming each "
    "(`details.undelivered`, `details.missing_dependencies`), and nothing is written."
)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ProfileBody,
    operation_id="create_profile",
    summary="Create an engine profile",
    description=(
        "Create an engine profile at version 1, owned by `owner` (default: the requesting "
        f"principal; never `system`). {_SETTINGS_RULES}"
    ),
)
async def create_profile(
    body: ProfileCreate, response: Response, ctx: ContextDep, engine: EngineDep
) -> ProfileBody:
    write = await engine.create_profile(ctx, body)
    return answered(ProfileBody.of(write), ctx, response, replayed=write.replayed)


@router.get(
    "",
    response_model=ProfileListBody,
    operation_id="list_profiles",
    summary="List engine profiles",
    description=(
        "Profiles at their latest version, newest first, presets included, `limit` at a time; "
        "`cursor` (a page's `next_cursor`) continues. Deleted profiles are listed only with "
        "`include_deleted=true`. `owner` and `relation` (`name:principal`, or `name:` for any "
        "principal; repeatable, all must hold) narrow the list only when a request asks: they "
        "gate nothing (FR-AUTH-8)."
    ),
)
async def list_profiles(
    ctx: ContextDep,
    engine: EngineDep,
    owner: Annotated[
        str | None,
        Query(pattern=PRINCIPAL_PATTERN, description="Only the profiles this principal owns."),
        WithJsonSchema(OWNER_FILTER_SCHEMA),
    ] = None,
    relation: Annotated[
        list[RelationFilterParam] | None,
        Query(
            description="Only the profiles with this relation: `name:principal`, or `name:` "
            "for any principal (repeatable; all must hold)."
        ),
        WithJsonSchema(RELATION_FILTERS_SCHEMA),
    ] = None,
    include_deleted: Annotated[bool, Query(description="List deleted profiles too.")] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    cursor: Annotated[
        str | None, Query(max_length=200, description="The previous page's `next_cursor`.")
    ] = None,
) -> ProfileListBody:
    page = await engine.list_profiles(
        ctx,
        owner=owner,
        relation=relation or (),
        include_deleted=include_deleted,
        limit=limit,
        cursor=cursor,
    )
    return ProfileListBody.of(page)


@router.get(
    "/schema",
    response_model=ProfileSchemaBody,
    operation_id="profile_schema",
    summary="Read what an engine profile may set",
    description=(
        "The JSON Schema of the modules and levels this deployment has delivered (FR-PROF-2), "
        "every delivered setting's tags (`behavioral`/`data_producing`, "
        "`interpretive`/`prospective`, quality `relevant`/`irrelevant`), the delivered modules "
        "with their levels, and the model purposes `models.purposes` may route."
    ),
)
async def profile_schema(ctx: ContextDep, engine: EngineDep) -> ProfileSchemaBody:
    return ProfileSchemaBody.of(await engine.profile_schema(ctx))


@router.get(
    "/{profile_id}",
    response_model=EngineProfile,
    operation_id="get_profile",
    summary="Read an engine profile",
    description=(
        "The profile at its latest version; `version` reads another one (404 when it does not "
        "exist). A deleted profile still reads, with its `deleted_at`."
    ),
)
async def get_profile(
    profile_id: ProfileId, ctx: ContextDep, engine: EngineDep, version: VersionQuery = None
) -> EngineProfile:
    return await engine.get_profile(ctx, profile_id, version=version)


@router.put(
    "/{profile_id}",
    response_model=ProfileEditBody,
    operation_id="update_profile",
    summary="Edit an engine profile (a new version)",
    description=(
        "Replace the profile's name and settings. A new version is made only when the settings "
        "differ from the latest version's; a name-only edit makes none, and an edit that "
        "changes nothing records nothing. A preset is edited like any profile (FR-PROF-3). A "
        "deleted profile cannot be edited (422 `profile_deleted`; copy it instead). "
        f"{_SETTINGS_RULES}"
        "\n\nAn edit runs in two stages (FR-PROF-7). Its `impact` report lists every story "
        "that follows the profile's latest version: the capabilities each gains and loses, its "
        "fingerprint before and after with the capabilities enabled and the quality status "
        "after (FR-PROF-16), whether a backfill would be needed and whether its upgrade is "
        "unavailable. When the report has `warnings` and the body does not set "
        "`ignore_warnings` (`ignoreWarnings`), nothing is written and the answer is 200 with "
        "`applied: false` and the report; the `Idempotency-Key` is not kept, so the "
        "confirmation may reuse it. With `ignore_warnings`, or a report without warnings, the "
        "edit applies: `applied: true`, the report, and the profile in `result`. A "
        "data-producing increase (none is delivered in M3) cannot apply to a following story "
        "with history (a committed turn still on its timeline): that story keeps the version "
        "it ran on, and every other story moves to the new one. Settings that cannot run are "
        "refused whatever `ignore_warnings` says."
    ),
)
async def update_profile(
    profile_id: ProfileId,
    body: ProfileUpdate,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ProfileEditBody:
    edit = await engine.update_profile(ctx, profile_id, body)
    return answered(ProfileEditBody.of(edit), ctx, response, replayed=edit.replayed)


@router.post(
    "/{profile_id}/copy",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ProfileBody,
    operation_id="copy_profile",
    summary="Copy an engine profile",
    description=(
        "A new profile at version 1 with the settings of this profile's latest version "
        "(`version`: that one's), owned by `owner` (default: the requesting principal; never "
        "`system`). A preset or a deleted profile is copied like any other; the copy is an "
        "ordinary, live profile."
    ),
)
async def copy_profile(
    profile_id: ProfileId,
    body: ProfileCopy,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    version: VersionQuery = None,
) -> ProfileBody:
    write = await engine.copy_profile(ctx, profile_id, body, version=version)
    return answered(ProfileBody.of(write), ctx, response, replayed=write.replayed)


@router.post(
    "/{profile_id}/validate",
    response_model=ProfileValidation,
    operation_id="validate_profile",
    summary="Validate an engine profile's settings",
    description=(
        "Validate the body's `settings` (a dry run of an edit), or the profile's latest version "
        "when the call sends no body or a body without `settings`, against the delivered "
        "modules and the dependency graph (FR-PROF-2,4). A valid profile is answered 200; an "
        "invalid one is 422 `invalid_profile` naming the missing dependencies and the "
        "undelivered settings. Nothing is written: the call takes no `Idempotency-Key`."
    ),
    **route_flags(mutating=False),
)
async def validate_profile(
    profile_id: ProfileId,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[ProfileValidate | None, Body()] = None,
) -> ProfileValidation:
    return await engine.validate_profile(ctx, profile_id, body)


@router.get(
    "/{profile_id}/versions",
    response_model=ProfileVersionsBody,
    operation_id="list_profile_versions",
    summary="List an engine profile's versions",
    description="Every version of the profile, oldest first: its number, time and author.",
)
async def list_profile_versions(
    profile_id: ProfileId, ctx: ContextDep, engine: EngineDep
) -> ProfileVersionsBody:
    return ProfileVersionsBody(items=await engine.list_profile_versions(ctx, profile_id))


@router.delete(
    "/{profile_id}",
    response_model=ProfileDeletedBody,
    operation_id="delete_profile",
    summary="Delete an engine profile",
    description=(
        "Delete the profile (FR-PROF-1): it leaves listings (`include_deleted` lists it) and can "
        "no longer be made the default (422 `profile_deleted`), while every story that uses it "
        "keeps running on its last version. A fork of such a story runs under it too: a fork "
        "copies its story, profile included, as it copies a retired rating. The answer warns of "
        "those stories and of the scenarios that recommend the profile. The retention sweep "
        "purges it, with its versions, once no story uses it, forks included. The deployment's "
        "default profile cannot be deleted (409 `validation_error`, `reason: is_default`); a "
        "preset is deleted like any profile once it is not the default (FR-PROF-3). Deleting a "
        "deleted profile changes nothing."
    ),
)
async def delete_profile(
    profile_id: ProfileId, response: Response, ctx: ContextDep, engine: EngineDep
) -> ProfileDeletedBody:
    deleted = await engine.delete_profile(ctx, profile_id)
    return answered(ProfileDeletedBody.of(deleted), ctx, response, replayed=deleted.replayed)


mount_ownership_routes(router, EntityType.ENGINE_PROFILE)
