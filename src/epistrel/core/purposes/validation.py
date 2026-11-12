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

#847 closes the rest of that class. A filter's attribute name is checked wherever Jinja takes it:
the ``attribute`` keyword, or its position in the filter's own signature (``sum`` first,
``unique``/``min``/``max``/``join`` second, ``sort`` third; ``selectattr``, ``rejectattr`` and
``groupby`` first). ``map('<filter>', …)`` is checked as that filter with the remaining arguments,
so ``map('attr', …)`` is ``attr``, and the filter's name is a constant. No filter, test or call
takes ``*args`` or ``**kwargs``. ``str.format`` and ``format_map`` are refused (a format field
reads attributes and items by name), as is a ``.get`` whose key is computed or starts with ``_``.
A ``%`` whose left side may be a printf format and whose right side may be a mapping is refused
unless the format is a constant whose ``%(name)`` keys do not start with ``_``. ``self``, which
the undeclared-variable check does not see, is refused by name.

#848: a constant key may be negative (``[-1]``): ``-``/``+`` of a numeric constant is a constant.

#852: Jinja's item lookup falls back to the attribute, so a constant string key is checked as the
attribute it may name: ``['format']`` and ``['format_map']`` are refused like ``.format``, and
``['get']`` is held to ``.get``'s rule. A filter's attribute name is a dotted path Jinja looks up
the same way, so no segment of it may name ``format``, ``format_map`` or ``get``
(``READING_METHODS``).

#853: what the scan can see would fail at render is refused too. A test (``is …``, or the name
``select``, ``reject``, ``selectattr`` and ``rejectattr`` take) is a constant naming a Jinja
built-in. ``super`` is refused by name, like ``self``. A call of a method the immutable sandbox
refuses (``MUTATING_METHODS``: ``pop``, ``append``, ``update`` and the rest, on a list, dict, set
or deque) is refused, as is taking a dict's (``.pop``, ``.update``, ``.setdefault``,
``.popitem``, ``.clear``), which the sandbox always refuses on a mapping; any of these names stays
accepted as a key that is read and not called. A slice's bounds are constant integers.

#857: a consumer's ``inputs`` are JSON, and a JSON value is never callable, so a call of a
mutating-name access can never render: whatever it reaches is either a refused method or a JSON
value. Besides the direct call (#853: ``inputs.list.append(1)``), the scan refuses a call whose
callee is a simple name that ``{% set %}`` or ``{% with %}`` bound earlier in the template to such
an access (``{% set p = inputs['pop'] %}{{ p('voter') }}``), and a call whose callee is a filter
chain (through filters and constant integer indexes) in which ``map(attribute=…)`` or another
filter's attribute name (``ATTRIBUTE_POSITIONS``, positional or keyword) names one of
``MUTATING_METHODS`` in any segment (``([inputs]|map(attribute='pop')|first)('voter')``). Binding
or mapping such a name without calling it stays accepted, because it reads the key when the key
exists. The alias is one level deep: an alias of an alias or a macro argument is not followed,
and such a call fails cleanly when it is made.

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

import inspect
import json
import re
from collections import deque
from collections.abc import Collection, Mapping
from typing import Any, Final, cast

from jinja2 import StrictUndefined, Template, TemplateError, meta, nodes
from jinja2.filters import FILTERS
from jinja2.sandbox import ImmutableSandboxedEnvironment, modifies_known_mutable
from jinja2.tests import TESTS

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

_PASSED: Final = frozenset({"environment", "eval_ctx", "context"})
"""Parameters Jinja passes a filter itself (``pass_environment`` and the like)."""


def _attribute_positions() -> dict[str, int]:
    """Each built-in filter that reads an attribute by name, mapped to the index of that name
    among the arguments a template passes it (#847): from the filter's own signature where it
    has an ``attribute`` parameter, and the first argument for ``selectattr``/``rejectattr``."""
    positions = {"selectattr": 0, "rejectattr": 0}
    for name, function in FILTERS.items():
        parameters = [p for p in inspect.signature(function).parameters if p not in _PASSED]
        if "attribute" in parameters:
            # The first remaining parameter is the filtered value, which the template does
            # not pass as an argument.
            positions[name] = parameters.index("attribute") - 1
    return positions


ATTRIBUTE_POSITIONS: Final[Mapping[str, int]] = _attribute_positions()
"""Built-in filters that read attributes by a name argument, and that argument's position
(#837, #847)."""
FORMAT_METHODS: Final = frozenset({"format", "format_map"})
"""String methods whose format fields read attributes and items by name (#847)."""
READING_METHODS: Final = FORMAT_METHODS | {"get"}
"""Methods that read attributes or items by a name they are given (#847, #852)."""
TEST_POSITIONS: Final[Mapping[str, int]] = {
    "select": 0,
    "reject": 0,
    "selectattr": 1,
    "rejectattr": 1,
}
"""Filters that apply a test named by an argument, and that argument's position (#853)."""


def _mutating(*values: object) -> frozenset[str]:
    """The method names the immutable sandbox refuses on ``values`` (#853)."""
    return frozenset(
        name for value in values for name in dir(value) if modifies_known_mutable(value, name)
    )


MUTATING_METHODS: Final = _mutating([], {}, set(), deque())
"""Methods the immutable sandbox refuses on a list, dict, set or deque (#853)."""
MAPPING_MUTATING_METHODS: Final = _mutating({})
"""The dict's own: a mapping always has them, so Jinja never falls back to an item (#853)."""
_PRINTF_KEY = re.compile(r"%\(([^)]*)\)")

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


def _number(arg: nodes.Expr | None, kinds: type | tuple[type, ...]) -> bool:
    """A constant of ``kinds``, or ``-``/``+`` of one (#848); never a bool."""
    if isinstance(arg, (nodes.Neg, nodes.Pos)):
        arg = arg.node
    return (
        isinstance(arg, nodes.Const)
        and isinstance(arg.value, kinds)
        and not isinstance(arg.value, bool)
    )


def _constant_key(arg: nodes.Expr) -> bool:
    """A constant item key: a constant, ``-``/``+`` of a numeric constant (#848), or a slice
    whose bounds are constant integers (#853)."""
    if isinstance(arg, nodes.Slice):
        return all(
            bound is None or _number(bound, int) for bound in (arg.start, arg.stop, arg.step)
        )
    if isinstance(arg, (nodes.Neg, nodes.Pos)):
        return _number(arg, (int, float))
    return isinstance(arg, nodes.Const)


def _member(node: nodes.Node) -> str | None:
    """The name ``node`` looks up: an attribute's, or a constant string key's, which Jinja
    looks up as an attribute when the item is missing (#852)."""
    if isinstance(node, nodes.Getattr):
        return node.attr
    if (
        isinstance(node, nodes.Getitem)
        and isinstance(node.arg, nodes.Const)
        and isinstance(node.arg.value, str)
    ):
        return node.arg.value
    return None


def _segments(name: str) -> list[str]:
    """The names in a filter's attribute argument: Jinja's dotted path, or ``sort``'s and
    ``unique``'s comma-separated list."""
    return [raw.strip() for raw in re.split(r"[.,]", name)]


def _attribute_args(
    name: str, args: list[nodes.Expr], kwargs: list[nodes.Keyword]
) -> list[nodes.Expr]:
    """The arguments that filter ``name`` reads as attribute names (#837, #847): the
    ``attribute`` keyword and the position ``ATTRIBUTE_POSITIONS`` gives. ``map('<filter>', …)``
    is that filter with the remaining arguments; ``map(attribute=…)`` reads the attribute
    itself."""
    if name == "map" and args:
        first = args[0]
        if isinstance(first, nodes.Const) and isinstance(first.value, str):
            return _attribute_args(first.value, args[1:], kwargs)
        return []
    if name not in ATTRIBUTE_POSITIONS and name != "map":
        return []
    named = [kw.value for kw in kwargs if kw.key == "attribute"]
    position = ATTRIBUTE_POSITIONS.get(name)
    if position is not None and len(args) > position:
        named.append(args[position])
    return named


def _safe_attribute(arg: nodes.Expr) -> bool:
    """A constant attribute name (Jinja's dotted path or an index) with no segment that starts
    with ``_`` or names one of ``READING_METHODS`` (#852)."""
    if not isinstance(arg, nodes.Const):
        return False
    if isinstance(arg.value, int):
        return True
    return isinstance(arg.value, str) and not any(
        _private(part) or part in READING_METHODS for part in _segments(arg.value)
    )


def _mutating_attribute(node: nodes.Filter) -> str | None:
    """The ``MUTATING_METHODS`` name a segment of filter ``node``'s attribute argument names
    (#857); ``None`` when there is none."""
    # Jinja annotates a filter's kwargs as `Pair`s, but the parser builds `Keyword`s.
    kwargs = cast(list[nodes.Keyword], node.kwargs)
    for arg in _attribute_args(node.name, node.args, kwargs):
        if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
            for part in _segments(arg.value):
                if part in MUTATING_METHODS:
                    return part
    return None


def _mutating_access(expr: nodes.Node) -> str | None:
    """The ``MUTATING_METHODS`` name ``expr`` reaches (#857): an attribute or constant key
    naming one, or a filter chain (through filters and constant integer indexes) with a filter
    whose attribute argument names one. A JSON value is never callable, so a call of what this
    names never renders. ``None`` when ``expr`` reaches none."""
    member = _member(expr)
    if member in MUTATING_METHODS:
        return member
    node: nodes.Node | None = expr
    while node is not None:
        if isinstance(node, nodes.Filter):
            found = _mutating_attribute(node)
            if found is not None:
                return found
            node = node.node
        elif isinstance(node, nodes.Getitem) and _number(node.arg, int):
            node = node.node
        else:
            return None
    return None


def _bind(aliases: dict[str, str], target: nodes.Node, value: nodes.Node) -> None:
    """Record (or clear) simple name ``target`` as an alias of a mutating-name access (#857).
    One level: a name bound to another alias is not followed."""
    if not isinstance(target, nodes.Name):
        return
    found = _mutating_access(value)
    if found is None:
        aliases.pop(target.name, None)
    else:
        aliases[target.name] = found


def _filter_problem(
    name: str, args: list[nodes.Expr], kwargs: list[nodes.Keyword], value: nodes.Expr | None
) -> str | None:
    """What is refused about filter ``name`` applied to ``value`` (``None`` when ``map``
    applies it to each item) with ``args`` and ``kwargs``; ``None`` when nothing is."""
    if name not in FILTERS:
        return f"the template uses the filter {name!r}, which is not a Jinja built-in"
    if name == "attr":
        return "the template uses the filter 'attr', which consumer templates may not"
    if name == "map" and args:
        # ``map('<filter>', …)`` applies that filter to each item with the remaining arguments.
        first = args[0]
        if not (isinstance(first, nodes.Const) and isinstance(first.value, str)):
            return "the filter 'map' names a filter that is computed"
        return _filter_problem(first.value, args[1:], kwargs, None)
    if not all(_safe_attribute(arg) for arg in _attribute_args(name, args, kwargs)):
        return (
            f"the filter {name!r} names an attribute that is computed, starts with '_' or "
            "names a method that reads by name"
        )
    position = TEST_POSITIONS.get(name)
    if position is not None and len(args) > position:
        test = args[position]
        if not (isinstance(test, nodes.Const) and test.value in TESTS):
            return f"the filter {name!r} names a test that is computed or not a Jinja built-in"
    if name == "format":
        if any(_private(kw.key) for kw in kwargs):
            return "the filter 'format' names a field that starts with '_'"
        if isinstance(value, nodes.Const) and _private_printf(value.value):
            return "the filter 'format' formats a field whose name starts with '_'"
    return None


def _private_printf(text: object) -> bool:
    """A string whose printf ``%(name)`` fields include a name starting with ``_``."""
    return isinstance(text, str) and any(
        _private(key) for key in _PRINTF_KEY.findall(text.replace("%%", ""))
    )


def _printf_problem(node: nodes.Mod) -> str | None:
    """A ``%`` that may read a mapping's item by a name the scan cannot see (#847)."""
    if isinstance(node.left, nodes.Const):
        if _private_printf(node.left.value):
            return "the template formats a field whose name starts with '_'"
        return None
    # The format is computed: safe only when the right side cannot be a mapping.
    if isinstance(node.right, (nodes.Const, nodes.Tuple, nodes.List)):
        return None
    return "the template formats with a computed format string and a computed value"


def _get_problem(node: nodes.Call) -> str | None:
    """A ``.get(key, …)`` whose key is computed or starts with ``_``."""
    key = node.args[0] if node.args else None
    if not isinstance(key, nodes.Const):
        return "the template reads an item by a computed key; item keys are constants"
    if _private(key.value):
        return "the template reads an item whose name starts with '_'"
    return None


def _scan(tree: nodes.Template) -> str | None:
    """The first construct the scan refuses, described; ``None`` when there is none."""
    # A ``.get`` (or ``['get']``, #852) is checked where it is called; one taken without a
    # call could be called with any key later.
    calls = {id(call.node): call for call in tree.find_all(nodes.Call)}
    # #857: names a `{% set %}` or `{% with %}` bound to a mutating-name access, in document
    # order, each mapped to the method's name.
    aliases: dict[str, str] = {}
    for node in tree.find_all(nodes.Node):
        if isinstance(node, nodes.Assign):
            _bind(aliases, node.target, node.node)
        if isinstance(node, nodes.With):
            for target, value in zip(node.targets, node.values, strict=False):
                _bind(aliases, target, value)
        if isinstance(node, nodes.Call):
            callee = node.node
            called = (
                aliases.get(callee.name)
                if isinstance(callee, nodes.Name)
                else _mutating_access(callee)
            )
            if called is not None:
                return f"the template uses {called!r}, a method that changes its value"
        refused = REFUSED_STATEMENTS.get(type(node))
        if refused is not None:
            return f"the template uses {{% {refused} %}}, which consumer templates may not"
        if isinstance(node, nodes.Getattr) and _private(node.attr):
            return "the template reads an attribute whose name starts with '_'"
        if isinstance(node, nodes.Getitem):
            if isinstance(node.arg, nodes.Const) and _private(node.arg.value):
                return "the template reads an item whose name starts with '_'"
            if isinstance(node.arg, nodes.Slice) and not _constant_key(node.arg):
                return "the template slices by a computed bound; slice bounds are constant integers"
            if not _constant_key(node.arg):
                return "the template reads an item by a computed key; item keys are constants"
        member = _member(node)
        if member in FORMAT_METHODS:
            return f"the template uses {member!r}, which consumer templates may not"
        call = calls.get(id(node))
        if member == "get":
            problem = (
                _get_problem(call)
                if call is not None
                else "the template takes 'get' without calling it"
            )
            if problem is not None:
                return problem
        if member in MUTATING_METHODS and (
            call is not None
            or (isinstance(node, nodes.Getattr) and member in MAPPING_MUTATING_METHODS)
        ):
            return f"the template uses {member!r}, a method that changes its value"
        if isinstance(node, nodes.Name) and node.name in ("self", "super"):
            return f"the template references {node.name!r}, which consumer templates may not"
        if isinstance(node, nodes.Test) and node.name not in TESTS:
            return f"the template uses the test {node.name!r}, which is not a Jinja built-in"
        if isinstance(node, (nodes.Filter, nodes.Test, nodes.Call)) and (
            node.dyn_args is not None or node.dyn_kwargs is not None
        ):
            return "the template passes *args or **kwargs, which consumer templates may not"
        if isinstance(node, nodes.Filter):
            # Jinja annotates a filter's kwargs as `Pair`s, but the parser builds `Keyword`s.
            kwargs = cast(list[nodes.Keyword], node.kwargs)
            problem = _filter_problem(node.name, node.args, kwargs, node.node)
            if problem is not None:
                return problem
        if isinstance(node, nodes.Mod):
            problem = _printf_problem(node)
            if problem is not None:
                return problem
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
    "ATTRIBUTE_POSITIONS",
    "DESCRIPTION_MAX",
    "FORMAT_METHODS",
    "MAPPING_MUTATING_METHODS",
    "MUTATING_METHODS",
    "NAME_INVALID",
    "NAME_PATTERN",
    "NAME_TAKEN",
    "PURPOSE_LIMIT",
    "PURPOSE_LIMIT_REACHED",
    "READING_METHODS",
    "SCHEMA_INVALID",
    "SCHEMA_MAX_BYTES",
    "SCHEMA_MAX_DEPTH",
    "TEMPLATE_INVALID",
    "TEMPLATE_MAX_BYTES",
    "TEMPLATE_VARIABLES",
    "TEST_POSITIONS",
    "PurposeRefused",
    "canonical_json",
    "check_name",
    "check_schema",
    "check_template",
    "environment",
]
