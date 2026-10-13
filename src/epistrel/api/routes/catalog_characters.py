"""``/catalog/characters``: catalog characters, as drafts and immutable published versions
(FR-CAT-1,3,9, FR-SCN-10, FR-API-6; #103).

- ``POST /catalog/characters``: a new entry whose draft holds the request's fields (draft
  revision 1, status ``draft``, the deployment's first listing status, ``private`` by
  default); 201 with the draft in ``full``.
- ``PATCH /catalog/characters/{id}``: a JSON merge patch over the draft with the
  ``draft_revision`` it is based on; 409 ``draft_stale`` when another save or publish came first.
- ``POST /catalog/characters/{id}/publish``: the draft becomes the next immutable version (201).
- ``GET /catalog/characters/{id}``: the latest published version (``?version=n`` another one,
  ``?draft=true`` the draft) as its public ``card`` (the default) or in ``full``.
- ``GET /catalog/characters/{id}/versions``: every published version, oldest first.
- ``POST /catalog/characters/{id}/archive`` and ``…/unarchive`` (200); ``DELETE
  /catalog/characters/{id}`` (204).
- ``GET /catalog/characters``: list and search, a page of cards (#110; ``routes.catalog_listing``).
- ``PUT /catalog/characters/{id}/listing-status``: the entry's listing status (FR-CAT-11; #110).
- ``PUT /catalog/characters/{id}/owner``, ``POST``/``GET …/relations`` and ``DELETE
  …/relations/{relation}/{principal}``: the entry's owner and relations (``routes.ownership``,
  #104). Nothing gates on them (FR-AUTH-8).

Every handler makes one ``Engine`` call. Writes are deployment-scoped: idempotent under
``Idempotency-Key``, answering the deployment's ``config_revision`` (which catalog writes leave
as it is). Nothing here reads or changes a story (FR-CAT-9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import CharacterId, ContextDep, EngineDep
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
from epistrel.core.catalog.characters import (
    REVISION_MAX,
    CharacterCard,
    CharacterCreate,
    CharacterFull,
    CharacterListItem,
    CharacterPatch,
    DraftWrite,
    Projection,
    Published,
    PublishReport,
    PublishRequest,
    ReportEntry,
    StatusChange,
    VersionSummary,
)
from epistrel.core.catalog.listing import ListingStatusRequest
from epistrel.core.catalog.search import DEFAULT_PAGE as DEFAULT_LIST_PAGE
from epistrel.core.catalog.search import CatalogPage
from epistrel.core.enums import EntryStatus
from epistrel.core.tables.ownership import EntityType

router = APIRouter(prefix="/catalog/characters", tags=["catalog"])


class CharacterDraftBody(CharacterFull, DeploymentMutationResponse):
    """The draft after a create or save, in ``full``: ``draft_revision`` is the revision the
    next save or publish must send; ``config_revision`` is the deployment's (unchanged)."""

    draft_revision: int

    @classmethod
    def of(cls, write: DraftWrite) -> CharacterDraftBody:
        return cls(**write.character.model_dump(), config_revision=write.config_revision)


class ReportEntryBody(BaseModel):
    """One finding of the publish validation: a stable ``code``, the draft ``field`` it concerns
    (a dotted path, e.g. ``definition.personality``) and a message."""

    code: str
    field: str
    message: str

    @classmethod
    def of(cls, entry: ReportEntry) -> ReportEntryBody:
        return cls(code=entry.code, field=entry.field, message=entry.message)


class PublishReportBody(BaseModel):
    """What the publish validation found: ``errors`` (none on a publish that succeeded; a 422
    ``publish_invalid`` carries them in ``details.report``), ``warnings``, and the names of
    checks that could not run (``unavailable``)."""

    errors: list[ReportEntryBody]
    warnings: list[ReportEntryBody]
    unavailable: list[str]

    @classmethod
    def of(cls, report: PublishReport) -> PublishReportBody:
        return cls(
            errors=[ReportEntryBody.of(e) for e in report.errors],
            warnings=[ReportEntryBody.of(e) for e in report.warnings],
            unavailable=list(report.unavailable),
        )


class CharacterPublishedBody(DeploymentMutationResponse):
    """The version a publish made: its number, the draft's new ``draft_revision`` (the draft
    still equals what was published), the entry's ``status``, when it was published, and the
    validation ``report``."""

    id: str
    version: int
    draft_revision: int
    status: EntryStatus
    published_at: datetime
    report: PublishReportBody

    @classmethod
    def of(cls, published: Published) -> CharacterPublishedBody:
        return cls(
            id=published.id,
            version=published.version,
            draft_revision=published.draft_revision,
            status=published.status,
            published_at=published.published_at,
            report=PublishReportBody.of(published.report),
            config_revision=published.config_revision,
        )


class CharacterStatusBody(DeploymentMutationResponse):
    """The entry after archive or unarchive: its status, listing status, latest published
    version and when it was published, its timestamps and its companion (``null`` until #116)."""

    id: str
    status: EntryStatus
    listing_status: str
    current_version: int | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    companion_id: str | None

    @classmethod
    def of(cls, change: StatusChange) -> CharacterStatusBody:
        entry = change.character
        return cls(
            id=entry.id,
            status=entry.status,
            listing_status=entry.listing_status,
            current_version=entry.current_version,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            published_at=entry.published_at,
            companion_id=entry.companion_id,
            config_revision=change.config_revision,
        )


class CharacterVersionBody(BaseModel):
    """One published version: its number, when and by which principal it was published."""

    version: int
    published_at: datetime
    published_by: str


class CharacterVersionsBody(BaseModel):
    """Every published version of a catalog character, oldest first."""

    items: list[CharacterVersionBody]

    @classmethod
    def of(cls, found: list[VersionSummary]) -> CharacterVersionsBody:
        return cls(
            items=[
                CharacterVersionBody(
                    version=v.version, published_at=v.published_at, published_by=v.published_by
                )
                for v in found
            ]
        )


class CharacterListBody(BaseModel):
    """A page of catalog characters as cards (#110), newest first (a search: best match
    first), and the cursor of the next page (``null`` on the last). No total."""

    items: list[CharacterListItem]
    next_cursor: str | None

    @classmethod
    def of(cls, page: CatalogPage) -> CharacterListBody:
        return cls(**page_fields(page))


@router.get(
    "",
    response_model=CharacterListBody,
    operation_id="search_catalog_character",
    summary="List and search catalog characters",
    description=list_description(
        "catalog characters", "the name, aliases, labels and public description"
    ),
)
async def search_catalog_character(
    ctx: ContextDep,
    engine: EngineDep,
    q: QParam = None,
    label: LabelParam = None,
    content_rating: ContentRatingParam = None,
    listing_status: ListingStatusParam = None,
    owner: OwnerParam = None,
    relation: RelationParam = None,
    status: StatusParam = None,
    limit: LimitParam = DEFAULT_LIST_PAGE,
    cursor: CursorParam = None,
    projection: ProjectionParam = "card",
) -> CharacterListBody:
    page = await engine.search_catalog(
        ctx,
        EntityType.CHARACTER,
        q=q,
        label=label or (),
        content_rating=content_rating,
        listing_status=listing_status,
        owner=owner,
        relation=relation or (),
        status=status,
        limit=limit,
        cursor=cursor,
    )
    return CharacterListBody.of(page)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=CharacterDraftBody,
    operation_id="create_character_draft",
    summary="Create a catalog character (a draft)",
    description=(
        "Create a catalog character whose draft holds the given fields: draft revision 1, "
        "status `draft`, and the first of the deployment's `listing_status_values` (`private` "
        "by default) as its listing status. `owner` names its owner principal (default: the "
        "requesting principal; never `system`). Answers 201 with the draft in `full`. A draft "
        "larger than 256 KiB is 413 `draft_too_large`."
    ),
)
async def create_character_draft(
    body: CharacterCreate, response: Response, ctx: ContextDep, engine: EngineDep
) -> CharacterDraftBody:
    write = await engine.create_character_draft(ctx, body)
    return answered(CharacterDraftBody.of(write), ctx, response, replayed=write.replayed)


@router.patch(
    "/{character_id}",
    response_model=CharacterDraftBody,
    operation_id="save_character_draft",
    summary="Save the draft (a JSON merge patch)",
    description=(
        "Change the draft with a JSON merge patch (RFC 7386): a key sets that field, `null` "
        "removes it, and `definition` merges the same way; unknown keys are 422. "
        "`draft_revision` is the revision the patch is based on: when another save or publish "
        "came first it is 409 `draft_stale` (`details.current_draft_revision`) and nothing "
        "changes (FR-CAT-3). Every save moves the draft to the next revision. An archived entry "
        "is 422 `entry_archived`."
    ),
)
async def save_character_draft(
    character_id: CharacterId,
    body: CharacterPatch,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CharacterDraftBody:
    write = await engine.save_character_draft(ctx, character_id, body)
    return answered(CharacterDraftBody.of(write), ctx, response, replayed=write.replayed)


@router.post(
    "/{character_id}/publish",
    status_code=http_status.HTTP_201_CREATED,
    response_model=CharacterPublishedBody,
    operation_id="publish_character",
    summary="Publish the draft as a new version",
    description=(
        "Publish the draft as the next immutable version (FR-CAT-3): `draft_revision` is "
        "compared like a save's (409 `draft_stale`). The draft must be publishable (422 "
        "`publish_invalid` with `details.report`: it needs a `name`). The draft stays equal to "
        "what was published, at the next revision. Publishing an unchanged draft still makes a "
        "new version."
    ),
)
async def publish_character(
    character_id: CharacterId,
    body: PublishRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CharacterPublishedBody:
    published = await engine.publish_character(ctx, character_id, body)
    return answered(
        CharacterPublishedBody.of(published), ctx, response, replayed=published.replayed
    )


@router.get(
    "/{character_id}",
    response_model=CharacterCard | CharacterFull,
    operation_id="get_character",
    summary="Read a catalog character",
    description=(
        "The latest published version; `version` reads another one (404 when it does not "
        "exist) and `draft=true` the draft, with its `draft_revision` (not both: 422). Before "
        "the first publish, a read without `draft=true` is 422 `not_published`. `projection` "
        "is `card` (the default: only the public card, never a definition field) or `full` "
        "(FR-SCN-10). Archived entries read as any other."
    ),
)
async def get_character(
    character_id: CharacterId,
    ctx: ContextDep,
    engine: EngineDep,
    version: Annotated[
        int | None, Query(ge=1, le=REVISION_MAX, description="A published version's number.")
    ] = None,
    draft: Annotated[bool, Query(description="Read the draft instead of a version.")] = False,
    projection: Annotated[
        Projection, Query(description="`card` (the default) or `full` (FR-SCN-10).")
    ] = Projection.CARD,
) -> CharacterCard | CharacterFull:
    return await engine.get_character(
        ctx, character_id, version=version, draft=draft, projection=projection
    )


@router.get(
    "/{character_id}/versions",
    response_model=CharacterVersionsBody,
    operation_id="list_character_versions",
    summary="List a catalog character's published versions",
    description="Every published version, oldest first: its number, time and publisher.",
)
async def list_character_versions(
    character_id: CharacterId, ctx: ContextDep, engine: EngineDep
) -> CharacterVersionsBody:
    return CharacterVersionsBody.of(await engine.list_character_versions(ctx, character_id))


@router.post(
    "/{character_id}/archive",
    response_model=CharacterStatusBody,
    operation_id="archive_character",
    summary="Archive a catalog character",
    description=(
        "Archive the entry: it still reads in both projections, but its draft cannot be saved "
        "or published (422 `entry_archived`), and it cannot be used to create a story, until "
        "it is unarchived. Stories created from it are untouched (FR-CAT-9). Archiving an "
        "archived entry changes nothing (200)."
    ),
)
async def archive_character(
    character_id: CharacterId, response: Response, ctx: ContextDep, engine: EngineDep
) -> CharacterStatusBody:
    change = await engine.archive_character(ctx, character_id)
    return answered(CharacterStatusBody.of(change), ctx, response, replayed=change.replayed)


@router.post(
    "/{character_id}/unarchive",
    response_model=CharacterStatusBody,
    operation_id="unarchive_character",
    summary="Unarchive a catalog character",
    description=(
        "Restore an archived entry to `published` (`draft` if it was never published); any "
        "other entry is left as it is (200)."
    ),
)
async def unarchive_character(
    character_id: CharacterId, response: Response, ctx: ContextDep, engine: EngineDep
) -> CharacterStatusBody:
    change = await engine.unarchive_character(ctx, character_id)
    return answered(CharacterStatusBody.of(change), ctx, response, replayed=change.replayed)


@router.put(
    "/{character_id}/listing-status",
    response_model=ListingStatusBody,
    operation_id="set_listing_status_character",
    summary="Set a catalog character's listing status",
    description=SET_LISTING_STATUS + " The character's companion scenarios change with it.",
)
async def set_listing_status_character(
    character_id: CharacterId,
    body: ListingStatusRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ListingStatusBody:
    change = await engine.set_listing_status(ctx, EntityType.CHARACTER, character_id, body)
    return answered(ListingStatusBody.of(change), ctx, response, replayed=change.replayed)


@router.delete(
    "/{character_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_character",
    summary="Delete a catalog character",
    description=(
        "Remove the entry and every published version, from any status (204), recorded in the "
        "audit log. Stories created from it keep their snapshot and their provenance "
        "(`source_catalog_id@version`; FR-CAT-9). A retry under the same `Idempotency-Key` is a "
        "204 with `Idempotent-Replay: true`."
    ),
)
async def delete_character(
    character_id: CharacterId, ctx: ContextDep, engine: EngineDep
) -> Response:
    deleted = await engine.delete_character(ctx, character_id)
    return no_content(ctx, replayed=deleted.replayed)


mount_ownership_routes(router, EntityType.CHARACTER)
