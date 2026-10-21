"""The single mapping from errors to the REST envelope ``{"error": {code, message, details}}``.

A model failure that escapes a turn arrives here as ``ModelUnavailable`` (503
``model_unavailable``, ``details.purpose``/``reason``), after its operation was rolled back.

A lost database connection (SQLAlchemy's ``OperationalError`` or ``InterfaceError``: the server
went away, restarted, or refused the connection) is 503 ``database_unavailable`` with
``details.reason`` ``connection``, and the connection pool is disposed so the next request
connects afresh (NFR-REL-3, X-06; #75). A transaction that was cut off rolled back; the client
retries under the same ``Idempotency-Key``. An ``OperationalError`` the server answered on a
live connection (a SQLSTATE outside class 08 and the shutdown codes: a statement timeout) is
answered the same way, but the pool is kept (#452): disposing it would drop every healthy
connection and make each request under load connect again. A story commit that times out on
the story lock never reaches this handler: the commit raises 423 ``story_busy`` with
``Retry-After`` instead (#472).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException

from epistrel.api.idempotency import accepted_handler, reference_handler
from epistrel.core.errors import DatabaseUnavailable, EpistrelError, InvalidRequest, field_path
from epistrel.core.idempotency import ReferenceReplay
from epistrel.core.turn_gate import TurnAccepted


async def epistrel_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Every ``EpistrelError``, with the headers it asks for: ``WWW-Authenticate`` on 401, and
    the provider's ``Retry-After`` on a rate-limited 503 ``model_unavailable`` (#53)."""
    assert isinstance(exc, EpistrelError)  # noqa: S101 - registered for EpistrelError only
    headers = exc.response_headers() or None
    return JSONResponse(exc.envelope(), status_code=exc.http_status, headers=headers)


HEADER_LOCATION = "header"
"""FastAPI's ``loc`` for a header parameter; the envelope names it ``headers.<name>``
(docs/02 §9), as the Engine's own header checks do."""


def _documented_loc(error: dict[str, object]) -> dict[str, object]:
    loc = error.get("loc")
    if isinstance(loc, tuple | list) and loc and loc[0] == HEADER_LOCATION:
        return {**error, "loc": ("headers", *loc[1:])}
    return error


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's request validation (body, query, path, headers) in the one envelope: 422
    ``validation_error`` with ``details.field`` (the first failure's dotted path) and
    ``details.errors`` (every failure), before the handler or the Engine runs (FR-API-4)."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for it only
    error = InvalidRequest.from_errors([_documented_loc(dict(e)) for e in exc.errors()])
    return JSONResponse(error.envelope(), status_code=error.http_status)


SERVER_GONE = frozenset({"57P01", "57P02", "57P03"})
"""``admin_shutdown``, ``crash_shutdown``, ``cannot_connect_now``: the server is going away."""


def connection_lost(exc: Exception) -> bool:
    """Whether ``exc`` (an ``OperationalError`` or ``InterfaceError``) means the connection
    is gone: an interface error, a driver error with no SQLSTATE (refused, reset, timed out
    connecting), a connection exception (class 08) or a server shutdown. A SQLSTATE the server
    sent on a live connection (``lock_not_available``, ``query_canceled``) is not."""
    if isinstance(exc, InterfaceError):
        return True
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate is None or str(sqlstate).startswith("08") or sqlstate in SERVER_GONE


async def database_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A lost database connection: 503 ``database_unavailable``, and a fresh pool (see the
    module docstring); the pool is kept when the connection was not lost."""
    engine = getattr(request.app.state, "engine", None)
    if engine is not None and connection_lost(exc):
        await engine.dispose()
    error = DatabaseUnavailable(
        "the database is unavailable; retry with the same Idempotency-Key",
        {"reason": "connection"},
    )
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


__all__ = ["connection_lost", "field_path", "install"]


def install(app: FastAPI) -> None:
    app.add_exception_handler(EpistrelError, epistrel_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(OperationalError, database_error_handler)
    app.add_exception_handler(InterfaceError, database_error_handler)
    app.add_exception_handler(ReferenceReplay, reference_handler)
    app.add_exception_handler(TurnAccepted, accepted_handler)
