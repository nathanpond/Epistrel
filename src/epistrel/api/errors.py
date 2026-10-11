"""The single mapping from errors to the REST envelope ``{"error": {code, message, details}}``.

A model failure that escapes a turn arrives here as ``ModelUnavailable`` (503
``model_unavailable``, ``details.purpose``/``reason``), after its operation was rolled back.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from epistrel.api.idempotency import accepted_handler, reference_handler
from epistrel.core.errors import EpistrelError, InvalidRequest, field_path
from epistrel.core.idempotency import ReferenceReplay
from epistrel.core.turn_gate import TurnAccepted


async def epistrel_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Every ``EpistrelError``, with the headers it asks for: ``WWW-Authenticate`` on 401, and
    the provider's ``Retry-After`` on a rate-limited 503 ``model_unavailable`` (#53)."""
    assert isinstance(exc, EpistrelError)  # noqa: S101 - registered for EpistrelError only
    headers = exc.response_headers() or None
    return JSONResponse(exc.envelope(), status_code=exc.http_status, headers=headers)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's request validation (body, query, path, headers) in the one envelope: 422
    ``validation_error`` with ``details.field`` (the first failure's dotted path) and
    ``details.errors`` (every failure), before the handler or the Engine runs (FR-API-4)."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for it only
    error = InvalidRequest.from_errors(exc.errors())
    return JSONResponse(error.envelope(), status_code=error.http_status)


HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}
BODY_UNREADABLE = 400
"""FastAPI's status for a body it could not decode (not UTF-8): answered as a validation error."""


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Starlette's and FastAPI's own HTTP errors in the one envelope: an unknown path (404
    ``not_found``), a method the path does not serve (405 ``method_not_allowed``), and a body
    that is not text (422 ``validation_error`` at ``body``, FR-API-4)."""
    assert isinstance(exc, HTTPException)  # noqa: S101 - registered for it only
    if exc.status_code == BODY_UNREADABLE:
        error = InvalidRequest("the request body is not valid JSON", {"field": "body"})
        return JSONResponse(error.envelope(), status_code=error.http_status)
    code = HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    body = {"error": {"code": code, "message": str(exc.detail), "details": {}}}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


__all__ = ["field_path", "install"]


def install(app: FastAPI) -> None:
    app.add_exception_handler(EpistrelError, epistrel_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(ReferenceReplay, reference_handler)
    app.add_exception_handler(TurnAccepted, accepted_handler)
