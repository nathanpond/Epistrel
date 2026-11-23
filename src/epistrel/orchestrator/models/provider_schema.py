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

Strict mode also compiles the schema into a grammar with a size limit, and the Pydantic form
spends that budget on annotations (#1009): a ``$ref`` to a ``$defs`` entry and a ``title`` on
every node. Checked live on 2026-11-23 with a consumer purpose about 3 levels deep with about 35
fields: the Pydantic form was a 400 ("The compiled grammar is too large"), and the same schema
with its ``$ref``s inlined and its ``title``s dropped was a 200. So the wire form inlines every
local ``$ref`` (``#/$defs/...`` or ``#/definitions/...``), drops ``$defs``/``definitions`` and
drops each node's ``title``. Neither changes what the schema accepts. A recursive ``$ref`` has
no inline form, so such a schema has no wire form. A schema that is still too large comes back
as that 400, which ``errors.map_provider_error`` maps to ``SchemaTooLarge``, and
``structured_call`` remembers it per endpoint, model and schema (``ResponseFormatMemo``) and
sends the schema as parse-validate-retry.

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

_SCHEMA_MAPS = frozenset({"properties", "patternProperties"})
_DEFS = ("$defs", "definitions")
_ANNOTATIONS = frozenset({"title"})
"""Node keywords dropped from the wire schema: they cost grammar size and accept nothing (#1009)."""
_SCHEMA_LISTS = frozenset({"anyOf", "allOf", "oneOf", "prefixItems"})
_SCHEMA_VALUES = frozenset({"items", "not", "contains"})


def is_anthropic(base_url: str) -> bool:
    """Whether ``base_url`` is api.anthropic.com (the host ``provider_headers`` keys on, #235)."""
    return (urllib.parse.urlsplit(base_url).hostname or "") == ANTHROPIC_HOST


class _NoWireForm(Exception):
    """A keyword strict mode cannot carry without changing what the schema accepts."""


def _resolve(ref: str, defs: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix) and ref[len(prefix) :] in defs:
            name = ref[len(prefix) :]
            return name, defs[name]
    raise _NoWireForm(ref)  # a remote or unknown ref: nothing to inline


def _strip(node: Any, defs: dict[str, dict[str, Any]], seen: tuple[str, ...] = ()) -> Any:
    if not isinstance(node, dict):
        return node
    if isinstance(ref := node.get("$ref"), str):
        name, target = _resolve(ref, defs)
        if name in seen:
            raise _NoWireForm("$ref")  # recursive: no inline form
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        return _strip({**target, **siblings}, defs, (*seen, name))
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in UNSUPPORTED_BOUNDS or key in _ANNOTATIONS or key in _DEFS:
            continue
        if key == "minItems" and isinstance(value, int) and value > 1:
            continue
        if key == "additionalProperties" and value is not False:
            raise _NoWireForm(key)
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _strip(sub, defs, seen) for name, sub in value.items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_strip(sub, defs, seen) for sub in value]
        elif key in _SCHEMA_VALUES:
            out[key] = _strip(value, defs, seen)
        else:
            out[key] = value  # data (enum, const, default, required, ...) or a plain keyword
    if out.get("type") == "object" and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def strict_wire_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """``schema`` as api.anthropic.com's strict mode accepts it, or ``None`` when it has no
    wire form (module docs). ``schema`` itself is not modified."""
    defs: dict[str, dict[str, Any]] = {}
    for key in _DEFS:
        if isinstance(found := schema.get(key), dict):
            defs.update(found)
    try:
        stripped: dict[str, Any] = _strip(schema, defs)
    except _NoWireForm:
        return None
    return stripped


GRAMMAR_TOO_LARGE = "compiled grammar is too large"
"""The phrase in api.anthropic.com's 400 for a strict schema over its grammar limit (#1009)."""


def rejects_grammar_size(message: str) -> bool:
    """Whether a 400's text is strict mode's "compiled grammar is too large" (#1009)."""
    return GRAMMAR_TOO_LARGE in message.casefold()


def json_schema_mode(base_url: str, schema: type[BaseModel]) -> bool:
    """Whether a structured call for ``schema`` to ``base_url`` can go in ``json_schema`` mode
    at all: everywhere except api.anthropic.com for a schema with no wire form."""
    return not is_anthropic(base_url) or strict_wire_schema(schema.model_json_schema()) is not None


__all__ = [
    "ANTHROPIC_HOST",
    "GRAMMAR_TOO_LARGE",
    "UNSUPPORTED_BOUNDS",
    "is_anthropic",
    "json_schema_mode",
    "rejects_grammar_size",
    "strict_wire_schema",
]
