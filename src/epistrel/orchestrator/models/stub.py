"""The deterministic stub adapter (FR-MODEL-11).

With nothing scripted, the stub answers ``[stub <purpose> <sha256[:12]>] <first 40 characters
of the last user message>``, where the hash covers the purpose, the messages and the params:
the same request yields byte-identical output in every process. With a ``response_schema`` it
answers a schema-conformant JSON object instead (``{"violation": false, "confidence": 1.0, ...}``
for the guardrail verdict).

Tests drive it through the Model Access Layer's ``stub`` (the ``stub`` fixture):
``script(purpose, [responses])`` queues answers (FIFO; exhausted → the default),
``when(substring, response)`` answers any request whose messages contain ``substring``,
``fault(purpose, kind, n)`` makes the next ``n`` calls fail (``timeout`` and ``http_500`` raise
the ``openai`` SDK's own exception types; ``garbage`` returns unparseable text), ``hold(purpose)``
latches calls until released, and ``calls`` records every request (purpose, messages, params,
declared field paths, model). ``nondeterministic = True`` salts the hash per call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import types
import typing
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal

import httpx2
import openai
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from epistrel.orchestrator.models.adapter import (
    REQUEST_FIELDS,
    Message,
    ModelParams,
    ModelResult,
)

STUB_URL = "http://stub.invalid/v1/chat/completions"
STUB_MODEL = "stub"
GARBAGE = '<<stub garbage>> {"violation": tru'
DEFAULT_PREVIEW_CHARS = 40

type FaultKind = Literal["timeout", "http_500", "garbage"]
type ModelLookup = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class StubCall:
    """One request the stub received. ``fields`` are declared input paths, never values."""

    purpose: str
    messages: tuple[Message, ...]
    params: ModelParams
    fields: tuple[str, ...] | None
    model: str
    response_schema: str | None


@dataclass
class StubLatch:
    """Holds a purpose's calls until ``release()``; ``entered`` is set when one arrives."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    def release(self) -> None:
        self.released.set()


def canonical_digest(purpose: str, messages: Sequence[Message], params: ModelParams) -> str:
    """SHA-256 over canonical JSON of the request: the same in every process."""
    body = {
        "purpose": purpose,
        "messages": [m.model_dump() for m in messages],
        "params": params.wire(),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _bound(metadata: list[Any], name: str, fallback: float) -> float:
    """The ``ge``/``le`` bound a field declares (pydantic keeps them as metadata objects)."""
    for item in metadata:
        value = getattr(item, name, None)
        if isinstance(value, int | float):
            return float(value)
    return fallback


def _conformant(annotation: Any, metadata: list[Any]) -> Any:
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        inner, *extra = typing.get_args(annotation)
        return _conformant(inner, [*metadata, *extra])
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return conformant_example(annotation)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return next(iter(annotation)).value
    if origin is Literal:
        return typing.get_args(annotation)[0]
    if origin in (list, tuple, set, frozenset):
        return []
    if origin is dict:
        return {}
    if origin is types.UnionType or origin is typing.Union:
        if type(None) in typing.get_args(annotation):
            return None
        return _conformant(typing.get_args(annotation)[0], metadata)
    if annotation is bool:
        return False
    if annotation is int:
        return int(_bound(metadata, "ge", 0.0))
    if annotation is float:
        upper = _bound(metadata, "le", 1.0)
        return min(1.0, upper)
    return ""


def _field_value(info: FieldInfo) -> Any:
    if not info.is_required():
        return info.get_default(call_default_factory=True)
    return _conformant(info.annotation, list(info.metadata))


def conformant_example(schema: type[BaseModel]) -> dict[str, Any]:
    """The stub's answer for ``schema``: defaults where declared, else the most conservative
    value of each type (``False``, ``0``, ``1.0`` capped by the field's bound, empty)."""
    values = {name: _field_value(info) for name, info in schema.model_fields.items()}
    return schema.model_validate(values).model_dump(mode="json")


def _fault_error(kind: FaultKind) -> Exception | None:
    request = httpx2.Request("POST", STUB_URL)
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    if kind == "http_500":
        response = httpx2.Response(500, request=request)
        return openai.InternalServerError("stub fault: http_500", response=response, body=None)
    return None


class StubAdapter:
    """Deterministic answers, scripted responses, and fault injection for tests."""

    name = "stub"

    def __init__(self, *, model_lookup: ModelLookup | None = None, latency_s: float = 0.0) -> None:
        self._model_lookup = model_lookup
        self.latency_s = latency_s
        self.nondeterministic = False
        self.calls: list[StubCall] = []
        self._scripts: defaultdict[str, deque[str]] = defaultdict(deque)
        self._faults: defaultdict[str, deque[FaultKind]] = defaultdict(deque)
        self._when: list[tuple[str, str]] = []
        self._latches: dict[str, StubLatch] = {}

    # -- test controls --------------------------------------------------------------------

    def script(self, purpose: str, responses: Iterable[str]) -> None:
        self._scripts[purpose].extend(responses)

    def when(self, substring: str, response: str) -> None:
        self._when.append((substring, response))

    def fault(self, purpose: str, kind: FaultKind = "timeout", n: int = 1) -> None:
        self._faults[purpose].extend([kind] * n)

    def hold(self, purpose: str) -> StubLatch:
        latch = StubLatch()
        self._latches[purpose] = latch
        return latch

    def reset(self) -> None:
        """Forget scripts, faults, latches, matches and recorded calls."""
        self.calls.clear()
        self._scripts.clear()
        self._faults.clear()
        self._when.clear()
        self._latches.clear()
        self.nondeterministic = False

    # -- the adapter ----------------------------------------------------------------------

    async def _model(self, purpose: str) -> str:
        if self._model_lookup is None:
            return STUB_MODEL
        return await self._model_lookup(purpose) or STUB_MODEL

    def _default_text(self, purpose: str, digest: str, messages: Sequence[Message]) -> str:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return f"[stub {purpose} {digest[:12]}] {last_user[:DEFAULT_PREVIEW_CHARS]}"

    def _answer(
        self,
        purpose: str,
        messages: Sequence[Message],
        digest: str,
        response_schema: type[BaseModel] | None,
    ) -> str:
        if self._scripts[purpose]:
            return self._scripts[purpose].popleft()
        for substring, response in self._when:
            if any(substring in m.content for m in messages):
                return response
        if response_schema is not None:
            example = conformant_example(response_schema)
            return json.dumps(example, sort_keys=True, separators=(",", ":"))
        return self._default_text(purpose, digest, messages)

    async def complete(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResult:
        model = await self._model(purpose)
        self.calls.append(
            StubCall(
                purpose=purpose,
                messages=tuple(messages),
                params=params,
                fields=REQUEST_FIELDS.get(),
                model=model,
                response_schema=response_schema.__name__ if response_schema else None,
            )
        )
        latch = self._latches.get(purpose)
        if latch is not None:
            latch.entered.set()
            await latch.released.wait()
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        digest = canonical_digest(purpose, messages, params)
        if self.nondeterministic:
            digest = hashlib.sha256(digest.encode() + os.urandom(16)).hexdigest()
        text: str | None = None
        if self._faults[purpose]:
            kind = self._faults[purpose].popleft()
            error = _fault_error(kind)
            if error is not None:
                raise error
            text = GARBAGE
        if text is None:
            text = self._answer(purpose, messages, digest, response_schema)
        return ModelResult(
            text=text,
            finish_reason="stop",
            model=model,
            latency_ms=1,
            tokens_in=sum(len(m.content) for m in messages) // 4,
            tokens_out=len(text) // 4,
            cost=0.0,
            request_id=f"stub-{digest[:24]}",
        )

    async def aclose(self) -> None:
        return None


__all__ = [
    "GARBAGE",
    "STUB_MODEL",
    "StubAdapter",
    "StubCall",
    "StubLatch",
    "canonical_digest",
    "conformant_example",
]
