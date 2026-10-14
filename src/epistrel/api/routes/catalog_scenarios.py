"""``/catalog/scenarios``: scenarios, as drafts and immutable published versions (FR-SCN-1..4,10,
FR-API-6; #108). The same lifecycle as ``/catalog/characters``:

- ``POST /catalog/scenarios``: a new scenario whose draft holds the request's groups (draft
  revision 1, status ``draft``, the deployment's first listing status, ``private`` by
  default); 201 with the draft in ``full``.
- ``PATCH /catalog/scenarios/{id}``: a JSON merge patch over the draft with the
  ``draft_revision`` it is based on; 409 ``draft_stale`` when another save or publish came first.
- ``POST /catalog/scenarios/{id}/publish``: the draft becomes the next immutable version (201),
  once it validates (422 ``publish_invalid`` with ``details.report`` otherwise; #111).
- ``POST /catalog/scenarios/{id}/validate``: the same validation as a dry run, of the draft or a
  published version; ``{valid, report}``, nothing written, no ``Idempotency-Key`` (#111).
- ``GET /catalog/scenarios/{id}``: the latest published version (``?version=n`` another one,
  ``?draft=true`` the draft) as its public ``card`` (the default) or in ``full``.
- ``GET /catalog/scenarios/{id}/versions``: every published version, oldest first.
- ``POST /catalog/scenarios/{id}/archive`` and ``…/unarchive`` (200); ``DELETE
  /catalog/scenarios/{id}`` (204). A companion scenario (#116) keeps its slots, is pinned to its
  character's latest published version when published alone, and is archived, unarchived and
  deleted only with its character (422 ``companion_follows_character``).
- ``GET /catalog/scenarios``: list and search, a page of cards, companions only when asked
  (#110; ``routes.catalog_listing``).
- ``PUT /catalog/scenarios/{id}/listing-status``: the scenario's listing status (FR-CAT-11;
  #110); a companion's follows its character's (422 ``companion_follows_character``).
- ``PUT /catalog/scenarios/{id}/owner``, ``POST``/``GET …/relations`` and ``DELETE
  …/relations/{relation}/{principal}``: the scenario's owner and relations
  (``routes.ownership``, #104); a companion's follow its character's (#116). Nothing gates on
  them (FR-AUTH-8).

Every handler makes one ``Engine`` call. Writes are deployment-scoped: idempotent under
``Idempotency-Key``, answering the deployment's ``config_revision`` (which catalog writes leave
as it is). Nothing here reads or changes a story (FR-CAT-9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, ScenarioId, route_flags
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.api.routes.catalog_listing import (
    SET_LISTING_STATUS,
    ContentRatingParam,
    CursorParam,
    LabelParam,
    LimitParam,
    ListingStatusBody,
    ListingStatusParam,
    OwnerParam,
    ProjectionParam,
    QParam,
    RelationParam,
    StatusParam,
    list_description,
    page_fields,
)
from epistrel.api.routes.ownership import mount_ownership_routes
from epistrel.core.catalog.listing import ListingStatusRequest
from epistrel.core.catalog.report import ValidationReport, ValidationResult
from epistrel.core.catalog.scenario_models import (
    ScenarioCard,
    ScenarioCreate,
    ScenarioFull,
    ScenarioListItem,
    ScenarioPatch,
)
from epistrel.core.catalog.scenarios import ScenarioDraftWrite, ScenarioStatusChange
from epistrel.core.catalog.search import DEFAULT_PAGE as DEFAULT_LIST_PAGE
from epistrel.core.catalog.search import CatalogPage
from epistrel.core.catalog.service import (
    REVISION_MAX,
    Projection,
    Published,
    PublishRequest,
    ValidateRequest,
    VersionSummary,
)
from epistrel.core.enums import EntryStatus
from epistrel.core.tables.ownership import EntityType

router = APIRouter(prefix="/catalog/scenarios", tags=["catalog"])


class ScenarioDraftBody(ScenarioFull, DeploymentMutationResponse):
    """The scenario's draft after a create or save, in ``full``: ``draft_revision`` is the
    revision the next save or publish must send; ``config_revision`` is the deployment's
    (unchanged)."""

    draft_revision: int

    @classmethod
    def of(cls, write: ScenarioDraftWrite) -> ScenarioDraftBody:
        return cls(**write.scenario.model_dump(), config_revision=write.config_revision)


class ScenarioPublishedBody(DeploymentMutationResponse):
    """The version a scenario's publish made: its number, the draft's new ``draft_revision``
    (the draft still equals what was published), the scenario's ``status``, when it was
    published, and the validation ``report`` (no errors; its warnings and the checks this
    deployment cannot run), which the version keeps."""

    id: str
    version: int
    draft_revision: int
    status: EntryStatus
    published_at: datetime
    report: ValidationReport

    @classmethod
    def of(cls, published: Published) -> ScenarioPublishedBody:
        return cls(
            id=published.id,
            version=published.version,
            draft_revision=published.draft_revision,
            status=published.status,
            published_at=published.published_at,
            report=published.report,
            config_revision=published.config_revision,
        )


class ScenarioStatusBody(DeploymentMutationResponse):
    """The scenario after archive or unarchive: its status, listing status, latest published
    version and when it was published, its timestamps, and the character it is the companion
    of (``null`` for an authored scenario; #116)."""

    id: str
    status: EntryStatus
    listing_status: str
    current_version: int | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    companion_of: str | None

    @classmethod
    def of(cls, change: ScenarioStatusChange) -> ScenarioStatusBody:
        entry = change.scenario
        return cls(
            id=entry.id,
            status=entry.status,
            listing_status=entry.listing_status,
            current_version=entry.current_version,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            published_at=entry.published_at,
            companion_of=entry.companion_of,
            config_revision=change.config_revision,
        )


class ScenarioVersionBody(BaseModel):
    """One published version of a scenario: its number, when and by which principal it was
    published."""

    version: int
    published_at: datetime
    published_by: str


class ScenarioVersionsBody(BaseModel):
    """Every published version of a scenario, oldest first."""

    items: list[ScenarioVersionBody]

    @classmethod
    def of(cls, found: list[VersionSummary]) -> ScenarioVersionsBody:
        return cls(
            items=[
                ScenarioVersionBody(
                    version=v.version, published_at=v.published_at, published_by=v.published_by
                )
                for v in found
            ]
        )


class ScenarioListBody(BaseModel):
    """A page of scenarios as cards (#110), newest first (a search: best match first), and the
    cursor of the next page (``null`` on the last). No total."""

    items: list[ScenarioListItem]
    next_cursor: str | None

    @classmethod
    def of(cls, page: CatalogPage) -> ScenarioListBody:
        return cls(**page_fields(page))


@router.get(
    "",
    response_model=ScenarioListBody,
    operation_id="search_catalog_scenario",
    summary="List and search scenarios",
    description=list_description(
        "scenarios",
        "the name, labels, every role's public label and the public description",
        "Companion scenarios (a character's, `companion_of` set) are listed only with "
        "`include_companions=true`; they still read directly. ",
    ),
)
async def search_catalog_scenario(
    ctx: ContextDep,
    engine: EngineDep,
    q: QParam = None,
    label: LabelParam = None,
    content_rating: ContentRatingParam = None,
    listing_status: ListingStatusParam = None,
    owner: OwnerParam = None,
    relation: RelationParam = None,
    status: StatusParam = None,
    include_companions: Annotated[
        bool, Query(description="List companion scenarios too (default: authored ones only).")
    ] = False,
    limit: LimitParam = DEFAULT_LIST_PAGE,
    cursor: CursorParam = None,
    projection: ProjectionParam = "card",
) -> ScenarioListBody:
    page = await engine.search_catalog(
        ctx,
        EntityType.SCENARIO,
        q=q,
        label=label or (),
        content_rating=content_rating,
        listing_status=listing_status,
        owner=owner,
        relation=relation or (),
        status=status,
        include_companions=include_companions,
        limit=limit,
        cursor=cursor,
    )
    return ScenarioListBody.of(page)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ScenarioDraftBody,
    operation_id="create_scenario_draft",
    summary="Create a scenario (a draft)",
    description=(
        "Create a scenario whose draft holds the given groups: draft revision 1, status "
        "`draft`, and the first of the deployment's `listing_status_values` (`private` by "
        "default) as its listing status. `owner` names its owner principal (default: the "
        "requesting principal; never `system`). The draft must keep the save rules (422 "
        "`validation_error` at the field): unique, unreserved role ids; at most one player "
        "role, `open`; a `locked` or `default` role names its `character` and an `open` one "
        "none; relationships, holders, starting locations and the opening name declared roles "
        "and locations. A `max_characters_at_start` above the deployment's ceiling is 422 "
        "`cast_limit_exceeded`; a content rating or media asset that cannot be referenced is "
        "422 `rating_retired` or `media_retired`. Each seed gets an `id`. Answers 201 with the "
        "draft in `full`. A draft larger than 1 MiB is 413 `draft_too_large`."
    ),
)
async def create_scenario_draft(
    body: ScenarioCreate, response: Response, ctx: ContextDep, engine: EngineDep
) -> ScenarioDraftBody:
    write = await engine.create_scenario_draft(ctx, body)
    return answered(ScenarioDraftBody.of(write), ctx, response, replayed=write.replayed)


@router.patch(
    "/{scenario_id}",
    response_model=ScenarioDraftBody,
    operation_id="save_scenario_draft",
    summary="Save the scenario's draft (a JSON merge patch)",
    description=(
        "Change the draft with a JSON merge patch (RFC 7386) over its groups: a key sets that "
        "field, `null` removes it, an object merges and an array (`roles`, `seeded_memories`, "
        "…) replaces the stored one; unknown keys are 422. The merged draft keeps the save "
        "rules, as on create. `draft_revision` is the revision the patch is based on: when "
        "another save or publish came first it is 409 `draft_stale` "
        "(`details.current_draft_revision`) and nothing changes (FR-CAT-3). Every save moves "
        "the draft to the next revision. An archived scenario is 422 `entry_archived`. A "
        "companion scenario (FR-CAT-5) keeps its two slots: a patch that adds, removes or "
        "changes a role's `id`, `kind`, `fill` or `character` (roles are matched by `id`, so "
        "their order is free; a slot echoed unchanged is accepted) is 422 "
        "`companion_slots_fixed` (`details.role`, `details.field`), before any other rule; "
        "every other field is editable. A patch that names a companion's "
        "`metadata.content_rating_id` sets its rating explicitly: until then it takes its "
        "character's."
    ),
)
async def save_scenario_draft(
    scenario_id: ScenarioId,
    body: ScenarioPatch,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ScenarioDraftBody:
    write = await engine.save_scenario_draft(ctx, scenario_id, body)
    return answered(ScenarioDraftBody.of(write), ctx, response, replayed=write.replayed)


@router.post(
    "/{scenario_id}/publish",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ScenarioPublishedBody,
    operation_id="publish_scenario",
    summary="Publish the scenario's draft as a new version",
    description=(
        "Publish the draft as the next immutable version (FR-SCN-1): `draft_revision` is "
        "compared like a save's (409 `draft_stale`). The draft is validated first (FR-SCN-5): "
        "any error is 422 `publish_invalid` with `details.report` (a `ValidationReport`) and "
        "nothing is written. Errors include a missing name, content rating, role, world "
        "description or opening narration; a role limit exceeded (`cast_limit_exceeded`); a "
        "`locked` or `default` role's character missing, unpublished, archived or failing the "
        "role's eligibility; a seed without its holder, audience or disclosure "
        "(`seed_missing_holder`, `seed_missing_audience`, `seed_missing_disclosure`); a media "
        "asset missing, retired, of a kind, type or size not "
        "allowed; and an undeclared provider. Warnings never block: a looser-rated character "
        "or asset (`rating_looser`), text that fails an input policy (`input_policy`), and role "
        "text that asserts personality (`personality_in_role`). The 201 answer carries the "
        "report, with the checks this deployment cannot run listed in `unavailable`, and the "
        "version keeps it. The draft stays equal to what was published, at the next revision. "
        "Publishing an unchanged draft still makes a new version. A companion scenario "
        "published alone (FR-CAT-10) is pinned to its character's latest published version, "
        "never to a character draft (`pinned_character_version`); before the character's "
        "first publish it is refused with `character_not_published`, and a rating that differs "
        "from the character's is a `rating_looser` warning."
    ),
)
async def publish_scenario(
    scenario_id: ScenarioId,
    body: PublishRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ScenarioPublishedBody:
    published = await engine.publish_scenario(ctx, scenario_id, body)
    return answered(ScenarioPublishedBody.of(published), ctx, response, replayed=published.replayed)


@router.post(
    "/{scenario_id}/validate",
    response_model=ValidationResult,
    operation_id="validate_scenario",
    summary="Validate a scenario (a dry run)",
    description=(
        "Validate the scenario's draft, or with `version` one of its published versions, as "
        "the catalog and this deployment stand now (FR-SCN-5): the same checks and the same "
        "report a publish runs, answered 200 `{valid, report}` (`valid`: the report has no "
        "error). Every check this deployment can run is run; the others are listed in "
        "`report.unavailable` (in M3 `seed_normalization`, `fact_consistency` and "
        "`state_schema`). The body's optional `draft_revision` names the draft revision the "
        "caller holds: when the draft has moved on it is 409 `draft_stale` (not with "
        "`version`: 422). Archived scenarios validate too. Nothing is written: no version, no "
        "operation, and the call takes no `Idempotency-Key`."
    ),
    **route_flags(mutating=False),
)
async def validate_scenario(
    scenario_id: ScenarioId,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[ValidateRequest | None, Body()] = None,
    version: Annotated[
        int | None,
        Query(ge=1, le=REVISION_MAX, description="A published version to validate instead."),
    ] = None,
) -> ValidationResult:
    return await engine.validate_scenario(ctx, scenario_id, body, version=version)


@router.get(
    "/{scenario_id}",
    response_model=ScenarioCard | ScenarioFull,
    operation_id="get_scenario",
    summary="Read a scenario",
    description=(
        "The latest published version; `version` reads another one (404 when it does not "
        "exist) and `draft=true` the draft, with its `draft_revision` (not both: 422). Before "
        "the first publish, a read without `draft=true` is 422 `not_published`. `projection` "
        "is `card` (the default: the public card only — name, public description, media, "
        "labels, content rating, and each role's id, public label, setup description, kind "
        "and fill mode) or `full` (every field; FR-SCN-10). Archived scenarios read as any "
        "other."
    ),
)
async def get_scenario(
    scenario_id: ScenarioId,
    ctx: ContextDep,
    engine: EngineDep,
    version: Annotated[
        int | None, Query(ge=1, le=REVISION_MAX, description="A published version's number.")
    ] = None,
    draft: Annotated[bool, Query(description="Read the draft instead of a version.")] = False,
    projection: Annotated[
        Projection, Query(description="`card` (the default) or `full` (FR-SCN-10).")
    ] = Projection.CARD,
) -> ScenarioCard | ScenarioFull:
    return await engine.get_scenario(
        ctx, scenario_id, version=version, draft=draft, projection=projection
    )


@router.get(
    "/{scenario_id}/versions",
    response_model=ScenarioVersionsBody,
    operation_id="list_scenario_versions",
    summary="List a scenario's published versions",
    description="Every published version, oldest first: its number, time and publisher.",
)
async def list_scenario_versions(
    scenario_id: ScenarioId, ctx: ContextDep, engine: EngineDep
) -> ScenarioVersionsBody:
    return ScenarioVersionsBody.of(await engine.list_scenario_versions(ctx, scenario_id))


@router.post(
    "/{scenario_id}/archive",
    response_model=ScenarioStatusBody,
    operation_id="archive_scenario",
    summary="Archive a scenario",
    description=(
        "Archive the scenario: it still reads in both projections, but its draft cannot be "
        "saved or published (422 `entry_archived`), and it cannot be used to create a story, "
        "until it is unarchived. Stories created from it are untouched (FR-CAT-9). Archiving "
        "an archived scenario changes nothing (200). A companion scenario is archived only with "
        "its character: 422 `companion_follows_character`."
    ),
)
async def archive_scenario(
    scenario_id: ScenarioId, response: Response, ctx: ContextDep, engine: EngineDep
) -> ScenarioStatusBody:
    change = await engine.archive_scenario(ctx, scenario_id)
    return answered(ScenarioStatusBody.of(change), ctx, response, replayed=change.replayed)


@router.post(
    "/{scenario_id}/unarchive",
    response_model=ScenarioStatusBody,
    operation_id="unarchive_scenario",
    summary="Unarchive a scenario",
    description=(
        "Restore an archived scenario to `published` (`draft` if it was never published); any "
        "other is left as it is (200). A companion scenario is unarchived only with its "
        "character: 422 `companion_follows_character`."
    ),
)
async def unarchive_scenario(
    scenario_id: ScenarioId, response: Response, ctx: ContextDep, engine: EngineDep
) -> ScenarioStatusBody:
    change = await engine.unarchive_scenario(ctx, scenario_id)
    return answered(ScenarioStatusBody.of(change), ctx, response, replayed=change.replayed)


@router.put(
    "/{scenario_id}/listing-status",
    response_model=ListingStatusBody,
    operation_id="set_listing_status_scenario",
    summary="Set a scenario's listing status",
    description=SET_LISTING_STATUS
    + " A companion scenario's listing status follows its character's: 422 "
    "`companion_follows_character` (change it on the character).",
)
async def set_listing_status_scenario(
    scenario_id: ScenarioId,
    body: ListingStatusRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ListingStatusBody:
    change = await engine.set_listing_status(ctx, EntityType.SCENARIO, scenario_id, body)
    return answered(ListingStatusBody.of(change), ctx, response, replayed=change.replayed)


@router.delete(
    "/{scenario_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_scenario",
    summary="Delete a scenario",
    description=(
        "Remove the scenario and every published version, from any status (204), recorded in "
        "the audit log. Stories created from it keep their snapshot and their provenance "
        "(FR-CAT-9). A retry under the same `Idempotency-Key` is a 204 with "
        "`Idempotent-Replay: true`. A companion scenario is deleted only with its character: "
        "422 `companion_follows_character`."
    ),
)
async def delete_scenario(scenario_id: ScenarioId, ctx: ContextDep, engine: EngineDep) -> Response:
    deleted = await engine.delete_scenario(ctx, scenario_id)
    return no_content(ctx, replayed=deleted.replayed)


mount_ownership_routes(router, EntityType.SCENARIO)
