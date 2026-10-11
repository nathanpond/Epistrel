"""Invariant 4 guard: every REST route except the five exempt ones requires the credential."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.api.auth import EXEMPT_ROUTES, unguarded_routes
from tests.auth_support import auth_settings, make_test_router


@pytest.fixture
def app(dummy_dsn: str) -> FastAPI:
    app = create_app(auth_settings(dummy_dsn))
    include_protected(app, make_test_router())
    return app


def test_route_guard_bites_on_an_undecorated_route(app: FastAPI) -> None:
    """The guard fails for a route added straight onto the app without the dependency."""

    @app.get("/leak")
    async def leak() -> dict[str, str]:
        return {}

    assert unguarded_routes(app) == ["GET /leak"]


def test_route_guard(app: FastAPI) -> None:
    """Every served route carries `require_service_credential` except the exempt five.

    No `part` marker: docs/04 §15 lists only S-TRUST-BOUNDARY for this requirement, so the
    requirement's M2 part is `test_s_trust_boundary__auth`; this is the invariant 4 guard."""
    assert unguarded_routes(app) == []
    paths = {route.path for route in iter_route_contexts(app.routes)}
    assert "/_test/write" in paths  # the guard looked at a protected route, not nothing
    assert {path for _, path in EXEMPT_ROUTES} == {
        "/health",
        "/metrics",
        "/openapi.json",
        "/docs",
        "/redoc",
    }
    assert "/docs/oauth2-redirect" not in paths


def test_route_guard_fails_closed_on_non_api_routes(app: FastAPI) -> None:
    async def asgi(scope: object, receive: object, send: object) -> None: ...

    app.mount("/static", asgi)
    app.router.add_websocket_route("/ws", asgi)  # type: ignore[arg-type]
    problems = unguarded_routes(app)
    assert any("/static" in p for p in problems)
    assert any("/ws" in p for p in problems)


def test_route_guard_head_inherits_only_get_exemption(app: FastAPI) -> None:
    @app.post("/health")
    async def post_health() -> dict[str, str]:
        return {}

    assert unguarded_routes(app) == ["POST /health"]
