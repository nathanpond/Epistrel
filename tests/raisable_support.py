"""What each REST route can raise, from a static walk of the code (#885).

There is no sound way to derive it: Python dispatches at run time. This walk is a mechanical
approximation, and ``tests/test_openapi_declared.py::test_every_raisable_code_is_declared``
checks every code it finds against the route's declared responses.

mypy type-checks ``src/epistrel`` with its expression types exported (about 25 s, no cache), so
a call such as ``self._store.read(...)`` or a nested ``run`` handed to ``_keyed`` resolves to
the function it names. From a route's endpoint and its dependencies the walk:

- follows every function of ``WALKED`` (the REST layer and the Engine facade) that a function it
  walks calls or names (a function passed as an argument counts), and the overrides of a method
  in its nominal subclasses;
- takes the direct ``raise`` sites of every other ``epistrel`` function such a function calls
  or names (one step into core or the orchestrator), and does not follow that function's calls;
- reads a ``raise`` site's error class from the static type of the raised expression;
- drops an error raised inside a ``try`` whose handler catches it (by class, ``Exception`` or a
  bare ``except``) without a bare ``raise``.

What it does NOT see (a green carries only this coverage):

- raises two or more calls outside ``WALKED``: most of core's and the orchestrator's own
  refusals. A code core raises in the function the Engine calls is found; one raised deeper
  is not;
- dispatch the types do not name: a value typed ``Any`` (the ownership routes reach the Engine
  through ``**values: Any``; the test lists such routes as ``UNWALKED``), a ``Protocol`` or
  ``Callable`` value, ``getattr``, a function kept in a mapping, a structural implementation;
- ``raise exc`` with ``exc`` typed as ``EpistrelError`` itself, and the errors the app's handlers
  build from other exceptions (``database_unavailable``, FastAPI's ``validation_error``);
- conditions, the other way: a raise behind a branch a route never takes is still found. The
  caller's ``exempt`` table names each such raise with its reason.
"""

from __future__ import annotations

import importlib
import inspect
import os
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from mypy import build
from mypy.main import process_options
from mypy.nodes import (
    Argument,
    Block,
    ClassDef,
    Decorator,
    Expression,
    FuncDef,
    LambdaExpr,
    MemberExpr,
    MypyFile,
    NameExpr,
    Node,
    OverloadedFuncDef,
    RaiseStmt,
    Statement,
    TryStmt,
    TupleExpr,
    TypeInfo,
)
from mypy.patterns import Pattern
from mypy.types import CallableType, Instance, Overloaded, Type, UnionType, get_proper_type

from epistrel.core.errors import EpistrelError
from tests.conftest import REPO_ROOT

WALKED = ("epistrel.api.", "epistrel.engine.")
"""Modules whose functions the walk follows call by call."""

CATCH_ALL = frozenset({"builtins.Exception", "builtins.BaseException"})

_SKIPPED_CHILDREN = frozenset(
    {
        # references to definitions elsewhere, not children
        "node",
        "def_var",
        "info",
        "var",
        "original_def",
        # copies or unanalyzed forms of a child already walked
        "expanded",
        "def_or_infer_vars",
        "setter",
        "unanalyzed_items",
        "unanalyzed_type",
        "removed_statements",
        "removed_base_type_exprs",
        "original_decorators",
        "type",
    }
)
_CHILD_KINDS = (Statement, Expression, Block, Argument, Pattern)
_CHILD_ATTRIBUTES: dict[type, list[str]] = {}


def _children(node: Node) -> Iterator[Node]:
    """A node's statements and expressions. mypy's compiled visitors cannot be subclassed from
    interpreted code, so the walk reads each node class's attributes."""
    kind = type(node)
    names = _CHILD_ATTRIBUTES.get(kind)
    if names is None:
        names = [
            name
            for name in dir(kind)
            if not name.startswith("_")
            and name not in _SKIPPED_CHILDREN
            and type(getattr(kind, name, None)).__name__
            in ("getset_descriptor", "member_descriptor")
        ]
        _CHILD_ATTRIBUTES[kind] = names
    for name in names:
        value = getattr(node, name, None)
        if isinstance(value, _CHILD_KINDS):
            yield value
        elif isinstance(value, list | tuple):
            for item in value:
                if isinstance(item, _CHILD_KINDS):
                    yield item
                elif isinstance(item, list | tuple):
                    yield from (inner for inner in item if isinstance(inner, _CHILD_KINDS))


def _function(node: Node) -> FuncDef | None:
    """The function a definition runs: a decorated function's, an overload's implementation."""
    if isinstance(node, Decorator):
        return node.func
    if isinstance(node, OverloadedFuncDef):
        return None if node.impl is None else _function(node.impl)
    return node if isinstance(node, FuncDef) else None


def _infos(typ: Type) -> list[TypeInfo]:
    proper = get_proper_type(typ)
    if isinstance(proper, Instance):
        return [proper.type]
    if isinstance(proper, UnionType):
        return [info for item in proper.items for info in _infos(item)]
    return []


def _targets(typ: Type) -> list[FuncDef]:
    """The functions an expression of type ``typ`` names: a function or bound method, or a
    class (its ``__init__`` and ``__post_init__``)."""
    proper = get_proper_type(typ)
    if isinstance(proper, Overloaded):
        return [target for item in proper.items for target in _targets(item)]
    if not isinstance(proper, CallableType):
        return []
    found: list[Node | None]
    if proper.is_type_obj():
        info = proper.type_object()
        found = [info.get_method(name) for name in ("__init__", "__post_init__")]
    else:
        found = [proper.definition]
    return [f for node in found if node is not None and (f := _function(node)) is not None]


Caught = frozenset[str] | None
"""One ``try``'s handlers: the full names of the classes they stop, ``None`` for everything."""


def _caught(handler: Expression | None) -> Caught:
    if handler is None:
        return None
    if isinstance(handler, TupleExpr):
        names: set[str] = set()
        for item in handler.items:
            inner = _caught(item)
            if inner is None:
                return None
            names |= inner
        return frozenset(names)
    if isinstance(handler, NameExpr | MemberExpr) and isinstance(handler.node, TypeInfo):
        if handler.node.fullname in CATCH_ALL:
            return None
        return frozenset({handler.node.fullname})
    return frozenset()


def _reraises(node: Node) -> bool:
    if isinstance(node, RaiseStmt) and node.expr is None:
        return True
    if isinstance(node, FuncDef | LambdaExpr):
        return False
    return any(_reraises(child) for child in _children(node))


def _stopped(info: TypeInfo, caught: tuple[Caught, ...]) -> bool:
    names = {ancestor.fullname for ancestor in info.mro}
    return any(stop is None or not stop.isdisjoint(names) for stop in caught)


@dataclass(frozen=True)
class _Site:
    """A raise of ``error``, or a reference to ``target``, under ``caught`` (the enclosing
    ``try`` handlers, innermost last)."""

    caught: tuple[Caught, ...]
    error: TypeInfo | None = None
    target: FuncDef | None = None


def _sites(body: Block, types: Mapping[Expression, Type], base: TypeInfo) -> list[_Site]:
    sites: list[_Site] = []

    def visit(node: Node, caught: tuple[Caught, ...]) -> None:
        if isinstance(node, RaiseStmt) and node.expr is not None and node.expr in types:
            for info in _infos(types[node.expr]):
                if info is not base and base in info.mro:
                    sites.append(_Site(caught, error=info))
        if isinstance(node, NameExpr | MemberExpr) and node in types:
            for target in _targets(types[node]):
                if target.fullname.startswith("epistrel."):
                    sites.append(_Site(caught, target=target))
        if isinstance(node, FuncDef):
            # A nested function runs where it is called, not under a ``try`` around its
            # definition; it is walked inline and through its references.
            caught = ()
        if isinstance(node, TryStmt):
            stops: set[str] = set()
            stops_all = False
            for handler_type, handler in zip(node.types, node.handlers, strict=True):
                if _reraises(handler):
                    continue
                handled = _caught(handler_type)
                if handled is None:
                    stops_all = True
                else:
                    stops |= handled
            visit(node.body, (*caught, None if stops_all else frozenset(stops)))
            rest: list[Node | None] = [*node.types, *node.handlers]
            for part in (*rest, node.else_body, node.finally_body):
                if part is not None:
                    visit(part, caught)
            return
        for child in _children(node):
            visit(child, caught)

    visit(body, ())
    return sites


Path = tuple[str, ...]
"""The functions from a root to the raise, by full name."""


@dataclass
class Raisable:
    """Every function's raisable errors and the tree they came from."""

    files: Mapping[str, MypyFile]
    raises: dict[int, dict[TypeInfo, Path]]
    """A walked function's errors (by ``id`` of its ``FuncDef``), each with one path to it."""
    exempted: set[tuple[str, str]]
    """The ``exempt`` entries the walk met."""
    nested: Mapping[str, list[FuncDef]]
    """Each module's functions, nested ones included."""
    errors: list[TypeInfo]
    """Every ``EpistrelError`` subclass in ``src/epistrel``."""
    calls: Mapping[int, set[str]]
    """The full names of the functions a walked function reaches through walked functions."""

    def function(self, call: Callable[..., Any] | type) -> FuncDef | None:
        """The definition of a runtime function, method or class (its ``__call__`` for an
        instance); ``None`` outside ``epistrel``."""
        if not inspect.isfunction(call) and not inspect.ismethod(call):
            kind = call if isinstance(call, type) else type(call)
            member = "__init__" if isinstance(call, type) else "__call__"
            if member not in vars(kind):
                return None
            call = getattr(kind, member)
        call = getattr(call, "__func__", call)
        module = getattr(call, "__module__", "") or ""
        if not module.startswith("epistrel."):
            return None
        qualname: str = call.__qualname__
        if "<locals>" in qualname:
            # A route a factory defines (``mount_ownership_routes``): by name and line.
            first = call.__code__.co_firstlineno
            candidates = [
                f
                for f in self.nested.get(module, [])
                if f.name == call.__name__ and f.line >= first
            ]
            assert candidates, f"{module}.{qualname} not in the tree"
            return min(candidates, key=lambda f: f.line)
        node: Node | None = self.files[module]
        for part in qualname.split("."):
            table = node.names if isinstance(node, MypyFile | TypeInfo) else {}
            assert part in table, f"{module}.{qualname} not in the tree"
            node = table[part].node
        found = _function(node) if node is not None else None
        assert found is not None, f"{module}.{qualname} is not a function"
        return found

    def of(self, call: Callable[..., Any] | type) -> dict[TypeInfo, Path]:
        """What ``call`` (a route's endpoint or dependency) can raise, each with one path."""
        found = self.function(call)
        return {} if found is None else self.raises.get(id(found), {})

    def reaches(self, call: Callable[..., Any] | type) -> set[str]:
        """The functions ``call``'s walk reaches, by full name."""
        found = self.function(call)
        return set() if found is None else set(self.calls.get(id(found), set()))


@cache
def _tree() -> tuple[Mapping[str, MypyFile], Mapping[Expression, Type]]:
    """``src/epistrel`` type-checked with its expression types (about 25 s)."""
    sources, options = process_options(
        [str(REPO_ROOT / "src" / "epistrel"), "--config-file", str(REPO_ROOT / "pyproject.toml")]
    )
    options.export_types = True
    options.preserve_asts = True
    options.incremental = False
    options.cache_dir = os.devnull
    result = build.build(sources=sources, options=options)
    files = {
        name: tree
        for name, tree in result.files.items()
        if name == "epistrel" or name.startswith("epistrel.")
    }
    return files, result.types


def raisable(exempt: Mapping[tuple[str, str], str]) -> Raisable:
    """Each walked function's errors. ``exempt`` maps (function, error class), by full names,
    to why the function never raises that error although the walk finds it: the function drops
    it from what it raises or passes on."""
    files, types = _tree()
    base = files["epistrel.core.errors"].names["EpistrelError"].node
    assert isinstance(base, TypeInfo)

    functions: dict[int, FuncDef] = {}
    classes: list[TypeInfo] = []
    nested: dict[str, list[FuncDef]] = defaultdict(list)

    def find(node: Node, module: str) -> None:
        if isinstance(node, FuncDef):
            functions[id(node)] = node
            nested[module].append(node)
        if isinstance(node, ClassDef):
            classes.append(node.info)
        for child in _children(node):
            find(child, module)

    for module, tree in files.items():
        for definition in tree.defs:
            find(definition, module)
    sites = {key: _sites(f.body, types, base) for key, f in functions.items()}

    overrides: dict[int, set[int]] = defaultdict(set)
    for info in classes:
        for name, symbol in info.names.items():
            mine = _function(symbol.node) if symbol.node is not None else None
            if mine is None:
                continue
            for ancestor in info.mro[1:]:
                theirs = ancestor.names.get(name)
                if theirs is not None and theirs.node is not None:
                    overridden = _function(theirs.node)
                    if overridden is not None:
                        overrides[id(overridden)].add(id(mine))

    exempted: set[tuple[str, str]] = set()
    calls: dict[int, set[str]] = defaultdict(set)

    def keep(function: FuncDef, info: TypeInfo) -> bool:
        entry = (function.fullname, info.fullname)
        if entry in exempt:
            exempted.add(entry)
            return False
        return True

    def direct(function: FuncDef) -> dict[TypeInfo, Path]:
        return {
            site.error: (function.fullname,)
            for site in sites.get(id(function), [])
            if site.error is not None
            and not _stopped(site.error, site.caught)
            and keep(function, site.error)
        }

    walked = {key: f for key, f in functions.items() if f.fullname.startswith(WALKED)}
    raises: dict[int, dict[TypeInfo, Path]] = {key: {} for key in walked}
    changed = True
    while changed:
        changed = False
        for key, function in walked.items():
            found = raises[key]
            for site in sites[key]:
                reached: dict[TypeInfo, Path]
                if site.error is not None:
                    reached = {site.error: (function.fullname,)}
                elif site.target is not None:
                    reached = {}
                    for target_key in {id(site.target)} | overrides.get(id(site.target), set()):
                        target = functions.get(target_key)
                        if target is None:
                            continue
                        before = len(calls[key])
                        calls[key].add(target.fullname)
                        if target_key in walked:
                            inner = raises[target_key]
                            calls[key] |= calls[target_key]
                        else:
                            inner = direct(target)
                        changed = changed or len(calls[key]) != before
                        for info, path in inner.items():
                            reached.setdefault(info, (function.fullname, *path))
                else:
                    continue
                for info, path in reached.items():
                    if (
                        info not in found
                        and not _stopped(info, site.caught)
                        and keep(function, info)
                    ):
                        found[info] = path
                        changed = True
    errors = [info for info in classes if info is not base and base in info.mro]
    return Raisable(files, raises, exempted, nested, errors, calls)


def error_class(info: TypeInfo) -> type[EpistrelError]:
    """The runtime class of a ``TypeInfo``."""
    module = importlib.import_module(info.module_name)
    found: Any = module
    for part in info.fullname.removeprefix(info.module_name + ".").split("."):
        found = getattr(found, part)
    assert isinstance(found, type), info.fullname
    assert issubclass(found, EpistrelError), info.fullname
    return found
