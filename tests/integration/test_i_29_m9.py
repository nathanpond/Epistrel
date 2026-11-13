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

import dataclasses
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
    GuardrailViolation,
    IdempotencyConflict,
    InvalidRequest,
    ModuleOff,
    OutputPolicyViolation,
    PurposeArchived,
    PurposeNotFound,
    PurposeVersionNotFound,
    PurposeVersionStale,
    StoryNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.purposes.output import output_model, output_of
from epistrel.core.purposes.store import PurposeCreate, PurposeReplace
from epistrel.core.purposes.validation import (
    ATTRIBUTE_POSITIONS,
    PURPOSE_LIMIT,
    PurposeRefused,
    check_template,
    environment,
)
from epistrel.engine import Engine
from epistrel.orchestrator.models.adapter import Message, UnknownPurpose
from epistrel.orchestrator.models.registry import DELIVERED, PORTS, validate_purpose
from epistrel.orchestrator.purposes.call import GUIDANCE_HEADING, PREAMBLE
from epistrel.testing.profiles import structured_m4
from tests import genesis_support as g
from tests import seeded_partitions_support as sp
from tests.auth_support import auth_settings, bearer
from tests.concurrency_support import start_turn
from tests.event_support import rows as engine_rows
from tests.gate_support import input_policies as gate_input_policies
from tests.minors_support import recognise, subject_checks
from tests.model_support import restored_model_config
from tests.policy_support import rated_policy_rating, words
from tests.profile_support import nest
from tests.respond_support import body as turn_body
from tests.respond_support import new_story, respond, respond_client
from tests.story_support import READ, WRITE, character, create, override

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


CALL: dict[str, Any] = {"inputs": {"voter": "Ash", "target": "Briar"}}
"""A call of ``purpose_body``'s template (#825)."""
SECOND_ENDPOINT = "http://second-stub.invalid/v1"


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


def _nested_answer(depth: int) -> dict[str, Any]:
    """A value valid against ``_nested(depth)``."""
    value: dict[str, Any] = {"inner": "deep"}
    for _ in range(depth - 1):
        value = {"inner": value}
    return value


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
    (
        "attr_filter",
        {"template": "{{ inputs|attr('__class__') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "computed_item",
        {"template": "{{ inputs['_' ~ '_class__'] }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "private_map_attribute",
        {"template": "{{ inputs.items|map(attribute='__class__')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "private_selectattr",
        {"template": "{{ inputs.items|selectattr('_x')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "computed_attribute",
        {"template": "{{ inputs.items|map(**inputs.k)|list }}"},
        "template_invalid",
        "body.template",
    ),
    # #847: an attribute or item named at render time by any other route.
    (
        "positional_sum",
        {"template": "{{ inputs.items|sum('_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "positional_sort",
        {"template": "{{ inputs.items|sort(false, false, '_y')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "positional_unique",
        {"template": "{{ inputs.items|unique(false, '_y')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "positional_min",
        {"template": "{{ inputs.items|min(false, '__class__') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "positional_max",
        {"template": "{{ inputs.items|max(false, '_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "positional_join",
        {"template": "{{ inputs.items|join(',', '_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_attr",
        {"template": "{{ inputs.items|map('attr', '__class__')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_nested",
        {"template": "{{ inputs.items|map('sum', '_y')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_computed",
        {"template": "{{ inputs.items|map(inputs.f)|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "str_format",
        {"template": "{{ '{0.__class__}'.format(inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "format_map",
        {"template": "{{ '{_y}'.format_map(inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "format_taken",
        {"template": "{% set f = inputs.s.format %}{{ f(inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "get_private",
        {"template": "{{ inputs.get('_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "get_computed",
        {"template": "{{ inputs.get(inputs.k) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "get_taken",
        {"template": "{% set g = inputs.get %}{{ g('_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "printf_private",
        {"template": "{{ '%(_y)s' % inputs }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "printf_computed",
        {"template": "{{ inputs.f % inputs }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "format_filter_kwargs",
        {"template": "{{ '%(_y)s'|format(**inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "self",
        {"template": "{{ self }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "self_block",
        {"template": "{% block b %}x{% endblock %}{{ self.b() }}"},
        "template_invalid",
        "body.template",
    ),
    # #852: a string key or a filter's attribute name that reaches a method reading by name.
    (
        "key_format",
        {"template": "{{ '{0[_y]}'['format'](inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "key_format_map",
        {"template": "{{ '{_y}'['format_map'](inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "key_get",
        {"template": "{{ inputs['get']('_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "key_get_taken",
        {"template": "{% set g = inputs['get'] %}{{ g('_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_attribute_get",
        {"template": "{{ ([inputs]|map(attribute='get')|first)('_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_attribute_format",
        {"template": "{{ (['{0[_y]}']|map(attribute='format')|first)(inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "nested_key_format",
        {"template": "{{ inputs.ffmt['format'](inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "key_format_dunder",
        {"template": "{{ '{0.__class__}'['format'](inputs) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "groupby_get",
        {"template": "{{ (inputs.list|groupby('get')|first).grouper('_y') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "dotted_attribute_get",
        {"template": "{{ inputs.list|map(attribute='a.get')|list }}"},
        "template_invalid",
        "body.template",
    ),
    # #853: what would fail at render, refused at save.
    (
        "unknown_test",
        {"template": "{% if inputs.voter is nosuchtest %}x{% endif %}"},
        "template_invalid",
        "body.template",
    ),
    (
        "select_unknown_test",
        {"template": "{{ inputs.list|select('nosuchtest')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "selectattr_unknown_test",
        {"template": "{{ inputs.list|selectattr('name', 'nosuchtest')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "select_computed_test",
        {"template": "{{ inputs.list|select(inputs.t)|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_select_unknown_test",
        {"template": "{{ inputs.list|map('select', 'nosuchtest')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "super",
        {"template": "{% block b %}{{ super() }}{% endblock %}"},
        "template_invalid",
        "body.template",
    ),
    (
        "setdefault",
        {"template": "{{ inputs.setdefault('a', 1) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "pop",
        {"template": "{{ inputs.pop('a') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "key_pop",
        {"template": "{{ inputs['pop']('a') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "append",
        {"template": "{{ inputs.list.append(1) }}"},
        "template_invalid",
        "body.template",
    ),
    # #857: a call of a mutating name reached through a `{% set %}`/`{% with %}` alias, or
    # through a filter's attribute name.
    (
        "map_pop_called",
        {"template": "{{ ([inputs]|map(attribute='pop')|first)('voter') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_append_called",
        {"template": "{{ ([inputs.list]|map(attribute='append')|first)(1) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "set_append_called",
        {"template": "{% set p = inputs.list.append %}{{ p(1) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "set_key_pop_called",
        {"template": "{% set p = inputs['pop'] %}{{ p('voter') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "with_setdefault_called",
        {"template": "{% with p = inputs['setdefault'] %}{{ p('a', 1) }}{% endwith %}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_setdefault_called",
        {"template": "{{ ([inputs]|map(attribute='setdefault')|first)('a', 1) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "set_extend_called",
        {"template": "{% set p = inputs.list.extend %}{{ p([1]) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "sum_extend_called",
        {"template": "{{ ([inputs.list]|sum(attribute='extend'))([1]) }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "with_key_clear_called",
        {"template": "{% with c = inputs['clear'] %}{{ c() }}{% endwith %}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_dotted_clear_called",
        {"template": "{{ ([inputs]|map(attribute='list.clear')|list)[0]() }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "groupby_positional_clear_called",
        {"template": "{{ (inputs.list|groupby('clear')|first)[0]() }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "set_map_pop_called",
        {"template": "{% set p = [inputs]|map(attribute='pop')|first %}{{ p('voter') }}"},
        "template_invalid",
        "body.template",
    ),
    # #858: what fails on every render: a `{% set x.y %}`, a slice step of 0, a filter or
    # test missing its arguments, directly or through `map('<filter>', …)`.
    (
        "set_namespace_attribute",
        {"template": "{% set inputs.x = 1 %}x"},
        "template_invalid",
        "body.template",
    ),
    (
        "set_namespace_private",
        {"template": "{% set inputs._y = 1 %}x"},
        "template_invalid",
        "body.template",
    ),
    (
        "set_block_namespace",
        {"template": "{% set inputs.x %}y{% endset %}x"},
        "template_invalid",
        "body.template",
    ),
    (
        "slice_step_zero",
        {"template": "{{ inputs.list[::0] }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "slice_step_negative_zero",
        {"template": "{{ inputs.list[1:2:-0] }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "slice_step_positive_zero",
        {"template": "{{ inputs.list[::+0] }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_no_filter",
        {"template": "{{ inputs.list|map|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_keyword_no_filter",
        {"template": "{{ inputs.list|map(default=1)|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "selectattr_no_args",
        {"template": "{{ inputs.list|selectattr|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "rejectattr_no_args",
        {"template": "{{ inputs.list|rejectattr|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "replace_no_args",
        {"template": "{{ 'a'|replace }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "replace_one_arg",
        {"template": "{{ inputs.voter|replace('a') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "replace_one_keyword",
        {"template": "{{ inputs.voter|replace(new='b') }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "slice_zero",
        {"template": "{{ inputs.list|slice(0)|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "slice_keyword_zero",
        {"template": "{{ inputs.list|slice(slices=0)|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_replace_one_arg",
        {"template": "{{ inputs.list|map('replace', 'a')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_slice_zero",
        {"template": "{{ inputs.list|map('slice', 0)|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_selectattr_no_args",
        {"template": "{{ inputs.list|map('selectattr')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "select_test_no_args",
        {"template": "{{ inputs.list|select('divisibleby')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "reject_test_no_args",
        {"template": "{{ inputs.list|reject('sameas')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "selectattr_test_no_args",
        {"template": "{{ inputs.list|selectattr('votes', 'divisibleby')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "rejectattr_test_no_args",
        {"template": "{{ inputs.list|rejectattr('votes', 'eq')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "map_select_test_no_args",
        {"template": "{{ inputs.list|map('select', 'divisibleby')|list }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "is_test_no_args",
        {"template": "{% if inputs.voter is divisibleby %}x{% endif %}"},
        "template_invalid",
        "body.template",
    ),
    (
        "is_comparison_no_args",
        {"template": "{% if inputs.voter is gt %}x{% endif %}"},
        "template_invalid",
        "body.template",
    ),
    (
        "pop_taken",
        {"template": "{{ inputs.pop }}"},
        "template_invalid",
        "body.template",
    ),
    (
        "computed_slice",
        {"template": "{{ inputs.list[inputs.voter:] }}"},
        "template_invalid",
        "body.template",
    ),
    ("global_cycler", {"template": "{{ cycler }}"}, "template_invalid", "body.template"),
    ("global_joiner", {"template": "{{ joiner() }}"}, "template_invalid", "body.template"),
    ("global_namespace", {"template": "{{ namespace() }}"}, "template_invalid", "body.template"),
    ("global_dict", {"template": "{{ dict(a=1) }}"}, "template_invalid", "body.template"),
    ("global_lipsum", {"template": "{{ lipsum() }}"}, "template_invalid", "body.template"),
    ("global_range", {"template": "{{ range(3) }}"}, "template_invalid", "body.template"),
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


REPLACE_REFUSALS: list[str] = [
    "{{ '{0[_y]}'['format'](inputs) }}",
    "{{ inputs['get']('_y') }}",
    "{{ ([inputs]|map(attribute='get')|first)('_y') }}",
    "{{ ([inputs]|map(attribute='pop')|first)('voter') }}",
    "{{ ([inputs.list]|map(attribute='append')|first)(1) }}",
    "{% set p = inputs.list.append %}{{ p(1) }}",
    "{% set p = inputs['pop'] %}{{ p('voter') }}",
    "{% set inputs.x = 1 %}x",
    "{{ inputs.list[::0] }}",
    "{{ inputs.list|map|list }}",
    "{{ inputs.list|select('divisibleby')|list }}",
]
"""#852's, #857's and #858's routes, refused on ``PUT`` as on ``POST``."""


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_replace_refusals(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """A replacement is checked like a registration: each of #852's, #857's and #858's
    templates is
    refused on ``PUT /admin/purposes/{name}`` with 422 ``template_invalid``, and the purpose
    stays at version 1.

    Complement asserted: a valid replacement then writes version 2."""
    client, _ = served
    register, _ = registered
    name = unique("replaced")
    assert (await register(purpose_body(name))).status_code == 201
    for template in REPLACE_REFUSALS:
        refused = await client.put(
            f"/admin/purposes/{name}", json=replace_body(1, template=template), headers=OPERATOR
        )
        status, code, details = reason_of(refused)
        assert (status, code, details["reason"]) == (422, "validation_error", "template_invalid")
        assert details["field"] == "body.template", template
    found = await client.get(f"/admin/purposes/{name}", headers=bearer())
    assert found.json()["version"] == 1, found.text
    replaced = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(1, template="{{ inputs['get']('voter') }}"),
        headers=OPERATOR,
    )
    assert (replaced.status_code, replaced.json()["version"]) == (200, 2), replaced.text


MUTATING_CALLS: list[tuple[str, str]] = [
    ("{{ ([inputs]|map(attribute='pop')|first)('voter') }}", "pop"),
    ("{{ ([inputs.list]|map(attribute='append')|first)(1) }}", "append"),
    ("{% set p = inputs.list.append %}{{ p(1) }}", "append"),
    ("{% set p = inputs['pop'] %}{{ p('voter') }}", "pop"),
    ("{% with p = inputs['setdefault'] %}{{ p('a', 1) }}{% endwith %}", "setdefault"),
    ("{{ ([inputs]|map(attribute='setdefault')|first)('a', 1) }}", "setdefault"),
    ("{% set p = inputs.list.extend %}{{ p([1]) }}", "extend"),
    ("{{ ([inputs.list]|sum(attribute='extend'))([1]) }}", "extend"),
    ("{{ ([inputs]|map('sum', attribute='extend')|first)([1]) }}", "extend"),
    ("{% with c = inputs['clear'] %}{{ c() }}{% endwith %}", "clear"),
    ("{{ ([inputs]|map(attribute='list.clear')|list)[0]() }}", "clear"),
    ("{{ (inputs.list|groupby('clear')|first)[0]() }}", "clear"),
    ("{% set p = [inputs]|map(attribute='pop')|first %}{{ p('voter') }}", "pop"),
]
"""#857: calls of a mutating name reached through an alias or a filter's attribute name."""

READ_NOT_CALLED: list[tuple[str, str]] = [
    ("{% set p = inputs['pop'] %}{{ p }}", "P"),
    ("{{ ([inputs]|map(attribute='pop')|first) }}", "P"),
    ("{{ inputs['sort'] }}", "S"),
    ("{{ inputs.add }}", "A"),
    ("{% set x = inputs.append_text %}{{ x }}", "T"),
    # A rebinding clears the alias; a reading method of a mapped list is still callable.
    ("{% set p = inputs['pop'] %}{% set p = inputs.list.count %}{{ p(1) }}", "1"),
    ("{{ (inputs.rows|map(attribute='pop')|list).count(1) }}", "1"),
]
"""#857's complement: a mutating name bound or mapped and not called reads the key."""


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
def test_i_29__m9_mutating_calls() -> None:
    """#857: ``check_template`` refuses a call of a mutating method reached through a
    ``{% set %}``/``{% with %}`` alias or a filter's attribute name, naming the method, as it
    refuses the direct call (#853). Each would fail at render: the sandbox refuses the method,
    and a JSON value is not callable.

    Complement asserted: the same names bound or mapped without a call are accepted and render
    the key's value."""
    inputs = {
        "voter": "Ash",
        "list": [1, 2, 3],
        "pop": "P",
        "sort": "S",
        "add": "A",
        "append_text": "T",
        "rows": [{"pop": 1}],
    }
    for template, method in MUTATING_CALLS:
        with pytest.raises(PurposeRefused) as refused:
            check_template(template, "body.template")
        details = refused.value.details
        assert (details["reason"], details["field"], details["message"]) == (
            "template_invalid",
            "body.template",
            f"the template uses {method!r}, a method that changes its value",
        ), template
    for template, rendered in READ_NOT_CALLED:
        assert check_template(template, "body.template").render(inputs=inputs) == rendered


ALWAYS_FAILING: list[tuple[str, str]] = [
    ("{% set inputs.x = 1 %}x", "the template sets inputs.x, and 'inputs' is not a namespace"),
    ("{% set inputs._y = 1 %}x", "the template sets inputs._y, and 'inputs' is not a namespace"),
    ("{{ inputs.list[::0] }}", "the template uses a slice step of 0"),
    ("{{ inputs.list[::-0] }}", "the template uses a slice step of 0"),
    ("{{ inputs.list|map|list }}", "'map' needs 1 argument(s)"),
    ("{{ inputs.list|map(default=1)|list }}", "'map' needs 1 argument(s)"),
    ("{{ inputs.list|selectattr|list }}", "'selectattr' needs 1 argument(s)"),
    ("{{ inputs.list|rejectattr|list }}", "'rejectattr' needs 1 argument(s)"),
    ("{{ inputs.voter|replace }}", "'replace' needs 2 argument(s)"),
    ("{{ inputs.voter|replace('a') }}", "'replace' needs 2 argument(s)"),
    ("{{ inputs.voter|replace(old='a') }}", "'replace' needs 2 argument(s)"),
    ("{{ inputs.list|slice(0)|list }}", "the filter 'slice' makes 0 slices"),
    ("{{ inputs.list|slice(slices=0)|list }}", "the filter 'slice' makes 0 slices"),
    ("{{ inputs.list|map('replace', 'a')|list }}", "'replace' needs 2 argument(s)"),
    ("{{ inputs.list|map('slice', 0)|map('list')|list }}", "the filter 'slice' makes 0 slices"),
    ("{{ inputs.list|map('map')|map('list')|list }}", "'map' needs 1 argument(s)"),
    ("{{ inputs.list|select('divisibleby')|list }}", "'divisibleby' needs 1 argument(s)"),
    ("{{ inputs.list|reject('in')|list }}", "'in' needs 1 argument(s)"),
    ("{{ inputs.rows|selectattr('a', 'divisibleby')|list }}", "'divisibleby' needs 1 argument(s)"),
    ("{{ inputs.rows|rejectattr('a', 'eq')|list }}", "'eq' needs 1 argument(s)"),
    ("{{ inputs.list|select('eq', b=1)|list }}", "'eq' needs 1 argument(s)"),
    ("{{ inputs.voter is sameas }}", "'sameas' needs 1 argument(s)"),
    (
        "{{ inputs.list|map('select', 'divisibleby')|map('list')|list }}",
        "'divisibleby' needs 1 argument(s)",
    ),
]
"""#858: templates that fail on every render, each with the message that names its construct."""

STILL_SAVED: list[tuple[str, str]] = [
    ("{{ inputs.list|select|list }}", "[1, 2, 3]"),
    ("{{ inputs.list|reject|list }}", "[]"),
    ("{{ inputs.list[::2] }}", "[1, 3]"),
    ("{{ inputs.list[::-1] }}", "[3, 2, 1]"),
    ("{{ inputs.names|map('upper')|join }}", "ABCD"),
    ("{{ inputs.rows|selectattr('a')|list }}", "[{'a': 1}]"),
    ("{{ inputs.voter|replace('A', 'E') }}", "Esh"),
    ("{{ inputs.voter|replace(old='A', new='E') }}", "Esh"),
    ("{{ inputs.list|select('divisibleby', 3)|list }}", "[3]"),
    ("{{ inputs.list|select('divisibleby', num=3)|list }}", "[3]"),
    ("{{ inputs.list[2] is divisibleby(3) }}", "True"),
    ("{{ inputs.list|slice(2)|list }}", "[[1, 2], [3]]"),
    ("{{ inputs.rows|map(attribute='a', default=0)|list }}", "[1, 0]"),
    ("{{ inputs.names|map('replace', old='a', new='A')|join }}", "Abcd"),
    ("{{ inputs.rows|selectattr('a', 'eq', 1)|list }}", "[{'a': 1}]"),
]
"""#858's complement: the accepted neighbours of ``ALWAYS_FAILING``, with what they render."""


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
def test_i_29__m9_always_failing() -> None:
    """#858: ``check_template`` refuses a template the scan can see fails on every render,
    naming the construct: a ``{% set x.y %}`` target (no namespace exists), a constant zero
    slice step, ``slice(0)``, ``map`` with no filter, ``selectattr``/``rejectattr`` with no
    arguments, and a filter or test given fewer arguments than it needs, directly, through
    ``select``/``reject``/``selectattr``/``rejectattr`` or through ``map('<filter>', …)``.
    Each does fail when rendered without the scan (a generator ``map`` yields fails once it
    is read, so those rows read it).

    Complement asserted: the neighbours in ``STILL_SAVED`` are accepted and render."""
    inputs = {
        "voter": "Ash",
        "list": [1, 2, 3],
        "names": ["ab", "cd"],
        "rows": [{"a": 1}, {"a": 0}],
    }
    env = environment()
    for template, message in ALWAYS_FAILING:
        with pytest.raises(PurposeRefused) as refused:
            check_template(template, "body.template")
        details = refused.value.details
        assert (details["reason"], details["field"], details["message"]) == (
            "template_invalid",
            "body.template",
            message,
        ), template
        with pytest.raises(Exception):  # noqa: B017, PT011 - any render error
            env.from_string(template).render(inputs=inputs)
    for template, rendered in STILL_SAVED:
        assert check_template(template, "body.template").render(inputs=inputs) == rendered


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-14")
async def test_i_29__m9_valid_edges(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """The edges the refusals sit beside are accepted: a schema exactly 6 levels deep, a
    template using ``viewpoint``, ``story`` and built-in filters, a ``system_template``, and
    ``const``/``pattern``/``minItems`` keywords. #853: the template also renders, on a call
    with no story, to the text its edges give.

    #847: the scan checks each attribute-reading filter's name argument at the position
    Jinja's own signature gives it; the positions are pinned here so a Jinja upgrade that moves
    one fails this test rather than opening a gap."""
    assert dict(ATTRIBUTE_POSITIONS) == {
        "selectattr": 0,
        "rejectattr": 0,
        "groupby": 0,
        "sum": 0,
        "join": 1,
        "unique": 1,
        "min": 1,
        "max": 1,
        "sort": 2,
    }
    client, lib = served
    register, _ = registered
    name = unique("edges")
    body = purpose_body(
        name,
        template=(
            "{% for t in inputs.names %}{{ t | upper }}{% endfor %}|"
            # #837: constant keys, a slice and a constant attribute name stay accepted.
            "{{ inputs['voter'] }}|{{ inputs.list[0].name }}|"
            "{{ inputs.list[1:] | join(',', 'name') }}|"
            "{{ inputs.list | map(attribute='name') | join(',') }}|"
            # #848: a negative constant index is a constant key; #853: constant slice bounds.
            "{{ inputs.list[-1].name }}|{{ inputs.list[-1]['name'] }}|"
            "{{ inputs.names[-2:-1] | join }}|{{ inputs.names[::2] | join }}|"
            # #847: the same routes with constant, public names stay accepted.
            "{{ inputs.list | sum('votes') }}|"
            "{{ inputs.list | sort(false, false, 'name') | map(attribute='name') | join }}|"
            "{{ inputs.list | join(', ', 'name') }}|{{ inputs.names | map('upper') | join }}|"
            "{{ inputs.get('voter') }}|{{ inputs.get('missing', '-') }}|{{ '%(voter)s' % inputs }}|"
            "{{ '%s and %%(_y)s' % inputs.voter }}|"
            "{% for t in inputs.names %}{{ loop.index % 2 }}{% endfor %}|"
            # #852: a constant key naming 'get' is held to .get's rule, not refused.
            "{{ inputs['get']('voter') }}|"
            # #853: built-in tests, by name and through select/selectattr; a key that is read.
            "{{ inputs.names | select('string') | join }}|"
            "{{ inputs.list | selectattr('votes', 'odd') | map(attribute='name') | join }}|"
            "{% if inputs.voter is string %}s{% endif %}|{{ inputs['sort'] }}|"
            # #857: a mutating name that is bound or mapped and not called reads the key.
            "{% set p = inputs['pop'] %}{{ p }}|{{ ([inputs]|map(attribute='pop')|first) }}|"
            "{{ inputs.add }}|{% set x = inputs.append_text %}{{ x }}|"
            # #858: truthiness select/reject, nonzero steps and filters/tests given their
            # arguments.
            "{{ inputs.names | select | join }}|{{ inputs.names | reject | list }}|"
            "{{ inputs.names[::-1] | join }}|{{ inputs.voter | replace('A', 'E') }}|"
            "{{ inputs.voter | replace(old='s', new='S') }}|"
            "{{ inputs.list | selectattr('name') | map(attribute='name') | join }}|"
            "{{ inputs.list | map(attribute='votes') | select('divisibleby', 2) | join }}|"
            "{% if inputs.list[1].votes is divisibleby(2) %}even{% endif %}|"
            "{% if viewpoint %}{{ viewpoint.name }}{% endif %}"
            "{% if story %}{{ story.title | default('') }}{% endif %}"
        ),
        system_template="Answer as {{ inputs.voter }}.",
        output_schema=_nested(6),
    )
    made = await register(body)
    assert made.status_code == 201, made.text
    lib.models.stub.script(name, [json.dumps(_nested_answer(6))])
    inputs = {
        "voter": "Ash",
        "names": ["ab", "cd", "ef"],
        "list": [{"name": "x", "votes": 1}, {"name": "y", "votes": 2}],
        "sort": "kept",
        "pop": "P",
        "add": "A",
        "append_text": "T",
    }
    called = await call(client, name, {"inputs": inputs})
    assert called.status_code == 200, called.text
    assert lib.models.stub.calls[-1].messages[1].content == (
        "ABCDEF|Ash|x|y|x,y|y|y|cd|abef|3|xy|x, y|ABCDEF|Ash|-|Ash|Ash and %(_y)s|101|Ash|"
        "abcdef|x|s|kept|P|P|A|T|abcdef|[]|efcdab|Esh|ASh|xy|2|even|"
    )
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
    model; with the override dropped it goes to ``generation``'s again. A REST call (#825,
    ``POST /purposes/{name}/calls``) follows the same route end to end: with no override it
    reaches ``generation``'s endpoint and model, and with one it reaches the second endpoint,
    which ``model.endpoint`` in its answer names. An archived purpose's
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
        # #825: the REST call goes where the configuration routes it, and says so.
        house_endpoint = (await lib.models.endpoint("generation"))[0]
        called = await client.post(f"/purposes/{name}/calls", json=CALL, headers=WRITE)
        assert called.status_code == 200, called.text
        assert called.json()["model"] == {
            "endpoint": house_endpoint,
            "model": "house-model",
            "version": None,
        }
        assert (lib.models.stub.calls[-1].purpose, lib.models.stub.calls[-1].model) == (
            name,
            "house-model",
        )

        own = await client.put(
            f"/admin/model-config/{name}",
            json={"model": "consumer-model", "base_url": SECOND_ENDPOINT},
            headers=OPERATOR,
        )
        assert own.status_code == 200, own.text
        assert (own.json()["model"], own.json()["sources"]["model"]) == ("consumer-model", "own")
        await lib.models.send(name, [Message(role="user", content="hello")], None, None)
        assert lib.models.stub.calls[-1].model == "consumer-model"
        called = await client.post(f"/purposes/{name}/calls", json=CALL, headers=WRITE)
        assert called.status_code == 200, called.text
        assert called.json()["model"]["endpoint"] == SECOND_ENDPOINT
        assert called.json()["model"]["model"] == "consumer-model"
        assert (lib.models.stub.calls[-1].purpose, lib.models.stub.calls[-1].model) == (
            name,
            "consumer-model",
        )
        assert house_endpoint != SECOND_ENDPOINT
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


# -- #825: calling a consumer purpose with no story (FR-MODEL-15) ------------------------------

MARKER = "QUOKKA-MARKER-825"
"""A unique string an input or an output carries; it must never reach a log line."""
BANNED = "zanzibarbanned"
"""The word the call-rating test policy refuses, on input and output."""
UNSAFE_PHRASE = "the nightswim scene"
"""Text the stub's guardrail model flags as sexualizing a depicted minor (in checks of the
age-conditioned policy only: ``minors_support.recognise``)."""


def answer(reason: str = "Because.", **fields: Any) -> str:
    """A reply valid against ``SCHEMA``, as the model's JSON."""
    return json.dumps({"reason": reason, "score": 3, "mood": "calm", "tags": [], **fields})


def own_calls(lib: Engine, name: str) -> int:
    """Calls to the purpose's own model (guardrail checks are not counted)."""
    return sum(1 for call in lib.models.stub.calls if call.purpose == name)


async def call(
    client: httpx.AsyncClient, name: str, body: dict[str, Any] | None = None, **headers: str
) -> httpx.Response:
    return await client.post(
        f"/purposes/{name}/calls", json=CALL if body is None else body, headers={**WRITE, **headers}
    )


@pytest.fixture
async def banned_rating(served: tuple[httpx.AsyncClient, Engine], engine: AsyncEngine) -> str:
    """A rating whose one policy refuses ``BANNED`` on input and output."""
    _, lib = served
    rating_id, _ = await rated_policy_rating(
        lib, engine, words(BANNED), applies_to=["text_input", "text_output"]
    )
    return rating_id


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A call with no story answers 200 ``{purpose, version, output, model, usage}``, its
    ``output`` valid against the purpose's schema; the template renders the inputs after
    Epistrel's preamble; a repeat with the same ``Idempotency-Key`` answers the first result
    with no model call; and neither an input nor an output string reaches a log line.

    Complements asserted: a pinned older version answers as that version; a different body
    under the same key is 409 ``idempotency_conflict``."""
    client, lib = served
    register, _ = registered
    name = unique("called")
    caplog.set_level(logging.DEBUG)
    assert (await register(purpose_body(name))).status_code == 201

    lib.models.stub.script(name, [answer(f"{MARKER} out", tags=["a", "b"])])
    body = {"inputs": {"voter": f"Ash {MARKER}", "target": "Briar", "rounds": [1, 2]}}
    first = await call(client, name, body, **{"Idempotency-Key": "k-825-call"})
    assert first.status_code == 200, first.text
    found = first.json()
    assert (found["purpose"], found["version"]) == (name, 1)
    assert found["output"] == json.loads(answer(f"{MARKER} out", tags=["a", "b"]))
    output_model(SCHEMA).model_validate(found["output"])
    assert found["usage"]["calls"] == 1
    assert found["usage"]["tokens_in"] > 0
    assert found["idempotency_key"] == "k-825-call"
    sent = [c for c in lib.models.stub.calls if c.purpose == name][-1]
    assert sent.messages[0].role == "system"
    assert sent.messages[0].content.startswith(GUIDANCE_HEADING)
    assert PREAMBLE in sent.messages[0].content
    assert sent.messages[1].content == f"Explain why Ash {MARKER} voted for Briar."

    replay = await call(client, name, body, **{"Idempotency-Key": "k-825-call"})
    assert replay.status_code == 200, replay.text
    assert replay.json() == found
    assert replay.headers["Idempotent-Replay"] == "true"
    assert own_calls(lib, name) == 1
    other = await call(client, name, CALL, **{"Idempotency-Key": "k-825-call"})
    assert reason_of(other)[:2] == (409, "idempotency_conflict")
    assert own_calls(lib, name) == 1

    replaced = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(1, template="Name {{ inputs.target }} only."),
        headers=OPERATOR,
    )
    assert replaced.status_code == 200, replaced.text
    latest = await call(client, name)
    assert latest.json()["version"] == 2
    assert lib.models.stub.calls[-1].messages[1].content == "Name Briar only."
    pinned = await call(client, name, {**CALL, "version": 1})
    assert pinned.json()["version"] == 1
    assert lib.models.stub.calls[-1].messages[1].content == "Explain why Ash voted for Briar."
    assert MARKER not in caplog.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call_refusals(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """An unknown purpose or version is 404, an archived purpose 410 whatever version is
    pinned, inputs that do not fit 422 (``too_large``, ``too_deep``, an unknown rating, a
    template that does not render: ``template_render_error``), all before any model call; an
    answer that never matches the schema is 502 ``model_output_invalid``; a passed deadline is
    503 ``model_unavailable`` with ``reason: deadline``.

    Complement asserted: inputs exactly at the size limit pass the size check."""
    client, lib = served
    register, _ = registered
    name = unique("refused")
    assert (await register(purpose_body(name))).status_code == 201

    unknown = await call(client, unique("nobody"))
    assert reason_of(unknown)[:2] == (404, "not_found")
    builtin = await call(client, "generation")
    assert reason_of(builtin)[:2] == (404, "not_found")
    version = await call(client, name, {**CALL, "version": 9})
    assert reason_of(version)[:2] == (404, "version_not_found")

    big = {"inputs": {"voter": "x" * (64 * 1024), "target": "y"}}
    status, code, details = reason_of(await call(client, name, big))
    assert (status, code, details["reason"], details["field"]) == (
        422,
        "validation_error",
        "too_large",
        "body.inputs",
    )
    padding = 64 * 1024 - len(json.dumps({"target": "y", "voter": ""}, separators=(",", ":")))
    exact = {"inputs": {"voter": "x" * padding, "target": "y"}}
    lib.models.stub.script(name, [answer()])
    assert (await call(client, name, exact)).status_code == 200
    deep: Any = "leaf"
    for _ in range(6):
        deep = [deep]
    status, code, details = reason_of(await call(client, name, {"inputs": {"voter": deep}}))
    assert (status, code, details["reason"]) == (422, "validation_error", "too_deep")
    rating = await call(client, name, {**CALL, "content_rating": "NO-SUCH-RATING"})
    status, code, details = reason_of(rating)
    assert (status, code, details["field"]) == (422, "validation_error", "body.content_rating")
    missing = await call(client, name, {"inputs": {"voter": "Ash"}})
    status, code, details = reason_of(missing)
    assert (status, code, details["field"]) == (422, "template_render_error", "template")
    depicted = [{"label": "Ash", "true_age": 30}, {"label": "Ash", "true_age": 31}]
    twice = await call(client, name, {**CALL, "depicted": depicted})
    assert reason_of(twice)[:2] == (422, "validation_error")
    calls_before = own_calls(lib, name)

    lib.models.stub.script(name, ["not json at all"] * 3)
    invalid = await call(client, name)
    status, code, details = reason_of(invalid)
    assert (status, code, details["reason"]) == (502, "model_output_invalid", "parse_error")
    assert "not json" not in invalid.text
    assert own_calls(lib, name) == calls_before + 3

    lib.models.stub.fault(name, "hang")
    late = await call(client, name, {**CALL, "deadline_ms": 300})
    status, code, details = reason_of(late)
    assert (status, code, details["reason"]) == (503, "model_unavailable", "deadline")

    archived = await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
    assert archived.status_code == 200, archived.text
    for body in (CALL, {**CALL, "version": 1}):
        assert reason_of(await call(client, name, body))[:2] == (410, "purpose_archived")


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call_input_gate(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    banned_rating: str,
    engine: AsyncEngine,
) -> None:
    """A string input that breaks a policy of the named rating, at any depth, is 422
    ``guardrail_violation`` naming its locator, with zero calls to the purpose's model and a
    ``guardrail_input_rejected`` audit row (no story, entry point ``purpose_call``, no text).
    With ``depicted`` naming a minor, an input that sexualizes them is refused by the minors
    policy, again with no call to the purpose's model.

    Complements asserted: the same input under the default rating passes (the rating is what
    refuses it), and the same minors input with only adults depicted passes."""
    client, lib = served
    register, _ = registered
    name = unique("gated_in")
    assert (await register(purpose_body(name))).status_code == 201
    banned = {"inputs": {"voter": "Ash", "target": "Briar", "notes": {"said": ["ok", BANNED]}}}

    refused = await call(client, name, {**banned, "content_rating": banned_rating})
    status, code, details = reason_of(refused)
    assert (status, code) == (422, "guardrail_violation")
    assert details["violations"][0]["locator"] == "inputs.notes.said[1]"
    assert own_calls(lib, name) == 0
    rows = await engine_rows(
        engine,
        "SELECT story_id, details FROM audit_log WHERE kind = 'guardrail_input_rejected' "
        "AND details->>'entry_point' = 'purpose_call' AND principal = 'p-author' "
        "ORDER BY id DESC LIMIT 1",
    )
    assert rows
    assert rows[0][0] is None
    assert BANNED not in json.dumps(rows[0][1])

    lib.models.stub.script(name, [answer()])
    passed = await call(client, name, banned)
    assert passed.status_code == 200, passed.text
    assert own_calls(lib, name) == 1

    recognise(lib.models.stub, UNSAFE_PHRASE)
    minor_body = {
        "inputs": {"voter": f"Ash in {UNSAFE_PHRASE}", "target": "Kit"},
        "depicted": [{"label": "Kit", "true_age": 15}],
    }
    minor = await call(client, name, minor_body)
    status, code, details = reason_of(minor)
    assert (status, code) == (422, "guardrail_violation")
    assert "a minor is involved or present" in minor.json()["error"]["message"]
    assert "Kit" not in minor.text
    assert own_calls(lib, name) == 1
    lib.models.stub.script(name, [answer()])
    adults = {**minor_body, "depicted": [{"label": "Kit", "true_age": 25}]}
    grown = await call(client, name, adults)
    assert grown.status_code == 200, grown.text
    assert own_calls(lib, name) == 2


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call_output_gate(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    banned_rating: str,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An output whose string leaf breaks a policy of the call's rating is retried once; a
    second violation is 422 ``output_policy_violation`` with no part of the output in the body
    (two calls to the purpose's model) and a ``guardrail_output_rejected`` audit row. With a
    minor depicted, an output that sexualizes them fails closed the same way.

    Complements asserted: a violation followed by a clean answer is 200 with the clean one;
    the same minors output with only adults depicted passes; no output text reaches a log."""
    client, lib = served
    register, _ = registered
    name = unique("gated_out")
    caplog.set_level(logging.DEBUG)
    assert (await register(purpose_body(name))).status_code == 201
    rated = {**CALL, "content_rating": banned_rating}
    bad = answer(f"{MARKER} said {BANNED}")

    lib.models.stub.script(name, [bad, answer(f"{MARKER} also {BANNED}", tags=[BANNED])])
    refused = await call(client, name, rated)
    status, code, details = reason_of(refused)
    assert (status, code, details["attempts"]) == (422, "output_policy_violation", 2)
    assert MARKER not in refused.text
    assert BANNED not in refused.text
    assert own_calls(lib, name) == 2
    rows = await engine_rows(
        engine,
        "SELECT story_id, details FROM audit_log WHERE kind = 'guardrail_output_rejected' "
        "AND details->>'purpose' = :name",
        name=name,
    )
    assert len(rows) == 1
    assert rows[0][0] is None
    assert MARKER not in json.dumps(rows[0][1])

    lib.models.stub.script(name, [bad, answer("Clean.")])
    retried = await call(client, name, rated)
    assert retried.status_code == 200, retried.text
    assert retried.json()["output"]["reason"] == "Clean."
    assert retried.json()["usage"]["calls"] == 2
    assert own_calls(lib, name) == 4

    recognise(lib.models.stub, UNSAFE_PHRASE)
    unsafe = answer(f"Kit at {UNSAFE_PHRASE}")
    minor_body = {**CALL, "depicted": [{"label": "Kit", "true_age": 15}]}
    lib.models.stub.script(name, [unsafe, unsafe])
    minor = await call(client, name, minor_body)
    assert reason_of(minor)[:2] == (422, "output_policy_violation")
    assert UNSAFE_PHRASE not in minor.text
    assert own_calls(lib, name) == 6
    checks = subject_checks(lib.models.stub)
    assert checks
    assert all("Kit" not in check for check in checks)
    lib.models.stub.script(name, [unsafe])
    adults = {**CALL, "depicted": [{"label": "Kit", "true_age": 25}]}
    grown = await call(client, name, adults)
    assert grown.status_code == 200, grown.text
    assert grown.json()["output"]["reason"] == f"Kit at {UNSAFE_PHRASE}"
    assert MARKER not in caplog.text
    assert UNSAFE_PHRASE not in caplog.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call_library(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """``Engine.call_purpose`` answers as REST does, and refuses as REST does.

    Complement asserted: a mistyped ``deadline_ms`` is refused at its parameter."""
    _, lib = served
    register, _ = registered
    name = unique("lib_call")
    assert (await register(purpose_body(name))).status_code == 201
    lib.models.stub.script(name, [answer("Library.")])
    result = await lib.call_purpose(
        LIBRARY, name, CALL["inputs"], depicted=[{"label": "Ash", "true_age": 40}]
    )
    assert (result.purpose, result.version, result.output["reason"]) == (name, 1, "Library.")
    assert result.usage.calls == 1
    with pytest.raises(PurposeNotFound):
        await lib.call_purpose(LIBRARY, unique("nobody"), CALL["inputs"])
    with pytest.raises(InvalidRequest) as mistyped:
        await lib.call_purpose(LIBRARY, name, CALL["inputs"], deadline_ms=True)
    assert mistyped.value.details["field"] == "query.deadline_ms"


def keyed(key: str) -> RequestContext:
    """A fresh library context under ``Idempotency-Key`` ``key`` (#842)."""
    return dataclasses.replace(LIBRARY, idempotency_key=key)


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call_library_idempotent(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """``Engine.call_purpose`` under an ``Idempotency-Key`` answers a repeat with the stored
    result and no second call to the purpose's model; a different body under the same key is
    refused with ``IdempotencyConflict``, as REST answers 409 ``idempotency_conflict`` (#842)."""
    _, lib = served
    register, _ = registered
    name = unique("lib_keyed")
    assert (await register(purpose_body(name))).status_code == 201
    ctx = keyed(unique("k-842-lib"))

    lib.models.stub.script(name, [answer("Stored.")])
    first = await lib.call_purpose(ctx, name, CALL["inputs"])
    assert first.output["reason"] == "Stored."
    assert own_calls(lib, name) == 1
    lib.models.stub.script(name, [answer("Second.")])
    again = await lib.call_purpose(ctx, name, CALL["inputs"])
    assert (first.replayed, again.replayed) == (False, True)
    assert dataclasses.replace(again, replayed=False) == first
    assert own_calls(lib, name) == 1
    with pytest.raises(IdempotencyConflict):
        await lib.call_purpose(ctx, name, {"voter": "Briar", "target": "Ash"})
    assert own_calls(lib, name) == 1


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call_library_gates(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    banned_rating: str,
) -> None:
    """``Engine.call_purpose`` runs the input gate (``GuardrailViolation`` naming its locator,
    no call to the purpose's model) and the output gate (one retry, then
    ``OutputPolicyViolation`` with no output), including a depicted minor on each side, as
    ``test_i_29__m9_call_input_gate`` and ``_output_gate`` assert over REST (#842). Audit rows
    are asserted over REST only."""
    _, lib = served
    register, _ = registered
    name = unique("lib_gated")
    assert (await register(purpose_body(name))).status_code == 201
    banned = {"voter": "Ash", "target": "Briar", "notes": {"said": ["ok", BANNED]}}

    with pytest.raises(GuardrailViolation) as refused_in:
        await lib.call_purpose(
            keyed(unique("k-842-in")), name, banned, content_rating=banned_rating
        )
    assert refused_in.value.details["violations"][0]["locator"] == "inputs.notes.said[1]"
    assert own_calls(lib, name) == 0

    lib.models.stub.script(name, [answer(f"said {BANNED}"), answer(f"also {BANNED}")])
    with pytest.raises(OutputPolicyViolation) as refused_out:
        await lib.call_purpose(
            keyed(unique("k-842-out")), name, CALL["inputs"], content_rating=banned_rating
        )
    assert refused_out.value.details["attempts"] == 2
    assert BANNED not in json.dumps(refused_out.value.details)
    assert own_calls(lib, name) == 2

    recognise(lib.models.stub, UNSAFE_PHRASE)
    minor = [{"label": "Kit", "true_age": 15}]
    with pytest.raises(GuardrailViolation) as minor_in:
        await lib.call_purpose(
            keyed(unique("k-842-minor-in")),
            name,
            {"voter": f"Ash in {UNSAFE_PHRASE}", "target": "Kit"},
            depicted=minor,
        )
    assert "a minor is involved or present" in minor_in.value.message
    assert own_calls(lib, name) == 2
    unsafe = answer(f"Kit at {UNSAFE_PHRASE}")
    lib.models.stub.script(name, [unsafe, unsafe])
    with pytest.raises(OutputPolicyViolation):
        await lib.call_purpose(
            keyed(unique("k-842-minor-out")), name, CALL["inputs"], depicted=minor
        )
    assert own_calls(lib, name) == 4


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-15")
async def test_i_29__m9_call_refusal_not_stored(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    banned_rating: str,
) -> None:
    """A refusal is never stored under its ``Idempotency-Key`` (FR-MODEL-15; #842): over REST
    and through the library, after a ``guardrail_violation`` or an ``output_policy_violation``
    under a key, the same key with a valid different body is answered 200 (not 409
    ``idempotency_conflict``) from a fresh call to the purpose's model. REST and the library
    use separate keys."""
    client, lib = served
    register, _ = registered
    name = unique("unstored")
    assert (await register(purpose_body(name))).status_code == 201
    banned = {"voter": "Ash", "target": "Briar", "notes": {"said": ["ok", BANNED]}}
    bad = answer(f"said {BANNED}")

    rest_in = {"Idempotency-Key": unique("k-842-rest-in")}
    refused = await call(
        client, name, {"inputs": banned, "content_rating": banned_rating}, **rest_in
    )
    assert reason_of(refused)[:2] == (422, "guardrail_violation")
    before = own_calls(lib, name)
    lib.models.stub.script(name, [answer("After input.")])
    retried = await call(client, name, CALL, **rest_in)
    assert retried.status_code != 409, retried.text
    assert retried.status_code == 200, retried.text
    assert retried.json()["output"]["reason"] == "After input."
    assert own_calls(lib, name) == before + 1

    rest_out = {"Idempotency-Key": unique("k-842-rest-out")}
    lib.models.stub.script(name, [bad, bad])
    refused = await call(client, name, {**CALL, "content_rating": banned_rating}, **rest_out)
    assert reason_of(refused)[:2] == (422, "output_policy_violation")
    before = own_calls(lib, name)
    lib.models.stub.script(name, [answer("After output.")])
    retried = await call(client, name, CALL, **rest_out)
    assert retried.status_code != 409, retried.text
    assert retried.status_code == 200, retried.text
    assert retried.json()["output"]["reason"] == "After output."
    assert own_calls(lib, name) == before + 1

    lib_in = keyed(unique("k-842-lib-in"))
    with pytest.raises(GuardrailViolation):
        await lib.call_purpose(lib_in, name, banned, content_rating=banned_rating)
    before = own_calls(lib, name)
    lib.models.stub.script(name, [answer("Library after input.")])
    result = await lib.call_purpose(lib_in, name, CALL["inputs"])
    assert result.output["reason"] == "Library after input."
    assert own_calls(lib, name) == before + 1

    lib_out = keyed(unique("k-842-lib-out"))
    lib.models.stub.script(name, [bad, bad])
    with pytest.raises(OutputPolicyViolation):
        await lib.call_purpose(lib_out, name, CALL["inputs"], content_rating=banned_rating)
    before = own_calls(lib, name)
    lib.models.stub.script(name, [answer("Library after output.")])
    result = await lib.call_purpose(lib_out, name, CALL["inputs"])
    assert result.output["reason"] == "Library after output."
    assert own_calls(lib, name) == before + 1


# -- a consumer purpose called inside a story (FR-MODEL-16; #826) -----------------------------

VIEWPOINT_TEMPLATE = (
    "{{ viewpoint.name }} | {{ story.id }} | {{ story.location }} | "
    "{% for name in story.present %}{{ name }};{% endfor %}\n{{ story.scene }}\n"
    "{{ viewpoint.text }}"
)
"""A story-bound purpose that shows everything a viewpoint call hands its template."""
STORY_TEMPLATE = "{{ story.id }} / {{ story.title }} / {{ inputs.voter }}"
SECRET_ROOT = "letter"
"""The Mother's affair (``seeded_partitions_support.AFFAIR``), by its root word."""


async def story_call(
    client: httpx.AsyncClient,
    story_id: str,
    name: str,
    body: dict[str, Any] | None = None,
    **headers: str,
) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/purposes/{name}/calls",
        json=CALL if body is None else body,
        headers={**WRITE, **headers},
    )


def prompt_of(lib: Engine, name: str, *, everything: bool = False) -> str:
    """The user message of the purpose's latest call (``everything``: every message, joined)."""
    sent = [c for c in lib.models.stub.calls if c.purpose == name][-1]
    if everything:
        return "\n".join(m.content for m in sent.messages)
    return sent.messages[-1].content


async def unchanged(engine: AsyncEngine, story_id: str) -> tuple[int, int, int]:
    """The story's timeline and config revisions and its event count."""
    found = await engine_rows(
        engine,
        "SELECT s.timeline_revision, s.config_revision, "
        "(SELECT count(*) FROM events e WHERE e.story_id = s.id) "
        "FROM stories s WHERE s.id = :s",
        s=story_id,
    )
    return int(found[0][0]), int(found[0][1]), int(found[0][2])


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_viewpoint(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The viewpoint is the character's alone (NFR-PRIV-4). In the Sunday Dinner with
    per-character partitions, only the Mother knows of her affair (her seeded secret: a belief,
    a canon fact and a memory visible to her alone). A call asking who keeps the love letters,
    with the Father's viewpoint, sends a prompt with no trace of it, neither its words nor their
    root; with the Mother's, the prompt carries it. Each prompt holds the viewpoint's own
    knowledge (the Father knows of the sale), its name, and the story's id, location, present
    names and the viewpoint's scene line. Neither call changes the story: its revisions and its
    event count are as they were, and no viewpoint text reaches a log.

    Complement asserted: run against a viewpoint built from the omniscient context, the
    Father's prompt carries the affair and this test fails (#826's fail-first run)."""
    client, lib = served
    register, _ = registered
    name = unique("viewpoint")
    caplog.set_level(logging.DEBUG)
    assert (await register(purpose_body(name, template=VIEWPOINT_TEMPLATE))).status_code == 201
    profile = await sp.partitioned_profile(client)
    characters = await sp.cast(client)
    scenario_id, _ = await g.published_scenario(client, sp.family())
    made = await g.create_from(client, sp.request(scenario_id, characters, profile))
    story = made["story_id"]
    by_role = {c["role_id"]: c["id"] for c in made["characters"]}
    before = await unchanged(engine, story)
    question = {"inputs": {"question": "Who keeps the love letters?"}}

    async def seen(role: str) -> str:
        response = await story_call(
            client, story, name, {**question, "viewpoint_character_id": by_role[role]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["timeline_revision"] == before[0]
        return prompt_of(lib, name)

    father = await seen("father")
    assert father.startswith(f"Frank Harrow | {story} | ")
    assert SECRET_ROOT not in prompt_of(lib, name, everything=True).casefold()
    assert sp.AFFAIR not in father
    assert "sale contract" in father.casefold()
    assert "## You are Frank Harrow" in father
    mother = await seen("mother")
    assert mother.startswith(f"Linda Harrow | {story} | ")
    assert "love letters" in mother.casefold()
    assert await unchanged(engine, story) == before
    assert SECRET_ROOT not in caplog.text.casefold()


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_refusals(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """A story-bound call answers 200 with ``timeline_revision``, and refuses in the planned
    order, each before any model call: an unknown story 404; an archived story 409
    ``story_archived`` (before its character is looked at); an unknown character 404; the
    player's character 422 ``player_has_no_context``; a viewpoint on a Lite story 409
    ``module_off`` (after the character checks); an unknown purpose 404; a template reading
    ``viewpoint`` when none was asked for 422 ``template_render_error``.

    Complements asserted: the same Lite story answers a call with no viewpoint, whose template
    sees the story's id and title; a structured story with shared character knowledge accepts a
    viewpoint."""
    client, lib = served
    register, _ = registered
    name, needs = unique("in_story"), unique("needs_viewpoint")
    assert (await register(purpose_body(name, template=STORY_TEMPLATE))).status_code == 201
    assert (await register(purpose_body(needs, template=VIEWPOINT_TEMPLATE))).status_code == 201
    story, ids = await new_story(client, "Ash", "Briar")
    calls_before = len(lib.models.stub.calls)

    def ask(character: str) -> dict[str, Any]:
        return {**CALL, "viewpoint_character_id": character}

    assert reason_of(await story_call(client, new_ulid(), name))[:2] == (404, "not_found")
    missing = await story_call(client, story, name, ask(new_ulid()))
    assert reason_of(missing)[:2] == (404, "not_found")
    status, code, details = reason_of(await story_call(client, story, name, ask(ids["Ash"])))
    assert (status, code, details["reason"]) == (422, "validation_error", "player_has_no_context")
    assert details["field"] == "body.viewpoint_character_id"
    lite = await story_call(client, story, name, ask(ids["Briar"]))
    assert reason_of(lite)[:2] == (409, "module_off")
    unknown = await story_call(client, story, unique("nobody"))
    assert reason_of(unknown)[:2] == (404, "not_found")
    rendered = await story_call(client, story, needs)
    status, code, details = reason_of(rendered)
    assert (status, code, details["field"]) == (422, "template_render_error", "template")
    assert len(lib.models.stub.calls) == calls_before

    lib.models.stub.script(name, [answer()])
    plain = await story_call(client, story, name)
    assert plain.status_code == 200, plain.text
    assert plain.json()["timeline_revision"] >= 0
    assert prompt_of(lib, name).endswith(f"{story} / A story / Ash")

    shared, shared_ids = await new_story(client, "Ash", "Briar")
    assigned = await client.put(
        f"/stories/{shared}/engine-profile",
        json={"profile_id": structured_m4(), "version": 1, "ignore_warnings": True},
        headers=WRITE,
    )
    assert assigned.status_code == 200, assigned.text
    config = (await client.get(f"/stories/{shared}/effective-config", headers=READ)).json()
    assert config["profile"] == {
        "id": structured_m4(),
        "version": 1,
        "pinned": True,
        "deleted": False,
    }
    assert config["effective"]["character_knowledge"]["partitions"] is False
    lib.models.stub.script(needs, [answer()])
    accepted = await story_call(client, shared, needs, ask(shared_ids["Briar"]))
    assert accepted.status_code == 200, accepted.text
    assert prompt_of(lib, needs).startswith(f"Briar | {shared} | ")

    archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
    assert archived.status_code == 200, archived.text
    late = await story_call(client, story, name, ask(new_ulid()))
    assert reason_of(late)[:2] == (409, "story_archived")


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_guardrails(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    banned_rating: str,
    engine: AsyncEngine,
) -> None:
    """The story's guardrails apply, as to a turn. Under the story's rating, an input that
    breaks its policy is 422 ``guardrail_violation`` with zero model calls (none at all) and a
    ``guardrail_input_rejected`` row naming the story; an output that breaks it twice is 422
    ``output_policy_violation``; the rating's prompt guidance leads the system message. With a
    15-year-old in the cast, an input that sexualizes is refused by the minors policy, with no
    call to the purpose's model.

    Complements asserted: a violation followed by a clean answer is 200; the same minors input
    in a story whose cast is all adults passes."""
    client, lib = served
    register, _ = registered
    name = unique("story_gated")
    assert (await register(purpose_body(name))).status_code == 201
    story, _ = await new_story(client, "Ash", "Briar", content_rating_id=banned_rating)

    calls_before = len(lib.models.stub.calls)
    banned = {"inputs": {"voter": "Ash", "target": "Briar", "notes": [BANNED]}}
    refused = await story_call(client, story, name, banned)
    status, code, details = reason_of(refused)
    assert (status, code) == (422, "guardrail_violation")
    assert details["violations"][0]["locator"] == "inputs.notes[0]"
    assert len(lib.models.stub.calls) == calls_before
    rows = await engine_rows(
        engine,
        "SELECT story_id, details FROM audit_log WHERE kind = 'guardrail_input_rejected' "
        "AND details->>'entry_point' = 'purpose_call' AND story_id = :s",
        s=story,
    )
    assert len(rows) == 1
    assert BANNED not in json.dumps(rows[0][1])

    bad = answer(f"said {BANNED}")
    lib.models.stub.script(name, [bad, bad])
    out = await story_call(client, story, name)
    assert reason_of(out)[:2] == (422, "output_policy_violation")
    assert BANNED not in out.text
    assert own_calls(lib, name) == 2
    lib.models.stub.script(name, [bad, answer("Clean.")])
    retried = await story_call(client, story, name)
    assert retried.status_code == 200, retried.text
    assert retried.json()["output"]["reason"] == "Clean."

    rated, _ = await new_story(client, "Ash", "Briar")
    lib.models.stub.script(name, [answer()])
    assert (await story_call(client, rated, name)).status_code == 200
    system = [c for c in lib.models.stub.calls if c.purpose == name][-1].messages[0].content
    assert system.startswith(GUIDANCE_HEADING)
    assert PREAMBLE in system

    recognise(lib.models.stub, UNSAFE_PHRASE)
    minor_cast = await create(
        client,
        {
            "name": "A story",
            "characters": [
                character("Ash", 30, is_player=True),
                character("Kit", 15),
            ],
        },
    )
    unsafe = {"inputs": {"voter": f"Ash in {UNSAFE_PHRASE}", "target": "Kit"}}
    own_before = own_calls(lib, name)
    minor = await story_call(client, minor_cast["story_id"], name, unsafe)
    assert reason_of(minor)[:2] == (422, "guardrail_violation")
    assert "a minor is involved or present" in minor.json()["error"]["message"]
    assert own_calls(lib, name) == own_before
    adults = await create(
        client,
        {
            "name": "A story",
            "characters": [character("Ash", 30, is_player=True), character("Kit", 25)],
        },
    )
    lib.models.stub.script(name, [answer()])
    grown = await story_call(client, adults["story_id"], name, unsafe)
    assert grown.status_code == 200, grown.text

    # #839: the output side of the minors policy, on the story-bound call's own guard context
    # (the latest scene's present set): an output sexualizing with the 15-year-old present is
    # refused after its retry, and the same output passes with the all-adult cast.
    unsafe_out = answer(f"Kit at {UNSAFE_PHRASE}")
    lib.models.stub.script(name, [unsafe_out, unsafe_out])
    own_before = own_calls(lib, name)
    minor_out = await story_call(client, minor_cast["story_id"], name)
    status, code, details = reason_of(minor_out)
    assert (status, code, details["attempts"]) == (422, "output_policy_violation", 2)
    assert UNSAFE_PHRASE not in minor_out.text
    assert own_calls(lib, name) == own_before + 2
    lib.models.stub.script(name, [unsafe_out])
    grown_out = await story_call(client, adults["story_id"], name)
    assert grown_out.status_code == 200, grown_out.text
    assert grown_out.json()["output"]["reason"] == f"Kit at {UNSAFE_PHRASE}"


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_read_only(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    engine: AsyncEngine,
) -> None:
    """A call reads the story and never changes it: while a turn of the story is in flight
    (held inside its model call), a call answers 200, and the story's timeline and config
    revisions and its event count are what they were before it; the turn then commits.

    #854: the call takes no story lock. While the story's real turn slot is held
    (``turn_gate.acquire``, as a queued turn's drainer holds it), a call still answers 200 with
    nothing changed, and the slot is still its holder's afterwards.

    Complements asserted: the held turn itself moves the revision once it commits, so the
    measure would see a write; and a write under the same held slot is 423 ``story_busy``
    (``test_i_29__m9_memory_refusals``)."""
    from epistrel.core import turn_gate

    client, lib = served
    register, _ = registered
    name = unique("read_only")
    assert (await register(purpose_body(name))).status_code == 201
    story, _ = await new_story(client, "Ash", "Briar")
    before = await unchanged(engine, story)
    held = await start_turn(engine, story, "Hello there.")
    try:
        lib.models.stub.script(name, [answer()])
        called = await story_call(client, story, name)
        assert called.status_code == 200, called.text
        assert called.json()["timeline_revision"] == before[0]
        assert await unchanged(engine, story) == before
        assert not held.done
    finally:
        await held.release()
        await held.task
    assert (await unchanged(engine, story))[2] > before[2]

    before = await unchanged(engine, story)
    holder = new_ulid()
    assert await turn_gate.acquire(engine, story, holder, lease_s=60) is None
    try:
        lib.models.stub.script(name, [answer()])
        called = await story_call(client, story, name)
        assert called.status_code == 200, called.text
        assert called.json()["timeline_revision"] == before[0]
        assert await unchanged(engine, story) == before
        assert await turn_gate.acquire(engine, story, new_ulid(), lease_s=60) is not None
    finally:
        await turn_gate.release_now(engine, story, holder)


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_version_and_paused(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    engine: AsyncEngine,
) -> None:
    """#855: a story-bound call takes the optional ``version`` as a story-less one does. With
    none it answers the latest version, and a pinned one answers that version with its
    template; a version the purpose never had is 404 ``version_not_found``. A paused
    story is still read (FR-MODEL-16): the call answers 200 and the revisions and event count
    are unchanged.

    Complement asserted: a turn of the paused story is 409 ``story_paused``, so the pause is
    real."""
    client, lib = served
    register, _ = registered
    name = unique("story_pinned")
    assert (await register(purpose_body(name))).status_code == 201
    replaced = await client.put(
        f"/admin/purposes/{name}",
        json=replace_body(1, template="Name {{ inputs.target }} only."),
        headers=OPERATOR,
    )
    assert replaced.status_code == 200, replaced.text
    story, ids = await new_story(client, "Ash", "Briar")

    lib.models.stub.script(name, [answer(), answer()])
    latest = await story_call(client, story, name)
    assert (latest.status_code, latest.json()["version"]) == (200, 2), latest.text
    assert prompt_of(lib, name) == "Name Briar only."
    pinned = await story_call(client, story, name, {**CALL, "version": 1})
    assert (pinned.status_code, pinned.json()["version"]) == (200, 1), pinned.text
    assert prompt_of(lib, name) == "Explain why Ash voted for Briar."
    missing = await story_call(client, story, name, {**CALL, "version": 9})
    assert reason_of(missing)[:2] == (404, "version_not_found")

    # What a tightening's history check leaves while it runs (``core.pause``).
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE stories SET status = 'paused' WHERE id = :s"), {"s": story})
    before = await unchanged(engine, story)
    lib.models.stub.script(name, [answer()])
    paused = await story_call(client, story, name)
    assert paused.status_code == 200, paused.text
    assert await unchanged(engine, story) == before
    turn = await respond(client, story, turn_body(ids["Ash"]))
    assert reason_of(turn)[:2] == (409, "story_paused")
    assert await unchanged(engine, story) == before


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_routing(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    engine: AsyncEngine,
) -> None:
    """A story-bound call follows the purpose's own model configuration (#824), not the
    story's: routed to a second endpoint, the call reaches it and says so, though the story's
    profile routes ``generation`` to a model of its own.

    Complement asserted: with the purpose's override dropped, the call reaches ``generation``'s
    deployment model, still never the story's."""
    client, lib = served
    register, _ = registered
    name = unique("story_routed")
    assert (await register(purpose_body(name))).status_code == 201
    story, _ = await new_story(
        client,
        "Ash",
        "Briar",
        overrides=nest("models.purposes", {"generation": {"model": "story-model"}}),
    )
    async with restored_model_config(engine):
        generation = await client.put(
            "/admin/model-config/generation", json={"model": "house-model"}, headers=OPERATOR
        )
        assert generation.status_code == 200, generation.text
        own = await client.put(
            f"/admin/model-config/{name}",
            json={"model": "consumer-model", "base_url": SECOND_ENDPOINT},
            headers=OPERATOR,
        )
        assert own.status_code == 200, own.text
        called = await story_call(client, story, name)
        assert called.status_code == 200, called.text
        assert called.json()["model"]["endpoint"] == SECOND_ENDPOINT
        assert called.json()["model"]["model"] == "consumer-model"
        assert lib.models.stub.calls[-1].model == "consumer-model"
        dropped = await client.delete(f"/admin/model-config/{name}", headers=OPERATOR)
        assert dropped.status_code == 200, dropped.text
        again = await story_call(client, story, name)
        assert again.status_code == 200, again.text
        assert again.json()["model"]["model"] == "house-model"
        assert lib.models.stub.calls[-1].model == "house-model"


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_library(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """``Engine.call_story_purpose`` answers as REST does, and refuses as REST does.

    Complement asserted: a mistyped ``viewpoint_character_id`` is refused at its parameter."""
    client, lib = served
    register, _ = registered
    name = unique("lib_story")
    assert (await register(purpose_body(name, template=STORY_TEMPLATE))).status_code == 201
    story, ids = await new_story(client, "Ash", "Briar")
    lib.models.stub.script(name, [answer("Library.")])
    result = await lib.call_story_purpose(LIBRARY, story, name, CALL["inputs"])
    assert (result.purpose, result.version, result.output["reason"]) == (name, 1, "Library.")
    assert result.timeline_revision is not None
    assert prompt_of(lib, name).endswith(f"{story} / A story / Ash")
    with pytest.raises(StoryNotFound):
        await lib.call_story_purpose(LIBRARY, new_ulid(), name, CALL["inputs"])
    with pytest.raises(ModuleOff):
        await lib.call_story_purpose(
            LIBRARY, story, name, CALL["inputs"], viewpoint_character_id=ids["Briar"]
        )
    with pytest.raises(InvalidRequest) as mistyped:
        await lib.call_story_purpose(
            LIBRARY,
            story,
            name,
            CALL["inputs"],
            viewpoint_character_id=7,  # type: ignore[arg-type]
        )
    assert mistyped.value.details["field"] == "body.viewpoint_character_id"


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_erased(pg_url: str, engine: AsyncEngine) -> None:
    """An input naming a subject erased from the story is 409 ``erased_subject_referenced``
    with no model call, and the erased character is no viewpoint (404: erasure deletes it).

    Complement asserted: the same call naming someone never erased answers 200."""
    from tests import erasure_support as es

    async with respond_client(es.erasure_settings(pg_url), engine) as (client, lib):
        name = unique("erased")
        made = await client.post("/admin/purposes", json=purpose_body(name), headers=OPERATOR)
        assert made.status_code == 201, made.text
        try:
            story, ids = await new_story(client, "Ash", "Briar", "Quillon")
            await es.erase(client, "character", ids["Quillon"], story, confirm_all=True)
            calls_before = len(lib.models.stub.calls)
            named = {"inputs": {"voter": "Quillon", "target": "Briar"}}
            refused = await story_call(client, story, name, named)
            assert reason_of(refused)[:2] == (409, "erased_subject_referenced")
            gone = await story_call(
                client, story, name, {**CALL, "viewpoint_character_id": ids["Quillon"]}
            )
            assert reason_of(gone)[:2] == (404, "not_found")
            assert len(lib.models.stub.calls) == calls_before
            lib.models.stub.script(name, [answer()])
            assert (await story_call(client, story, name)).status_code == 200
        finally:
            archived = await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
            assert archived.status_code == 200, archived.text


SCOPED = "marzipanbanned"
"""A word only a character-scoped policy (Briar's) refuses (#850)."""


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-16")
async def test_i_29__m9_story_character_policy(
    served: tuple[httpx.AsyncClient, Engine], registered: tuple[Register, list[str]]
) -> None:
    """A character-scoped policy applies only for the viewpoint character (#850). With a
    policy scoped to Briar on text input and output, an input carrying its word is 422
    ``guardrail_violation`` naming that policy, with zero calls to the purpose's model, when
    Briar is the viewpoint; an output carrying it twice is 422 ``output_policy_violation``.

    Complements asserted: the same input and the same output pass with Cato as the viewpoint
    and with no viewpoint, so the policy is neither the whole cast's nor the story's."""
    client, lib = served
    register, _ = registered
    name = unique("scoped")
    assert (await register(purpose_body(name))).status_code == 201
    story, ids = await partitioned_story(client)
    briar, cato = ids["Briar"], ids["Cato"]
    (policy,) = await gate_input_policies(
        lib,
        story,
        {
            "scope": "character",
            "scope_id": briar,
            "applies_to": ["text_input", "text_output"],
            "rule": words(SCOPED),
        },
    )
    said = {"inputs": {"voter": "Ash", "target": "Briar", "notes": [f"some {SCOPED}"]}}

    own_before = own_calls(lib, name)
    refused = await story_call(client, story, name, {**said, "viewpoint_character_id": briar})
    status, code, details = reason_of(refused)
    assert (status, code) == (422, "guardrail_violation"), refused.text
    assert [v["policy_id"] for v in details["violations"]] == [policy.id]
    assert own_calls(lib, name) == own_before
    for viewpoint in (cato, None):
        lib.models.stub.script(name, [answer()])
        body = said if viewpoint is None else {**said, "viewpoint_character_id": viewpoint}
        passed = await story_call(client, story, name, body)
        assert passed.status_code == 200, (viewpoint, passed.text)

    bad = answer(f"said {SCOPED}")
    lib.models.stub.script(name, [bad, bad])
    out = await story_call(client, story, name, {**CALL, "viewpoint_character_id": briar})
    assert reason_of(out)[:2] == (422, "output_policy_violation"), out.text
    assert SCOPED not in out.text
    for viewpoint in (cato, None):
        lib.models.stub.script(name, [bad])
        body = CALL if viewpoint is None else {**CALL, "viewpoint_character_id": viewpoint}
        passed = await story_call(client, story, name, body)
        assert passed.status_code == 200, (viewpoint, passed.text)
        assert passed.json()["output"]["reason"] == f"said {SCOPED}"


# -- a purpose result stored as the viewpoint character's memory (FR-MODEL-17; #827) ---------

MEMORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memory": {"type": "string", "description": "What the character remembers."},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "detail": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
            "additionalProperties": False,
        },
    },
    "required": ["memory", "score", "detail"],
    "additionalProperties": False,
}
"""An output whose ``memory`` field is a top-level string (``score`` and ``detail.note`` are
not one)."""
MEMORY_TEMPLATE = "Why did {{ viewpoint.name }} vote for {{ inputs.target }}?"
REMEMBERED = "PELICAN-827"
"""A unique word the stored memory carries; it must never reach a log line."""
RECALL_TEMPLATE = "{{ viewpoint.name }}\n{{ viewpoint.text }}\n{{ inputs.question }}"
"""A viewpoint call that shows the character's context (#849): it renders ``viewpoint.text``,
which ``MEMORY_TEMPLATE`` does not."""
RECALL = {"inputs": {"question": "Who did you vote for, and why?"}}
"""A question that does not carry ``REMEMBERED``, so only the viewpoint can put it in a prompt."""
STORE = {"text_field": "memory"}


def remembered(text: str = f"I voted for Briar because of the {REMEMBERED} pact.") -> str:
    """A reply valid against ``MEMORY_SCHEMA``, as the model's JSON."""
    return json.dumps({"memory": text, "score": 4, "detail": {"note": "n"}})


def store_body(character_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "inputs": {"target": "Briar"},
        "viewpoint_character_id": character_id,
        "store_as_memory": STORE,
        **fields,
    }


async def partitioned_story(
    client: httpx.AsyncClient, *names: str, **fields: Any
) -> tuple[str, dict[str, str]]:
    """A story (the first name the player) under ``Structured (M4)`` with per-character
    partitions (``seeded_partitions_support.partitioned_profile``)."""
    story, ids = await new_story(client, *(names or ("Ash", "Briar", "Cato")), **fields)
    profile = await sp.partitioned_profile(client, f"Partitions {new_ulid()[-6:]}")
    assigned = await client.put(
        f"/stories/{story}/engine-profile",
        json={"profile_id": profile, "ignore_warnings": True},
        headers=WRITE,
    )
    assert assigned.status_code == 200, assigned.text
    return story, ids


async def memories_of(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's consumer purpose memories, active or not."""
    return await engine_rows(
        engine,
        "SELECT id, text, holder_character_id, character_id, visibility, provenance, "
        "trust_tier, kind, fidelity, channel, membership, importance, proposition_ids, "
        "participants, event_type, operation_id, source_event_ids FROM memories "
        "WHERE story_id = :s AND provenance = 'consumer_purpose' ORDER BY id",
        s=story_id,
    )


async def revision_of(engine: AsyncEngine, story_id: str) -> int:
    return (await unchanged(engine, story_id))[0]


@pytest.fixture
async def memory_purpose(registered: tuple[Register, list[str]]) -> str:
    """A live purpose whose output has a top-level string ``memory``."""
    register, _ = registered
    name = unique("remember")
    made = await register(purpose_body(name, template=MEMORY_TEMPLATE, output_schema=MEMORY_SCHEMA))
    assert made.status_code == 201, made.text
    return name


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-17")
async def test_i_29__m9_memory_isolation(
    served: tuple[httpx.AsyncClient, Engine],
    registered: tuple[Register, list[str]],
    memory_purpose: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``store_as_memory`` writes the named output field as one episodic memory of the
    viewpoint character alone: holder and visibility ``characters: [Briar]``, fidelity
    ``content``, channel ``self``, provenance ``consumer_purpose`` (trust tier ``in_world``), the
    text as given, no propositions, no event type, participants ``[Briar]`` and no importance
    yet. It is written by an ``inter_turn`` slot operation at ``N⁺1`` that records one
    ``purpose_memory_stored`` event holding identifiers only, and the call answers the memory's
    id and the revision the write left (one more). Only that character remembers: a later
    viewpoint call whose template renders ``viewpoint.text`` (built by ``build_context``) and
    whose question does not name the memory sends Briar's context holding it, and Cato's
    without it. The memory's text never reaches a log.

    Complements asserted: the same call without ``store_as_memory`` stores nothing, answers
    ``memory_id: null`` and leaves the revision where it was; before the write, Briar's context
    does not hold the memory either (#849: so the later check is the memory's doing, and a
    viewpoint that leaked it to every character, or held no memories, fails this test)."""
    client, lib = served
    name = memory_purpose
    caplog.set_level(logging.DEBUG)
    story, ids = await partitioned_story(client)
    briar, cato = ids["Briar"], ids["Cato"]
    before = await revision_of(engine, story)
    register, _ = registered
    recall = unique("recall")
    made = await register(
        purpose_body(recall, template=RECALL_TEMPLATE, output_schema=MEMORY_SCHEMA)
    )
    assert made.status_code == 201, made.text
    lib.models.stub.script(recall, [remembered("Not yet.")])
    unaware = await story_call(client, story, recall, {**RECALL, "viewpoint_character_id": briar})
    assert unaware.status_code == 200, unaware.text
    assert REMEMBERED not in prompt_of(lib, recall, everything=True)

    lib.models.stub.script(name, [remembered()])
    plain = await story_call(client, story, name, {**store_body(briar), "store_as_memory": None})
    assert plain.status_code == 200, plain.text
    assert (plain.json()["memory_id"], plain.json()["timeline_revision"]) == (None, before)
    assert await memories_of(engine, story) == []

    lib.models.stub.script(name, [remembered()])
    stored = await story_call(client, story, name, store_body(briar))
    assert stored.status_code == 200, stored.text
    body = stored.json()
    assert body["timeline_revision"] == before + 1 == await revision_of(engine, story)
    assert body["output"]["memory"].endswith(f"{REMEMBERED} pact.")
    (row,) = await memories_of(engine, story)
    assert row.id == body["memory_id"]
    assert row.text == f"I voted for Briar because of the {REMEMBERED} pact."
    assert (row.holder_character_id, row.character_id) == (briar, briar)
    assert row.visibility == {"scope": "characters", "characters": [briar]}
    assert (row.provenance, row.trust_tier, row.kind) == (
        "consumer_purpose",
        "in_world",
        "episodic",
    )
    assert (row.fidelity, row.channel, row.membership) == ("content", "self", "active")
    assert (row.importance, row.proposition_ids, row.event_type) == (None, [], None)
    assert row.participants == [briar]
    (op,) = await engine_rows(
        engine,
        "SELECT kind, narrative_turn, slot_k, membership FROM operations WHERE operation_id = :o",
        o=row.operation_id,
    )
    assert (op.kind, op.narrative_turn, op.slot_k, op.membership) == ("inter_turn", 0, 1, "active")
    (event,) = await engine_rows(
        engine,
        "SELECT id, type, category, payload FROM events WHERE operation_id = :o",
        o=row.operation_id,
    )
    assert (event.type, event.category) == ("purpose_memory_stored", "derived")
    assert event.payload == {
        "purpose": name,
        "version": 1,
        "memory_id": row.id,
        "character_id": briar,
    }
    assert row.source_event_ids == [event.id]

    # #849: the question does not carry the marker, so it reaches a prompt only through the
    # viewpoint's own context.
    lib.models.stub.script(recall, [remembered("Again."), remembered("Again.")])
    own = await story_call(client, story, recall, {**RECALL, "viewpoint_character_id": briar})
    assert own.status_code == 200, own.text
    assert f"{REMEMBERED} pact." in prompt_of(lib, recall)
    other = await story_call(client, story, recall, {**RECALL, "viewpoint_character_id": cato})
    assert other.status_code == 200, other.text
    assert prompt_of(lib, recall).startswith("Cato\n")
    assert REMEMBERED not in prompt_of(lib, recall, everything=True)
    assert "voted for Briar" not in prompt_of(lib, recall, everything=True)
    assert REMEMBERED not in caplog.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-17")
async def test_i_29__m9_memory_timeline(
    served: tuple[httpx.AsyncClient, Engine],
    memory_purpose: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The memory follows its slot like any record. Truncating the story to before the call
    (the boundary ``0``, without its slot) discards it; untruncating brings it back. A fork
    through the slot (``at_turn`` 0, ``slot`` 1) copies it, the copy the fork's own (its new
    character's, under new ids); a fork through ``0`` alone does not.

    Complement asserted: before the truncation the memory is active, so the discard is the
    truncation's."""
    client, lib = served
    name = memory_purpose
    story, ids = await partitioned_story(client)
    lib.models.stub.script(name, [remembered()])
    stored = await story_call(client, story, name, store_body(ids["Briar"]))
    assert stored.status_code == 200, stored.text
    assert [r.membership for r in await memories_of(engine, story)] == ["active"]

    through = await client.post(
        f"/stories/{story}/fork", json={"at_turn": 0, "slot": 1}, headers=WRITE
    )
    assert through.status_code in (200, 201), through.text
    forked = through.json()["story_id"]
    (copy,) = await memories_of(engine, forked)
    assert copy.text.endswith(f"{REMEMBERED} pact.")
    assert copy.membership == "active"
    assert copy.holder_character_id not in (ids["Briar"], None)
    (holder,) = await engine_rows(
        engine,
        "SELECT name FROM characters WHERE story_id = :s AND id = :c",
        s=forked,
        c=copy.holder_character_id,
    )
    assert holder.name == "Briar"
    before = await client.post(f"/stories/{story}/fork", json={"at_turn": 0}, headers=WRITE)
    assert before.status_code in (200, 201), before.text
    assert await memories_of(engine, before.json()["story_id"]) == []

    cut = await client.post(f"/stories/{story}/truncate", json={"to_turn": 0}, headers=WRITE)
    assert cut.status_code == 200, cut.text
    assert [r.membership for r in await memories_of(engine, story)] == ["discarded"]
    back = await client.post(f"/stories/{story}/untruncate", json={}, headers=WRITE)
    assert back.status_code == 200, back.text
    assert [r.membership for r in await memories_of(engine, story)] == ["active"]


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-17")
async def test_i_29__m9_memory_erasure(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Erasure reaches the memory like any memory (the erasure index): after Quillon is erased
    from the story, no memory names Quillon. Then a result whose text names Quillon is refused
    with 409 ``erased_subject_referenced``: nothing is stored, no output is returned and the
    revision stays.

    Complement asserted: before the erasure the stored memory names Quillon."""
    from tests import erasure_support as es

    async with respond_client(es.erasure_settings(pg_url), engine) as (client, lib):
        name = unique("erased_memory")
        body = purpose_body(name, template=MEMORY_TEMPLATE, output_schema=MEMORY_SCHEMA)
        made = await client.post("/admin/purposes", json=body, headers=OPERATOR)
        assert made.status_code == 201, made.text
        try:
            story, ids = await partitioned_story(client, "Ash", "Briar", "Quillon")
            lib.models.stub.script(name, [remembered("Quillon cried at the vote.")])
            stored = await story_call(client, story, name, store_body(ids["Briar"]))
            assert stored.status_code == 200, stored.text
            assert [r.text for r in await memories_of(engine, story)] == [
                "Quillon cried at the vote."
            ]
            await es.erase(client, "character", ids["Quillon"], story, confirm_all=True)
            assert (await es.residue(engine, story, ["Quillon"]))["memories"] == 0

            before = await revision_of(engine, story)
            count = len(await memories_of(engine, story))
            lib.models.stub.script(name, [remembered("Quillon was right.")])
            refused = await story_call(client, story, name, store_body(ids["Briar"]))
            assert reason_of(refused)[:2] == (409, "erased_subject_referenced")
            assert "Quillon" not in refused.text
            assert await revision_of(engine, story) == before
            assert len(await memories_of(engine, story)) == count
        finally:
            archived = await client.post(f"/admin/purposes/{name}/archive", headers=OPERATOR)
            assert archived.status_code == 200, archived.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-17")
async def test_i_29__m9_memory_idempotent(
    served: tuple[httpx.AsyncClient, Engine],
    memory_purpose: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """A repeat under the same ``Idempotency-Key`` answers the first result (the same
    ``memory_id`` and revision) and writes nothing new: one memory, one model call, the
    revision moved once. The same key with a different ``store_as_memory`` is 409
    ``idempotency_conflict``.

    Complement asserted: a new key stores a second memory."""
    client, lib = served
    name = memory_purpose
    story, ids = await partitioned_story(client)
    key = {"Idempotency-Key": new_ulid()}
    lib.models.stub.script(name, [remembered()])
    first = await story_call(client, story, name, store_body(ids["Briar"]), **key)
    assert first.status_code == 200, first.text
    calls = own_calls(lib, name)
    again = await story_call(client, story, name, store_body(ids["Briar"]), **key)
    assert again.status_code == 200, again.text
    assert again.json()["memory_id"] == first.json()["memory_id"]
    assert again.json()["timeline_revision"] == first.json()["timeline_revision"]
    assert own_calls(lib, name) == calls
    assert len(await memories_of(engine, story)) == 1
    assert await revision_of(engine, story) == first.json()["timeline_revision"]
    changed = store_body(ids["Briar"], store_as_memory={"text_field": "other"})
    conflict = await story_call(client, story, name, changed, **key)
    assert reason_of(conflict)[:2] == (409, "idempotency_conflict")

    lib.models.stub.script(name, [remembered()])
    fresh = await story_call(client, story, name, store_body(ids["Briar"]))
    assert fresh.status_code == 200, fresh.text
    assert len(await memories_of(engine, story)) == 2


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-17")
async def test_i_29__m9_memory_refusals(
    served: tuple[httpx.AsyncClient, Engine],
    memory_purpose: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """Each refusal stores nothing, returns no output and leaves the revision:
    ``store_as_memory`` without a viewpoint is 422 (``viewpoint_required``), before a Lite
    story's 409 ``module_off`` for its viewpoint; a structured story with shared knowledge is
    409 ``module_off`` (``partitions``), and with episodic memory off 409 ``module_off``
    (``episodic``); a ``text_field`` that is missing, nested or not a string is 422
    ``text_field_invalid`` with no model call; an empty or whitespace text is 422
    ``memory_text_empty`` and one over 2 000 characters 422 ``memory_text_too_long``; while a
    turn holds the story's turn slot, the write is 423 ``story_busy``.

    Complement asserted: once the turn gives the slot back, the same call stores the memory."""
    from epistrel.core import turn_gate

    client, lib = served
    name = memory_purpose
    lite, lite_ids = await new_story(client, "Ash", "Briar")
    no_viewpoint = {"inputs": {"target": "Briar"}, "store_as_memory": STORE}
    status, code, details = reason_of(await story_call(client, lite, name, no_viewpoint))
    assert (status, code, details["reason"]) == (422, "validation_error", "viewpoint_required")
    assert details["field"] == "body.viewpoint_character_id"
    on_lite = await story_call(client, lite, name, store_body(lite_ids["Briar"]))
    assert reason_of(on_lite)[:2] == (409, "module_off")

    shared, shared_ids = await new_story(client, "Ash", "Briar")
    assigned = await client.put(
        f"/stories/{shared}/engine-profile",
        json={"profile_id": structured_m4(), "version": 1, "ignore_warnings": True},
        headers=WRITE,
    )
    assert assigned.status_code == 200, assigned.text
    status, code, details = reason_of(
        await story_call(client, shared, name, store_body(shared_ids["Briar"]))
    )
    assert (status, code, details.get("module")) == (409, "module_off", "partitions"), details

    story, ids = await partitioned_story(client)
    briar = ids["Briar"]
    before = await revision_of(engine, story)
    calls = own_calls(lib, name)
    for field in ("absent", "score", "detail", "note"):
        status, code, details = reason_of(
            await story_call(
                client, story, name, store_body(briar, store_as_memory={"text_field": field})
            )
        )
        assert (status, code, details["reason"]) == (422, "validation_error", "text_field_invalid")
    assert own_calls(lib, name) == calls

    for text_, refusal in (
        ("", "memory_text_empty"),
        ("   ", "memory_text_empty"),
        ("x" * 2_001, "memory_text_too_long"),
    ):
        lib.models.stub.script(name, [remembered(text_)])
        refused = await story_call(client, story, name, store_body(briar))
        assert reason_of(refused)[:2] == (422, refusal)
        assert "output" not in refused.json()

    holder = new_ulid()
    assert await turn_gate.acquire(engine, story, holder, lease_s=60) is None
    try:
        lib.models.stub.script(name, [remembered()])
        busy = await story_call(client, story, name, store_body(briar))
        assert reason_of(busy)[:2] == (423, "story_busy")
    finally:
        await turn_gate.release_now(engine, story, holder)
    assert await revision_of(engine, story) == before
    assert await memories_of(engine, story) == []

    lib.models.stub.script(name, [remembered()])
    stored = await story_call(client, story, name, store_body(briar))
    assert stored.status_code == 200, stored.text
    assert len(await memories_of(engine, story)) == 1

    off = {"memory": {"episodic": {"enabled": False}, "consolidation": {"enabled": False}}}
    await override(client, story, off)
    status, code, details = reason_of(await story_call(client, story, name, store_body(briar)))
    assert (status, code, details["module"]) == (409, "module_off", "episodic")


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-17")
async def test_i_29__m9_memory_output_gate(
    served: tuple[httpx.AsyncClient, Engine],
    memory_purpose: str,
    banned_rating: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """#838: an output the story's output gate refuses twice under ``store_as_memory`` is 422
    ``output_policy_violation`` and stores nothing: no memory row, no ``purpose_memory_stored``
    event, the revision unchanged.

    Complement asserted: a refused output followed by a clean one stores the clean text only."""
    client, lib = served
    name = memory_purpose
    story, ids = await partitioned_story(client, content_rating_id=banned_rating)
    briar = ids["Briar"]
    before = await revision_of(engine, story)
    bad = remembered(f"I voted {BANNED} for Briar.")

    lib.models.stub.script(name, [bad, bad])
    refused = await story_call(client, story, name, store_body(briar))
    status, code, details = reason_of(refused)
    assert (status, code, details["attempts"]) == (422, "output_policy_violation", 2)
    assert BANNED not in refused.text
    assert await memories_of(engine, story) == []
    assert await revision_of(engine, story) == before
    stored_events = await engine_rows(
        engine,
        "SELECT id FROM events WHERE story_id = :s AND type = 'purpose_memory_stored'",
        s=story,
    )
    assert stored_events == []

    lib.models.stub.script(name, [bad, remembered()])
    retried = await story_call(client, story, name, store_body(briar))
    assert retried.status_code == 200, retried.text
    (row,) = await memories_of(engine, story)
    assert row.text == f"I voted for Briar because of the {REMEMBERED} pact."
    assert BANNED not in row.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-MODEL-17")
async def test_i_29__m9_memory_library(
    served: tuple[httpx.AsyncClient, Engine],
    memory_purpose: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """``Engine.call_story_purpose(..., store_as_memory=...)`` stores as REST does and
    answers the memory's id.

    Complement asserted: without a viewpoint it is refused at ``viewpoint_character_id``."""
    client, lib = served
    name = memory_purpose
    story, ids = await partitioned_story(client)
    lib.models.stub.script(name, [remembered()])
    result = await lib.call_story_purpose(
        LIBRARY,
        story,
        name,
        {"target": "Briar"},
        viewpoint_character_id=ids["Briar"],
        store_as_memory={"text_field": "memory"},
    )
    assert result.memory_id is not None
    assert [r.id for r in await memories_of(engine, story)] == [result.memory_id]
    with pytest.raises(InvalidRequest) as missing:
        await lib.call_story_purpose(
            LIBRARY, story, name, {"target": "Briar"}, store_as_memory=STORE
        )
    assert missing.value.details["reason"] == "viewpoint_required"
