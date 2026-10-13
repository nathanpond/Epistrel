"""The one OpenAPI document (FR-API-1, NFR-MAINT-2; #73).

FastAPI builds the paths and the response/request components from the routes; ``build`` turns
that into the contract consumers generate clients from, and ``install`` makes the app serve it
at ``/openapi.json``. ``epistrel.tools.openapi`` writes the same document to
``docs/openapi.json`` and CI fails when the two differ.

What ``build`` adds or changes:

- OpenAPI 3.1, ``info.version`` fixed at the milestone (``"m2"``, not the package version, so a
  release does not churn the file), no ``servers``.
- FastAPI's default validation components (``HTTPValidationError``, ``ValidationError``) are
  removed: every error answers the one envelope (``Error``), and each operation declares the
  statuses and error codes it can answer, from the rules below plus ``OPERATION_ERRORS``.
- The headers the handlers read without declaring them: ``X-Epistrel-Principal`` (required on
  every mutation), ``Idempotency-Key``, ``X-Epistrel-Expected-Revision`` (story-scoped
  mutations), ``Prefer: respond-async`` (respond and ingest) and the ``viewpoint`` query.
- Response headers: ``Idempotency-Key`` and ``Idempotent-Replay`` on every mutation's success,
  ``Location`` on creations and 202s, ``Retry-After`` on 423 and 503, ``Content-Range`` on 416.
- ``GET /media/{media_id}/content`` (``auth.SIGNED_ROUTES``, #107) is documented as taking the
  credential or nothing (``security: [{HTTPBearer: []}, {}]``): a valid signed URL's ``exp`` and
  ``sig`` are its query parameters. ``PUT /media/uploads/{id}/parts/{n}`` takes raw bytes
  (``RAW_BODIES``).
- Components only the document needs: ``Error``, ``QueuedTurn`` (a queued turn's 202),
  ``StorySettingsPatch`` (``PATCH /stories/{id}/settings``'s body, which the Engine validates)
  and ``MediaUpload`` (the ``metadata`` part of ``POST /media``'s multipart body, which the
  route reads itself, #105; ``MULTIPART_BODIES``).
- Tags: stories, timeline, history, operations, settings, catalog, media, profiles, ownership,
  feedback, admin, ops. Every
  owned type's owner and relation routes (``routes.ownership``, #104) are tagged ``ownership``,
  whichever router serves them.
- A route flagged ``mutating=False`` (``deps.route_flags``) is documented as a read with a
  principal: no ``Idempotency-Key``, idempotency errors, replay headers or expected revision.
- A read that answers the requesting principal's own record (``PRINCIPAL_READS``: a principal's
  rating of a message, #112) documents ``X-Epistrel-Principal`` as required, with its 400.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import models_json_schema

from epistrel.api.auth import EXEMPT_ROUTES, MUTATING_METHODS, PRINCIPAL_HEADER, SIGNED_ROUTES
from epistrel.api.deps import (
    EXPECTED_REVISION_HEADER,
    MUTATING_EXTRA,
    PREFER_HEADER,
    RESPOND_ASYNC,
)
from epistrel.api.responses import STORY_PATH_PARAMETER
from epistrel.api.routes.ownership import OWNERSHIP_TAG, ownership_operation_ids
from epistrel.api.routes.stories import StorySettingsPatch
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.idempotency import IDEMPOTENCY_HEADER, IDEMPOTENT_REPLAY_HEADER, KEY_PATTERN
from epistrel.core.media.assets import POSTER_FILE_FIELD, MediaUpload
from epistrel.core.ownership import REGISTRY

SCHEMA_VERSION = "m2"
"""``info.version``: the milestone whose contract the document describes."""
OPENAPI_VERSION = "3.1.0"
TITLE = "Epistrel Engine"
REF = "#/components/schemas/{model}"

TAGS: list[dict[str, str]] = [
    {"name": "stories", "description": "Story lifecycle, turns, ingest and story-level text."},
    {"name": "timeline", "description": "Truncate, undo, regenerate, swap, variants and fork."},
    {"name": "history", "description": "The corrected history, for one output viewpoint."},
    {"name": "operations", "description": "One operation: the reference of a retried call."},
    {"name": "settings", "description": "A story's settings, guardrails and config changes."},
    {
        "name": "catalog",
        "description": "Catalog characters and scenarios: drafts, immutable published versions "
        "and their `card`/`full` projections (FR-CAT, FR-SCN-1..4,10).",
    },
    {
        "name": "media",
        "description": "Media assets: immutable, content-addressed images and videos with their "
        "declared depicted subjects, an optional moderation hook, and delete-as-retire "
        "(FR-MEDIA-1,2); byte ranges, short-lived signed URLs, resumable uploads and a poster "
        "for every video (FR-MEDIA-3).",
    },
    {
        "name": "profiles",
        "description": "Engine profiles: named, versioned sets of capability modules and dials "
        "that set how hard the engine works for a story (FR-PROF-1..4). The schema exposes only "
        "the modules and levels this deployment has delivered; presets are ordinary profiles.",
    },
    {
        "name": OWNERSHIP_TAG,
        "description": "Each owned entity's one owner and its relations (FR-AUTH-5,6): "
        "transfers and relation changes, recorded with the acting principal. Ownership and "
        "relations exist for listing, attribution and offboarding only; nothing gates on them "
        "(FR-AUTH-8).",
    },
    {
        "name": "feedback",
        "description": "Message feedback: each principal's rating (1 to 5) of a generated message, "
        "with reason tags and free text, recorded with the configuration the message was "
        "produced under (FR-FB-1). Feedback is an audit record: nothing in the story reads it, "
        "and it changes no engine behavior (FR-FB-2).",
    },
    {"name": "admin", "description": "Operator endpoints, all under `/admin` (FR-AUTH-1)."},
    {"name": "ops", "description": "Unauthenticated operational probes."},
]
TAG_NAMES = frozenset(tag["name"] for tag in TAGS)

ROUTE_TAGS: dict[str, str] = {
    "history": "history",
    "story_settings": "settings",
    "update_story_settings": "settings",
    "config_changes_story": "settings",
    **{
        operation_id: OWNERSHIP_TAG
        for entity_type in REGISTRY
        for operation_id in ownership_operation_ids(entity_type)
    },
}
"""Operations whose router tag is broader than the tag they belong under: some story routes,
and every owned type's owner and relation routes."""


class ErrorBody(BaseModel):
    """What went wrong: a stable snake_case ``code``, a human ``message`` and structured
    ``details`` (``field`` and ``errors`` for a ``validation_error``)."""

    model_config = ConfigDict(title="ErrorBody")

    code: str = Field(description="Stable, snake_case; branch on it, not on the message.")
    message: str
    details: dict[str, Any]


class Error(BaseModel):
    """The one error envelope every surface uses (docs/02 §9): REST answers it with the error's
    status; the library raises the matching ``EpistrelError``."""

    model_config = ConfigDict(
        title="Error",
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "validation_error",
                        "message": "Field required",
                        "details": {
                            "field": "body.actor_character_id",
                            "errors": [
                                {
                                    "field": "body.actor_character_id",
                                    "message": "Field required",
                                    "type": "missing",
                                }
                            ],
                        },
                    }
                }
            ]
        },
    )

    error: ErrorBody


class QueuedTurn(BaseModel):
    """A turn that waits in the story's queue (FR-CONC-5): its operation and its place in the
    queue. ``GET /operations/{id}`` (the ``Location``) reports it once it has played."""

    model_config = ConfigDict(title="QueuedTurn")

    operation_id: str
    position: int


# -- errors ------------------------------------------------------------------------------------

ERROR_STATUS: dict[str, int] = {
    "principal_required": 400,
    "unauthenticated": 401,
    "omniscient_required": 403,
    "signature_invalid": 403,
    "url_expired": 403,
    "not_found": 404,
    "client_event_conflict": 409,
    "idempotency_conflict": 409,
    "nothing_to_undo": 409,
    "revision_conflict": 409,
    "story_archived": 409,
    "timeline_changed": 409,
    "unknown_chain": 409,
    "config_conflict": 409,
    "draft_stale": 409,
    "feedback_tag_in_use": 409,
    "listing_status_in_use": 409,
    "upload_closed": 409,
    "upload_incomplete": 409,
    "length_required": 411,
    "draft_too_large": 413,
    "media_too_large": 413,
    "use_multipart": 413,
    "media_type_not_allowed": 415,
    "range_not_satisfiable": 416,
    "budget_below_safety_minimum": 422,
    "cannot_relax": 422,
    "cast_limit_exceeded": 422,
    "checksum_mismatch": 422,
    "companion_follows_character": 422,
    "cursor_invalid": 422,
    "entry_archived": 422,
    "guardrail_violation": 422,
    "inheritance_cycle": 422,
    "invalid_listing_status": 422,
    "invalid_profile": 422,
    "media_retired": 422,
    "moderation_rejected": 422,
    "not_published": 422,
    "nothing_to_regenerate": 422,
    "part_invalid": 422,
    "poster_extraction_failed": 422,
    "poster_invalid": 422,
    "poster_required": 422,
    "profile_deleted": 422,
    "publish_invalid": 422,
    "rating_retired": 422,
    "subjects_required": 422,
    "unknown_feedback_tag": 422,
    "unsupported_event_kind": 422,
    "validation_error": 422,
    "viewpoint_unresolved": 422,
    "story_busy": 423,
    "budget_exhausted": 503,
    "config_key_missing": 503,
    "config_missing": 503,
    "database_unavailable": 503,
    "media_store_unavailable": 503,
    "model_unavailable": 503,
    "moderation_unavailable": 503,
    "original_in_flight_timeout": 503,
    "turn_interrupted": 503,
}
"""Every error code a route documents, with its status. ``config_conflict`` stands for the 409
a configuration write the current state refuses (its ``code`` is ``validation_error``, with
``details.reason``)."""

STATUS_TEXT = {
    400: "Bad request",
    401: "No valid service credential",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict; nothing was written",
    411: "The body's length is required; nothing was written",
    413: "Too large; nothing was written",
    415: "Unsupported media type; nothing was written",
    416: "Range not satisfiable",
    422: "Invalid request; nothing was written",
    423: "The story is busy",
    503: "Unavailable; nothing was written",
}

_TURN = ("model_unavailable", "budget_exhausted", "turn_interrupted", "timeline_changed")
_UPLOADED = (
    "subjects_required",
    "media_type_not_allowed",
    "media_too_large",
    "rating_retired",
    "moderation_rejected",
    "moderation_unavailable",
    "config_key_missing",
    "media_retired",
    "poster_invalid",
    "poster_required",
    "poster_extraction_failed",
    "media_store_unavailable",
)
"""What storing an upload can answer, directly or from a resumable session (#105, #107)."""
_GATED = ("guardrail_violation", "budget_below_safety_minimum", "model_unavailable")
_SCENARIO_SAVE = ("draft_too_large", "cast_limit_exceeded", "rating_retired", "media_retired")
"""What a scenario's create or save can answer besides the rules' ``validation_error`` (#108)."""
_ADMIN_CONFIG = ("config_conflict",)

OPERATION_ERRORS: dict[str, tuple[str, ...]] = {
    "create_story": (*_GATED, "rating_retired", "viewpoint_unresolved"),
    "respond": (
        *_GATED,
        *_TURN,
        "story_archived",
        "story_busy",
        "viewpoint_unresolved",
    ),
    "regenerate": (
        *_GATED,
        *_TURN,
        "story_archived",
        "story_busy",
        "viewpoint_unresolved",
        "nothing_to_regenerate",
    ),
    "ingest": (
        *_GATED,
        "story_archived",
        "story_busy",
        "client_event_conflict",
        "timeline_changed",
        "unsupported_event_kind",
    ),
    "set_pinned_memories": (*_GATED, "story_archived", "timeline_changed"),
    "set_custom_instructions": (*_GATED, "story_archived", "timeline_changed"),
    "truncate": ("story_archived", "story_busy"),
    "untruncate": ("story_archived", "story_busy", "nothing_to_undo"),
    "swap": ("story_archived", "story_busy", "unknown_chain"),
    "fork": ("unknown_chain", "database_unavailable"),
    "archive_story": ("story_busy",),
    "unarchive_story": ("story_busy",),
    "delete_story": ("story_busy",),
    "update_story_settings": ("cannot_relax", "viewpoint_unresolved", "story_archived"),
    "rebuild_projections": ("story_busy",),
    "history": ("omniscient_required", "viewpoint_unresolved", "unknown_chain"),
    "get_story": ("viewpoint_unresolved",),
    "get_operation": ("idempotency_conflict", "viewpoint_unresolved"),
    "create_content_rating": (*_ADMIN_CONFIG, "inheritance_cycle", "rating_retired"),
    "update_content_rating": (*_ADMIN_CONFIG, "inheritance_cycle", "rating_retired"),
    "retire_content_rating": _ADMIN_CONFIG,
    "update_deployment_settings": (
        *_ADMIN_CONFIG,
        "rating_retired",
        "profile_deleted",
        "listing_status_in_use",
        "feedback_tag_in_use",
    ),
    "policies_create": (*_ADMIN_CONFIG, "cannot_relax"),
    "policies_update": (*_ADMIN_CONFIG, "cannot_relax"),
    "policies_retire": _ADMIN_CONFIG,
    "replace_model_config": (*_ADMIN_CONFIG, "config_key_missing"),
    "remove_model_override": _ADMIN_CONFIG,
    "set_model_api_key": (*_ADMIN_CONFIG, "config_key_missing"),
    "remove_model_api_key": _ADMIN_CONFIG,
    "create_character_draft": ("draft_too_large",),
    "save_character_draft": ("draft_stale", "draft_too_large", "entry_archived"),
    "publish_character": ("draft_stale", "entry_archived", "publish_invalid"),
    "get_character": ("not_published",),
    "create_scenario_draft": _SCENARIO_SAVE,
    "save_scenario_draft": ("draft_stale", "entry_archived", *_SCENARIO_SAVE),
    "publish_scenario": ("draft_stale", "entry_archived", "publish_invalid", "rating_retired"),
    # The dry run (#111): a draft revision the draft has moved on from.
    "validate_scenario": ("draft_stale",),
    "get_scenario": ("not_published",),
    # Listing status and listings (#110): a value outside the deployment's set, a companion's
    # status (it follows its character's), a cursor given for other filters.
    "set_listing_status_character": ("invalid_listing_status",),
    "set_listing_status_scenario": ("invalid_listing_status", "companion_follows_character"),
    "search_catalog_character": ("invalid_listing_status", "cursor_invalid"),
    "search_catalog_scenario": ("invalid_listing_status", "cursor_invalid"),
    "upload_media": (
        *_UPLOADED,
        "use_multipart",
    ),
    "get_media_content": (
        "url_expired",
        "signature_invalid",
        "range_not_satisfiable",
        "media_store_unavailable",
    ),
    "get_media_url": ("config_missing", "config_key_missing", "media_store_unavailable"),
    "open_upload": (
        "subjects_required",
        "media_type_not_allowed",
        "media_too_large",
        "rating_retired",
        "media_retired",
        "poster_invalid",
        "poster_required",
        "media_store_unavailable",
    ),
    "put_upload_part": (
        "length_required",
        "part_invalid",
        "checksum_mismatch",
        "upload_closed",
        "media_store_unavailable",
    ),
    "complete_upload": (
        *_UPLOADED,
        "upload_incomplete",
        "upload_closed",
        "checksum_mismatch",
    ),
    # Engine profiles (#106): settings that cannot run, a deleted profile, the default.
    "create_profile": ("invalid_profile",),
    "update_profile": ("invalid_profile", "profile_deleted"),
    "validate_profile": ("invalid_profile",),
    "delete_profile": _ADMIN_CONFIG,
    # A story's configuration (#109): a preview refuses what the change itself would.
    "impact_preview": ("invalid_profile", "profile_deleted"),
    # Message feedback (#112): a tag outside the deployment's set; the read of one's own rating
    # needs the principal.
    "rate_message": ("unknown_feedback_tag",),
    "get_feedback": ("principal_required",),
    # A companion scenario's owner and relations follow its character (#104, #116).
    **{
        operation_id: ("companion_follows_character",)
        for entity_type, owned in REGISTRY.items()
        if owned.follows is not None
        for operation_id in ownership_operation_ids(entity_type)[:3]
    },
}
"""Error codes beyond the ones every operation of its kind can answer (``_rule_errors``)."""

QUEUEING_OPERATIONS = frozenset({"respond", "ingest"})
"""Operations a ``queue`` story answers with 202 ``QueuedTurn`` (and ``Prefer: respond-async``)."""

VIEWPOINT_OPERATIONS = frozenset({"respond", "regenerate", "history", "get_story", "get_operation"})
"""Operations that read the output viewpoint from ``?viewpoint=`` (FR-RESP-12)."""

PRINCIPAL_READS = frozenset({"get_feedback"})
"""Reads that answer the requesting principal's own record, so ``X-Epistrel-Principal`` is
required on them too: a principal's rating of a message (FR-FB-1, FR-AUTH-4; #112)."""

CREATING_OPERATIONS = frozenset({"create_story", "fork"})
"""Operations that answer 201 with ``Location: /stories/{id}``."""

MEDIA_CREATING_OPERATIONS = frozenset({"complete_upload"})
"""Operations that answer 201 with ``Location: /media/{id}`` (#107)."""

DOCUMENTED_BODIES = {"update_story_settings": "StorySettingsPatch"}
"""Operations whose handler takes a raw JSON object (validated by the Engine) and the component
that documents it."""

MULTIPART_BODIES = {"upload_media": "MediaUpload"}
"""Operations whose handler reads a ``multipart/form-data`` body itself (#105): a ``metadata``
part (the component named here, as JSON) followed by a binary ``file`` part and, for a video,
an optional binary ``poster_file`` part (#107)."""

RAW_BODIES = frozenset({"put_upload_part"})
"""Operations whose handler streams a raw ``application/octet-stream`` body itself (#107)."""


def _multipart_body(component: str) -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["metadata", "file"],
                    "properties": {
                        "metadata": {"$ref": REF.format(model=component)},
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": "The bytes, as the part's own `Content-Type` "
                            "declares them (or `application/octet-stream`).",
                        },
                        POSTER_FILE_FIELD: {
                            "type": "string",
                            "format": "binary",
                            "description": "A video only, after `file`: its poster image "
                            "(stored with the video's rating, owner and subjects), instead of "
                            "`poster` or a frame ffmpeg extracts.",
                        },
                    },
                    "additionalProperties": False,
                },
                "encoding": {"metadata": {"contentType": "application/json"}},
            }
        },
    }


def _rule_errors(method: str, path: str, *, mutating: bool) -> list[str]:
    codes = ["unauthenticated", "validation_error", "database_unavailable"]
    if "{" in path:
        codes.append("not_found")
    if method in MUTATING_METHODS:
        codes.append("principal_required")
    if mutating:
        codes += ["idempotency_conflict", "original_in_flight_timeout"]
        if STORY_PATH_PARAMETER in path:
            codes.append("revision_conflict")
    return codes


def _error_responses(codes: Iterable[str]) -> dict[str, Any]:
    by_status: dict[int, list[str]] = {}
    for code in codes:
        listed = by_status.setdefault(ERROR_STATUS[code], [])
        if code not in listed:
            listed.append(code)
    responses: dict[str, Any] = {}
    for status in sorted(by_status):
        names = ", ".join(f"`{code}`" for code in sorted(by_status[status]))
        response: dict[str, Any] = {
            "description": f"{STATUS_TEXT[status]}: {names}.",
            "content": {"application/json": {"schema": {"$ref": REF.format(model="Error")}}},
        }
        if status in (423, 503):
            response["headers"] = {"Retry-After": _header("Seconds to wait before retrying.")}
        if status == 401:
            response["headers"] = {"WWW-Authenticate": _header("`Bearer`.")}
        if status == 416:
            response["headers"] = {"Content-Range": _header("`bytes */<size>`.")}
        responses[str(status)] = response
    return responses


# -- parameters and headers ----------------------------------------------------------------------


def _header(description: str, *, schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"description": description, "schema": dict(schema or {"type": "string"})}


def _parameter(
    name: str, location: str, description: str, *, required: bool = False, **schema: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "in": location,
        "required": required,
        "description": description,
        "schema": {"type": "string", **schema},
    }


def _parameters(
    operation_id: str, method: str, path: str, *, mutating: bool
) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    if method in MUTATING_METHODS:
        added.append(
            _parameter(
                PRINCIPAL_HEADER,
                "header",
                "The opaque requesting principal (FR-AUTH-4): required on every mutation (400 "
                "`principal_required` when missing, blank, longer than 256 characters, holding a "
                "control character, or the reserved `system`), recorded for attribution and "
                "never interpreted. Surrounding spaces and tabs are trimmed.",
                required=True,
                pattern=PRINCIPAL_PATTERN,
            )
        )
    elif operation_id in PRINCIPAL_READS:
        added.append(
            _parameter(
                PRINCIPAL_HEADER,
                "header",
                "The opaque requesting principal (FR-AUTH-4), whose own record this read "
                "answers: required (400 `principal_required` when missing, blank, longer than "
                "256 characters, holding a control character, or the reserved `system`). "
                "Surrounding spaces and tabs are trimmed.",
                required=True,
                pattern=PRINCIPAL_PATTERN,
            )
        )
    if mutating:
        added.append(
            _parameter(
                IDEMPOTENCY_HEADER,
                "header",
                "Makes the call idempotent (FR-CONC-6): a retry with the same key and request "
                "returns the stored response; generated when absent and echoed either way.",
                pattern=f"^{KEY_PATTERN.pattern}$",
            )
        )
        if STORY_PATH_PARAMETER in path:
            added.append(
                _parameter(
                    EXPECTED_REVISION_HEADER,
                    "header",
                    "The story revision the call expects; a stale one is 409 "
                    "`revision_conflict` with nothing written (FR-RESP-6).",
                    pattern=r"^\s*[+-]?[0-9]+\s*$",
                )
            )
    if operation_id in QUEUEING_OPERATIONS:
        added.append(
            _parameter(
                PREFER_HEADER,
                "header",
                f"`{RESPOND_ASYNC}`: answer a queued turn with 202 at once instead of waiting.",
                enum=[RESPOND_ASYNC],
            )
        )
    if operation_id in VIEWPOINT_OPERATIONS:
        added.append(
            _parameter(
                "viewpoint",
                "query",
                "The output viewpoint: `character:<id>` of the cast or `omniscient` (FR-RESP-12).",
                examples=["omniscient"],
            )
        )
    return added


def _success_headers(
    operation_id: str, method: str, status: str, *, mutating: bool
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if mutating:
        headers[IDEMPOTENCY_HEADER] = _header("The request's key, echoed (generated if absent).")
        headers[IDEMPOTENT_REPLAY_HEADER] = _header(
            "`true` when this is a stored response replayed for a retry.", schema={"enum": ["true"]}
        )
    if status == "201" and operation_id in CREATING_OPERATIONS:
        headers["Location"] = _header("`/stories/{id}` of the new story.")
    if status == "201" and operation_id in MEDIA_CREATING_OPERATIONS:
        headers["Location"] = _header("`/media/{id}` of the new asset.")
    if status == "202" and operation_id in QUEUEING_OPERATIONS:
        headers["Location"] = _header("`/operations/{id}` of the queued turn.")
    return headers


# -- the document --------------------------------------------------------------------------------


def _extra_components() -> dict[str, Any]:
    _, schema = models_json_schema(
        [
            (Error, "serialization"),
            (QueuedTurn, "serialization"),
            (StorySettingsPatch, "validation"),
            (MediaUpload, "validation"),
        ],
        ref_template=REF,
    )
    components: dict[str, Any] = schema.get("$defs", {})
    return components


def protected_operations(app: FastAPI) -> Iterable[tuple[str, str, APIRoute]]:
    """``(method, path, route)`` for every served route that is not exempt (invariant 4)."""
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(context.methods or ()):
            if (method, context.path or "") not in EXEMPT_ROUTES:
                yield method, context.path or "", route


def build(app: FastAPI) -> dict[str, Any]:
    """The app's OpenAPI document (see the module docstring)."""
    schema = get_openapi(
        title=TITLE,
        version=SCHEMA_VERSION,
        openapi_version=OPENAPI_VERSION,
        description=app.description,
        routes=app.routes,
        tags=TAGS,
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        components.pop(name, None)
    components.update(_extra_components())
    for path, item in schema.get("paths", {}).items():
        for method_lower, operation in item.items():
            _finish_operation(operation, method_lower.upper(), path)
    return schema


def _finish_operation(operation: dict[str, Any], method: str, path: str) -> None:
    operation_id = str(operation.get("operationId", ""))
    if operation_id in ROUTE_TAGS:
        operation["tags"] = [ROUTE_TAGS[operation_id]]
    if operation_id in DOCUMENTED_BODIES:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": REF.format(model=DOCUMENTED_BODIES[operation_id])}
                }
            },
        }
    if operation_id in MULTIPART_BODIES:
        operation["requestBody"] = _multipart_body(MULTIPART_BODIES[operation_id])
    if operation_id in RAW_BODIES:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    if (method, path) in SIGNED_ROUTES:
        # The credential, or nothing but a valid signed URL's ``exp`` and ``sig`` (#107).
        operation["security"] = [*operation.get("security", []), {}]
    responses: dict[str, Any] = operation.setdefault("responses", {})
    exempt = (method, path) in EXEMPT_ROUTES or (
        method == "HEAD" and ("GET", path) in EXEMPT_ROUTES
    )
    responses.pop("422", None)
    if exempt:
        return
    # A route flagged ``mutating=False`` (``deps.route_flags``) keeps the principal rule of its
    # method but takes no key and checks no revision.
    mutating = method in MUTATING_METHODS and operation.get(MUTATING_EXTRA, True) is not False
    for status, response in responses.items():
        if status.startswith("2"):
            headers = _success_headers(operation_id, method, status, mutating=mutating)
            if headers:
                response.setdefault("headers", {}).update(headers)
    if operation_id in QUEUEING_OPERATIONS:
        responses["202"] = {
            "description": "The turn is queued behind the running one (FR-CONC-5).",
            "headers": _success_headers(operation_id, method, "202", mutating=mutating),
            "content": {"application/json": {"schema": {"$ref": REF.format(model="QueuedTurn")}}},
        }
    codes = [
        *_rule_errors(method, path, mutating=mutating),
        *OPERATION_ERRORS.get(operation_id, ()),
    ]
    responses.update(_error_responses(codes))
    present = {(p["name"], p["in"]) for p in operation.get("parameters", [])}
    for parameter in _parameters(operation_id, method, path, mutating=mutating):
        if (parameter["name"], parameter["in"]) not in present:
            operation.setdefault("parameters", []).append(parameter)


def dumps(schema: Mapping[str, Any]) -> str:
    """The committed form: keys sorted, two-space indent, a final newline."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def install(app: FastAPI) -> None:
    """Serve ``build(app)`` at ``/openapi.json`` (built once, on first request)."""
    cached: dict[str, Any] = {}

    def openapi() -> dict[str, Any]:
        if not cached:
            cached.update(build(app))
        return cached

    app.openapi = openapi  # type: ignore[method-assign]


__all__ = [
    "MULTIPART_BODIES",
    "OPERATION_ERRORS",
    "SCHEMA_VERSION",
    "TAG_NAMES",
    "Error",
    "QueuedTurn",
    "build",
    "dumps",
    "install",
    "protected_operations",
]
