"""The single mapping from errors to the REST envelope ``{"error": {code, message, details}}``.

A model failure that escapes a turn arrives here as ``ModelUnavailable`` (503
``model_unavailable``, ``details.purpose``/``reason``), after its operation was rolled back.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from epistrel.api.idempotency import accepted_handler, reference_handler
from epistrel.core.errors import EpistrelError, InvalidRequest
from epistrel.core.idempotency import ReferenceReplay
from epistrel.core.turn_gate import TurnAccepted


def field_path(loc: tuple[int | str, ...] | list[int | str]) -> str:
    """A validation error's location as a dotted path with list indexes in brackets, matching
    the paths the Engine's own errors use: ``body.characters[0].true_age``."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path


async def epistrel_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Every ``EpistrelError``, with the headers it asks for: ``WWW-Authenticate`` on 401, and
    the provider's ``Retry-After`` on a rate-limited 503 ``model_unavailable`` (#53)."""
    assert isinstance(exc, EpistrelError)  # noqa: S101 - registered for EpistrelError only
    headers = exc.response_headers() or None
    return JSONResponse(exc.envelope(), status_code=exc.http_status, headers=headers)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for it only
    errors = exc.errors()
    first = errors[0] if errors else {}
    field = field_path(first.get("loc", ()))
    error = InvalidRequest(
        str(first.get("msg", "invalid request")),
        details={"field": field, "errors": len(errors)},
    )
    return JSONResponse(error.envelope(), status_code=error.http_status)


def install(app: FastAPI) -> None:
    app.add_exception_handler(EpistrelError, epistrel_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(ReferenceReplay, reference_handler)
    app.add_exception_handler(TurnAccepted, accepted_handler)
