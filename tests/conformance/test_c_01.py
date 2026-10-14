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
ratings, deployment settings, catalog characters and scenarios, media, engine profiles) never
reach the session database. Excluded from generation: ``delete_story`` and ``archive_story``
(they would end the fixture story every later example needs), ``delete_character`` and
``delete_scenario`` (they would end the fixture catalog character and scenario, #103, #108), the
API-key routes (they need ``EPISTREL_CONFIG_KEY``) and ``upload_media``
(#105: a streamed multipart body whose ``metadata`` part is JSON, which generation cannot
encode; ``test_c_01__malformed`` sends its malformed cases by hand); all are covered by C-03 and
their own tests. ``replace_model_config`` is generated, and each successful write is undone so
that later examples still reach the stub model. So is ``set_profile_overrides`` (#113): a
generated ``budgets.deadline_ms`` of a few milliseconds, or a call cap below what a turn needs,
is a valid write on the fixture story, after which its later turns would answer a documented
503 or 422, so each successful one is followed by a patch removing every override. The fixture
media asset may be retired by a generated ``delete_media``: a retired asset still reads, so
later examples still reach it. The fixture upload session (#107) may be completed or aborted by
a generated call: a closed session still reads, and its parts and completion answer 409
``upload_closed``, a documented answer.
``PUT /media/uploads/{id}/parts/{n}`` takes a raw ``application/octet-stream`` body, which no
component describes (``_body_problems`` checks only that it is binary). ``GET /media/{id}/url``
signs with the app's ``EPISTREL_CONFIG_KEY`` under the seeded ``public_base_url``, which is put
back after each generated settings write, since a valid write may unset it and the route would
then answer a documented 503 ``config_missing``. The fixture engine profile (#106) may be
deleted by a generated ``delete_profile``: a deleted profile still reads and copies, and an
edit of it answers a documented 422 ``profile_deleted``. The fixture turn's reply is the message
the message-rating routes (#112) are generated against: a generated rating, re-rating or
retraction writes no ``operations``, ``events`` or ``config_changes`` row, and a generated
``feedback_tags`` write that would drop a tag a rating carries answers a documented 409. The
fixture persona (#114) is generated against, and ``delete_persona`` is excluded (it would end
it). A promotion's ``character_id`` is the fixture story's non-player character, Ada (the path
parameter shares its name with the catalog character's), so generated promotions reach the
operation; each makes a new catalog draft, and generated characters added to the fixture story
fill its cast up to its cap, after which an addition answers a documented 422. The fixture
offboarding (#117) offboards a principal that owns nothing, so its job's progress and report
read; a generated offboarding of a principal with one queued answers a documented 409, and the
principal lookup is generated against the fixture author. ``set_content_rating`` (#118) is
excluded: a generated tightening would pause the fixture story for every later example (no
worker runs its check); C-03 and its own tests cover it. A generated rating check read names
the fixture offboarding's job, which is no check of the story: a documented 404.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from epistrel.api.openapi import DOCUMENTED_BODIES, TAG_NAMES, dumps
from epistrel.api.routes.stories import StorySettingsPatch
from epistrel.config import Settings
from epistrel.core.catalog.characters import CharacterCreate, PublishRequest
from epistrel.core.catalog.personas import PersonaCreate
from epistrel.core.catalog.scenario_models import ScenarioCreate
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.deployment import SettingsPatch
from epistrel.core.genesis.models import StoryFromScenario
from epistrel.core.media.assets import MediaUpload
from epistrel.core.media.uploads import UploadOpen
from epistrel.core.offboarding import OffboardingQueued, OffboardingRequest
from epistrel.core.profiles.store import ProfileCreate
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.settings_schema import StorySettings
from epistrel.core.stories import StorySpec
from epistrel.engine import Engine
from epistrel.orchestrator.lite.schemas import RespondRequest
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from epistrel.tools import openapi as openapi_tool
from tests.auth_support import auth_settings, bearer
from tests.catalog_support import character as catalog_character
from tests.conformance.conftest import migrated_database
from tests.conftest import REPO_ROOT
from tests.media_support import files, metadata, png
from tests.model_support import config_key
from tests.respond_support import AUTHOR, body
from tests.scenario_support import scenario as catalog_scenario
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

EXCLUDED = (
    "delete_story",
    "archive_story",
    "delete_character",
    "delete_scenario",
    "delete_persona",
    "set_content_rating",
    "set_model_api_key",
    "remove_model_api_key",
    "upload_media",
)
"""Not generated against (see the module docstring); every other operation is."""
COUNTED_TABLES = ("operations", "events", "config_changes")
_COUNT = "SELECT count(*) FROM {}"
REFUSED = frozenset({400, 401, 404, 409, 422})
RESTORED = "replace_model_config"
"""Generated, then undone after each success (``_restore_model_config``)."""
SETTINGS_WRITE = "update_deployment_settings"
OVERRIDES_WRITE = "set_profile_overrides"
"""Generated, then undone after each success (``_restore_overrides``, #113)."""
PUBLIC_BASE_URL = "https://media.example.test"
"""Seeded, and put back after each generated settings write (``_restore_public_base``)."""
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
    character_id: str
    media_id: str
    upload_id: str
    scenario_id: str
    profile_id: str
    message_id: str
    persona_id: str
    story_character_id: str
    job_id: str

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
            "character_id": self.character_id,
            "media_id": self.media_id,
            "upload_id": self.upload_id,
            "scenario_id": self.scenario_id,
            "profile_id": self.profile_id,
            "message_id": self.message_id,
            "persona_id": self.persona_id,
            "job_id": self.job_id,
            "principal_id": AUTHOR.principal or "",
        }

    def path_ids_of(self, operation_id: str | None) -> dict[str, str]:
        """``path_ids``, with the parameters an operation names differently: a promotion's
        ``character_id`` is one of the story's characters (#114), not a catalog character."""
        found = dict(self.path_ids)
        if operation_id == "promote_character":
            found["character_id"] = self.story_character_id
        return found


async def _seed(settings: Settings) -> tuple[str, ...]:
    """A two-character story with one played turn, a published catalog character, an
    uploaded image, an open upload session (#107), a published scenario (#108), an engine
    profile (#106), a persona (#114) and an offboarding (#117): the story's id, the turn's
    operation id, the character's id, the image's id, the session's id, the scenario's id, the
    profile's id, the turn's reply (#112), the persona's id, the story's non-player character's
    id (#114) and the offboarding's job id (#117)."""
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
        draft = await engine.create_character_draft(
            AUTHOR, CharacterCreate.model_validate(catalog_character("Mara Venn"))
        )
        character_id = draft.character.id
        await engine.publish_character(AUTHOR, character_id, PublishRequest(draft_revision=1))
        image = await engine.upload_media(AUTHOR, MediaUpload.model_validate(metadata()), png())
        await engine.update_deployment_settings(
            AUTHOR, SettingsPatch(public_base_url=PUBLIC_BASE_URL)
        )
        session_bytes = png()
        session = await engine.open_upload(
            AUTHOR,
            UploadOpen.model_validate(
                {
                    **metadata(),
                    "size": len(session_bytes),
                    "mime_type": "image/png",
                    "sha256": hashlib.sha256(session_bytes).hexdigest(),
                }
            ),
        )
        drafted = await engine.create_scenario_draft(
            AUTHOR, ScenarioCreate.model_validate(catalog_scenario("Sunday Dinner"))
        )
        scenario_id = drafted.scenario.id
        await engine.publish_scenario(AUTHOR, scenario_id, PublishRequest(draft_revision=1))
        profile = await engine.create_profile(
            AUTHOR,
            ProfileCreate.model_validate(
                {"name": "C-01 profile", "settings": {"budgets": {"deadline_ms": 30_000}}}
            ),
        )
        queued = await engine.offboard_principal(
            AUTHOR, OffboardingRequest(principal="c01-offboarded")
        )
        assert isinstance(queued, OffboardingQueued)
        persona = await engine.create_persona(
            AUTHOR,
            PersonaCreate.model_validate(
                {"name": "C-01 persona", "age": 30, "description": "A persona to edit."}
            ),
        )
        return (
            created.story_id,
            played.beats[0].operation_id,
            character_id,
            image.asset.id,
            session.upload_id,
            scenario_id,
            profile.profile.id,
            played.message.message_id,
            persona.persona.id,
            created.characters[1].id,
            queued.job_id,
        )
    finally:
        await engine.models.aclose()
        await db.dispose()


@pytest.fixture(scope="module")
def live(pg_url: str) -> Iterator[Live]:
    """The app on a fresh, migrated scratch database (dropped afterwards)."""
    with migrated_database(pg_url, "epistrel_c01") as url:
        try:
            settings = auth_settings(url, model_adapter="stub", config_key=config_key())
            ids = asyncio.run(_seed(settings))
            yield Live(create_app(settings), url, *ids)
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
    known = live.path_ids_of(case.operation.definition.raw.get("operationId"))
    for name, value in list((case.path_parameters or {}).items()):
        if name in known and isinstance(value, str) and not _path_negated(case):
            case.path_parameters[name] = known[name]
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
    operation_id = case.operation.definition.raw.get("operationId")
    if operation_id == RESTORED and response.status_code == 200:
        _restore_model_config(live)
    if operation_id == SETTINGS_WRITE and response.status_code == 200:
        _restore_public_base(live)
    if operation_id == OVERRIDES_WRITE and response.status_code == 200:
        _restore_overrides(live)


def _restore_overrides(live: Live) -> None:
    """Remove every override of the fixture story after a generated ``PATCH
    /stories/{id}/profile-overrides`` succeeded (#113): ``null`` for each group the published
    schema names, confirmed. A generated deadline of a few milliseconds is a valid write, but
    every later example that plays a turn on the story would answer a documented 503
    ``model_unavailable``, so the write is undone through the API, as
    ``replace_model_config``'s is."""
    groups = COMMITTED["components"]["schemas"]["ProfileOverridesPatch"]["properties"]
    restore = SCHEMA["/stories/{story_id}/profile-overrides"]["PATCH"].Case(
        path_parameters={"story_id": live.story_id},
        body={
            "settings": dict.fromkeys(groups["settings"]["properties"]),
            "ignore_warnings": True,
        },
        headers={"X-Epistrel-Principal": "c01-restore"},
    )
    restored = restore.call(app=live.app, headers=bearer())
    assert restored.status_code == 200, restored.text
    assert restored.json()["result"]["overrides"]["settings"] == {}, restored.text


def _restore_public_base(live: Live) -> None:
    """Put the seeded ``public_base_url`` back after a generated settings write (#107)."""
    restore = SCHEMA["/admin/settings"]["PUT"].Case(
        body={"public_base_url": PUBLIC_BASE_URL},
        headers={"X-Epistrel-Principal": "c01-restore"},
    )
    restored = restore.call(app=live.app, headers=bearer())
    assert restored.status_code == 200, restored.text


def _restore_model_config(live: Live) -> None:
    """Put ``generation`` back on the stub after a generated ``PUT /admin/model-config``
    succeeded. A generated ``base_url`` or model is a valid write, but it would make every later
    example that calls a model answer a documented 503 ``model_unavailable``, so (like the
    excluded story routes) the write is undone through the API, which moves the configuration
    revision the model cache reads."""
    restore = SCHEMA["/admin/model-config/{purpose}"]["DELETE"].Case(
        path_parameters={"purpose": live.path_ids["purpose"]},
        headers={"X-Epistrel-Principal": "c01-restore"},
    )
    restored = restore.call(app=live.app, headers=bearer())
    assert restored.status_code in (200, 204), restored.text


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


MULTIPART_MODELS: dict[str, Any] = {"upload_media": MediaUpload}
"""Routes that read a multipart body themselves (#105): the model of its JSON ``metadata``
part, which the document's ``MULTIPART_BODIES`` component describes."""


DOCUMENTED_MODELS: dict[str, Any] = {
    "StorySettingsPatch": StorySettingsPatch,
    "StorySpec": StorySpec,
    "StoryFromScenario": StoryFromScenario,
}
"""The model behind each component ``DOCUMENTED_BODIES`` names: a handler that takes a raw JSON
object has no body type to validate an example against, so the component's own model does
(``POST /stories`` is a blank story or one from a scenario, #119)."""


def _body_routes(app: FastAPI) -> dict[str, Any]:
    """``operation_id → the handler's body type`` for every route that takes a body (for a
    multipart upload, the type of its ``metadata`` part)."""
    found: dict[str, Any] = dict(MULTIPART_MODELS)
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and route.body_field is not None:
            found[route.operation_id or route.name] = route.body_field.field_info.annotation
    return found


def schema_problems(schema: dict[str, Any], app: FastAPI) -> list[str]:
    problems: list[str] = []
    if schema.get("openapi") != "3.1.0":
        problems.append(f"openapi {schema.get('openapi')}")
    if schema["info"].get("version") != "m3" or "servers" in schema:
        problems.append("info.version must be m3 and there must be no servers")
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
    content = request["content"]
    if "application/octet-stream" in content:
        binary = content["application/octet-stream"]["schema"]
        if binary != {"type": "string", "format": "binary"}:
            return [f"{operation_id}: the raw body is not binary"]
        return []
    if "multipart/form-data" in content:
        form = content["multipart/form-data"]["schema"]
        if set(form["required"]) != {"metadata", "file"}:
            return [f"{operation_id}: the multipart body is not `metadata` then `file`"]
        schema = form["properties"]["metadata"]
    else:
        schema = content["application/json"]["schema"]
    refs = [schema.get("$ref")] + [option.get("$ref") for option in schema.get("anyOf", [])]
    names = [ref.rsplit("/", 1)[-1] for ref in refs if ref]
    if not names or (len(names) > 1 and "anyOf" not in schema):
        return [f"{operation_id}: the body is not one component"]
    problems: list[str] = []
    annotation = bodies.get(operation_id)
    # A body that is one of several components (``POST /stories``: a blank story or one from a
    # scenario, #119) holds each of them to the same rules.
    # A raw-object handler (``DOCUMENTED_BODIES``) validates each example against the model of
    # the component that documents it, so no documented body goes unchecked.
    raw = operation_id in DOCUMENTED_BODIES
    for name in names:
        component = components[name]
        if component.get("additionalProperties") is not False:
            problems.append(f"{operation_id}: {name} accepts unknown fields")
        examples = component.get("examples") or []
        if not examples:
            problems.append(f"{operation_id}: {name} has no example")
        model = DOCUMENTED_MODELS.get(name) if raw else annotation
        if raw and model is None:
            problems.append(f"{operation_id}: no model is known for {name}'s examples")
            continue
        for example in examples:
            if model is None:
                continue
            try:
                TypeAdapter(model).validate_python(example)
            except Exception as exc:
                problems.append(f"{operation_id}: {name}'s example does not validate ({exc})")
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


def test_raw_object_body_examples_are_validated(dummy_dsn: str) -> None:
    """Every example of a body a raw-object handler takes (``DOCUMENTED_BODIES``) is validated
    against the model of the component that documents it: an invalid example of either
    ``POST /stories`` body is reported (#119), and every documented component has a model.

    Complement asserted: the served document reports nothing."""
    app = create_app(auth_settings(dummy_dsn, model_adapter="stub"))
    served = app.openapi()
    assert schema_problems(served, app) == []
    for documented in DOCUMENTED_BODIES.values():
        names = (documented,) if isinstance(documented, str) else documented
        assert set(names) <= set(DOCUMENTED_MODELS), names
    for name, field in (("StorySpec", "characters"), ("StoryFromScenario", "slots")):
        invalid = json.loads(dumps(served))
        _components(invalid)[name]["examples"][0][field] = "not a collection"
        found = schema_problems(invalid, app)
        assert len(found) == 1, found
        assert found[0].startswith(f"create_story: {name}'s example does not validate"), found


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
        {"json": {"regenerate_retry_budget": "soon"}},
        "body.regenerate_retry_budget",
    ),
    # The column is INTEGER: a value past int32 must stop at validation, not reach Postgres (#280).
    (
        "int32 overflow on create",
        "POST",
        "/admin/content-ratings",
        {"json": {"name": "Overflow", "display_order": 2**31}},
        "body.display_order",
    ),
    (
        "int32 underflow on replace",
        "PUT",
        "/admin/content-ratings/{rating}",
        {
            "json": {
                "name": "Everyone",
                "description": "",
                "display_order": -(2**31) - 1,
                "prompt_guidance": "",
                "image_guidance": "",
                "policy_ids": [],
                "inherits_from": [],
            }
        },
        "body.display_order",
    ),
    # Catalog characters (#103): a draft revision past int32, an unknown draft field, a
    # projection that is not one, and a version that cannot exist.
    (
        "catalog draft revision past int32",
        "PATCH",
        "/catalog/characters/{character}",
        {"json": {"draft_revision": 2**31, "public_card": {"name": "Overflow"}}},
        "body.draft_revision",
    ),
    (
        "catalog unknown draft field",
        "POST",
        "/catalog/characters",
        {"json": {"public_card": {"name": "Mara"}, "secret_past": "x"}},
        "body.secret_past",
    ),
    (
        "catalog projection",
        "GET",
        "/catalog/characters/{character}",
        {"params": {"projection": "everything"}},
        "query.projection",
    ),
    (
        "catalog version zero",
        "GET",
        "/catalog/characters/{character}",
        {"params": {"version": 0}},
        "query.version",
    ),
    # Media (#105): the upload's metadata part, a path id, and the listing's filters.
    (
        "media metadata kind",
        "POST",
        "/media",
        {"files": files(png(), metadata(kind="sound"))},
        "body.metadata.kind",
    ),
    (
        "media metadata unknown field",
        "POST",
        "/media",
        {"files": files(png(), metadata(secret="x"))},
        "body.metadata.secret",
    ),
    ("media upload not multipart", "POST", "/media", {"json": metadata()}, "body"),
    ("media path id", "GET", "/media/not-a-ulid", {}, "path.media_id"),
    ("media list state", "GET", "/media", {"params": {"state": "lost"}}, "query.state"),
    ("media list limit", "GET", "/media", {"params": {"limit": 0}}, "query.limit"),
    # Scenarios (#108): an unknown field inside a group, a role rule, a revision past int32.
    (
        "scenario unknown group field",
        "POST",
        "/catalog/scenarios",
        {"json": {"public_card": {"name": "Sunday", "secret": "x"}}},
        "body.public_card.secret",
    ),
    (
        "scenario locked role without its character",
        "POST",
        "/catalog/scenarios",
        {
            "json": {
                "cast": {
                    "roles": [
                        {
                            "id": "mother",
                            "public_label": "Mother",
                            "setup_description": "Home for the weekend.",
                            "kind": "npc",
                            "fill": "locked",
                        }
                    ]
                }
            }
        },
        "body.cast.roles[0].character",
    ),
    (
        "scenario draft revision past int32",
        "PATCH",
        "/catalog/scenarios/{scenario}",
        {"json": {"draft_revision": 2**31}},
        "body.draft_revision",
    ),
]


def _malformed(live: Live) -> list[str]:
    problems: list[str] = []
    headers = bearer() | {"X-Epistrel-Principal": "p-c01"}
    with schemathesis_asgi.get_client(cast(Any, live.app)) as client:
        for name, method, template, kwargs, field in MALFORMED:
            before = live.counts()
            sent = {**kwargs, "headers": headers | kwargs.get("headers", {})}
            path = template.format(
                story=live.story_id,
                rating=EVERYONE_ID,
                character=live.character_id,
                scenario=live.scenario_id,
            )
            response = client.request(method, path, **sent)
            error = response.json().get("error", {}) if response.status_code == 422 else {}
            if response.status_code != 422 or error.get("details", {}).get("field") != field:
                problems.append(f"{name}: {response.status_code} {response.text[:200]}")
            if live.counts() != before:
                problems.append(f"{name}: wrote rows")
        # The complement: a well-formed request of the same shape is accepted and writes. It
        # must change the value: a write that changes nothing writes nothing, and the generated
        # ``PATCH /stories/{id}/settings`` cases may have left the budget at any value.
        current = client.get(f"/stories/{live.story_id}/settings", headers=headers)
        budget = 5 if current.json()["settings"]["regenerate_retry_budget"] == 4 else 4
        before = live.counts()
        ok = client.patch(
            f"/stories/{live.story_id}/settings",
            json={"regenerate_retry_budget": budget},
            headers=headers,
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
