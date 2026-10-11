"""Request bodies are validated as the schema types them (FR-API-4; #73).

FastAPI parses a JSON body and validates the resulting Python objects in pydantic's *lax* mode,
which coerces: ``"5"`` becomes ``5``, ``true`` becomes ``1.0``. The OpenAPI document promises a
number there, so a client generated from it and a hand-written request must meet the same
rule. ``strict_json_body`` (a dependency of every protected route, after the credential and
principal checks) validates the raw body against the route's body type in strict JSON mode
first: JSON's own types only, enums and literals by value. A refusal is the usual 422
``validation_error`` at ``body.<field>``, before the handler or the Engine runs.

Every string a protected call carries in its path, its query or its JSON body (keys included)
must be free of NUL characters, which PostgreSQL cannot store: one is 422 at its field instead of
a database error (HTTP itself refuses NUL in headers). The Engine refuses NUL in every mutation's
input too (``core.errors.refuse_nul``), so the library answers the same.

A route whose handler takes a raw JSON object that the Engine validates (``PATCH
/stories/{id}/settings``) registers the model that documents its body with
``register_body_model``; that model is the one checked.
"""

from __future__ import annotations

import email.message
import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter, ValidationError

from epistrel.core.errors import nul_location

_BODY_MODELS: dict[str, type[BaseModel]] = {}
_ROUTE_ADAPTER = "_epistrel_strict_adapter"
"""The route attribute caching its body adapter (never a cache keyed by ``id``: a later app's
route may reuse a collected one's id)."""
_MODEL_ADAPTERS: dict[type[BaseModel], TypeAdapter[Any]] = {}


def register_body_model(operation_id: str, model: type[BaseModel]) -> None:
    """Check ``operation_id``'s body against ``model`` (its documented schema)."""
    _BODY_MODELS[operation_id] = model


def _is_json(request: Request) -> bool:
    content_type = request.headers.get("content-type")
    if not content_type:
        return True
    message = email.message.Message()
    message["content-type"] = content_type
    subtype = message.get_content_subtype()
    return message.get_content_maintype() == "application" and (
        subtype == "json" or subtype.endswith("+json")
    )


def _adapter(route: APIRoute) -> TypeAdapter[Any] | None:
    model = _BODY_MODELS.get(route.operation_id or "")
    if model is not None:
        if model not in _MODEL_ADAPTERS:
            _MODEL_ADAPTERS[model] = TypeAdapter(model)
        return _MODEL_ADAPTERS[model]
    if route.body_field is None:
        return None
    adapter: TypeAdapter[Any] | None = getattr(route, _ROUTE_ADAPTER, None)
    if adapter is None:
        annotation: Any = route.body_field.field_info.annotation
        if not route.body_field.field_info.is_required():
            annotation = annotation | None
        adapter = TypeAdapter(annotation)
        setattr(route, _ROUTE_ADAPTER, adapter)
    return adapter


def _nul_error(loc: tuple[int | str, ...]) -> RequestValidationError:
    return RequestValidationError(
        [{"type": "string_nul", "loc": loc, "msg": "Text must not contain NUL characters"}]
    )


def _refuse_nul_parameters(request: Request) -> None:
    for name, value in request.path_params.items():
        if nul_location(value) is not None:
            raise _nul_error(("path", name))
    for name, value in request.query_params.multi_items():
        if nul_location([name, value]) is not None:
            raise _nul_error(("query", name))


async def strict_json_body(request: Request) -> None:
    """Refuse NUL characters anywhere, and a JSON body whose values only lax coercion would
    accept (422 at the field)."""
    _refuse_nul_parameters(request)
    route = request.scope.get("route")
    if not isinstance(route, APIRoute) or route.body_field is None or not _is_json(request):
        return
    raw = await request.body()
    if not raw:
        return
    try:
        document = json.loads(raw)
    except ValueError:
        return  # FastAPI answers malformed JSON itself (422 at ``body``)
    where = nul_location(document, ("body",))
    if where is not None:
        raise _nul_error(where)
    adapter = _adapter(route)
    if adapter is None:
        return
    try:
        adapter.validate_json(raw, strict=True)
    except ValidationError as exc:
        errors = [{**error, "loc": ("body", *error["loc"])} for error in exc.errors()]
        raise RequestValidationError(errors) from None


__all__ = ["register_body_model", "strict_json_body"]
