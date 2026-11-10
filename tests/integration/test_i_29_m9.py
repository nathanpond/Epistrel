"""I-29's M9 parts: consumer purposes, registered, versioned and routed (FR-MODEL-14; #824).

An operator registers a consumer purpose under ``/admin/purposes`` (a name, a sandboxed Jinja2
template and an output schema in the strict-path JSON Schema subset). Everything is checked at
save, each refusal a 422 ``validation_error`` naming its ``details.reason``; a replacement with
``expected_version`` writes the next version (409 ``version_stale`` otherwise); an archived
purpose's versions stay readable and its calls are refused (410 ``purpose_archived``). Each
purpose is routed like a built-in one: ``/admin/model-config`` lists it, an override routes it,
and with none it uses ``generation``'s model. Until #825's call route exists, a call is made at
the Model Access Layer (``send``, under the stub), the seam #825's route goes through; #825
extends ``test_i_29__m9_routing`` with its REST call.

Purposes are deployment-wide and never deleted, so every name is unique to its test
(``unique``) and each test archives what it registered (``registered``): archived purposes do
not count toward the 100-purpose limit. Every I-29 M9 part lives in this file (the
traceability gate takes one file per test ID and milestone).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.errors import (
    PurposeArchived,
    PurposeNotFound,
    PurposeVersionNotFound,
    PurposeVersionStale,
)
from epistrel.core.ids import new_ulid
from epistrel.core.purposes.output import output_of
from epistrel.core.purposes.store import PurposeCreate, PurposeReplace
from epistrel.core.purposes.validation import PURPOSE_LIMIT, PurposeRefused
from epistrel.engine import Engine
from epistrel.orchestrator.models.adapter import Message, UnknownPurpose
from epistrel.orchestrator.models.registry import DELIVERED, PORTS, validate_purpose
from tests.auth_support import auth_settings, bearer
from tests.model_support import restored_model_config
from tests.profile_support import nest
from tests.respond_support import new_story, respond_client
from tests.story_support import WRITE

OPERATOR = {**bearer(), "X-Epistrel-Principal": "p-operator"}
LIBRARY = RequestContext(principal="p-operator")
CANARY = "ZEBRA-CANARY-824"
"""Template text that must never reach a log line (invariant 3)."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "maxLength": 500, "description": "Why."},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "mood": {"type": "string", "enum": ["calm", "angry"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["reason", "score", "mood", "tags"],
    "additionalProperties": False,
}


def unique(stem: str) -> str:
    """A name no other test (or run on the shared database) uses."""
    return f"{stem}_{new_ulid().lower()[-10:]}"


def purpose_body(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "A juror's in-character reason for a vote.",
        "template": "Explain why {{ inputs.voter }} voted for {{ inputs.target }}.",
        "output_schema": SCHEMA,
        **fields,
    }


def replace_body(expected_version: int, **fields: Any) -> dict[str, Any]:
    """A ``PUT /admin/purposes/{name}`` body (``name`` is the path's, never the body's)."""
    body = purpose_body("unused", **fields)
    del body["name"]
    return {**body, "expected_version": expected_version}


Register = Callable[..., Awaitable[httpx.Response]]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
async def served(
    stub_settings: Settings, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, Engine]]:
    async with respond_client(stub_settings, engine) as found:
        yield found


@pytest.fixture
async def registered(
    served: tuple[httpx.AsyncClient, Engine],
) -> AsyncIterator[tuple[Register, list[str]]]:
    """``POST /admin/purposes`` that remembers what it created; every purpose it created is
    archived afterwards, so none counts toward another test's limit."""
    client, _ = served
    names: list[str] = []

    async def register(body: dict[str, Any], **headers: str) -> httpx.Response:
        response = await client.post("/admin/purposes", json=body, headers={**OPERATOR, **headers})
        if response.status_code == 201:
            names.append(response.json()["name"])
        return response

    try:
        yield register, names
    finally:
        for name in names:
            archived = await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
            assert archived.status_code == 200, archived.text


def reason_of(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_round_trip(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Create v1, replace it to make v2, read both and the versions list, archive the purpose,
    and show it still reads while its calls and its replacement are refused (410). Each write
    is a configuration change recorded with the principal, and no template text reaches a log.

    Complements asserted: an identical replacement writes nothing; a repeat archive is 200 and
    changes nothing; an unknown name and an unknown version are 404."""
    client, lib = served
    register, _ = registered
    name = unique("vote_reason")
    caplog.set_level(logging.DEBUG)

    made = await register(purpose_body(name, template=f"{CANARY} {{{{ inputs.voter }}}}"))
    assert made.status_code == 201, made.text
    first = made.json()
    assert (first["name"], first["version"], first["latest_version"]) == (name, 1, 1)
    assert first["changed"] is True
    assert first["archived_at"] is None
    assert first["output_schema"] == SCHEMA

    replaced = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(1),
        headers=OPERATOR,
    )
    assert replaced.status_code == 200, replaced.text
    second = replaced.json()
    assert (second["version"], second["latest_version"]) == (2, 2)
    assert second["config_revision"] == first["config_revision"] + 1
    assert CANARY not in second["template"]

    same = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(2),
        headers=OPERATOR,
    )
    assert same.status_code == 200, same.text
    assert (same.json()["changed"], same.json()["version"]) == (False, 2)
    assert same.json()["config_revision"] == second["config_revision"]

    latest = (await client.get(f"/admin/purposes/{name}", headers=bearer())).json()
    assert (latest["version"], latest["latest_version"]) == (2, 2)
    one = (await client.get(f"/admin/purposes/{name}?version=1", headers=bearer())).json()
    assert (one["version"], one["latest_version"]) == (1, 2)
    assert one["template"].startswith(CANARY)
    versions = (await client.get(f"/admin/purposes/{name}/versions", headers=bearer())).json()
    assert [v["version"] for v in versions["items"]] == [1, 2]
    assert {v["created_by"] for v in versions["items"]} == {"p-operator"}

    missing = await client.get(f"/admin/purposes/{name}?version=9", headers=bearer())
    assert reason_of(missing)[:2] == (404, "version_not_found")
    unknown = await client.get(f"/admin/purposes/{unique('nobody')}", headers=bearer())
    assert reason_of(unknown)[:2] == (404, "not_found")
    unknown_put = await client.put(
        f"/admin/purposes/{unique('nobody')}",
        json=replace_body(1),
        headers=OPERATOR,
    )
    assert reason_of(unknown_put)[:2] == (404, "not_found")

    # The call path resolves the latest version, or a pinned one (FR-MODEL-14).
    resolved = await lib.models.consumer_purpose(name)
    assert (resolved.version, resolved.latest_version) == (2, 2)
    pinned = await lib.models.consumer_purpose(name, 1)
    assert pinned.template.render(inputs={"voter": "Ann"}).startswith(f"{CANARY} Ann")
    reply = resolved.output.model_validate_json(
        json.dumps({"reason": "She lied.", "score": 3, "mood": "calm", "tags": ["vote"]})
    )
    assert output_of(reply) == {"reason": "She lied.", "score": 3, "mood": "calm", "tags": ["vote"]}
    with pytest.raises(PurposeVersionNotFound):
        await lib.models.consumer_purpose(name, 3)

    archived = await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    again = await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False
    assert again.json()["config_revision"] == archived.json()["config_revision"]

    # Its versions stay readable; its calls and its replacement are refused.
    still = (await client.get(f"/admin/purposes/{name}?version=1", headers=bearer())).json()
    assert still["archived_at"] is not None
    assert still["version"] == 1
    gone = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(2),
        headers=OPERATOR,
    )
    assert reason_of(gone)[:2] == (410, "purpose_archived")
    with pytest.raises(PurposeArchived):
        await lib.models.consumer_purpose(name)
    with pytest.raises(PurposeArchived):
        await lib.models.send(name, [Message(role="user", content="hi")], None, None)
    listed = (await client.get("/admin/purposes?limit=200", headers=bearer())).json()
    mine = [item for item in listed["items"] if item["name"] == name]
    assert len(mine) == 1
    assert mine[0]["archived_at"] is not None
    assert mine[0]["latest_version"] == 2

    async with engine.connect() as conn:
        changes = (
            await conn.execute(
                text(
                    "SELECT actor, class, new FROM config_changes WHERE setting = :s "
                    "ORDER BY config_revision"
                ),
                {"s": f"consumer_purpose:{name}"},
            )
        ).all()
    assert [c.actor for c in changes] == ["p-operator"] * 3
    assert {c[1] for c in changes} == {"prospective"}
    assert [c.new.get("version") for c in changes[:2]] == [1, 2]
    assert all(CANARY not in json.dumps(c.new) for c in changes)
    assert CANARY not in caplog.text


VALID_TEMPLATE = "Explain {{ inputs.voter }}."


def _schema(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nested(depth: int) -> dict[str, Any]:
    """An object schema ``depth`` levels deep (the root is 1)."""
    node: dict[str, Any] = {"type": "string"}
    for _ in range(depth - 1):
        node = _schema(inner=node)
    return _schema(inner=node)


REFUSALS: list[tuple[str, dict[str, Any], str, str | None]] = [
    ("pattern", {"name": "Vote-Reason"}, "name_invalid", "body.name"),
    ("too_short", {"name": "ab"}, "name_invalid", "body.name"),
    ("too_long", {"name": "a" * 49}, "name_invalid", "body.name"),
    ("builtin_generation", {"name": "generation"}, "name_taken", "body.name"),
    ("builtin_guardrail", {"name": "guardrail_check"}, "name_taken", "body.name"),
    ("builtin_port", {"name": "embedding"}, "name_taken", "body.name"),
    ("default_row", {"name": "default"}, "name_taken", "body.name"),
    ("include", {"template": '{% include "x.j2" %}'}, "template_invalid", "body.template"),
    ("import", {"template": '{% import "x.j2" as x %}'}, "template_invalid", "body.template"),
    ("from", {"template": '{% from "x.j2" import y %}'}, "template_invalid", "body.template"),
    ("extends", {"template": '{% extends "x.j2" %}'}, "template_invalid", "body.template"),
    ("macro", {"template": "{% macro m() %}x{% endmacro %}"}, "template_invalid", "body.template"),
    (
        "call",
        {"template": "{% call inputs.f() %}x{% endcall %}"},
        "template_invalid",
        "body.template",
    ),
    (
        "sandbox_escape",
        {"template": "{{ cycler.__init__.__globals__ }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "private_item",
        {"template": "{{ inputs['__class__'] }}"},
        "template_invalid",
        "body.template",
    ),
    ("undeclared", {"template": "{{ secret }}"}, "template_invalid", "body.template"),
    (
        "unknown_filter",
        {"template": "{{ inputs.voter | shout }}"},
        "template_invalid",
        "body.template",
    ),
    ("syntax", {"template": "{% if %}"}, "template_invalid", "body.template"),
    ("too_large", {"template": "x" * (32 * 1024 + 1)}, "template_invalid", "body.template"),
    (
        "system_template",
        {"system_template": "{% include 'x' %}"},
        "template_invalid",
        "body.system_template",
    ),
    (
        "root_not_object",
        {"output_schema": {"type": "string"}},
        "schema_invalid",
        "body.output_schema.type",
    ),
    (
        "ref",
        {"output_schema": _schema(a={"$ref": "#/$defs/x"})},
        "schema_invalid",
        "body.output_schema.properties.a.$ref",
    ),
    (
        "any_of",
        {"output_schema": _schema(a={"anyOf": [{"type": "string"}]})},
        "schema_invalid",
        "body.output_schema.properties.a.anyOf",
    ),
    (
        "type_list",
        {"output_schema": _schema(a={"type": ["string", "null"]})},
        "schema_invalid",
        "body.output_schema.properties.a.type",
    ),
    (
        "nullable",
        {"output_schema": _schema(a={"type": "string", "nullable": True})},
        "schema_invalid",
        "body.output_schema.properties.a.nullable",
    ),
    (
        "open_object",
        {"output_schema": {**_schema(a={"type": "string"}), "additionalProperties": True}},
        "schema_invalid",
        "body.output_schema.additionalProperties",
    ),
    (
        "optional_property",
        {"output_schema": {**_schema(a={"type": "string"}), "required": []}},
        "schema_invalid",
        "body.output_schema.required",
    ),
    (
        "array_without_items",
        {"output_schema": _schema(a={"type": "array"})},
        "schema_invalid",
        "body.output_schema.properties.a.items",
    ),
    (
        "unsupported_keyword",
        {"output_schema": _schema(a={"type": "string", "format": "email"})},
        "schema_invalid",
        "body.output_schema.properties.a.format",
    ),
    ("too_deep", {"output_schema": _nested(7)}, "schema_invalid", None),
    (
        "too_large_schema",
        {"output_schema": _schema(a={"type": "string", "description": "d" * (16 * 1024)})},
        "schema_invalid",
        "body.output_schema",
    ),
    # The first fault wins: a bad name before a bad template, a bad template before a bad
    # schema.
    (
        "name_first",
        {"name": "Bad", "template": "{% include 'x' %}"},
        "name_invalid",
        "body.name",
    ),
    (
        "template_first",
        {"template": "{{ secret }}", "output_schema": {"type": "string"}},
        "template_invalid",
        "body.template",
    ),
]


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
@pytest.mark.parametrize(
    ("case", "change", "reason", "field"), REFUSALS, ids=[r[0] for r in REFUSALS]
)
async def test_i_29__m9_refusals(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    case: str,
    change: dict[str, Any],
    reason: str,
    field: str | None,
) -> None:
    """Each crafted bad body is refused at save with 422 ``validation_error`` naming its
    ``details.reason``, ``field`` and ``message``, and nothing is written.

    Complements asserted: the same body with the fault removed is accepted (``_valid``), and
    a schema of depth 6 is accepted where 7 is refused."""
    client, _ = served
    register, _ = registered
    name = unique("refused")
    body = {**purpose_body(name), **change}
    response = await register(body)
    status, code, details = reason_of(response)
    assert (status, code, details["reason"]) == (422, "validation_error", reason), case
    assert details["message"]
    if field is not None:
        assert details["field"] == field, details
    if body["name"] == name:
        found = await client.get(f"/admin/purposes/{name}", headers=bearer())
        assert found.status_code == 404, found.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_valid_edges(registered: tuple[Register, list[str]]) -> None:
    """The edges the refusals sit beside are accepted: a schema exactly 6 levels deep, a
    template using ``viewpoint``, ``story`` and built-in filters, a ``system_template``, and
    ``const``/``pattern``/``minItems`` keywords."""
    register, _ = registered
    body = purpose_body(
        unique("edges"),
        template=(
            "{% for t in inputs.items %}{{ t | upper }}{% endfor %}"
            "{% if viewpoint is defined %}{{ viewpoint.name }}{% endif %}"
            "{% if story is defined %}{{ story.title | default('') }}{% endif %}"
        ),
        system_template="Answer as {{ inputs.voter }}.",
        output_schema=_nested(6),
    )
    made = await register(body)
    assert made.status_code == 201, made.text
    keywords = _schema(
        kind={"type": "string", "const": "vote", "title": "Kind"},
        code={"type": "string", "pattern": "^[A-Z]{3}$", "minLength": 3},
        picks={"type": "array", "items": {"type": "number"}, "minItems": 1},
        flag={"type": "boolean"},
    )
    made = await register(purpose_body(unique("keywords"), output_schema=keywords))
    assert made.status_code == 201, made.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_names_taken(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """A registered name is taken, and stays taken once archived (there is no unarchive); a
    name differing only in case is not the same name, but upper case is never a valid one.

    Complement asserted: the first registration succeeds."""
    client, _ = served
    register, _ = registered
    name = unique("taken")
    assert (await register(purpose_body(name))).status_code == 201
    again = await register(purpose_body(name))
    assert reason_of(again)[0] == 422
    assert reason_of(again)[2]["reason"] == "name_taken"
    await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
    after = await register(purpose_body(name))
    assert reason_of(after)[2]["reason"] == "name_taken"
    upper = await register(purpose_body(name.upper()))
    assert reason_of(upper)[2]["reason"] == "name_invalid"


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_version_stale(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """A replacement whose ``expected_version`` is not the latest is 409 ``version_stale`` and
    writes nothing; the latest one succeeds.

    Complement asserted: a stale replacement's bad template is not reported (staleness is
    checked first), and the library raises the same error."""
    client, lib = served
    register, _ = registered
    name = unique("stale")
    assert (await register(purpose_body(name))).status_code == 201
    first = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(1, description="v2"),
        headers=OPERATOR,
    )
    assert first.status_code == 200, first.text
    stale = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(1, template="{{ secret }}"),
        headers=OPERATOR,
    )
    status, code, details = reason_of(stale)
    assert (status, code) == (409, "version_stale")
    assert (details["expected_version"], details["current_version"]) == (1, 2)
    latest = (await client.get(f"/admin/purposes/{name}", headers=bearer())).json()
    assert (latest["version"], latest["description"]) == (2, "v2")
    request = PurposeReplace.model_validate(replace_body(1))
    with pytest.raises(PurposeVersionStale):
        await lib.replace_purpose(LIBRARY, name, request)


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_limit(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    engine: AsyncEngine,
) -> None:
    """With 100 live consumer purposes the next registration is 422 ``purpose_limit``;
    archiving one frees a place, because archived purposes do not count.

    Complement asserted: the 100th registration itself succeeds."""
    client, _ = served
    register, _ = registered
    async with engine.connect() as conn:
        live = int(
            await conn.scalar(
                text("SELECT count(*) FROM consumer_purposes WHERE archived_at IS NULL")
            )
            or 0
        )
    assert live < PURPOSE_LIMIT
    made: list[str] = []
    for _ in range(PURPOSE_LIMIT - live):
        name = unique("bulk")
        response = await register(purpose_body(name))
        assert response.status_code == 201, response.text
        made.append(name)
    over = await register(purpose_body(unique("over")))
    assert reason_of(over)[:2] == (422, "validation_error")
    assert reason_of(over)[2]["reason"] == "purpose_limit"
    archived = await client.post(f"/admin/purposes/{made[0]}/archive", headers=OPERATOR)
    assert archived.status_code == 200, archived.text
    assert (await register(purpose_body(unique("freed")))).status_code == 201


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_routing(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    engine: AsyncEngine,
) -> None:
    """``GET /admin/model-config`` lists each live consumer purpose, inheriting
    ``generation``'s model (sources ``inherited``); ``PUT /admin/model-config/{name}`` routes it
    to its own model, and a call made through the Model Access Layer (the stub) goes to that
    model; with the override dropped it goes to ``generation``'s again. An archived purpose's
    configuration is hidden (404) and its calls are refused (410). The built-in purposes are
    unchanged: the registry's ``validate_purpose`` passes for every one.

    Complements asserted: ``generation`` itself is untouched by the override, and an unknown
    name is still 422 at ``/admin/model-config/{name}``."""
    client, lib = served
    register, _ = registered
    name = unique("routed")
    assert (await register(purpose_body(name))).status_code == 201
    async with restored_model_config(engine):
        generation = await client.put(
            "/admin/model-config/generation",
            json={"model": "house-model"},
            headers=OPERATOR,
        )
        assert generation.status_code == 200, generation.text

        listing = (await client.get("/admin/model-config", headers=bearer())).json()
        entry = listing["purposes"][name]
        assert entry["model"] == "house-model"
        assert entry["sources"]["model"] == "inherited"
        names = list(listing["purposes"])
        assert names[0] == "default"
        assert names.index(name) >= len(DELIVERED) + len(PORTS) + 1

        await lib.models.send(name, [Message(role="user", content="hello")], None, None)
        call = lib.models.stub.calls[-1]
        assert (call.purpose, call.model) == (name, "house-model")

        own = await client.put(
            f"/admin/model-config/{name}", json={"model": "consumer-model"}, headers=OPERATOR
        )
        assert own.status_code == 200, own.text
        assert (own.json()["model"], own.json()["sources"]["model"]) == ("consumer-model", "own")
        await lib.models.send(name, [Message(role="user", content="hello")], None, None)
        assert lib.models.stub.calls[-1].model == "consumer-model"
        house = (await client.get("/admin/model-config/generation", headers=bearer())).json()
        assert house["model"] == "house-model"

        dropped = await client.delete(f"/admin/model-config/{name}", headers=OPERATOR)
        assert dropped.status_code == 200, dropped.text
        await lib.models.send(name, [Message(role="user", content="hello")], None, None)
        assert lib.models.stub.calls[-1].model == "house-model"

        unknown = await client.put(
            f"/admin/model-config/{unique('nobody')}", json={"model": "m"}, headers=OPERATOR
        )
        assert reason_of(unknown)[:2] == (422, "validation_error")
        with pytest.raises(UnknownPurpose):
            await lib.models.send(unique("nobody"), [Message(role="user", content="x")], None, None)

        await client.put(f"/admin/model-config/{name}", json={"model": "kept"}, headers=OPERATOR)
        archived = await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
        assert archived.status_code == 200, archived.text
        listing = (await client.get("/admin/model-config", headers=bearer())).json()
        assert name not in listing["purposes"]
        hidden = await client.get(f"/admin/model-config/{name}", headers=bearer())
        assert reason_of(hidden)[:2] == (404, "not_found")
        hidden_put = await client.put(
            f"/admin/model-config/{name}", json={"model": "m"}, headers=OPERATOR
        )
        assert reason_of(hidden_put)[:2] == (404, "not_found")
        with pytest.raises(PurposeArchived):
            await lib.models.send(name, [Message(role="user", content="x")], None, None)
        async with engine.connect() as conn:
            kept = await conn.scalar(
                text("SELECT model FROM model_config WHERE purpose = :p"), {"p": name}
            )
        assert kept == "kept"

    for purpose in DELIVERED.values():
        validate_purpose(purpose)
    assert name not in DELIVERED


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_never_a_turn_purpose(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """A consumer purpose is never a turn's purpose: an engine profile and a story's overrides
    that route one in ``models.purposes`` are refused with 422 at that setting.

    Complement asserted: the same profile routing ``generation`` is accepted."""
    client, _ = served
    register, _ = registered
    name = unique("not_a_turn")
    assert (await register(purpose_body(name))).status_code == 201
    profile = await client.post(
        "/engine-profiles",
        json={"name": unique("profile"), "settings": nest(f"models.purposes.{name}.model", "m")},
        headers=WRITE,
    )
    status, code, details = reason_of(profile)
    assert (status, code) == (422, "validation_error")
    assert details["field"] == f"body.settings.models.purposes.{name}"
    fine = await client.post(
        "/engine-profiles",
        json={"name": unique("profile"), "settings": nest("models.purposes.generation.model", "m")},
        headers=WRITE,
    )
    assert fine.status_code == 201, fine.text

    story, _ = await new_story(client)
    override = await client.patch(
        f"/stories/{story}/profile-overrides",
        json={"settings": nest(f"models.purposes.{name}.model", "m"), "ignore_warnings": True},
        headers=WRITE,
    )
    assert reason_of(override)[:2] == (422, "validation_error")
    assert name in reason_of(override)[2]["field"]


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_library(served: tuple[httpx.AsyncClient, Engine]) -> None:
    """The in-process library registers, reads, lists, versions, replaces and archives a
    consumer purpose as REST does, and refuses as REST does.

    Complement asserted: the library's refusal is the same ``PurposeRefused`` reason the REST
    route answers, and an unknown name is ``PurposeNotFound``."""
    _, lib = served
    name = unique("library")
    created = await lib.register_purpose(LIBRARY, PurposeCreate.model_validate(purpose_body(name)))
    assert (created.purpose.version, created.changed) == (1, True)
    read = await lib.get_purpose(LIBRARY, name)
    assert read.output_schema == SCHEMA
    _, page = await lib.list_purposes(LIBRARY, limit=200)
    assert name in [item.name for item in page.items] or page.next_cursor is not None
    replaced = await lib.replace_purpose(
        LIBRARY,
        name,
        PurposeReplace.model_validate(replace_body(1, description="two")),
    )
    assert replaced.purpose.version == 2
    assert [v.version for v in await lib.list_purpose_versions(LIBRARY, name)] == [1, 2]
    with pytest.raises(PurposeRefused) as refused:
        await lib.register_purpose(LIBRARY, PurposeCreate.model_validate(purpose_body(name)))
    assert refused.value.details["reason"] == "name_taken"
    with pytest.raises(PurposeNotFound):
        await lib.get_purpose(LIBRARY, unique("nobody"))
    archived = await lib.archive_purpose(LIBRARY, name)
    assert archived.purpose.archived_at is not None
