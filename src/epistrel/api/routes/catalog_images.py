"""A catalog entry's generated image candidates (FR-IMG-4,7, FR-MEDIA-3; #260).

Per kind, under ``/catalog/characters/{character_id}`` and ``/catalog/scenarios/{scenario_id}``
(docs/02 §9), one route per kind sharing one handler, as #121's rating-suggestion routes do:

- ``POST …/images/generate``: ``{slot, n?}``; 202 ``{job_id, status: queued}`` with ``Location:
  /image-jobs/{id}``. Idempotent under ``Idempotency-Key`` (deployment-scoped).
- ``GET …/images/candidates`` (``?job_id=``, keyset pages): the entry's live candidates.
- ``GET …/images/candidates/{candidate_id}/content``: the bytes, with ``Range`` (#107),
  ``Cache-Control: private, no-store``.
- ``GET …/images/candidates/{candidate_id}/url``: a short-lived URL (S3's presigned GET, or on
  the local store the signed ``GET /media/{asset_id}/content``: invariant 4's one signed
  route, ADR-152; no new unauthenticated route).
- ``DELETE …/images/candidates/{candidate_id}``: discard; 204, idempotent, no key.

Attaching is not a route of its own: it is an ordinary draft save naming ``{candidate_id}`` in an
image slot (``PATCH /catalog/characters/{id}``, ``PATCH /catalog/scenarios/{id}``; docs/02
§4.13). Every route needs only the service credential: ownership gates nothing (FR-AUTH-8).
Every handler makes one ``Engine`` call.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response
from fastapi import status as http_status

from epistrel.api.deps import CharacterId, ContextDep, EngineDep, ScenarioId, route_flags
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.api.routes.media import (
    CONTENT_HEADERS_DOC,
    TTL_MAX,
    IfNoneMatchHeader,
    IfRangeHeader,
    MediaUrlBody,
    RangeHeader,
    content_response,
    entity_tags,
    parse_range,
)
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.images.candidates import LIST_DEFAULT, LIST_MAX, CandidatePage
from epistrel.core.media.checks import CHECK_STATEMENTS
from epistrel.orchestrator.images.targets.catalog import CatalogImageRequest, CatalogImagesQueued

characters = APIRouter(prefix="/catalog/characters", tags=["images"])
scenarios = APIRouter(prefix="/catalog/scenarios", tags=["images"])

CandidateId = Annotated[
    str,
    Path(pattern=ULID_PATTERN, max_length=26, description="The candidate's id (a ULID)."),
]
JobIdQuery = Annotated[str | None, Query(max_length=64, description="Only this job's candidates.")]
LimitQuery = Annotated[
    int, Query(ge=1, le=LIST_MAX, description=f"Candidates a page, 1 to {LIST_MAX}.")
]
CursorQuery = Annotated[
    str | None, Query(max_length=128, description="The previous page's `next_cursor`.")
]
TtlQuery = Annotated[
    int | None,
    Query(ge=1, le=TTL_MAX, description="Seconds the URL works (at most `media_signed_url_ttl`)."),
]


class CatalogImagesAccepted(DeploymentMutationResponse):
    """A catalog image request's answer (202): the job to follow at ``Location``."""

    job_id: str
    status: str

    @classmethod
    def of(cls, queued: CatalogImagesQueued) -> CatalogImagesAccepted:
        return cls(
            job_id=queued.job_id, status=queued.status, config_revision=queued.config_revision
        )


GENERATE = (
    "Generates `n` private candidate images (1 to 10; omitted: the deployment's "
    "`catalog_candidates_n`, 4 by default) for the {noun}'s {slots} image (FR-IMG-7), built "
    "from its draft's perceivable inputs, like `POST /image-prompts` (#259), under its rating's "
    "image guidance and image policies. 202 `{{job_id, status: queued}}` with `Location: "
    "/image-jobs/{{id}}`: follow the job there; it ends `candidates_ready` with the "
    "`candidate_ids`, or `failed` when no output passed its checks, or `target_gone` when the "
    "entry was deleted meanwhile. The job records the draft revision and image-input "
    "fingerprint it was built from. **Nothing is attached**: each candidate is private (listed "
    "and previewed only through `…/images/candidates`) until an author attaches it with an "
    "ordinary draft save naming `{{candidate_id}}` in an image slot with the current "
    "`draft_revision`; unattached candidates are deleted after `candidate_ttl` (7 days by "
    "default). The request never saves or advances the draft.\n\n"
    "Refused before any job, reservation or provider call: 404 for an unknown {noun}; 422 "
    "`entry_archived`; 409 `not_configured` without an image adapter; 409 "
    "`image_check_unconfigured` while an image-output policy applies and `image_check` is "
    "unset; 422 `validation_error` for a slot the {noun}'s images cannot have or an `n` "
    "outside 1 to 10, and for a draft lacking its true age, rating or appearance; 422 "
    "`guardrail_violation` when the built prompt violates an image-prompt policy; 503 "
    "`image_prompt_unavailable`. Idempotent under `Idempotency-Key` (deployment-scoped)."
)
LIST = (
    "The {noun}'s live candidates (FR-IMG-7), oldest first (generation order), in keyset "
    "pages: `job_id` narrows to one job's (an unknown job answers an empty page). Each item "
    "is `{{candidate_id, slot, job_id, draft_revision, expires_at, stale, media: {{mime_type, "
    "size, width, height, source, check_status, check, generation_metadata}}}}`; `stale` is "
    "true when its image-input fingerprint differs from the draft's now, so an attach would be "
    "refused 409 `candidate_stale`. A candidate past `expires_at` is left out even before the "
    "expiry task deletes it. Candidates never appear in `GET /media`, a card or full "
    "projection, or a story. 404 for an unknown {noun}. " + CHECK_STATEMENTS
)
CONTENT = (
    "A live candidate's bytes with its MIME type, `ETag` (its SHA-256), `Accept-Ranges: "
    "bytes` and `Cache-Control: private, no-store`. One `Range` is 206 with `Content-Range`, "
    "one no byte of which exists 416; `If-None-Match` matching the `ETag` is 304; `If-Range` "
    "applies the range only when it names the `ETag` (#107). Needs the service credential. 404 "
    "`not_found` for an unknown {noun}, and for a candidate that is unknown, another entry's, "
    "attached, discarded or expired alike."
)
URL = (
    "A URL that serves a live candidate's bytes without the service credential until "
    "`expires_at` (FR-MEDIA-3): on an S3-compatible store the store's presigned GET; on the "
    "local store Epistrel's signed `GET /media/{{asset_id}}/content?exp=&sig=` under "
    "`public_base_url` (the one signed route, ADR-152; a credentialed `GET /media/{{id}}` or "
    "`…/content` of a candidate stays 404). It works for `ttl` seconds, at most and by default "
    "`media_signed_url_ttl`. It stops serving once the candidate is discarded or expires; "
    "after an attach it serves until its expiry. 404 as the content read; 503 "
    "`config_missing` without `public_base_url`, `config_key_missing` without "
    "`EPISTREL_CONFIG_KEY`."
)
DISCARD = (
    "Discards a candidate before it expires (FR-IMG-7): its candidate record and asset are "
    "deleted, and its bytes unless another asset has the same checksum. 204 with no body. "
    "Idempotent by design, so it takes no `Idempotency-Key`: an unknown {noun} or candidate, "
    "or one already discarded or attached, also answers 204 and changes nothing (an attached "
    "candidate's asset is never touched)."
)
_CONTENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    http_status.HTTP_200_OK: {
        "description": "The candidate's bytes.",
        "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
        "headers": CONTENT_HEADERS_DOC,
    },
    http_status.HTTP_206_PARTIAL_CONTENT: {
        "description": "The requested byte range.",
        "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
        "headers": {
            **CONTENT_HEADERS_DOC,
            "Content-Range": {
                "description": "`bytes <first>-<last>/<size>`.",
                "schema": {"type": "string"},
            },
        },
    },
    http_status.HTTP_304_NOT_MODIFIED: {
        "description": "`If-None-Match` named the candidate's `ETag`: no body.",
        "headers": CONTENT_HEADERS_DOC,
    },
}


def _entry(kind: str, entry_id: str) -> tuple[str | None, str | None]:
    """``(character_id, scenario_id)``: the library call's entry."""
    return (entry_id, None) if kind == "character" else (None, entry_id)


async def _generate(
    kind: str,
    entry_id: str,
    body: CatalogImageRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CatalogImagesAccepted:
    c, sc = _entry(kind, entry_id)
    queued = await engine.request_catalog_images(ctx, body, character_id=c, scenario_id=sc)
    response.headers["Location"] = queued.location
    return answered(CatalogImagesAccepted.of(queued), ctx, response, replayed=queued.replayed)


async def _list(
    kind: str,
    entry_id: str,
    ctx: ContextDep,
    engine: EngineDep,
    job_id: str | None,
    limit: int,
    cursor: str | None,
) -> CandidatePage:
    c, sc = _entry(kind, entry_id)
    return await engine.list_image_candidates(
        ctx, character_id=c, scenario_id=sc, job_id=job_id, limit=limit, cursor=cursor
    )


async def _content(
    kind: str,
    entry_id: str,
    candidate_id: str,
    ctx: ContextDep,
    engine: EngineDep,
    range_header: str | None,
    if_none_match: str | None,
    if_range: str | None,
) -> Response:
    c, sc = _entry(kind, entry_id)
    content = await engine.get_image_candidate_content(
        ctx,
        candidate_id,
        character_id=c,
        scenario_id=sc,
        byte_range=parse_range(range_header),
        if_none_match=entity_tags(if_none_match),
        if_range=if_range.strip() if if_range is not None else None,
    )
    return await content_response(content)


async def _url(
    kind: str, entry_id: str, candidate_id: str, ctx: ContextDep, engine: EngineDep, ttl: int | None
) -> MediaUrlBody:
    c, sc = _entry(kind, entry_id)
    return MediaUrlBody.of(
        await engine.get_image_candidate_url(
            ctx, candidate_id, character_id=c, scenario_id=sc, ttl=ttl
        )
    )


async def _discard(
    kind: str, entry_id: str, candidate_id: str, ctx: ContextDep, engine: EngineDep
) -> Response:
    c, sc = _entry(kind, entry_id)
    await engine.discard_image_candidate(ctx, candidate_id, character_id=c, scenario_id=sc)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


# -- catalog characters -------------------------------------------------------------------------

_CHARACTER = {"noun": "character", "slots": "`primary` or `gallery`"}


@characters.post(
    "/{character_id}/images/generate",
    response_model=CatalogImagesAccepted,
    status_code=http_status.HTTP_202_ACCEPTED,
    operation_id="request_catalog_images_character",
    summary="Generate candidate images for a character",
    description=GENERATE.format(**_CHARACTER),
)
async def request_catalog_images_character(
    character_id: CharacterId,
    body: CatalogImageRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CatalogImagesAccepted:
    return await _generate("character", character_id, body, response, ctx, engine)


@characters.get(
    "/{character_id}/images/candidates",
    response_model=CandidatePage,
    operation_id="list_image_candidates_character",
    summary="List a character's image candidates",
    description=LIST.format(**_CHARACTER),
)
async def list_image_candidates_character(
    character_id: CharacterId,
    ctx: ContextDep,
    engine: EngineDep,
    job_id: JobIdQuery = None,
    limit: LimitQuery = LIST_DEFAULT,
    cursor: CursorQuery = None,
) -> CandidatePage:
    return await _list("character", character_id, ctx, engine, job_id, limit, cursor)


@characters.get(
    "/{character_id}/images/candidates/{candidate_id}/content",
    response_class=Response,
    operation_id="get_image_candidate_content_character",
    summary="Read a character image candidate's bytes",
    description=CONTENT.format(**_CHARACTER),
    responses=_CONTENT_RESPONSES,
)
async def get_image_candidate_content_character(
    character_id: CharacterId,
    candidate_id: CandidateId,
    ctx: ContextDep,
    engine: EngineDep,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatchHeader = None,
    if_range: IfRangeHeader = None,
) -> Response:
    return await _content(
        "character", character_id, candidate_id, ctx, engine, range_header, if_none_match, if_range
    )


@characters.get(
    "/{character_id}/images/candidates/{candidate_id}/url",
    response_model=MediaUrlBody,
    operation_id="get_image_candidate_url_character",
    summary="Get a short-lived URL for a character image candidate",
    description=URL.format(**_CHARACTER),
)
async def get_image_candidate_url_character(
    character_id: CharacterId,
    candidate_id: CandidateId,
    ctx: ContextDep,
    engine: EngineDep,
    ttl: TtlQuery = None,
) -> MediaUrlBody:
    return await _url("character", character_id, candidate_id, ctx, engine, ttl)


@characters.delete(
    "/{character_id}/images/candidates/{candidate_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="discard_image_candidate_character",
    summary="Discard a character image candidate",
    description=DISCARD.format(**_CHARACTER),
    **route_flags(mutating=False),
)
async def discard_image_candidate_character(
    character_id: CharacterId, candidate_id: CandidateId, ctx: ContextDep, engine: EngineDep
) -> Response:
    return await _discard("character", character_id, candidate_id, ctx, engine)


# -- scenarios ----------------------------------------------------------------------------------

_SCENARIO = {"noun": "scenario", "slots": "`cover`"}


@scenarios.post(
    "/{scenario_id}/images/generate",
    response_model=CatalogImagesAccepted,
    status_code=http_status.HTTP_202_ACCEPTED,
    operation_id="request_catalog_images_scenario",
    summary="Generate candidate images for a scenario",
    description=GENERATE.format(**_SCENARIO),
)
async def request_catalog_images_scenario(
    scenario_id: ScenarioId,
    body: CatalogImageRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CatalogImagesAccepted:
    return await _generate("scenario", scenario_id, body, response, ctx, engine)


@scenarios.get(
    "/{scenario_id}/images/candidates",
    response_model=CandidatePage,
    operation_id="list_image_candidates_scenario",
    summary="List a scenario's image candidates",
    description=LIST.format(**_SCENARIO),
)
async def list_image_candidates_scenario(
    scenario_id: ScenarioId,
    ctx: ContextDep,
    engine: EngineDep,
    job_id: JobIdQuery = None,
    limit: LimitQuery = LIST_DEFAULT,
    cursor: CursorQuery = None,
) -> CandidatePage:
    return await _list("scenario", scenario_id, ctx, engine, job_id, limit, cursor)


@scenarios.get(
    "/{scenario_id}/images/candidates/{candidate_id}/content",
    response_class=Response,
    operation_id="get_image_candidate_content_scenario",
    summary="Read a scenario image candidate's bytes",
    description=CONTENT.format(**_SCENARIO),
    responses=_CONTENT_RESPONSES,
)
async def get_image_candidate_content_scenario(
    scenario_id: ScenarioId,
    candidate_id: CandidateId,
    ctx: ContextDep,
    engine: EngineDep,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatchHeader = None,
    if_range: IfRangeHeader = None,
) -> Response:
    return await _content(
        "scenario", scenario_id, candidate_id, ctx, engine, range_header, if_none_match, if_range
    )


@scenarios.get(
    "/{scenario_id}/images/candidates/{candidate_id}/url",
    response_model=MediaUrlBody,
    operation_id="get_image_candidate_url_scenario",
    summary="Get a short-lived URL for a scenario image candidate",
    description=URL.format(**_SCENARIO),
)
async def get_image_candidate_url_scenario(
    scenario_id: ScenarioId,
    candidate_id: CandidateId,
    ctx: ContextDep,
    engine: EngineDep,
    ttl: TtlQuery = None,
) -> MediaUrlBody:
    return await _url("scenario", scenario_id, candidate_id, ctx, engine, ttl)


@scenarios.delete(
    "/{scenario_id}/images/candidates/{candidate_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="discard_image_candidate_scenario",
    summary="Discard a scenario image candidate",
    description=DISCARD.format(**_SCENARIO),
    **route_flags(mutating=False),
)
async def discard_image_candidate_scenario(
    scenario_id: ScenarioId, candidate_id: CandidateId, ctx: ContextDep, engine: EngineDep
) -> Response:
    return await _discard("scenario", scenario_id, candidate_id, ctx, engine)
