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
  ``Location`` on creations and 202s, ``Retry-After`` on 423 and 503.
- Components only the document needs: ``Error``, ``QueuedTurn`` (a queued turn's 202) and
  ``StorySettingsPatch`` (``PATCH /stories/{id}/settings``'s body, which the Engine validates).
- Tags: stories, timeline, history, operations, settings, admin, ops.
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

from epistrel.api.auth import EXEMPT_ROUTES, MUTATING_METHODS, PRINCIPAL_HEADER
from epistrel.api.deps import EXPECTED_REVISION_HEADER, PREFER_HEADER, RESPOND_ASYNC
from epistrel.api.responses import STORY_PATH_PARAMETER
from epistrel.api.routes.stories import StorySettingsPatch
from epistrel.core.idempotency import IDEMPOTENCY_HEADER, IDEMPOTENT_REPLAY_HEADER, KEY_PATTERN

PRINCIPAL_PATTERN = r"^[\t ]*[^\x00-\x1f\x7f]{1,256}[\t ]*$"
"""``X-Epistrel-Principal`` as sent: 1 to 256 characters without control characters, between
optional spaces and tabs (``require_principal`` trims those)."""

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
    {"name": "admin", "description": "Operator endpoints, all under `/admin` (FR-AUTH-1)."},
    {"name": "ops", "description": "Unauthenticated operational probes."},
]
TAG_NAMES = frozenset(tag["name"] for tag in TAGS)

ROUTE_TAGS: dict[str, str] = {
    "history": "history",
    "story_settings": "settings",
    "update_story_settings": "settings",
    "config_changes_story": "settings",
}
"""Operations whose router tag is broader than the tag they belong under."""


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
    "not_found": 404,
    "client_event_conflict": 409,
    "idempotency_conflict": 409,
    "nothing_to_undo": 409,
    "revision_conflict": 409,
    "story_archived": 409,
    "timeline_changed": 409,
    "unknown_chain": 409,
    "config_conflict": 409,
    "budget_below_safety_minimum": 422,
    "cannot_relax": 422,
    "guardrail_violation": 422,
    "inheritance_cycle": 422,
    "nothing_to_regenerate": 422,
    "rating_retired": 422,
    "unsupported_event_kind": 422,
    "validation_error": 422,
    "viewpoint_unresolved": 422,
    "story_busy": 423,
    "budget_exhausted": 503,
    "config_key_missing": 503,
    "database_unavailable": 503,
    "model_unavailable": 503,
    "original_in_flight_timeout": 503,
    "turn_interrupted": 503,
}
"""Every error code a route documents, with its status. ``config_conflict`` stands for the 409
a configuration write the current state refuses (its ``code`` is ``validation_error``, with
``details.reason``)."""

STATUS_TEXT = {
    400: "Bad request",
    401: "No valid service credential",
    403: "Forbidden for the requested viewpoint",
    404: "Not found",
    409: "Conflict; nothing was written",
    422: "Invalid request; nothing was written",
    423: "The story is busy",
    503: "Unavailable; nothing was written",
}

_TURN = ("model_unavailable", "budget_exhausted", "turn_interrupted", "timeline_changed")
_GATED = ("guardrail_violation", "budget_below_safety_minimum", "model_unavailable")
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
    "update_deployment_settings": (*_ADMIN_CONFIG, "rating_retired"),
    "policies_create": (*_ADMIN_CONFIG, "cannot_relax"),
    "policies_update": (*_ADMIN_CONFIG, "cannot_relax"),
    "policies_retire": _ADMIN_CONFIG,
    "replace_model_config": (*_ADMIN_CONFIG, "config_key_missing"),
    "remove_model_override": _ADMIN_CONFIG,
    "set_model_api_key": (*_ADMIN_CONFIG, "config_key_missing"),
    "remove_model_api_key": _ADMIN_CONFIG,
}
"""Error codes beyond the ones every operation of its kind can answer (``_rule_errors``)."""

QUEUEING_OPERATIONS = frozenset({"respond", "ingest"})
"""Operations a ``queue`` story answers with 202 ``QueuedTurn`` (and ``Prefer: respond-async``)."""

VIEWPOINT_OPERATIONS = frozenset({"respond", "regenerate", "history", "get_story", "get_operation"})
"""Operations that read the output viewpoint from ``?viewpoint=`` (FR-RESP-12)."""

CREATING_OPERATIONS = frozenset({"create_story", "fork"})
"""Operations that answer 201 with ``Location: /stories/{id}``."""

DOCUMENTED_BODIES = {"update_story_settings": "StorySettingsPatch"}
"""Operations whose handler takes a raw JSON object (validated by the Engine) and the component
that documents it."""


def _rule_errors(method: str, path: str) -> list[str]:
    codes = ["unauthenticated", "validation_error", "database_unavailable"]
    if "{" in path:
        codes.append("not_found")
    if method in MUTATING_METHODS:
        codes += ["principal_required", "idempotency_conflict", "original_in_flight_timeout"]
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


def _parameters(operation_id: str, method: str, path: str) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    mutating = method in MUTATING_METHODS
    if mutating:
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


def _success_headers(operation_id: str, method: str, status: str) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if method in MUTATING_METHODS:
        headers[IDEMPOTENCY_HEADER] = _header("The request's key, echoed (generated if absent).")
        headers[IDEMPOTENT_REPLAY_HEADER] = _header(
            "`true` when this is a stored response replayed for a retry.", schema={"enum": ["true"]}
        )
    if status == "201" and operation_id in CREATING_OPERATIONS:
        headers["Location"] = _header("`/stories/{id}` of the new story.")
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
    responses: dict[str, Any] = operation.setdefault("responses", {})
    exempt = (method, path) in EXEMPT_ROUTES or (
        method == "HEAD" and ("GET", path) in EXEMPT_ROUTES
    )
    responses.pop("422", None)
    if exempt:
        return
    for status, response in responses.items():
        if status.startswith("2"):
            headers = _success_headers(operation_id, method, status)
            if headers:
                response.setdefault("headers", {}).update(headers)
    if operation_id in QUEUEING_OPERATIONS:
        responses["202"] = {
            "description": "The turn is queued behind the running one (FR-CONC-5).",
            "headers": _success_headers(operation_id, method, "202"),
            "content": {"application/json": {"schema": {"$ref": REF.format(model="QueuedTurn")}}},
        }
    codes = [*_rule_errors(method, path), *OPERATION_ERRORS.get(operation_id, ())]
    responses.update(_error_responses(codes))
    present = {(p["name"], p["in"]) for p in operation.get("parameters", [])}
    for parameter in _parameters(operation_id, method, path):
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
