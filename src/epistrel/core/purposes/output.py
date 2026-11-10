"""A consumer purpose's output schema as a pydantic model (FR-MODEL-14; #824).

``output_model(schema)`` builds the closed, immutable model a reply is validated against (and,
for the structured path, whose JSON Schema is sent): each object of the schema a model with
``extra="forbid"``; each property a field named ``p<i>`` with the property's name as its
**alias** (so any property name the subset allows works, ``model_config`` and ``json``
included); ``enum`` and ``const`` as ``Literal``; ``minLength``/``maxLength``/``pattern``,
``minimum``/``maximum`` and ``minItems``/``maxItems`` as field constraints; ``description`` and
``title`` kept. ``output_of(instance)`` is the reply as the consumer's JSON object (by alias).

The schema is checked first (``validation.check_schema``), so this module trusts its shape.
Building is cheap but not free; the Orchestrator caches one model per purpose version.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

OUTPUT_MODEL_NAME = "ConsumerOutput"

_SCALAR_TYPES: Mapping[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
}


class ConsumerOutput(BaseModel):
    """Base of every consumer purpose's output model: closed, immutable, validated by alias."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)


def _constraints(node: Mapping[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, name in (
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
        ("minItems", "min_length"),
        ("maxItems", "max_length"),
        ("minimum", "ge"),
        ("maximum", "le"),
        ("pattern", "pattern"),
    ):
        if key in node:
            found[name] = node[key]
    return found


def _annotation(node: Mapping[str, Any], title: str) -> Any:
    kind = node["type"]
    if kind == "object":
        return _model(node, title)
    if kind == "array":
        return list[_annotation(node["items"], f"{title}Item")]  # type: ignore[misc]
    if "const" in node:
        return Literal[node["const"]]
    if "enum" in node:
        return Literal[tuple(node["enum"])]
    return _SCALAR_TYPES[kind]


def _model(node: Mapping[str, Any], title: str) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for index, (name, sub) in enumerate(node["properties"].items()):
        info = Field(
            alias=name,
            description=sub.get("description"),
            title=sub.get("title"),
            **_constraints(sub),
        )
        fields[f"p{index}"] = (Annotated[_annotation(sub, f"{title}_{index}"), info], ...)
    model: type[BaseModel] = create_model(
        title,
        __base__=ConsumerOutput,
        __doc__=node.get("description"),
        **fields,
    )
    return model


def output_model(schema: Mapping[str, Any]) -> type[BaseModel]:
    """The model of a checked output schema (module docs)."""
    return _model(schema, OUTPUT_MODEL_NAME)


def output_of(instance: BaseModel) -> dict[str, Any]:
    """A validated reply as the consumer's JSON object, keyed by the schema's property names."""
    dumped: dict[str, Any] = instance.model_dump(mode="json", by_alias=True)
    return dumped


__all__ = ["OUTPUT_MODEL_NAME", "ConsumerOutput", "output_model", "output_of"]
