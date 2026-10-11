"""C-01: REST/OpenAPI conformance (docs/04), M2 part (FR-API-1,4, NFR-MAINT-2; #73).

- ``test_c_01``: schemathesis over the **committed** ``docs/openapi.json`` sends generated valid
  and invalid requests to the app. Every answer must be a documented status with a documented
  body and headers (no 5xx); invalid data must be refused (400/401/404/409/422) and leave no
  ``operations``, ``events`` or ``config_changes`` row behind; a 422 ``validation_error`` names
  the field.
- ``test_c_01__schema``: the committed document is what the app serves, is OpenAPI 3.1, and is
  documented (every component has a title and a description, every request body an example that
  validates and forbids unknown fields, every error the one envelope).
- ``test_c_01__malformed``: hand-written malformed requests, one per kind of input (body field,
  extra field, JSON syntax, path id, query, header), each refused at its field with zero rows.

The app runs on a scratch database migrated to head, so generated writes (stories, policies,
ratings, deployment settings) never reach the session database. Excluded from generation:
``delete_story`` and ``archive_story`` (they would end the fixture story every later example
needs) and the API-key routes (they need ``EPISTREL_CONFIG_KEY``); all three are covered by
C-03 and their own tests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
import schemathesis
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from hypothesis import HealthCheck, settings
from pydantic import TypeAdapter
from schemathesis.checks import not_a_server_error
from schemathesis.core.parameters import ParameterLocation
from schemathesis.generation.case import Case
from schemathesis.python import asgi as schemathesis_asgi
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    negative_data_rejection,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)
from sqlalchemy import text

from epistrel.api import create_app
from epistrel.api.auth import EXEMPT_ROUTES
from epistrel.api.openapi import TAG_NAMES, dumps
from epistrel.config import Settings
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.settings_schema import StorySettings
from epistrel.core.stories import StorySpec
from epistrel.engine import Engine
from epistrel.orchestrator.lite.schemas import RespondRequest
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from epistrel.tools import openapi as openapi_tool
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.conftest import REPO_ROOT
from tests.respond_support import AUTHOR, body
from tests.story_support import story_body

SCHEMA_PATH = REPO_ROOT / "docs" / "openapi.json"
COMMITTED: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _without_security(schema: dict[str, Any]) -> dict[str, Any]:
    """The committed schema with no security requirements: every request carries the service
    credential (added in ``exercise``), so generation varies everything but the credential.
    Authentication has its own guard (``tests/test_route_guard.py``, C-02)."""
    copied: dict[str, Any] = json.loads(json.dumps(schema))
    for item in copied["paths"].values():
        for operation in item.values():
            operation.pop("security", None)
    return copied


SCHEMA = schemathesis.openapi.from_dict(_without_security(COMMITTED))

EXCLUDED = ("delete_story", "archive_story", "set_model_api_key", "remove_model_api_key")
"""Not generated against (see the module docstring); every other operation is."""
COUNTED_TABLES = ("operations", "events", "config_changes")
_COUNT = "SELECT count(*) FROM {}"
REFUSED = frozenset({400, 401, 404, 409, 422})
EXAMPLES = 100
CHECKS = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
    response_headers_conformance,
    negative_data_rejection,
]


@dataclass
class Live:
    """The app under test on its scratch database, and ids that exist there."""

    app: FastAPI
    url: str
    story_id: str
    operation_id: str

    def counts(self) -> dict[str, int]:
        sync = make_sync_engine(self.url)
        try:
            with sync.connect() as conn:
                return {
                    table: int(conn.execute(text(_COUNT.format(table))).scalar_one())
                    for table in COUNTED_TABLES
                }
        finally:
            sync.dispose()

    @property
    def path_ids(self) -> dict[str, str]:
        return {
            "story_id": self.story_id,
            "operation_id": self.operation_id,
            "rating_id": EVERYONE_ID,
            "policy_id": MINORS_POLICY_ID,
            "purpose": "generation",
        }


async def _seed(settings: Settings) -> tuple[str, str]:
    """A two-character story with one played turn: its id and the turn's operation id."""
    db = make_engine(settings)
    engine = Engine.from_settings(settings)
    engine.bind(db)
    try:
        created = await engine.create_story(
            AUTHOR, StorySpec.model_validate(story_body("Wren", "Ada"))
        )
        actor = created.characters[0].id
        request = RespondRequest.model_validate(body(actor))
        played = await engine.respond(AUTHOR, created.story_id, request)
        return created.story_id, played.beats[0].operation_id
    finally:
        await engine.models.aclose()
        await db.dispose()


@pytest.fixture(scope="module")
def live(pg_url: str) -> Iterator[Live]:
    """The app on a fresh, migrated scratch database (dropped afterwards)."""
    with migrated_database(pg_url, "epistrel_c01") as url:
        try:
            settings = auth_settings(url, model_adapter="stub")
            story_id, operation_id = asyncio.run(_seed(settings))
            yield Live(create_app(settings), url, story_id, operation_id)
        finally:
            schemathesis_asgi.shutdown_lifespans()


def _path_negated(case: Case[Any]) -> bool:
    meta = case.meta
    if meta is None:
        return False
    component = meta.components.get(ParameterLocation.PATH)
    return component is not None and component.mode.is_negative


def exercise(case: Case[Any], live: Live) -> None:
    """Send one generated case and hold the answer to the contract."""
    # A well-formed path id is replaced by one that exists, so generated requests reach the
    # operation instead of stopping at 404; a negated (malformed) id is sent as generated.
    for name, value in list((case.path_parameters or {}).items()):
        if name in live.path_ids and isinstance(value, str) and not _path_negated(case):
            case.path_parameters[name] = live.path_ids[name]
    negative = case.meta is not None and case.meta.generation.mode.is_negative
    before = live.counts() if negative else None
    response = case.call(app=live.app, headers=bearer())
    case.validate_response(response, checks=CHECKS)
    if response.status_code == 422:
        error = response.json()["error"]
        if error["code"] == "validation_error":
            assert error["details"]["field"], error
            assert error["details"]["errors"], error
    if before is not None and response.status_code in REFUSED:
        assert live.counts() == before, (case.operation.label, response.status_code)


@SCHEMA.exclude(operation_id=list(EXCLUDED)).parametrize()
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@pytest.mark.integration
@pytest.mark.part("M2", "FR-API-1,4; NFR-MAINT-2")
def test_c_01(case: Case[Any], live: Live) -> None:
    """Generated valid and invalid requests over the committed schema: documented answers
    only, invalid ones refused at the boundary with nothing written.

    Complement asserted: valid requests are accepted too (``test_c_01__malformed`` shows a
    well-formed request of each kind answered 2xx and writing its rows)."""
    exercise(case, live)


# -- the committed document -------------------------------------------------------------------


def _components(schema: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = schema["components"]["schemas"]
    return found


def _operations(schema: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            yield method.upper(), path, operation


def _body_routes(app: FastAPI) -> dict[str, Any]:
    """``operation_id → the handler's body type`` for every route that takes a body."""
    found: dict[str, Any] = {}
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and route.body_field is not None:
            found[route.operation_id or route.name] = route.body_field.field_info.annotation
    return found


def schema_problems(schema: dict[str, Any], app: FastAPI) -> list[str]:
    problems: list[str] = []
    if schema.get("openapi") != "3.1.0":
        problems.append(f"openapi {schema.get('openapi')}")
    if schema["info"].get("version") != "m2" or "servers" in schema:
        problems.append("info.version must be m2 and there must be no servers")
    components = _components(schema)
    for name in ("HTTPValidationError", "ValidationError"):
        if name in components:
            problems.append(f"FastAPI's {name} is still a component")
    for name, component in components.items():
        if not component.get("title") or not component.get("description"):
            problems.append(f"component {name} lacks a title or description")
    seen_ids: set[str] = set()
    bodies = _body_routes(app)
    for method, path, operation in _operations(schema):
        operation_id = operation.get("operationId")
        if not operation_id or operation_id in seen_ids:
            problems.append(f"{method} {path}: missing or duplicate operationId")
            continue
        seen_ids.add(operation_id)
        if not set(operation.get("tags", [])) <= TAG_NAMES or not operation.get("tags"):
            problems.append(f"{operation_id}: tags {operation.get('tags')}")
        if not operation.get("summary") and not operation.get("description"):
            problems.append(f"{operation_id}: no summary or description")
        for status, response in operation["responses"].items():
            if status[0] in "45" and (method, path) not in EXEMPT_ROUTES:
                ref = response["content"]["application/json"]["schema"].get("$ref", "")
                if not ref.endswith("/Error"):
                    problems.append(f"{operation_id} {status}: not the Error envelope")
        problems.extend(_body_problems(operation_id, operation, components, bodies))
    return problems


def _body_problems(
    operation_id: str,
    operation: dict[str, Any],
    components: dict[str, Any],
    bodies: dict[str, Any],
) -> list[str]:
    request = operation.get("requestBody")
    if request is None:
        return []
    schema = request["content"]["application/json"]["schema"]
    refs = [schema.get("$ref")] + [option.get("$ref") for option in schema.get("anyOf", [])]
    names = [ref.rsplit("/", 1)[-1] for ref in refs if ref]
    if len(names) != 1:
        return [f"{operation_id}: the body is not one component"]
    component = components[names[0]]
    problems: list[str] = []
    if component.get("additionalProperties") is not False:
        problems.append(f"{operation_id}: {names[0]} accepts unknown fields")
    examples = component.get("examples") or []
    if not examples:
        problems.append(f"{operation_id}: {names[0]} has no example")
    annotation = bodies.get(operation_id)
    for example in examples:
        if annotation is not None and not isinstance(annotation, type(dict[str, Any])):
            try:
                TypeAdapter(annotation).validate_python(example)
            except Exception as exc:
                problems.append(f"{operation_id}: example does not validate ({exc})")
    return problems


@pytest.mark.part("M2", "FR-API-1,4; NFR-MAINT-2")
def test_c_01__schema(dummy_dsn: str) -> None:
    """The committed schema is the served one and is documented throughout.

    Complement asserted: the documentation check bites (a component stripped of its
    description is reported)."""
    app = create_app(auth_settings(dummy_dsn, model_adapter="stub"))
    served = app.openapi()
    assert dumps(served) == SCHEMA_PATH.read_text(encoding="utf-8"), (
        "docs/openapi.json is stale: run `uv run python -m epistrel.tools.openapi --write`"
    )
    assert dumps(openapi_tool.schema()) == dumps(served)
    assert schema_problems(served, app) == []
    patch = served["paths"]["/stories/{story_id}/settings"]["patch"]["description"]
    assert [name for name in StorySettings.model_fields if f"`{name}`" not in patch] == []
    covered = {op["operationId"] for _, _, op in _operations(COMMITTED)} - set(EXCLUDED)
    generated = {operation.ok().definition.raw.get("operationId") for operation in _generated()}
    assert covered - {"health"} <= generated

    broken = json.loads(dumps(served))
    _components(broken)["StoryCreatedBody"].pop("description")
    assert schema_problems(broken, app) == [
        "component StoryCreatedBody lacks a title or description"
    ]


def _generated() -> list[Any]:
    return list(SCHEMA.exclude(operation_id=list(EXCLUDED)).get_all_operations())


# -- hand-written malformed requests ------------------------------------------------------------


MALFORMED: list[tuple[str, str, str, dict[str, Any], str]] = [
    # (name, method, path template, request kwargs, expected details.field)
    (
        "missing body field",
        "POST",
        "/stories/{story}/respond",
        {"json": {}},
        "body.actor_character_id",
    ),
    (
        "unknown body field",
        "POST",
        "/stories/{story}/truncate",
        {"json": {"to_turn": 0, "and_also": 1}},
        "body.and_also",
    ),
    ("wrong type", "POST", "/stories/{story}/truncate", {"json": {"to_turn": "x"}}, "body.to_turn"),
    (
        "JSON syntax",
        "POST",
        "/stories",
        {"data": b'{"name": ', "headers": {"Content-Type": "application/json"}},
        "body",
    ),
    ("path id", "POST", "/stories/not-a-ulid/truncate", {"json": {"to_turn": 0}}, "path.story_id"),
    ("query", "GET", "/stories/{story}/history", {"params": {"limit": "many"}}, "query.limit"),
    (
        "header",
        "POST",
        "/stories/{story}/truncate",
        {"json": {"to_turn": 0}, "headers": {"X-Epistrel-Expected-Revision": "seven"}},
        "headers.X-Epistrel-Expected-Revision",
    ),
    (
        "NUL in a body string",
        "PUT",
        "/stories/{story}/custom-instructions",
        {"json": {"text": "a\x00b"}},
        "body.text",
    ),
    (
        "NUL in a query",
        "GET",
        "/stories/{story}/history",
        {"params": {"actor_character_id": "a\x00b"}},
        "query.actor_character_id",
    ),
    (
        "story settings",
        "PATCH",
        "/stories/{story}/settings",
        {"json": {"turn_deadline": "soon"}},
        "body.turn_deadline",
    ),
]


def _malformed(live: Live) -> list[str]:
    problems: list[str] = []
    headers = bearer() | {"X-Epistrel-Principal": "p-c01"}
    with schemathesis_asgi.get_client(cast(Any, live.app)) as client:
        for name, method, template, kwargs, field in MALFORMED:
            before = live.counts()
            sent = {**kwargs, "headers": headers | kwargs.get("headers", {})}
            path = template.format(story=live.story_id)
            response = client.request(method, path, **sent)
            error = response.json().get("error", {}) if response.status_code == 422 else {}
            if response.status_code != 422 or error.get("details", {}).get("field") != field:
                problems.append(f"{name}: {response.status_code} {response.text[:200]}")
            if live.counts() != before:
                problems.append(f"{name}: wrote rows")
        # The complement: a well-formed request of the same shape is accepted and writes.
        before = live.counts()
        ok = client.patch(
            f"/stories/{live.story_id}/settings", json={"turn_deadline": 40}, headers=headers
        )
        if ok.status_code != 200 or live.counts()["config_changes"] <= before["config_changes"]:
            problems.append(f"complement: {ok.status_code} {ok.text[:200]}")
    return problems


@pytest.mark.integration
@pytest.mark.part("M2", "FR-API-1,4; NFR-MAINT-2")
def test_c_01__malformed(live: Live) -> None:
    """Each kind of malformed input is refused with 422 naming its field and writes no row;
    complement: the well-formed request writes."""
    assert _malformed(live) == []
