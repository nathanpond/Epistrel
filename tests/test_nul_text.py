"""NUL characters (#275, #278; FR-API-4, FR-MODEL-6) without a database: a model's answer
loses them in the Model Access Layer, raw or as a JSON escape in a structured answer, and every
Engine read refuses one in its arguments before any SQL. The integration halves are in
``tests/integration/test_nul_text.py``."""

from __future__ import annotations

import inspect
import io
import json
import logging
import re
from collections.abc import Awaitable, Iterator
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest, field_path, without_nul
from epistrel.core.settings_schema import StructuredSettings
from epistrel.engine import (
    Engine,
    library_only_methods,
    nul_checked_methods,
    nul_guarded_parameters,
    operations,
)
from epistrel.observability import logging as telemetry_logging
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import Message
from epistrel.orchestrator.models.errors import EmptyCompletion
from epistrel.orchestrator.models.registry import GuardrailCheckInputs, GuardrailVerdict
from epistrel.orchestrator.models.structured import parse
from tests.auth_support import auth_settings
from tests.conformance.test_c_02 import full_app, method_of, route_operations
from tests.conftest import DUMMY_DSN

NUL = "\x00"
ACCESS_LOGGER = "epistrel.orchestrator.models.access"
WARNING = logging.WARNING
HELLO = (Message(role="user", content="hello"),)
LIBRARY_READS = ("effective_budget", "policies_effective", "undo_state")
"""Library-only methods that read: served inside a ``GET`` route's answer (``library_only``)."""


class Said(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    tags: list[str]


def stub_models() -> ModelAccessLayer:
    return ModelAccessLayer(auth_settings(DUMMY_DSN, model_adapter="stub"))


# -- #275: model output ---------------------------------------------------------------------


def test_without_nul_strips_keys_and_strings_at_any_depth() -> None:
    document = {"a\x00": ["x\x00y", {"k": "\x00"}], "n": 1, "t": ("\x00z",), "none": None}
    assert without_nul(document) == {"a": ["xy", {"k": ""}], "n": 1, "t": ["z"], "none": None}


async def test_a_completion_leaves_the_access_layer_without_nul() -> None:
    """``send`` (so ``complete`` and every structured attempt) removes NUL from the text; an
    answer of only NUL is then empty, as one of only whitespace is. Complement asserted: text
    without NUL is returned unchanged."""
    mal = stub_models()
    mal.stub.script("generation", ["Ada\x00 waves.\x00", "plain", "\x00\x00"])
    assert (await mal.complete("generation", HELLO)).text == "Ada waves."
    assert (await mal.complete("generation", HELLO)).text == "plain"
    with pytest.raises(EmptyCompletion):
        await mal.complete("generation", HELLO)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def nul_logs() -> Iterator[tuple[io.StringIO, _Records]]:
    """What ``epistrel serve`` would write (Epistrel's JSON handler at DEBUG, on a buffer) and
    every record of any logger; the root logger restored after."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    stream = io.StringIO()
    handler = telemetry_logging.configure_logging(level="DEBUG", stream=stream)
    records = _Records()
    root.addHandler(records)
    try:
        yield stream, records
    finally:
        root.removeHandler(records)
        root.removeHandler(handler)
        root.handlers[:] = handlers
        root.setLevel(level)


async def test_the_nul_warning_logs_only_purpose_and_count(
    nul_logs: tuple[io.StringIO, _Records],
) -> None:
    """#275 AC3 (invariant 3): stripping NUL from a model's answer logs one warning that carries
    the purpose and the count of removed characters and nothing else. Neither the answer's text
    around the NUL nor any piece of it appears in any record or in the JSON line. Complement
    asserted: an answer without NUL logs no such warning."""
    stream, captured = nul_logs
    before, after = "Marisol hid the brass lantern", "under the third quay stone"
    mal = stub_models()
    mal.stub.script("generation", [f"{before}\x00 {after}.\x00", f"{before} {after}."])
    assert (await mal.complete("generation", HELLO)).text == f"{before} {after}."
    warnings = [r for r in captured.records if r.name == ACCESS_LOGGER and r.levelno >= WARNING]
    (warning,) = warnings
    assert warning.msg == "model answer held NUL characters purpose=%s removed=%d"
    assert warning.args == ("generation", 2)
    (line,) = [
        json.loads(raw)
        for raw in stream.getvalue().splitlines()
        if json.loads(raw).get("logger") == ACCESS_LOGGER and "NUL" in raw
    ]
    assert line["event"] == "model answer held NUL characters purpose=generation removed=2"
    pieces = [*before.split(), *after.split(), before, after]
    logged = [stream.getvalue(), *(_record_text(r) for r in captured.records)]
    leaked = [p for p in pieces if len(p) > 3 and any(p in text for text in logged)]
    assert leaked == []

    captured.records.clear()
    assert (await mal.complete("generation", HELLO)).text == f"{before} {after}."
    assert not [r for r in captured.records if r.name == ACCESS_LOGGER and "NUL" in r.msg]


def _record_text(record: logging.LogRecord) -> str:
    """Everything a record carries: its formatted message and every attribute's value."""
    return record.getMessage() + " " + " ".join(repr(v) for v in vars(record).values())


def test_a_structured_answer_loses_an_escaped_nul() -> None:
    """A JSON ``\\u0000`` escape decodes to NUL after the raw text was cleaned: ``parse`` removes
    it from the decoded keys and strings before validating. Complement asserted: the same
    answer without the escape parses to the same object."""
    escaped = parse('{"text": "a\\u0000b", "tags": ["\\u0000x"]}', Said)
    assert escaped == Said(text="ab", tags=["x"])
    assert parse('{"text": "ab", "tags": ["x"]}', Said) == escaped


async def test_a_structured_call_validates_the_cleaned_answer() -> None:
    mal = stub_models()
    mal.stub.script("guardrail_check", ['{"violation": false, "confidence": 1\x00}'])

    async def settings() -> StructuredSettings:
        return StructuredSettings()

    mal.structured_settings = settings  # type: ignore[method-assign]
    result = await mal.structured_call(
        "guardrail_check",
        GuardrailVerdict,
        GuardrailCheckInputs(text="t", policy_prompt="p"),
        deterministic_first=lambda: None,
        budget=TurnBudget(),
    )
    assert result == GuardrailVerdict(violation=False, confidence=1.0)
    assert len(mal.stub.calls) == 1  # the raw NUL was removed before the first parse


# -- #278: Engine reads ---------------------------------------------------------------------


def get_operations() -> set[str]:
    """The Engine operations a ``GET`` route serves."""
    names = operations()
    found = set()
    for operation_id, served in route_operations(full_app(DUMMY_DSN)).items():
        if served.startswith("GET "):
            method = method_of(operation_id, names)
            assert method is not None, operation_id
            found.add(method)
    return found


def test_every_read_refuses_nul_before_it_runs() -> None:
    """Every operation a ``GET`` route serves, and every library-only read, is ``refuses_nul``;
    no mutation is (``_keyed`` checks those under their own fields). Complement asserted: the
    library-only reads named here are library-only methods."""
    reads = get_operations()
    assert set(LIBRARY_READS) <= set(library_only_methods())
    assert set(nul_checked_methods()) == reads | set(LIBRARY_READS)


def _string_parameters(name: str) -> list[inspect.Parameter]:
    own = nul_checked_methods()[name]
    return [
        parameter
        for parameter in inspect.signature(getattr(Engine, name)).parameters.values()
        if parameter.name not in {"self", "ctx", *own}
        and re.search(r"\bstr\b", str(parameter.annotation))
    ]


def _many(parameter: inspect.Parameter) -> bool:
    return str(parameter.annotation).startswith(("Sequence[", "Mapping["))


def _call_values(name: str, nul: inspect.Parameter, text: str) -> tuple[list[Any], dict[str, Any]]:
    """Arguments for ``name``: ``text`` for ``nul`` (in a list for a sequence), a plain string
    for every other required one."""
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for parameter in list(inspect.signature(getattr(Engine, name)).parameters.values())[2:]:
        if parameter.name == nul.name:
            value: Any = [text] if _many(parameter) else text
        elif parameter.default is inspect.Parameter.empty:
            value = "x"
        else:
            continue
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[parameter.name] = value
        else:
            args.append(value)
    return args, kwargs


CASES = [
    (name, parameter)
    for name in sorted(nul_checked_methods())
    for parameter in _string_parameters(name)
]


def test_the_string_cases_cover_the_issue_examples() -> None:
    covered = {(name, parameter.name) for name, parameter in CASES}
    assert {
        ("get_story", "story_id"),
        ("history", "actor_character_id"),
        ("get_operation", "operation_id"),
    } <= covered


@pytest.mark.parametrize(("name", "parameter"), CASES, ids=[f"{n}.{p.name}" for n, p in CASES])
async def test_a_read_refuses_nul_in_each_string_argument(
    name: str, parameter: inspect.Parameter
) -> None:
    """422 ``validation_error`` (``string_nul``) at the argument before the method body runs
    (``self`` is a bare object, so the body cannot have run). Complement asserted: the same call
    without the NUL is not refused for one."""
    method = getattr(Engine, name)
    ctx = RequestContext(principal="p-reader")
    args, kwargs = _call_values(name, parameter, "a\x00b")
    with pytest.raises(InvalidRequest) as refused:
        await method(object(), ctx, *args, **kwargs)
    keyword = parameter.kind is inspect.Parameter.KEYWORD_ONLY
    loc: tuple[int | str, ...] = ("query", parameter.name) if keyword else (parameter.name,)
    assert refused.value.details["field"] == field_path((*loc, 0) if _many(parameter) else loc)
    assert [e["type"] for e in refused.value.details["errors"]] == ["string_nul"]

    args, kwargs = _call_values(name, parameter, "ab")
    assert "string_nul" not in await _refusal_types(method(object(), ctx, *args, **kwargs))


async def _refusal_types(call: Awaitable[Any]) -> list[str]:
    """The error types ``call`` was refused with: none when it ran (on a bare ``self`` it then
    fails some other way)."""
    try:
        await call
    except InvalidRequest as refused:
        return [str(e["type"]) for e in refused.details.get("errors", [])]
    except Exception:  # noqa: S110 - the body ran on a bare object: anything but a refusal
        pass
    return []


GATED = [
    (name, parameter)
    for name in operations() + sorted(library_only_methods())
    if name not in nul_checked_methods()
    for parameter in nul_guarded_parameters(getattr(Engine, name))
]
"""Mutations whose story or catalog id the erasure and catalog gates look up before ``_keyed``
refuses a NUL in it (#278: the gate's lookup failed with a database error first)."""


def test_the_gated_mutations_include_a_story_and_a_catalog_write() -> None:
    names = {(name, parameter.name) for name, parameter in GATED}
    assert {("set_pinned_memories", "story_id"), ("save_character_draft", "character_id")} <= names


@pytest.mark.parametrize(("name", "parameter"), GATED, ids=[f"{n}.{p.name}" for n, p in GATED])
async def test_a_gated_mutation_refuses_nul_in_its_id_before_the_gate(
    name: str, parameter: inspect.Parameter
) -> None:
    """422 at the id before the gate (or anything else) runs on a bare ``self``."""
    args, kwargs = _call_values(name, parameter, "a\x00b")
    with pytest.raises(InvalidRequest) as refused:
        await getattr(Engine, name)(object(), RequestContext(principal="p-w"), *args, **kwargs)
    keyword = parameter.kind is inspect.Parameter.KEYWORD_ONLY
    loc: tuple[int | str, ...] = ("query", parameter.name) if keyword else (parameter.name,)
    assert refused.value.details["field"] == field_path(loc)
    assert [e["type"] for e in refused.value.details["errors"]] == ["string_nul"]
