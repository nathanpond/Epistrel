"""The ``mutating=False`` route flag (#103's ``api.deps.route_flags``, for #109, #111, #121): a
POST that changes nothing takes no ``Idempotency-Key``, checks no expected revision and carries
no revision base, while its method still requires the principal."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.api.deps import MUTATING_EXTRA, ContextDep, mutates, route_flags
from epistrel.api.responses import response_base_problems
from tests.auth_support import auth_settings, bearer

PREFIX = "/_test/flags"


class Checked(BaseModel):
    """What a test check saw."""

    idempotency_key: str | None
    principal: str | None


def _router() -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    @router.post(
        "/check", response_model=Checked, operation_id="flag_check", **route_flags(mutating=False)
    )
    async def check(ctx: ContextDep) -> Checked:
        return Checked(idempotency_key=ctx.idempotency_key, principal=ctx.principal)

    @router.post(
        "/stories/{story_id}/preview",
        response_model=Checked,
        operation_id="flag_preview",
        **route_flags(mutating=False),
    )
    async def preview(story_id: str, ctx: ContextDep) -> Checked:
        return Checked(idempotency_key=ctx.idempotency_key, principal=ctx.principal)

    @router.post("/write", response_model=Checked, operation_id="flag_write", **route_flags())
    async def write(ctx: ContextDep) -> Checked:
        return Checked(idempotency_key=ctx.idempotency_key, principal=ctx.principal)

    return router


@pytest.fixture
def app(dummy_dsn: str) -> FastAPI:
    app = create_app(auth_settings(dummy_dsn))
    include_protected(app, _router())
    return app


def _operation(app: FastAPI, path: str) -> dict[str, Any]:
    found: dict[str, Any] = app.openapi()["paths"][f"{PREFIX}{path}"]["post"]
    return found


def test_a_flagged_route_takes_no_key(app: FastAPI) -> None:
    """A flagged POST gets no generated or echoed key (a sent one is passed through untouched,
    as on a read) but still needs the principal. Complement: an unflagged POST gets a key."""
    client = TestClient(app)
    write = {**bearer(), "X-Epistrel-Principal": "p-flags"}
    checked = client.post(f"{PREFIX}/check", headers=write)
    assert checked.status_code == 200, checked.text
    assert checked.json() == {"idempotency_key": None, "principal": "p-flags"}
    assert "Idempotency-Key" not in checked.headers
    sent = client.post(f"{PREFIX}/check", headers={**write, "Idempotency-Key": "k 1"})
    assert sent.json()["idempotency_key"] == "k 1"
    refused = client.post(f"{PREFIX}/check", headers=bearer())
    assert (refused.status_code, refused.json()["error"]["code"]) == (400, "principal_required")

    written = client.post(f"{PREFIX}/write", headers=write)
    assert written.json()["idempotency_key"] == written.headers["Idempotency-Key"]
    assert written.json()["idempotency_key"]


def test_a_flagged_route_is_documented_as_a_read(app: FastAPI) -> None:
    """The document gives a flagged POST its principal but no ``Idempotency-Key``, expected
    revision, idempotency errors or replay headers, and marks it ``x-epistrel-mutating: false``.
    Complement: the unflagged POST has them all."""
    for path in ("/check", "/stories/{story_id}/preview"):
        operation = _operation(app, path)
        names = {p["name"] for p in operation["parameters"]}
        assert "X-Epistrel-Principal" in names
        assert not names & {"Idempotency-Key", "X-Epistrel-Expected-Revision"}, path
        assert operation[MUTATING_EXTRA] is False
        described = " ".join(r["description"] for r in operation["responses"].values())
        assert "principal_required" in described
        for code in ("idempotency_conflict", "original_in_flight_timeout", "revision_conflict"):
            assert code not in described, (path, code)
        assert "headers" not in operation["responses"]["200"], path

    written = _operation(app, "/write")
    assert {"X-Epistrel-Principal", "Idempotency-Key"} <= {p["name"] for p in written["parameters"]}
    assert MUTATING_EXTRA not in written
    assert set(written["responses"]["200"]["headers"]) == {"Idempotency-Key", "Idempotent-Replay"}
    assert "idempotency_conflict" in written["responses"]["409"]["description"]


def test_a_flagged_route_needs_no_revision_base(app: FastAPI) -> None:
    """The response-base check skips flagged routes; the unflagged one is still held to it."""
    unflagged = f"POST {PREFIX}/write (needs DeploymentMutationResponse)"
    assert response_base_problems(app) == [unflagged]
    assert mutates("POST", None)
    assert not mutates("GET", None)
    assert not mutates("POST", type("Flagged", (), {"openapi_extra": {MUTATING_EXTRA: False}}))
