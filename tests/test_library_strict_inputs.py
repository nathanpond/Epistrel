"""The library validates its inputs' JSON types as REST does (FR-API-3, FR-API-4; #427, #445).

Over REST, ``strict_json_body`` (#73) refuses ``true``, ``"7"`` and ``2048.0`` for an integer
field. The in-process library has no such step: a caller builds the request models itself, in
pydantic's lax mode, so before #427 a ``true`` became ``1``. Every integer, number and boolean
field of a model the library takes is now strict, as #322 made the settings.

The guard walks every model a public ``Engine`` coroutine takes (its parameters' models and the
models nested in them) and fails on a lax ``int``, ``float`` or ``bool`` left anywhere in their
fields' schemas. Its scalar parameters (``limit``, ``as_of``, ``version``, ``discharged`` …) are
plain arguments, not model fields: ``strict_scalar_methods`` checks them strictly (#445), and
the guard here finds every one from its annotation's source text.
"""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.core.catalog.scenario_models import (
    RoleEligibility,
    ScenarioCast,
    StorySettingsDefaults,
)
from epistrel.core.catalog.service import PublishRequest
from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest
from epistrel.core.fork import ForkRequest
from epistrel.core.memory.consolidation import ConsolidateRequest
from epistrel.core.offboarding import OffboardingRequest
from epistrel.core.profiles.story import ProfileOverridesPatch
from epistrel.core.propositions.model import Bound
from epistrel.core.ratings import RatingFields
from epistrel.core.ratings.suggest import SuggestionRequest
from epistrel.core.settings_schema import GenerationSettings
from epistrel.core.timeline import TruncateRequest
from epistrel.core.variants import ChainRef
from epistrel.engine import (
    ISO_TIMES_ATTRIBUTE,
    STRICT_SCALARS_ATTRIBUTE,
    Engine,
    library_only_methods,
    operations,
)
from epistrel.orchestrator.lite.schemas import BudgetOverride, RegenerateRequest
from epistrel.orchestrator.models.adapter import ModelParams
from tests import catalog_support, scenario_support
from tests import consolidation_support as cs
from tests.auth_support import auth_settings, bearer
from tests.respond_support import respond_client

LAX = frozenset({"int", "float", "bool"})
SKIPPED_KEYS = frozenset({"serialization", "metadata", "json_schema_input_schema"})


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from _models_in(argument)


def library_input_models() -> dict[type[BaseModel], str]:
    """Every model a public ``Engine`` coroutine takes, with the first ``method(parameter)``
    that reaches it."""
    found: dict[type[BaseModel], str] = {}

    def visit(model: type[BaseModel], via: str) -> None:
        if model in found:
            return
        found[model] = via
        for field in model.model_fields.values():
            for nested in _models_in(field.annotation):
                visit(nested, via)
            # A field checked later against one of several models (#254: a model-config PUT's
            # ``params``, per purpose) names them as its ``json_schema_input_type``.
            for item in field.metadata:
                for nested in _models_in(getattr(item, "json_schema_input_type", None)):
                    visit(nested, via)

    for name, member in vars(Engine).items():
        if name.startswith("_") or not inspect.iscoroutinefunction(member):
            continue
        hints = typing.get_type_hints(inspect.unwrap(member))
        for parameter, annotation in hints.items():
            if parameter != "return":
                for model in _models_in(annotation):
                    visit(model, f"Engine.{name}({parameter})")
    return found


def _lax_types(schema: Any, definitions: dict[str, Any], seen: set[str]) -> set[str]:
    """The lax scalar types a core schema validates (a nested model is checked on its own;
    a plain validator decides for itself)."""
    if isinstance(schema, list | tuple):
        return set().union(*(_lax_types(item, definitions, seen) for item in schema))
    if not isinstance(schema, dict):
        return set()
    kind = schema.get("type")
    if kind == "model" or kind == "function-plain":
        return set()
    if kind == "definition-ref":
        ref = schema["schema_ref"]
        if ref in seen or ref not in definitions:
            return set()
        seen.add(ref)
        return _lax_types(definitions[ref], definitions, seen)
    found = {kind} if kind in LAX and not schema.get("strict") else set()
    for key, value in schema.items():
        if key not in SKIPPED_KEYS:
            found |= _lax_types(value, definitions, seen)
    return found


def lax_fields() -> list[str]:
    """``module.Model.field: types <- Engine.method(parameter)`` for every lax field."""
    lax: list[str] = []
    for model, via in library_input_models().items():
        for name, field in model.model_fields.items():
            annotation: Any = field.annotation
            if field.metadata:
                annotation = typing.Annotated[(annotation, *field.metadata)]
            schema = TypeAdapter(annotation).core_schema
            definitions: dict[str, Any] = {}
            if schema.get("type") == "definitions":
                definitions = {d["ref"]: d for d in schema["definitions"]}
            types = _lax_types(schema, definitions, set())
            if types:
                where = f"{model.__module__}.{model.__qualname__}.{name}"
                lax.append(f"{where}: {sorted(types)} <- {via}")
    return sorted(lax)


def test_every_library_input_field_is_strict() -> None:
    """No model the library takes validates an integer, number or boolean in lax mode
    (FR-API-4; #427): a new one fails here until it is typed ``StrictInt``, ``StrictFloat`` or
    ``StrictBool``."""
    models = library_input_models()
    assert {ForkRequest, StorySettingsDefaults, Bound, ModelParams} <= set(models)
    assert lax_fields() == []


NOT_AN_INTEGER: tuple[Any, ...] = (True, "7", 2048.0)
NOT_A_NUMBER: tuple[Any, ...] = (True, "0.5")
NOT_A_BOOLEAN: tuple[Any, ...] = (1, "true")

# (model, a valid document, the field probed, the values it must refuse)
CASES: list[tuple[type[BaseModel], dict[str, Any], str, tuple[Any, ...]]] = [
    (StorySettingsDefaults, {}, "audit_retention_days", NOT_AN_INTEGER),
    (StorySettingsDefaults, {}, "allow_new_arrivals", NOT_A_BOOLEAN),
    (ForkRequest, {}, "at_turn", NOT_AN_INTEGER),
    (TruncateRequest, {"to_turn": 0}, "to_turn", NOT_AN_INTEGER),
    (ChainRef, {"turn": 1, "variant": 1}, "variant", NOT_AN_INTEGER),
    (ConsolidateRequest, {"from_turn": 1, "to_turn": 2}, "to_turn", NOT_AN_INTEGER),
    (Bound, {"inner": 1, "outer": 1}, "outer", NOT_AN_INTEGER),
    (GenerationSettings, {}, "temperature", NOT_A_NUMBER),
    (GenerationSettings, {}, "length", NOT_AN_INTEGER),
    (BudgetOverride, {}, "max_calls", NOT_AN_INTEGER),
    (BudgetOverride, {}, "deadline_s", NOT_A_NUMBER),
    (RegenerateRequest, {}, "turn", NOT_AN_INTEGER),
    (RatingFields, {"name": "Teen"}, "display_order", NOT_AN_INTEGER),
    (ModelParams, {}, "max_tokens", NOT_AN_INTEGER),
    (ModelParams, {}, "temperature", NOT_A_NUMBER),
    (PublishRequest, {"draft_revision": 1}, "draft_revision", NOT_AN_INTEGER),
    (SuggestionRequest, {}, "version", NOT_AN_INTEGER),
    (OffboardingRequest, {"principal": "p-x"}, "dry_run", NOT_A_BOOLEAN),
    (ProfileOverridesPatch, {"settings": {}}, "expected_revision", NOT_AN_INTEGER),
    (ProfileOverridesPatch, {"settings": {}}, "ignore_warnings", NOT_A_BOOLEAN),
    (ScenarioCast, {}, "max_characters_at_start", NOT_AN_INTEGER),
    (RoleEligibility, {}, "age_range", ((True, None), ("7", None))),
]


@pytest.mark.parametrize(
    ("model", "valid", "name", "values"),
    CASES,
    ids=[f"{model.__name__}.{name}" for model, _, name, _ in CASES],
)
def test_a_mistyped_value_is_refused_at_its_field(
    model: type[BaseModel], valid: dict[str, Any], name: str, values: tuple[Any, ...]
) -> None:
    """A boolean, a numeric string or an integral float given to an integer field, a boolean or
    a numeric string to a number field, and a number or a string to a boolean field, is refused
    at that field when the library caller builds the model (FR-API-4; #427). Before #427
    ``StorySettingsDefaults.model_validate({"audit_retention_days": True})`` held ``1``."""
    for value in values:
        with pytest.raises(ValidationError) as refused:
            model.model_validate({**valid, name: value})
        locations = {error["loc"][0] for error in refused.value.errors()}
        assert locations == {name}, (model, name, value, locations)


# -- scalar parameters (#445) ----------------------------------------------------------------

SCALAR = re.compile(r"\b(int|float|bool)\b")
"""A scalar annotation, read from its source text: independent of the engine's own
``scalar_parameters``, so a form it misses fails here."""

MISTYPED: dict[str, tuple[Any, ...]] = {
    "int": (True, "7", 2.0),
    "float": (True, "0.5"),
    "bool": (1, "true"),
}
VALID: dict[str, Any] = {"int": 1, "float": 0.5, "bool": True}
TEXT_ALSO: frozenset[tuple[str, str]] = frozenset({("history", "as_of")})
"""Parameters annotated ``int | str``: a string is one of their types (#222: ``as_of`` takes a
turn or a checkpoint), so ``"7"`` is no mistyped value for them."""


def _scalar_cases() -> list[tuple[str, str, str]]:
    """``(method, parameter, scalar)`` for every scalar parameter of a public method."""
    found: list[tuple[str, str, str]] = []
    for name in operations() + sorted(library_only_methods()):
        raw = inspect.unwrap(getattr(Engine, name))
        for parameter in inspect.signature(raw).parameters.values():
            match = SCALAR.search(str(parameter.annotation))
            if match:
                found.append((name, parameter.name, match.group(1)))
    return found


SCALAR_CASES = _scalar_cases()


def test_every_scalar_parameter_is_strict() -> None:
    """Every ``int``, ``float`` or ``bool`` parameter of a public ``Engine`` method is one
    ``strict_scalar_methods`` checks (#445), the issue's examples among them: a new one fails
    here until it is covered."""
    assert {
        ("history", "limit"),
        ("inspect_ledger", "discharged"),
        ("put_upload_part", "n"),
        ("get_media_content", "exp"),
    } <= {(n, p) for n, p, _ in SCALAR_CASES}
    checked = {
        (name, parameter)
        for name in operations() + sorted(library_only_methods())
        for parameter in getattr(getattr(Engine, name), STRICT_SCALARS_ATTRIBUTE, ())
    }
    assert checked == {(n, p) for n, p, _ in SCALAR_CASES}


def _arguments(name: str, parameter: str, value: Any) -> tuple[list[Any], dict[str, Any]]:
    """Arguments for ``name``: ``value`` for ``parameter``, a valid one for every other required
    one (a plain string, or a valid scalar)."""
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for p in list(inspect.signature(getattr(Engine, name)).parameters.values())[2:]:
        if p.name == parameter:
            given = value
        elif p.default is inspect.Parameter.empty:
            scalar = SCALAR.search(str(p.annotation))
            given = VALID[scalar.group(1)] if scalar else "x"
        else:
            continue
        if p.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[p.name] = given
        else:
            args.append(given)
    return args, kwargs


async def _call(name: str, parameter: str, value: Any) -> Exception | None:
    """What calling ``name`` on a bare ``self`` with ``value`` for ``parameter`` raised."""
    args, kwargs = _arguments(name, parameter, value)
    try:
        await getattr(Engine, name)(object(), RequestContext(principal="p-reader"), *args, **kwargs)
    except Exception as raised:
        return raised
    return None


@pytest.mark.parametrize(
    ("name", "parameter", "scalar"), SCALAR_CASES, ids=[f"{n}.{p}" for n, p, _ in SCALAR_CASES]
)
async def test_a_mistyped_scalar_is_refused_at_its_parameter(
    name: str, parameter: str, scalar: str
) -> None:
    """``limit=True``, ``as_of="7"``, ``limit=2.0`` or ``discharged=1`` is 422
    ``validation_error`` at ``query.<name>`` (``path.<name>`` for a positional parameter), as
    REST refuses ``?limit=true``, before anything else runs (FR-API-3, FR-API-4; #445). Before
    #445 ``limit=True`` was taken as ``1``. Complement: a valid value passes the check (the call
    then fails on the bare ``self``, never at the parameter)."""
    kind = inspect.signature(getattr(Engine, name)).parameters[parameter].kind
    where = "query" if kind is inspect.Parameter.KEYWORD_ONLY else "path"
    for value in MISTYPED[scalar]:
        if isinstance(value, str) and (name, parameter) in TEXT_ALSO:
            continue
        refused = await _call(name, parameter, value)
        assert isinstance(refused, InvalidRequest), (name, parameter, value, refused)
        assert refused.details["field"] == f"{where}.{parameter}", (value, refused.details)
    passed = await _call(name, parameter, VALID[scalar])
    assert not (
        isinstance(passed, InvalidRequest) and passed.details.get("field") == f"{where}.{parameter}"
    ), passed


# -- date and date-time query parameters (#788) -----------------------------------------------

NOT_A_TIME: tuple[str, ...] = ("258", "0.0", "-1")
"""Numbers, which pydantic's lax ``datetime`` and ``date`` read as seconds since the epoch: the
schema's ``date-time`` and ``date`` promise no such form (FR-API-4)."""
STRICTER: frozenset[tuple[str, str]] = frozenset(
    {
        ("/admin/evals/escalation-yield", "from"),
        ("/admin/evals/escalation-yield", "to"),
        ("/admin/evals/extraction-shadow", "from"),
        ("/admin/evals/extraction-shadow", "to"),
    }
)
"""``admin/verify.py``'s ``from``/``to``: strings with an RFC 3339 pattern, stricter than
``date-time``, so the schema names no format for them and they are listed here."""
EXTRA_PARAMS: dict[str, dict[str, str]] = {
    "/admin/stories/{story_id}/facts": {"viewpoint": "omniscient"},
    "/feedback/aggregates": {"group_by": "day"},
}
"""The other parameters a route needs to answer 200."""
VALID_TIMES: dict[str, tuple[str, ...]] = {
    "date-time": ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+02:00"),
    "date": ("2026-01-01",),
}


def _formats(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    found = {schema["format"]} if "format" in schema else set()
    for key in ("anyOf", "oneOf", "allOf"):
        for member in schema.get(key, ()):
            found |= _formats(member)
    return found


def dated_query_parameters(document: dict[str, Any]) -> dict[tuple[str, str], str]:
    """``{(path, name): format}`` for every GET query parameter whose schema is a ``date-time``
    or a ``date``, and ``STRICTER``'s parameters as ``date-time``."""
    found: dict[tuple[str, str], str] = {}
    for path, item in document["paths"].items():
        for parameter in item.get("get", {}).get("parameters", ()):
            if parameter["in"] != "query":
                continue
            formats = _formats(parameter.get("schema")) & {"date", "date-time"}
            if formats:
                (found[(path, parameter["name"])],) = formats
    for key in STRICTER:
        assert key not in found, key
        found[key] = "date-time"
    return found


KNOWN_DATED: frozenset[tuple[str, str]] = frozenset(
    {
        ("/stories/{story_id}/operations", "since"),
        ("/stories/{story_id}/operations", "until"),
        ("/admin/stories/{story_id}/facts", "as_of_record_time"),
        ("/admin/image-usage", "from"),
        ("/feedback/aggregates", "to"),
    }
)
"""Parameters the enumeration must find, the issue's among them (a check of the walk itself)."""


async def _path_ids(client: Any, engine: AsyncEngine) -> dict[str, str]:
    """Real ids for the dated routes' path parameters: a story (on ``Structured (M4)``, so its
    canon reads answer), a catalog character and a scenario."""
    world = await cs.story(engine)
    character = await catalog_support.create(client)
    scenario = await scenario_support.create(client)
    return {
        "story_id": world.story_id,
        "character_id": character["id"],
        "scenario_id": scenario["id"],
    }


async def _dated_problems(client: Any, ids: dict[str, str], dated: dict[Any, str]) -> list[str]:
    """Each dated parameter that takes one of ``NOT_A_TIME`` (anything but 422
    ``validation_error`` at ``query.<name>``) or refuses one of ``VALID_TIMES`` (anything but
    200)."""
    problems: list[str] = []
    for (template, name), kind in sorted(dated.items()):
        path = template.format(**ids)
        extra = EXTRA_PARAMS.get(template, {})
        for value in NOT_A_TIME:
            answer = await client.get(path, params={**extra, name: value}, headers=bearer())
            error = answer.json().get("error", {}) if answer.status_code == 422 else {}
            found = (error.get("code"), error.get("details", {}).get("field"))
            if found != ("validation_error", f"query.{name}"):
                problems.append(f"{template} {name}={value}: {answer.status_code}")
        for value in VALID_TIMES[kind]:
            answer = await client.get(path, params={**extra, name: value}, headers=bearer())
            if answer.status_code != 200:
                problems.append(f"{template} {name}={value}: {answer.text}")
    return problems


@pytest.mark.integration
async def test_every_dated_query_parameter_refuses_a_number(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """``?until=258`` (``0.0``, ``-1``) is 422 ``validation_error`` at ``query.until``, and so is
    every other ``date-time`` or ``date`` query parameter the app documents, ``admin/verify``'s
    pattern-typed ``from``/``to`` among them: a lax one added later fails here (FR-API-4;
    #788, C-01's flakes). Before #788 ``GET /stories/{id}/operations?until=258`` answered 200.

    Complement asserted: ``2026-01-01T00:00:00Z`` and ``2026-01-01T00:00:00+02:00`` (a date
    parameter: ``2026-01-01``) still answer 200."""
    settings = auth_settings(pg_url, model_adapter="stub")
    dated = dated_query_parameters(create_app(settings).openapi())
    assert set(dated) >= KNOWN_DATED
    async with respond_client(settings, engine) as (client, _):
        ids = await _path_ids(client, engine)
        assert await _dated_problems(client, ids, dated) == []


def _dated_cases() -> list[tuple[str, str, type]]:
    """``(method, parameter, datetime | date)`` for every public method parameter typed
    ``datetime`` or ``date`` (alone or with ``None``)."""
    found: list[tuple[str, str, type]] = []
    for name in operations() + sorted(library_only_methods()):
        hints = typing.get_type_hints(inspect.unwrap(getattr(Engine, name)))
        for parameter, annotation in hints.items():
            members = set(typing.get_args(annotation)) or {annotation}
            for kind in (datetime, date):
                if parameter != "return" and kind in members:
                    found.append((name, parameter, kind))
    return found


DATED_CASES = _dated_cases()
VALID_VALUE: dict[type, Any] = {datetime: datetime(2026, 1, 1, tzinfo=UTC), date: date(2026, 1, 1)}


def test_every_dated_parameter_is_checked() -> None:
    """Every ``datetime`` or ``date`` parameter of a public ``Engine`` method is one the ISO
    check covers (#788), the issue's among them."""
    assert {
        ("list_operations", "since"),
        ("list_operations", "until"),
        ("list_facts", "as_of_record_time"),
    } <= {(n, p) for n, p, _ in DATED_CASES}
    checked = {
        (name, parameter)
        for name in operations() + sorted(library_only_methods())
        for parameter in getattr(getattr(Engine, name), ISO_TIMES_ATTRIBUTE, ())
    }
    assert checked == {(n, p) for n, p, _ in DATED_CASES}


@pytest.mark.parametrize(
    ("name", "parameter", "kind"), DATED_CASES, ids=[f"{n}.{p}" for n, p, _ in DATED_CASES]
)
async def test_a_number_is_refused_at_a_dated_parameter(
    name: str, parameter: str, kind: type
) -> None:
    """``Engine.list_operations(until=258)`` (``0.0``, ``-1``, ``"258"``, ``True``) is 422
    ``validation_error`` at ``query.until``, as REST refuses ``?until=258`` (FR-API-3; #788).
    Complement: a ``datetime`` (a ``date``) passes the check."""
    for value in (258, 0.0, -1, True, *NOT_A_TIME):
        refused = await _call(name, parameter, value)
        assert isinstance(refused, InvalidRequest), (name, parameter, value, refused)
        assert refused.details["field"] == f"query.{parameter}", (value, refused.details)
    passed = await _call(name, parameter, VALID_VALUE[kind])
    assert not (
        isinstance(passed, InvalidRequest) and passed.details.get("field") == f"query.{parameter}"
    ), passed
