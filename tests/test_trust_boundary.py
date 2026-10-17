"""The trust boundary: service credential, requesting principal, `/admin`, no MCP (docs/04
S-TRUST-BOUNDARY, M2): the auth half (#44) and the respond half (#57). M3 (#128): an `/admin`
erasure succeeds under any principal ID. M4 (#145): so does a low-level `commit_fact`."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine
from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.api.auth import TRUST_BOUNDARY_STATEMENT, match_credential
from epistrel.api.routes.admin import ADMIN_PREFIX, is_operator_path
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.errors import PrincipalRequired
from tests.auth_support import (
    CREDENTIAL_A,
    CREDENTIAL_B,
    TEST_PREFIX,
    auth_settings,
    bearer,
    make_test_router,
)
from tests.canon_support import at as canon_at
from tests.conftest import DUMMY_DSN, REPO_ROOT
from tests.consolidation_support import story as consolidation_story
from tests.erasure_support import erasure_settings
from tests.erasure_support import request as erasure_request
from tests.event_support import rows
from tests.respond_support import FIRST, PLAYER, body, new_story, respond_client
from tests.story_support import count

WRITE = f"{TEST_PREFIX}/write"
READ = f"{TEST_PREFIX}/read"
MCP_PATHS = ("/mcp", "/sse", "/.well-known/mcp", "/.well-known/mcp.json")


@pytest.fixture
def app(dummy_dsn: str) -> FastAPI:
    app = create_app(auth_settings(dummy_dsn))
    include_protected(app, make_test_router())
    return app


@pytest.fixture
def spy(app: FastAPI) -> AsyncMock:
    """Wraps the Engine call every protected handler makes; a rejected call never reaches it."""
    engine: Engine = app.state.epistrel
    wrapped = AsyncMock(side_effect=engine.noop)
    app.state.epistrel.noop = wrapped
    return wrapped


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _code(response: object) -> str:
    body = response.json()  # type: ignore[attr-defined]
    code: str = body["error"]["code"]
    return code


@pytest.mark.part("M2", "FR-AUTH-1,2,4; FR-API-2; NFR-SEC-5")
def test_s_trust_boundary__auth(client: TestClient, app: FastAPI, spy: AsyncMock) -> None:
    """Credential → principal → no MCP → documented boundary. Complement: a call with either
    configured credential and a principal is accepted and reaches the Engine."""
    principal = {"X-Epistrel-Principal": "p-1"}
    body = {"value": 1}

    # No credential, an unknown one, or the wrong scheme: 401 with no side effects.
    for headers in (
        principal,
        {**principal, "Authorization": "Bearer not-a-configured-credential-000000000"},
        {**principal, "Authorization": f"Basic {CREDENTIAL_A}"},
    ):
        response = client.post(WRITE, json=body, headers=headers)
        assert response.status_code == 401
        assert _code(response) == "unauthenticated"
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert set(response.json()["error"]) == {"code", "message", "details"}
    assert client.get(READ).status_code == 401
    spy.assert_not_called()

    # Either of two configured credentials is accepted (complement).
    for credential in (CREDENTIAL_A, CREDENTIAL_B):
        response = client.post(WRITE, json=body, headers={**bearer(credential), **principal})
        assert response.status_code == 200, response.text
        assert response.json() == {"principal": "p-1", "value": 1}
    assert spy.await_count == 2

    # A mutating call without a usable principal: 400 before any work, even before the body
    # is validated; nothing reaches the Engine.
    spy.reset_mock()
    for headers in (
        bearer(),
        {**bearer(), "X-Epistrel-Principal": "   "},
        {**bearer(), "X-Epistrel-Principal": "x" * 257},
        {**bearer(), "X-Epistrel-Principal": "a\tb"},
        {**bearer(), "X-Epistrel-Principal": "system"},
    ):
        response = client.post(WRITE, json=body, headers=headers)
        assert response.status_code == 400
        assert _code(response) == "principal_required"
        assert client.post(WRITE, json={"value": "nope"}, headers=headers).status_code == 400
    # A C1 control (U+0080 to U+009F) refuses too (#321): Starlette decodes header bytes as
    # latin-1, so the byte 0x85 arrives as U+0085.
    auth = {key.encode(): value.encode() for key, value in bearer().items()}
    for c1 in (b"p\x85a", b"\x80", b"a\x9f"):
        response = client.post(WRITE, json=body, headers={**auth, b"X-Epistrel-Principal": c1})
        assert response.status_code == 400
        assert _code(response) == "principal_required"
    spy.assert_not_called()

    # The principal is opaque: the same call under two principals differs only in attribution.
    one = client.post(WRITE, json=body, headers={**bearer(), "X-Epistrel-Principal": " u-1 "})
    two = client.post(WRITE, json=body, headers={**bearer(), "X-Epistrel-Principal": "x" * 256})
    assert one.status_code == two.status_code == 200
    assert {**one.json(), "principal": None} == {**two.json(), "principal": None}
    assert one.json()["principal"] == "u-1"

    # A read needs the credential but no principal; a principal on a read is carried.
    assert client.get(READ, headers=bearer()).json()["principal"] is None
    assert client.get(READ, headers={**bearer(), **principal}).json()["principal"] == "p-1"

    # No MCP endpoint is served.
    paths = [route.path or "" for route in iter_route_contexts(app.routes)]
    assert not [p for p in paths if p.startswith(MCP_PATHS)]
    for path in MCP_PATHS:
        assert client.get(path, headers=bearer()).status_code == 404

    # The API documentation starts by stating that Epistrel sits behind the consumer (#125
    # appends more after it).
    schema = client.get("/openapi.json").json()
    assert schema["info"]["description"].startswith(TRUST_BOUNDARY_STATEMENT)
    assert "behind the consumer" in TRUST_BOUNDARY_STATEMENT
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"


def test_in_process_library_needs_no_credential() -> None:
    """FR-AUTH-1, FR-AUTH-4: the library takes no credential but still requires a principal."""
    engine = Engine.from_settings(auth_settings(DUMMY_DSN))

    async def run() -> str:
        return await engine.noop(RequestContext(principal="p-9"))

    assert asyncio.run(run()) == "p-9"
    with pytest.raises(PrincipalRequired):
        asyncio.run(engine.noop(RequestContext()))


def test_exempt_routes_answer_without_credential(client: TestClient) -> None:
    for path in ("/health", "/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 200, path
    assert client.get("/docs/oauth2-redirect").status_code == 404


def test_request_context_carries_all_four_fields(client: TestClient) -> None:
    headers = {
        **bearer(),
        "X-Epistrel-Principal": "p-2",
        "Idempotency-Key": "k-1",
        "X-Epistrel-Expected-Revision": "7",
    }
    assert client.get(f"{READ}?viewpoint=omniscient", headers=headers).json() == {
        "principal": "p-2",
        "idempotency_key": "k-1",
        "expected_revision": 7,
        "viewpoint": "omniscient",
    }
    bad = client.get(READ, headers={**bearer(), "X-Epistrel-Expected-Revision": "seven"})
    assert bad.status_code == 422
    assert bad.json()["error"]["details"]["field"] == "headers.X-Epistrel-Expected-Revision"


def test_body_validation_error_envelope(client: TestClient) -> None:
    headers = {**bearer(), "X-Epistrel-Principal": "p-1"}
    response = client.post(WRITE, json={"value": "nope"}, headers=headers)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "body.value"


def test_auth_disabled_accepts_calls_only_in_debug(dummy_dsn: str) -> None:
    app = create_app(Settings(database_url=dummy_dsn, auth_disabled=True, debug=True))
    include_protected(app, make_test_router())
    client = TestClient(app)
    assert client.get(READ).status_code == 200
    assert client.post(WRITE, json={"value": 1}).status_code == 400  # principal still required


def test_match_credential_reports_index_only() -> None:
    settings = auth_settings(DUMMY_DSN)
    assert match_credential(CREDENTIAL_B, settings) == 1
    assert match_credential(CREDENTIAL_A, settings) == 0
    assert match_credential(CREDENTIAL_A[:-1], settings) is None


def test_operator_paths_live_under_admin(app: FastAPI) -> None:
    """FR-AUTH-1: no path outside `/admin` matches the operator enumeration."""
    for route in iter_route_contexts(app.routes):
        path = route.path or ""
        if not path.startswith(f"{ADMIN_PREFIX}/"):
            assert not is_operator_path(path), path
    # The patterns recognise the operator surface and leave the story surface alone.
    assert is_operator_path("/model-config")
    assert is_operator_path("/policies/abc")
    assert is_operator_path("/content-ratings")
    assert is_operator_path("/settings")
    assert is_operator_path("/stories/{id}/mutate")
    assert not is_operator_path("/stories/{id}/settings")
    assert not is_operator_path("/stories/{id}/content-rating")
    assert not is_operator_path("/stories/{id}/respond")


def test_auth_disabled_refused() -> None:
    """`EPISTREL_AUTH_DISABLED=1` with debug off exits 78 at startup, naming the variable.
    Complement: the same variable with debug on is accepted by the settings."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("EPISTREL_")}
    env |= {"EPISTREL_DATABASE_URL": DUMMY_DSN, "EPISTREL_AUTH_DISABLED": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "epistrel.cli", "serve"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 78
    assert "EPISTREL_AUTH_DISABLED" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert Settings(database_url=DUMMY_DSN, auth_disabled=True, debug=True).auth_disabled


def test_trust_boundary_statement_is_documented_verbatim() -> None:
    """FR-AUTH-2 (documentation half): README and docs/02 §9 carry the OpenAPI text word for word,
    including the opaque-principal recommendation."""
    readme = (REPO_ROOT / "README.md").read_text()
    architecture = (REPO_ROOT / "docs" / "02-technology-architecture.md").read_text()
    section_9 = architecture.split("## 9. API surfaces", 1)[1].split("\n## ", 1)[0]
    assert TRUST_BOUNDARY_STATEMENT in readme
    assert TRUST_BOUNDARY_STATEMENT in section_9
    assert "opaque IDs rather than names or email addresses" in TRUST_BOUNDARY_STATEMENT


def _without_ids(result: dict[str, Any]) -> dict[str, Any]:
    """A respond body with every per-story id removed: what two identical stories share."""
    return {
        "message": result["message"]["text"],
        "beats": [(b["beat"], b["kind"], b["channel"], b["text"]) for b in result["beats"]],
        "position": (result["turn"], result["variant"], result["timeline_revision"]),
        "calls": [(c["purpose"], c["outcome"], c["attempt"]) for c in result["model_calls"]],
        "audience_filter": result["audience_filter"],
    }


@pytest.mark.integration
@pytest.mark.part("M2", "FR-AUTH-1,4")
async def test_s_trust_boundary__respond(pg_url: str, engine: AsyncEngine) -> None:
    """The respond half (#57): the same respond request under two principal IDs gives the same
    result apart from the recorded principal, which is on the turn's operation and every one of
    its events; a respond with no principal is refused before anything is written.

    Complement asserted: each principal is recorded on its own turn, not the other's."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, _):
        results = {}
        for principal in ("u-1", "u-2"):
            story, ids = await new_story(client)
            response = await client.post(
                f"/stories/{story}/respond",
                json=body(ids[PLAYER]),
                headers={**bearer(), "X-Epistrel-Principal": principal},
            )
            assert response.status_code == 200, response.text
            results[principal] = (story, response.json())
        assert _without_ids(results["u-1"][1]) == _without_ids(results["u-2"][1])
        for principal, (story, _) in results.items():
            recorded = await rows(
                engine,
                "SELECT DISTINCT e.principal FROM events e JOIN operations o USING (operation_id) "
                "WHERE e.story_id = :s AND o.kind = 'turn' "
                "UNION SELECT principal FROM operations WHERE story_id = :s AND kind = 'turn'",
                s=story,
            )
            assert {r.principal for r in recorded} == {principal}

        story, ids = await new_story(client)
        before = await count(engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story)
        refused = await client.post(
            f"/stories/{story}/respond", json=body(ids[PLAYER]), headers=bearer()
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "principal_required"
        after = await count(engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story)
        assert after == before


@pytest.mark.integration
@pytest.mark.part("M3", "FR-AUTH-1")
async def test_s_trust_boundary__m3(pg_url: str, engine: AsyncEngine) -> None:
    """An ``/admin`` erasure succeeds under any principal ID: two principals each erase a
    character from their own story, with the same counts, and each is recorded on its own
    erasure's operation; an erasure with no principal is refused before anything is written.

    Complement asserted: the character's name is gone from its story only after the erasure,
    so each erasure did its work."""
    async with respond_client(erasure_settings(pg_url), engine) as (client, _):
        results: dict[str, tuple[str, dict[str, Any]]] = {}
        for principal in ("u-1", "u-2"):
            story, ids = await new_story(client)
            named = await count(
                engine,
                "SELECT count(*) FROM characters WHERE story_id = :s AND name = :n",
                s=story,
                n=FIRST,
            )
            assert named == 1
            response = await client.post(
                f"{ADMIN_PREFIX}/erasure",
                json=erasure_request("character", ids[FIRST], story),
                headers={**bearer(), "X-Epistrel-Principal": principal},
            )
            assert response.status_code == 200, response.text
            results[principal] = (story, response.json())
            named = await count(
                engine,
                "SELECT count(*) FROM characters WHERE story_id = :s AND name = :n",
                s=story,
                n=FIRST,
            )
            assert named == 0
        counts = {
            principal: {k: v for k, v in found.items() if isinstance(v, int) and k != "residue"}
            for principal, (_, found) in results.items()
        }
        assert counts["u-1"] == counts["u-2"]
        for principal, (_, found) in results.items():
            recorded = await rows(
                engine,
                "SELECT principal FROM operations WHERE operation_id = :o",
                o=found["operation_id"],
            )
            assert [r.principal for r in recorded] == [principal]

        story, ids = await new_story(client)
        before = await count(engine, "SELECT count(*) FROM operations")
        refused = await client.post(
            f"{ADMIN_PREFIX}/erasure",
            json=erasure_request("character", ids[FIRST], story),
            headers=bearer(),
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "principal_required"
        assert await count(engine, "SELECT count(*) FROM operations") == before


@pytest.mark.integration
@pytest.mark.part("M4", "FR-AUTH-1")
async def test_s_trust_boundary__m4(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A low-level ``commit_fact`` succeeds under any principal ID: two principals each commit
    the same author fact to their own structured story and get the same answer apart from the
    ids, each recorded on its own ``control`` operation; a ``commit_fact`` with no principal is
    refused before anything is written.

    Complement asserted: each fact is in its story's canon only after the call."""
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (client, _):
        answers: dict[str, dict[str, Any]] = {}
        for principal in ("u-1", "u-2"):
            w = await consolidation_story(engine)
            proposition = canon_at(w.people["Bobby"], w.places["garden"], 0, None)
            path = f"{ADMIN_PREFIX}/stories/{w.story_id}/facts"
            assert (
                await count(engine, "SELECT count(*) FROM facts WHERE story_id = :s", s=w.story_id)
                == 0
            )
            response = await client.post(
                path,
                json={"proposition": proposition.model_dump(mode="json")},
                headers={**bearer(), "X-Epistrel-Principal": principal},
            )
            assert response.status_code == 201, response.text
            answers[principal] = response.json()
            assert (
                await count(engine, "SELECT count(*) FROM facts WHERE story_id = :s", s=w.story_id)
                == 1
            )
            recorded = await rows(
                engine,
                "SELECT principal, kind::text AS kind FROM operations WHERE operation_id = :o",
                o=answers[principal]["operation_id"],
            )
            assert [(r.principal, r.kind) for r in recorded] == [(principal, "control")]
        shapes = {
            principal: (
                found["fact"]["provenance"],
                found["fact"]["authority"],
                found["superseded"],
                found["warnings"],
                found["timeline_revision"],
            )
            for principal, found in answers.items()
        }
        assert shapes["u-1"] == shapes["u-2"] == ("author", "author", [], [], shapes["u-1"][4])

        w = await consolidation_story(engine)
        before = await count(engine, "SELECT count(*) FROM operations")
        refused = await client.post(
            f"{ADMIN_PREFIX}/stories/{w.story_id}/facts",
            json={
                "proposition": canon_at(w.people["Bobby"], w.places["garden"], 0, None).model_dump(
                    mode="json"
                )
            },
            headers=bearer(),
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "principal_required"
        assert await count(engine, "SELECT count(*) FROM operations") == before
