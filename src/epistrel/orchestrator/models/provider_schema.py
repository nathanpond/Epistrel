"""The JSON Schema a provider's strict mode accepts on the wire (FR-MODEL-7; #761).

api.anthropic.com's OpenAI-compatible endpoint answers a ``json_schema`` ``response_format``
only with ``strict: true`` (a request without the key, or with ``false``, is a 400). Strict
mode refuses some keywords Pydantic emits. Checked live on 2026-11-07 with every registry
response schema:

- ``minimum``, ``maximum`` (and the rest of the numeric bounds) on ``number`` and ``integer``;
- ``minLength`` and ``maxLength`` on ``string``, and ``maxItems`` and any ``minItems`` above 1 on
  ``array``;
- every ``object`` must say ``additionalProperties: false``: absent, ``true`` and a value schema
  (a free-form ``dict``) are each a 400.

``strict_wire_schema`` drops the bounds and sets ``additionalProperties: false`` where it is
absent. Only bounds are dropped, so the wire schema is looser than the model's, never tighter,
and the engine still validates every reply strictly against the full schema
(``structured.parse``). A schema with a free-form ``dict`` field has no wire form: only
``false`` is accepted, and that would forbid every key the field exists to carry. So
``strict_wire_schema`` returns ``None``, and ``structured_call`` sends that call to that
endpoint as parse-validate-retry. ``pattern`` and string ``enum``/``const`` are accepted as
they are.

Every other endpoint gets the schema exactly as Pydantic generates it, with no ``strict`` key.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from pydantic import BaseModel

ANTHROPIC_HOST = "api.anthropic.com"

UNSUPPORTED_BOUNDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
    }
)
"""Keywords strict mode refuses; dropped from the wire schema (the reply is still checked)."""

_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})
_SCHEMA_LISTS = frozenset({"anyOf", "allOf", "oneOf", "prefixItems"})
_SCHEMA_VALUES = frozenset({"items", "not", "contains"})


def is_anthropic(base_url: str) -> bool:
    """Whether ``base_url`` is api.anthropic.com (the host ``provider_headers`` keys on, #235)."""
    return (urllib.parse.urlsplit(base_url).hostname or "") == ANTHROPIC_HOST


class _NoWireForm(Exception):
    """A keyword strict mode cannot carry without changing what the schema accepts."""


def _strip(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in UNSUPPORTED_BOUNDS:
            continue
        if key == "minItems" and isinstance(value, int) and value > 1:
            continue
        if key == "additionalProperties" and value is not False:
            raise _NoWireForm(key)
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _strip(sub) for name, sub in value.items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_strip(sub) for sub in value]
        elif key in _SCHEMA_VALUES:
            out[key] = _strip(value)
        else:
            out[key] = value  # data (enum, const, default, required, ...) or a plain keyword
    if out.get("type") == "object" and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def strict_wire_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """``schema`` as api.anthropic.com's strict mode accepts it, or ``None`` when it has no
    wire form (module docs). ``schema`` itself is not modified."""
    try:
        stripped: dict[str, Any] = _strip(schema)
    except _NoWireForm:
        return None
    return stripped


def json_schema_mode(base_url: str, schema: type[BaseModel]) -> bool:
    """Whether a structured call for ``schema`` to ``base_url`` can go in ``json_schema`` mode
    at all: everywhere except api.anthropic.com for a schema with no wire form."""
    return not is_anthropic(base_url) or strict_wire_schema(schema.model_json_schema()) is not None


__all__ = [
    "ANTHROPIC_HOST",
    "UNSUPPORTED_BOUNDS",
    "is_anthropic",
    "json_schema_mode",
    "strict_wire_schema",
]
