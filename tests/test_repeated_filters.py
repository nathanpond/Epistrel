"""A plain string in a collection-of-strings parameter of ``Engine`` is one value (#324), as
#110 made it for the catalog search and as one query parameter is over REST: a ``str`` is
itself a ``Sequence[str]``, so a filter read it one character at a time. Without a database:
every such parameter is found from its annotation and the method's body is shown to receive
the one-value tuple. The issue's examples against PostgreSQL are in
``tests/integration/test_repeated_filters.py``."""

from __future__ import annotations

import inspect
import re
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any

import pytest

from epistrel.core.context import RequestContext
from epistrel.engine import (
    REPEATED_ATTRIBUTE,
    Engine,
    library_only_methods,
    operations,
    repeated_parameters,
)

STRINGS = re.compile(
    r"\b(?:Sequence|Iterable|Collection|AbstractSet|Set|list|tuple|set|frozenset)\[str\b"
)
"""An annotation naming a collection of strings, read from its source text: independent of the
engine's own ``repeated_parameters``, so a form it misses fails here."""


def _public_methods() -> list[str]:
    return operations() + sorted(library_only_methods())


def _annotated_as_strings() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name in _public_methods():
        raw = inspect.unwrap(getattr(Engine, name))
        for parameter in inspect.signature(raw).parameters.values():
            if STRINGS.search(str(parameter.annotation)):
                found.append((name, parameter.name))
    return found


CASES = _annotated_as_strings()


def test_the_cases_cover_the_issue_examples() -> None:
    assert {
        ("config_changes", "settings"),
        ("list_stories", "relation"),
        ("list_media", "relation"),
        ("list_profiles", "relation"),
        ("get_media_content", "if_none_match"),
        ("search_catalog", "label"),
        ("list_personas", "relation"),
    } <= set(CASES)


def test_every_collection_of_strings_is_normalised() -> None:
    """Every parameter annotated as a collection of strings on a public ``Engine`` method is one
    ``normalise_repeated_methods`` takes a plain string in, and it takes no other; the
    annotation admits the plain string it takes (``str | Sequence[str]``)."""
    normalised = {
        (name, parameter)
        for name in _public_methods()
        for parameter in getattr(getattr(Engine, name), REPEATED_ATTRIBUTE, ())
    }
    assert normalised == set(CASES)
    for name, parameter in CASES:
        assert parameter in repeated_parameters(getattr(Engine, name))
        annotation = str(inspect.signature(getattr(Engine, name)).parameters[parameter].annotation)
        assert re.match(r"str \|", annotation), (name, parameter, annotation)


def _normalising_layer(method: Callable[..., Any]) -> Any:
    """The wrapper that normalises, found under the gates and guards in front of it; it must
    wrap the method's own body directly, so nothing between them sees the plain string."""
    layer: Any = method
    while getattr(getattr(layer, "__wrapped__", None), REPEATED_ATTRIBUTE, None) is not None:
        layer = layer.__wrapped__
    assert getattr(layer, REPEATED_ATTRIBUTE, None) is not None
    assert not hasattr(layer.__wrapped__, "__wrapped__")
    return layer


def _call_values(name: str, parameter: str, value: Any) -> tuple[list[Any], dict[str, Any]]:
    """Arguments for ``name``: ``value`` for ``parameter``, a plain string for every other
    required one."""
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for p in list(inspect.signature(getattr(Engine, name)).parameters.values())[2:]:
        if p.name == parameter:
            given = value
        elif p.default is inspect.Parameter.empty:
            given = "x"
        else:
            continue
        if p.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[p.name] = given
        else:
            args.append(given)
    return args, kwargs


async def _body_receives(name: str, parameter: str, value: Any) -> Any:
    """What the body of ``name`` holds in ``parameter`` on entry when called through its
    normalising layer with ``value`` (``self`` is a bare object: the body then fails, which is
    ignored)."""
    layer = _normalising_layer(getattr(Engine, name))
    body = layer.__wrapped__.__code__
    seen: list[Any] = []

    def profile(frame: FrameType, event: str, _: Any) -> None:
        if event == "call" and frame.f_code is body and not seen:
            seen.append(frame.f_locals[parameter])

    args, kwargs = _call_values(name, parameter, value)
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        await layer(object(), RequestContext(principal="p-reader"), *args, **kwargs)
    except Exception:  # noqa: S110 - the body ran on a bare object: anything but the entry
        pass
    finally:
        sys.setprofile(previous)
    assert seen, f"{name}'s body was not entered"
    return seen[0]


@pytest.mark.parametrize(("name", "parameter"), CASES, ids=[f"{n}.{p}" for n, p in CASES])
async def test_a_plain_string_is_one_value(name: str, parameter: str) -> None:
    """The body receives ``("editor:p-c",)`` for ``"editor:p-c"``. Complement asserted: a list
    reaches it as given."""
    assert await _body_receives(name, parameter, "editor:p-c") == ("editor:p-c",)
    given = ["editor:p-c", "co_author:"]
    assert await _body_receives(name, parameter, given) is given
