"""C-02: the interface surface (docs/04), M2 part: every operation is reachable over REST and the
in-process library; the operator endpoints delivered so far are served under ``/admin``; no MCP
server is exposed (#73, re-running #44's assertions against the full app). M3 part
(``test_c_02__admin_prefix``, #128): M3's operator endpoints (offboarding, erasure, feedback
export) are under ``/admin`` and matched by #44's operator patterns."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

import epistrel
from epistrel.api import create_app
from epistrel.api.auth import EXEMPT_ROUTES, unguarded_routes
from epistrel.api.routes.admin import ADMIN_PREFIX, is_operator_path
from epistrel.engine import Engine, library_only_methods, operations
from tests.auth_support import auth_settings, bearer

MCP_MARKERS = ("mcp", "/sse", "model-context")
MCP_PROBES = ("/mcp", "/mcp/", "/sse", "/.well-known/mcp", "/.well-known/mcp.json", "/messages")

DELIVERED_OPERATOR_PATHS = (
    "/admin/model-config",
    "/admin/model-config/{purpose}",
    "/admin/model-config/{purpose}/api-key",
    "/admin/policies",
    "/admin/policies/{policy_id}",
    "/admin/content-ratings",
    "/admin/content-ratings/{rating_id}",
    "/admin/content-ratings/{rating_id}/versions",
    "/admin/settings",
    "/admin/config-changes",
    "/admin/stories/{story_id}/rebuild",
)
"""docs/04 C-02 [M2]'s operator endpoints delivered so far: model configuration, global policies,
content-rating definitions, deployment settings (with their change log) and the projection
rebuild. Retention is a deployment setting (`audit_retention_days`, `job_retention_days`) and has
no endpoint of its own in M2."""


M3_OPERATOR_PATHS = (
    "/admin/offboarding",
    "/admin/offboarding/{job_id}",
    "/admin/erasure",
    "/admin/erasure/status",
    "/admin/erasure/boundary",
    "/admin/erasure/reconcile",
    "/admin/feedback/export",
    "/admin/feedback/exports",
    "/admin/feedback/exports/{export_id}",
    "/admin/feedback/exports/{export_id}/content",
)
"""docs/04 C-02 [M3]'s operator endpoints: principal offboarding (#117), erasure (#124, #125) and
the feedback export (#126)."""


def full_app(dsn: str) -> FastAPI:
    """``create_app`` exactly as ``epistrel serve`` builds it: no test routes."""
    return create_app(auth_settings(dsn, mode="all"))


def served(app: FastAPI) -> Iterator[tuple[str, str, APIRoute]]:
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute):
            for method in sorted(context.methods or ()):
                yield method, context.path or "", route


def route_operations(app: FastAPI) -> dict[str, str]:
    """``operation_id → "METHOD path"`` for every non-exempt route."""
    found: dict[str, str] = {}
    for method, path, route in served(app):
        if (method, path) in EXEMPT_ROUTES or method == "HEAD":
            continue
        assert route.operation_id, f"{method} {path} has no operation_id"
        assert route.operation_id not in found, f"duplicate operation_id {route.operation_id}"
        found[route.operation_id] = f"{method} {path}"
    return found


def method_of(operation_id: str, names: list[str]) -> str | None:
    """The Engine operation a route's ``operation_id`` names: equal, or ``<name>_<suffix>``."""
    if operation_id in names:
        return operation_id
    candidates = [n for n in names if operation_id.startswith(f"{n}_")]
    return max(candidates, key=len) if candidates else None


def unreachable(app: FastAPI, engine_class: type[Engine] = Engine) -> list[str]:
    """Engine operations no REST route serves."""
    routed = route_operations(app)
    names = operations(engine_class)
    covered = {method_of(op, names) for op in routed}
    return [name for name in names if name not in covered]


def unbacked(app: FastAPI, engine_class: type[Engine] = Engine) -> list[str]:
    """Routes whose ``operation_id`` names no Engine operation."""
    names = operations(engine_class)
    return sorted(op for op in route_operations(app) if method_of(op, names) is None)


def mcp_routes(app: FastAPI) -> list[str]:
    return [
        f"{method} {path}"
        for method, path, _ in served(app)
        if any(marker in path.lower() for marker in MCP_MARKERS)
    ]


@pytest.mark.part("M2", "FR-API-1,2,3; FR-AUTH-1")
def test_c_02(dummy_dsn: str) -> None:
    """Both surfaces serve every operation; operator endpoints live under ``/admin``; no MCP.

    Complements asserted: an Engine operation added without a route is reported (see
    ``test_c_02_bites_on_an_operation_without_a_route``), and the probes do reach the app
    (``/health`` answers 200 through the same client)."""
    app = full_app(dummy_dsn)
    client = TestClient(app)

    # Every operation over both surfaces, and every route backed by one operation.
    assert unreachable(app) == []
    assert unbacked(app) == []
    assert all(callable(getattr(Engine, name)) for name in operations())
    assert set(library_only_methods()).isdisjoint(operations())
    assert "Engine" in epistrel.__all__
    assert epistrel.Engine is Engine

    # Operator endpoints under /admin, and nothing operator-shaped outside it.
    paths = {path for _, path, _ in served(app)}
    assert set(DELIVERED_OPERATOR_PATHS) <= paths
    assert all(path.startswith(f"{ADMIN_PREFIX}/") for path in DELIVERED_OPERATOR_PATHS)
    misplaced = [p for p in paths if is_operator_path(p) and not p.startswith(ADMIN_PREFIX)]
    assert misplaced == []

    # Invariant 4 over the full app: every route but the exempt five needs the credential.
    assert unguarded_routes(app) == []

    # No MCP server: no route, and nothing answers at the usual MCP endpoints.
    assert mcp_routes(app) == []
    for probe in MCP_PROBES:
        assert client.get(probe, headers=bearer()).status_code == 404, probe
        assert client.post(probe, headers=bearer(), json={}).status_code in (404, 405), probe
    assert client.get("/health").status_code == 200


def test_c_02_bites_on_an_operation_without_a_route(dummy_dsn: str) -> None:
    """The surface check reports an Engine operation no route serves, and a route whose
    ``operation_id`` names no operation."""

    class Wider(Engine):
        async def brand_new_operation(self) -> None:
            """An operation added to the library only."""

    app = full_app(dummy_dsn)
    assert unreachable(app, Wider) == ["brand_new_operation"]

    @app.get("/stories/{story_id}/orphan", operation_id="no_such_operation")
    async def orphan(story_id: str) -> dict[str, str]:
        return {}

    assert unbacked(app) == ["no_such_operation"]


def test_c_02_worker_mode_serves_only_public_routes(dummy_dsn: str) -> None:
    """A ``worker`` process takes no API calls: only the exempt routes are served."""
    app = create_app(auth_settings(dummy_dsn, mode="worker"))
    served_paths = {(m, p) for m, p, _ in served(app) if m != "HEAD"}
    assert served_paths <= EXEMPT_ROUTES
    assert unguarded_routes(app) == []


@pytest.mark.part("M3", "FR-API-1; FR-AUTH-1")
def test_c_02__admin_prefix(dummy_dsn: str) -> None:
    """M3's operator endpoints are served under ``/admin``, each matched by the operator
    patterns, and nothing operator-shaped is served outside ``/admin`` (C-02's assertion re-run
    over the M3 app).

    Complement asserted: the same paths outside ``/admin`` would be reported as misplaced."""
    app = full_app(dummy_dsn)
    paths = {path for _, path, _ in served(app)}
    assert set(M3_OPERATOR_PATHS) <= paths
    assert all(path.startswith(f"{ADMIN_PREFIX}/") for path in M3_OPERATOR_PATHS)
    assert [path for path in M3_OPERATOR_PATHS if not is_operator_path(path)] == []
    misplaced = [p for p in paths if is_operator_path(p) and not p.startswith(ADMIN_PREFIX)]
    assert misplaced == []
    outside = [path.removeprefix(ADMIN_PREFIX) for path in M3_OPERATOR_PATHS]
    assert [path for path in outside if not is_operator_path(path)] == []
    assert unguarded_routes(app) == []
