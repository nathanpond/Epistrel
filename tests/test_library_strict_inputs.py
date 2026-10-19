"""The library validates its inputs' JSON types as REST does (FR-API-3, FR-API-4; #427).

Over REST, ``strict_json_body`` (#73) refuses ``true``, ``"7"`` and ``2048.0`` for an integer
field. The in-process library has no such step: a caller builds the request models itself, in
pydantic's lax mode, so before #427 a ``true`` became ``1``. Every integer, number and boolean
field of a model the library takes is now strict, as #322 made the settings.

The guard walks every model a public ``Engine`` coroutine takes (its parameters' models and the
models nested in them) and fails on a lax ``int``, ``float`` or ``bool`` left anywhere in their
fields' schemas.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from epistrel.core.catalog.scenario_models import (
    RoleEligibility,
    ScenarioCast,
    StorySettingsDefaults,
)
from epistrel.core.catalog.service import PublishRequest
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
from epistrel.engine import Engine
from epistrel.orchestrator.lite.schemas import BudgetOverride, RegenerateRequest
from epistrel.orchestrator.models.adapter import ModelParams

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
