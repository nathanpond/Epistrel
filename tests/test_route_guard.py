"""Invariant 4 guard: every REST route except the five exempt ones requires the credential, and
the one signed route (``GET /media/{media_id}/content``, #107) requires the credential or a valid
signed URL."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.api.auth import (
    EXEMPT_ROUTES,
    SIGNED_ROUTES,
    require_credential_or_signed_url,
    unguarded_routes,
)
from epistrel.core.media.assets import content_path
from epistrel.core.media.signing import Signer
from tests.auth_support import auth_settings, bearer, make_test_router
from tests.model_support import config_key

MEDIA_ID = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C6"


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
    """Every served route carries `require_service_credential` except the exempt five, and the
    signed media content route carries `require_credential_or_signed_url` instead.

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
    assert {("GET", "/media/{media_id}/content")} == SIGNED_ROUTES
    assert "/media/{media_id}/content" in paths  # the guard looked at the signed route too
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


def test_the_signed_guard_counts_only_on_the_signed_route(app: FastAPI) -> None:
    """``require_credential_or_signed_url`` stands in for the credential on ``SIGNED_ROUTES``
    only: on any other path, or another method of that path, the guard reports the route."""
    signed = APIRouter(dependencies=[Depends(require_credential_or_signed_url)])

    @signed.get("/_test/signed/{media_id}")
    async def elsewhere(media_id: str) -> dict[str, str]:
        return {}

    @signed.delete("/media/{media_id}/content")
    async def other_method(media_id: str) -> dict[str, str]:
        return {}

    app.include_router(signed)
    assert unguarded_routes(app) == [
        "GET /_test/signed/{media_id}",
        "DELETE /media/{media_id}/content",
    ]


def test_the_signed_route_needs_a_guard(dummy_dsn: str) -> None:
    """The signed route without its guard is reported like any unguarded route."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/media/{media_id}/content")
    async def content(media_id: str) -> dict[str, str]:
        return {}

    assert unguarded_routes(app) == ["GET /media/{media_id}/content"]


def test_the_signed_route_without_a_signature_needs_the_credential(dummy_dsn: str) -> None:
    """Behaviour: with neither the credential nor ``exp`` and ``sig``, the content route is 401
    before anything runs; a forged signature is 403 ``signature_invalid``, an expired one 403
    ``url_expired``; with the credential, a bad signature is ignored (the asset is then simply
    not found: there is no database here, so the call stops at it)."""
    settings = auth_settings(dummy_dsn, config_key=config_key())
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    path = content_path(MEDIA_ID)
    bare = client.get(path)
    assert (bare.status_code, bare.json()["error"]["code"]) == (401, "unauthenticated")
    assert bare.headers["www-authenticate"] == "Bearer"
    forged = client.get(f"{path}?exp=99999999999&sig=AAAA")
    assert (forged.status_code, forged.json()["error"]["code"]) == (403, "signature_invalid")
    signer = Signer(config_key())
    stale = 1_000_000_000
    expired = client.get(f"{path}?exp={stale}&sig={signer.url_signature('GET', path, stale)}")
    assert (expired.status_code, expired.json()["error"]["code"]) == (403, "url_expired")
    other = client.get(f"/media/{'0' * 26}/content?exp={stale}&sig=AAAA")
    assert other.status_code == 403
    credentialed = client.get(f"{path}?exp=1&sig=AAAA", headers=bearer())
    assert credentialed.status_code not in (401, 403)


def test_worker_mode_serves_no_signed_route(dummy_dsn: str) -> None:
    """A ``worker`` process serves only the exempt routes: not the signed one either."""
    app = create_app(auth_settings(dummy_dsn, mode="worker"))
    paths = {route.path for route in iter_route_contexts(app.routes)}
    assert "/media/{media_id}/content" not in paths
    api = create_app(auth_settings(dummy_dsn, mode="api"))
    assert "/media/{media_id}/content" in {r.path for r in iter_route_contexts(api.routes)}
