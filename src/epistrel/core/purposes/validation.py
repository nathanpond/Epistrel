"""What a consumer purpose may hold, checked at save, never at call time (FR-MODEL-14; #824).

**Names** match ``NAME_PATTERN`` (3 to 48 characters: a lowercase letter, then lowercase
letters, digits and ``_``). A name the Orchestrator's registry uses (every delivered purpose and
port, every reserved one and ``default``: ``reserved``, passed in by the Engine because Core does
not import the Orchestrator) or a consumer purpose already registered, archived ones included,
is taken.

**Templates** are Jinja2, rendered by ``environment()``: an ``ImmutableSandboxedEnvironment``
with ``StrictUndefined``, autoescape off and ``prompts.py``'s trim and lstrip settings. A
template is at most ``TEMPLATE_MAX_BYTES`` of UTF-8, parses, and passes a static scan of its
syntax tree: no ``include``, ``import``, ``from``, ``extends``, ``macro`` or ``call``; no
attribute or item access whose name starts with ``_`` (``cycler.__init__.__globals__`` is
refused here, before the sandbox would refuse it at render); only Jinja's built-in filters; and
no top-level variable other than ``inputs`` (the call's input object), ``viewpoint`` (#826) and
``story`` (#826). It then compiles.

#837: the scan also refuses every other way to name an attribute or item at render time. An
item's key is a constant (a slice is fine), never a computed one (``inputs['_' ~ '_class__']``).
The ``attr`` filter is refused, and a filter that reads attributes by name (``map``,
``selectattr``, ``rejectattr``, ``sort``, ``groupby``, ``sum``, ``unique``, ``min``, ``max``)
takes each name as a constant with no ``_``-prefixed segment. The environment has no globals
(``range``, ``cycler``, ``lipsum``, ``dict``, ``joiner``, ``namespace``), so the undeclared-variable
check names them like any other variable.

**Output schemas** are the JSON Schema subset the strict structured path (#761) can send as it
is: an object root; each node a single-string ``type`` among string, number, integer, boolean,
array and object; ``properties``, ``required`` (listing every property, as strict decoding
needs) and ``additionalProperties: false`` on every object; ``items`` on every array; ``enum``
and ``const`` on scalars; ``description`` and ``title``; ``minLength``/``maxLength`` and
``pattern`` on strings, ``minItems``/``maxItems`` on arrays, ``minimum``/``maximum`` on numbers.
``$ref``/``$defs``, ``anyOf``/``oneOf``/``allOf``/``not``, type lists, ``nullable`` and every
other keyword are refused. The root object is depth 1, and each nested object or ``items``
level adds 1, up to ``SCHEMA_MAX_DEPTH``. The schema is at most ``SCHEMA_MAX_BYTES`` as
canonical JSON (sorted keys, no whitespace) and must compile to a pydantic model
(``core.purposes.output.output_model``).

Every refusal is 422 ``validation_error`` whose ``details`` report the first fault as
``{reason, field, message}`` (``PurposeRefused``); the checks run name, template, schema, then
the live-purpose limit (``PURPOSE_LIMIT``, checked by the store in the create transaction).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from typing import Any, Final

from jinja2 import StrictUndefined, Template, TemplateError, meta, nodes
from jinja2.filters import FILTERS
from jinja2.sandbox import ImmutableSandboxedEnvironment

from epistrel.core.errors import InvalidRequest

NAME_PATTERN: Final = r"^[a-z][a-z0-9_]{2,47}$"
TEMPLATE_MAX_BYTES: Final = 32 * 1024
SCHEMA_MAX_BYTES: Final = 16 * 1024
SCHEMA_MAX_DEPTH: Final = 6
PURPOSE_LIMIT: Final = 100
"""Live consumer purposes a deployment may hold (archived ones do not count)."""
DESCRIPTION_MAX: Final = 1_000
TEMPLATE_VARIABLES: Final = frozenset({"inputs", "viewpoint", "story"})
PROPERTY_NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"

NAME_INVALID: Final = "name_invalid"
NAME_TAKEN: Final = "name_taken"
TEMPLATE_INVALID: Final = "template_invalid"
SCHEMA_INVALID: Final = "schema_invalid"
PURPOSE_LIMIT_REACHED: Final = "purpose_limit"

ATTRIBUTE_FILTERS: Final = frozenset(
    {"map", "selectattr", "rejectattr", "sort", "groupby", "sum", "unique", "min", "max"}
)
"""Built-in filters that read attributes by a name argument (#837)."""
POSITIONAL_ATTRIBUTE_FILTERS: Final = frozenset({"selectattr", "rejectattr", "groupby"})
"""Of those, the ones whose first argument is the attribute's name."""

_NAME = re.compile(NAME_PATTERN)
_PROPERTY = re.compile(PROPERTY_NAME_PATTERN)

REFUSED_STATEMENTS: Final[Mapping[type[nodes.Node], str]] = {
    nodes.Include: "include",
    nodes.Import: "import",
    nodes.FromImport: "from",
    nodes.Extends: "extends",
    nodes.Macro: "macro",
    nodes.CallBlock: "call",
}

SCALARS: Final = frozenset({"string", "number", "integer", "boolean"})
TYPES: Final = SCALARS | {"array", "object"}
COMMON_KEYS: Final = frozenset({"type", "description", "title"})
KEYS_BY_TYPE: Final[Mapping[str, frozenset[str]]] = {
    "string": frozenset({"enum", "const", "minLength", "maxLength", "pattern"}),
    "number": frozenset({"enum", "const", "minimum", "maximum"}),
    "integer": frozenset({"enum", "const", "minimum", "maximum"}),
    "boolean": frozenset({"enum", "const"}),
    "array": frozenset({"items", "minItems", "maxItems"}),
    "object": frozenset({"properties", "required", "additionalProperties"}),
}
REFUSED_KEYS: Final = frozenset(
    {"$ref", "$defs", "definitions", "anyOf", "oneOf", "allOf", "not", "nullable"}
)


class PurposeRefused(InvalidRequest):
    """A consumer purpose refused at save: 422 ``validation_error`` with ``details.reason``
    (``name_invalid``, ``name_taken``, ``template_invalid``, ``schema_invalid`` or
    ``purpose_limit``), ``details.field`` and ``details.message``."""

    def __init__(self, reason: str, field: str, message: str) -> None:
        super().__init__(message, {"reason": reason, "field": field, "message": message})


# -- names -------------------------------------------------------------------------------------


def check_name(name: str, reserved: Collection[str]) -> None:
    """``name_invalid`` off ``NAME_PATTERN``; ``name_taken`` for a built-in name. (A consumer
    purpose already registered is the store's check.)"""
    if not _NAME.fullmatch(name):
        raise PurposeRefused(
            NAME_INVALID,
            "body.name",
            "a purpose name is 3 to 48 characters: a lowercase letter, then lowercase "
            "letters, digits and '_'",
        )
    if name in reserved:
        raise PurposeRefused(
            NAME_TAKEN, "body.name", f"{name!r} is a built-in model purpose's name"
        )


# -- templates ---------------------------------------------------------------------------------


def environment() -> ImmutableSandboxedEnvironment:
    """The environment every consumer template is checked and rendered in (module docs)."""
    env = ImmutableSandboxedEnvironment(
        undefined=StrictUndefined,
        # Prompts are plain text sent to a model, never HTML.
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    # #837: no globals, so the scan's undeclared-variable check refuses them at save.
    env.globals.clear()
    return env


_ENVIRONMENT = environment()


def _private(name: object) -> bool:
    return isinstance(name, str) and name.startswith("_")


def _attribute_names(node: nodes.Filter) -> list[nodes.Expr]:
    """The arguments of an ``ATTRIBUTE_FILTERS`` filter that name an attribute."""
    named = [kw.value for kw in node.kwargs if kw.key == "attribute"]
    if node.name in POSITIONAL_ATTRIBUTE_FILTERS and node.args:
        named.append(node.args[0])
    return named


def _safe_attribute(arg: nodes.Expr) -> bool:
    """A constant attribute name (Jinja's dotted path or an index) with no ``_`` segment."""
    if not isinstance(arg, nodes.Const):
        return False
    if isinstance(arg.value, int):
        return True
    return isinstance(arg.value, str) and not any(
        _private(part.strip()) for part in re.split(r"[.,]", arg.value)
    )


def _scan(tree: nodes.Template) -> str | None:
    """The first construct the scan refuses, described; ``None`` when there is none."""
    for node in tree.find_all(nodes.Node):
        refused = REFUSED_STATEMENTS.get(type(node))
        if refused is not None:
            return f"the template uses {{% {refused} %}}, which consumer templates may not"
        if isinstance(node, nodes.Getattr) and _private(node.attr):
            return "the template reads an attribute whose name starts with '_'"
        if isinstance(node, nodes.Getitem):
            if isinstance(node.arg, nodes.Const) and _private(node.arg.value):
                return "the template reads an item whose name starts with '_'"
            if not isinstance(node.arg, (nodes.Const, nodes.Slice)):
                return "the template reads an item by a computed key; item keys are constants"
        if isinstance(node, nodes.Filter):
            if node.name not in FILTERS:
                return f"the template uses the filter {node.name!r}, which is not a Jinja built-in"
            if node.name == "attr":
                return "the template uses the filter 'attr', which consumer templates may not"
            if node.name in ATTRIBUTE_FILTERS and (
                node.dyn_args is not None
                or node.dyn_kwargs is not None
                or not all(_safe_attribute(arg) for arg in _attribute_names(node))
            ):
                return (
                    f"the filter {node.name!r} names an attribute that is computed or "
                    "starts with '_'"
                )
    undeclared = sorted(meta.find_undeclared_variables(tree) - TEMPLATE_VARIABLES)
    if undeclared:
        names = ", ".join(repr(name) for name in undeclared)
        allowed = ", ".join(sorted(TEMPLATE_VARIABLES))
        return f"the template references undeclared variable(s) {names} (allowed: {allowed})"
    return None


def check_template(source: str, field: str) -> Template:
    """``source`` compiled, or ``template_invalid`` at ``field`` (module docs)."""
    if len(source.encode("utf-8")) > TEMPLATE_MAX_BYTES:
        raise PurposeRefused(
            TEMPLATE_INVALID, field, f"the template exceeds {TEMPLATE_MAX_BYTES} bytes (UTF-8)"
        )
    if not source.strip():
        raise PurposeRefused(TEMPLATE_INVALID, field, "the template is empty")
    try:
        tree = _ENVIRONMENT.parse(source)
    except TemplateError as exc:
        raise PurposeRefused(
            TEMPLATE_INVALID, field, f"the template does not parse: {exc}"
        ) from None
    problem = _scan(tree)
    if problem is not None:
        raise PurposeRefused(TEMPLATE_INVALID, field, problem)
    try:
        return _ENVIRONMENT.from_string(source)
    except TemplateError as exc:
        raise PurposeRefused(
            TEMPLATE_INVALID, field, f"the template does not compile: {exc}"
        ) from None


# -- output schemas ----------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """``value`` as canonical JSON: sorted keys, no whitespace between tokens, UTF-8 text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bad(path: str, message: str) -> PurposeRefused:
    return PurposeRefused(SCHEMA_INVALID, path, message)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _matches(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return _is_number(value)


def _count(node: Mapping[str, Any], key: str, path: str) -> int | None:
    value = node.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _bad(f"{path}.{key}", f"{key} must be a non-negative integer")
    return value


def _bounds(node: Mapping[str, Any], low: str, high: str, path: str, *, count: bool) -> None:
    if count:
        lower, upper = _count(node, low, path), _count(node, high, path)
    else:
        for key in (low, high):
            if key in node and not _is_number(node[key]):
                raise _bad(f"{path}.{key}", f"{key} must be a number")
        lower, upper = node.get(low), node.get(high)
    if lower is not None and upper is not None and lower > upper:
        raise _bad(f"{path}.{low}", f"{low} is greater than {high}")


def _check_scalar(node: Mapping[str, Any], kind: str, path: str) -> None:
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, list) or not values:
            raise _bad(f"{path}.enum", "enum must be a non-empty list")
        if not all(_matches(kind, v) for v in values):
            raise _bad(f"{path}.enum", f"every enum value must be a {kind}")
        if len({canonical_json(v) for v in values}) != len(values):
            raise _bad(f"{path}.enum", "enum values must be unique")
    if "const" in node and not _matches(kind, node["const"]):
        raise _bad(f"{path}.const", f"const must be a {kind}")
    if kind == "string":
        _bounds(node, "minLength", "maxLength", path, count=True)
        if "pattern" in node:
            pattern = node["pattern"]
            if not isinstance(pattern, str):
                raise _bad(f"{path}.pattern", "pattern must be a string")
            try:
                re.compile(pattern)
            except re.error:
                raise _bad(f"{path}.pattern", "pattern is not a valid regular expression") from None
    elif kind in ("number", "integer"):
        _bounds(node, "minimum", "maximum", path, count=False)


def _check_object(node: Mapping[str, Any], path: str, level: int) -> None:
    if node.get("additionalProperties") is not False:
        raise _bad(
            f"{path}.additionalProperties", "every object must set additionalProperties: false"
        )
    properties = node.get("properties")
    if not isinstance(properties, dict):
        raise _bad(f"{path}.properties", "an object needs properties (an object of schemas)")
    required = node.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise _bad(f"{path}.required", "required must be a list of property names")
    if len(set(required)) != len(required) or set(required) != set(properties):
        raise _bad(f"{path}.required", "required must list every property exactly once")
    for name, sub in properties.items():
        if not _PROPERTY.fullmatch(name):
            raise _bad(
                f"{path}.properties",
                "property names are 1 to 64 characters: a letter or '_', then letters, "
                "digits, '_' and '-'",
            )
        # A nested object is one level deeper; a scalar or an array stays at this level.
        _check_node(sub, f"{path}.properties.{name}", level + 1 if _is_object(sub) else level)


def _is_object(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "object"


def _check_node(node: Any, path: str, level: int) -> None:
    """Check ``node``, which sits at nesting ``level`` (the root object is 1; each nested
    object or ``items`` adds 1)."""
    if not isinstance(node, dict):
        raise _bad(path, "a schema must be an object")
    refused = sorted(REFUSED_KEYS & node.keys())
    if refused:
        raise _bad(f"{path}.{refused[0]}", f"{refused[0]} is not supported")
    kind = node.get("type")
    if not isinstance(kind, str) or kind not in TYPES:
        raise _bad(f"{path}.type", "type must be one of " + ", ".join(sorted(TYPES)))
    unknown = sorted(node.keys() - COMMON_KEYS - KEYS_BY_TYPE[kind])
    if unknown:
        raise _bad(f"{path}.{unknown[0]}", f"{unknown[0]} is not supported on type {kind}")
    for key in ("description", "title"):
        if key in node and not isinstance(node[key], str):
            raise _bad(f"{path}.{key}", f"{key} must be a string")
    if level > SCHEMA_MAX_DEPTH:
        raise _bad(path, f"the schema is nested deeper than {SCHEMA_MAX_DEPTH} levels")
    if kind == "object":
        _check_object(node, path, level)
    elif kind == "array":
        if "items" not in node:
            raise _bad(f"{path}.items", "an array needs items")
        _bounds(node, "minItems", "maxItems", path, count=True)
        _check_node(node["items"], f"{path}.items", level + 1)
    else:
        _check_scalar(node, kind, path)


def check_schema(schema: Any, field: str = "body.output_schema") -> dict[str, Any]:
    """``schema`` as checked (module docs), or ``schema_invalid`` at the first fault."""
    if not isinstance(schema, dict):
        raise _bad(field, "the output schema must be a JSON object")
    if len(canonical_json(schema).encode("utf-8")) > SCHEMA_MAX_BYTES:
        raise _bad(field, f"the output schema exceeds {SCHEMA_MAX_BYTES} bytes as canonical JSON")
    if schema.get("type") != "object":
        raise _bad(f"{field}.type", "the output schema's root must be an object")
    _check_node(schema, field, 1)
    # Imported here: ``output`` builds on this module's limits.
    from epistrel.core.purposes.output import output_model

    try:
        output_model(schema)
    except Exception as exc:  # pydantic refuses what the static check could not see
        raise _bad(field, f"the output schema does not compile: {type(exc).__name__}") from None
    return schema


__all__ = [
    "DESCRIPTION_MAX",
    "NAME_INVALID",
    "NAME_PATTERN",
    "NAME_TAKEN",
    "PURPOSE_LIMIT",
    "PURPOSE_LIMIT_REACHED",
    "SCHEMA_INVALID",
    "SCHEMA_MAX_BYTES",
    "SCHEMA_MAX_DEPTH",
    "TEMPLATE_INVALID",
    "TEMPLATE_MAX_BYTES",
    "TEMPLATE_VARIABLES",
    "PurposeRefused",
    "canonical_json",
    "check_name",
    "check_schema",
    "check_template",
    "environment",
]
