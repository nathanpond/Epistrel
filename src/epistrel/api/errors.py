"""The single mapping from errors to the REST envelope ``{"error": {code, message, details}}``."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from epistrel.core.errors import EpistrelError, InvalidRequest, Unauthenticated


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
    assert isinstance(exc, EpistrelError)  # noqa: S101 - registered for EpistrelError only
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, Unauthenticated) else None
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
