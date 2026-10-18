"""The deterministic stub adapter (FR-MODEL-11).

With nothing scripted, the stub answers ``[stub <purpose> <sha256[:12]>] <first 40 characters
of the last user message>``, where the hash covers the purpose, the messages and the params:
the same request yields byte-identical output in every process. With a ``response_schema`` it
answers a schema-conformant JSON object instead (``{"confidence": 1.0, "violation": false}``
for the guardrail verdict).

Tests drive it through the Model Access Layer's ``stub`` (the ``stub`` fixture):
``script(purpose, [responses])`` queues answers (FIFO; exhausted → the default),
``when(substring, response, purpose=None)`` answers any request (of ``purpose``, when given)
whose messages contain ``substring`` (or every one of a tuple of substrings),
``case(case_id, response, purpose=None, seed=None)`` answers the requests an eval scope marks
as case ``case_id`` (their ``X-Eval-Case`` line, #134; only ``purpose``'s, and only under
``seed``, when given; the most specific match wins, before any queued script),
``fault(purpose, kind, n)`` makes the next ``n`` calls fail: ``timeout``, ``connection``,
``http_500``, ``http_429`` (with ``Retry-After``), ``http_401``, ``response_format_400`` and
``bad_body`` raise the ``openai`` SDK's own exception types; ``garbage`` answers unparseable
text, ``empty`` answers nothing, ``refusal`` and ``length`` answer with the provider's refusal
flag or a ``length`` finish; ``hang`` never answers (the Model Access Layer's timeout ends it).
``parrot(pattern, purpose=None, template="{canary}")`` (#145's poisoning-loop tests) answers a
request whose messages hold a match of the regular expression ``pattern`` (a canary planted in
the story's records) with ``template`` filled with the first match, as a model that repeats
what its context says would; a request with no match falls through to the default.
``fault_hook`` (a callable of the purpose) is consulted on every call with no queued fault,
so a chaos test can draw faults at random. ``hold(purpose)`` latches calls until released, and
``calls`` records every request (purpose, messages, params, declared field paths, model).
``nondeterministic = True`` salts the hash per call.

A purpose that declares a response schema (``guardrail_check``) answers schema-conformant JSON
by default whether or not the request carried ``response_format``, as a model following the
schema in its prompt would. ``importance_scoring`` (#135) answers ``{"importance": x}`` with
``x`` derived from the SHA-256 of the last user message (``digest_score``), so each memory gets
its own score, the same in every process. ``entailment`` (#142) answers its schema's defaults,
``{"confidence": 0.0, "verdict": "novel"}``: the most conservative answer, which the verifier
resolves to the safe disposition, so an unscripted escalation never supports a claim.
``salience_detection`` (#146) answers its schema's defaults too, ``{"salient": [], "texture":
[]}``: every beat is texture, so an unscripted turn owes nothing.
``extraction_backstop`` (#148) answers ``{"claims": []}``: the deterministic layers stand
alone. A response schema with a ``stub_example(digest, messages)`` classmethod answers that
(#150: a structured render's default is one ``think``, one ``say`` with intent ``sincere`` and
one ``do``, their texts from the request's hash; the age repair answers 40 for each character
it names).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
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
    PROBE_MESSAGES,
    REQUEST_FIELDS,
    Message,
    ModelParams,
    ModelResult,
    ProbeResult,
    eval_case_of,
)
from epistrel.orchestrator.models.registry import DELIVERED, IMPORTANCE_SCORING

STUB_URL = "http://stub.invalid/v1/chat/completions"
STUB_MODEL = "stub"
GARBAGE = '<<stub garbage>> {"violation": tru'
DEFAULT_PREVIEW_CHARS = 40
STUB_EXAMPLE = "stub_example"
"""A response schema's classmethod ``stub_example(digest, messages)``: its default answer
(#150), instead of ``conformant_example``."""

type FaultKind = Literal[
    "timeout",
    "connection",
    "http_500",
    "http_429",
    "http_401",
    "response_format_400",
    "bad_body",
    "garbage",
    "empty",
    "refusal",
    "length",
    "hang",
]
FAULT_KINDS: tuple[FaultKind, ...] = typing.get_args(FaultKind.__value__)
RETRY_AFTER = "2"
"""The ``Retry-After`` the stub's ``http_429`` fault sends."""
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


def digest_score(text: str) -> float:
    """A number in 0..1 from the SHA-256 of ``text`` (four decimals): the stub's importance."""
    value = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return round(value / 0xFFFFFFFF, 4)


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
    """The SDK exception a transport fault raises (``None`` for faults that answer)."""
    request = httpx2.Request("POST", STUB_URL)
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    if kind == "connection":
        return openai.APIConnectionError(request=request)
    if kind == "http_500":
        response = httpx2.Response(500, request=request)
        return openai.InternalServerError("stub fault: http_500", response=response, body=None)
    if kind == "http_429":
        response = httpx2.Response(429, request=request, headers={"Retry-After": RETRY_AFTER})
        return openai.RateLimitError("stub fault: http_429", response=response, body=None)
    if kind == "http_401":
        response = httpx2.Response(401, request=request)
        return openai.AuthenticationError("stub fault: http_401", response=response, body=None)
    if kind == "response_format_400":
        response = httpx2.Response(400, request=request)
        body = {"error": {"message": "response_format json_schema is not supported"}}
        return openai.BadRequestError(
            "stub fault: response_format is not supported", response=response, body=body
        )
    if kind == "bad_body":
        response = httpx2.Response(200, request=request)
        return openai.APIResponseValidationError(response=response, body=None)
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
        self._when: list[tuple[tuple[str, ...], str, str | None]] = []
        self._cases: dict[tuple[str, str | None, int | None], str] = {}
        self._latches: dict[str, StubLatch] = {}
        self._parrots: list[tuple[re.Pattern[str], str, str | None]] = []
        self.fault_hook: Callable[[str], FaultKind | None] | None = None
        """Consulted on every call with no queued fault (chaos tests draw faults at random)."""

    # -- test controls --------------------------------------------------------------------

    def script(self, purpose: str, responses: Iterable[str]) -> None:
        self._scripts[purpose].extend(responses)

    def when(
        self, substring: str | tuple[str, ...], response: str, purpose: str | None = None
    ) -> None:
        """Answer ``response`` to requests whose messages contain ``substring`` (each of them,
        for a tuple; only ``purpose``'s requests, when given). The first match wins."""
        needles = (substring,) if isinstance(substring, str) else substring
        self._when.append((needles, response, purpose))

    def case(
        self,
        case_id: str,
        response: str,
        *,
        purpose: str | None = None,
        seed: int | None = None,
    ) -> None:
        """Answer ``response`` to the requests marked as eval case ``case_id`` (see the module
        docstring); a later call for the same case, purpose and seed replaces the answer."""
        self._cases[(case_id, purpose, seed)] = response

    def _case_answer(
        self, purpose: str, messages: Sequence[Message], params: ModelParams
    ) -> str | None:
        case_id = eval_case_of(messages)
        if case_id is None or not self._cases:
            return None
        for key in (
            (case_id, purpose, params.seed),
            (case_id, None, params.seed),
            (case_id, purpose, None),
            (case_id, None, None),
        ):
            if key in self._cases:
                return self._cases[key]
        return None

    def parrot(
        self, pattern: str, *, purpose: str | None = None, template: str = "{canary}"
    ) -> None:
        """Echo a canary (see the module docstring): requests (of ``purpose``, when given) whose
        messages match ``pattern`` are answered ``template.format(canary=<first match>)``."""
        self._parrots.append((re.compile(pattern), template, purpose))

    def _parroted(self, purpose: str, messages: Sequence[Message]) -> str | None:
        for pattern, template, only in self._parrots:
            if only not in (None, purpose):
                continue
            for message in messages:
                found = pattern.search(message.content)
                if found is not None:
                    return template.format(canary=found.group(0))
        return None

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
        self._cases.clear()
        self._latches.clear()
        self._parrots.clear()
        self.fault_hook = None
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
        for needles, response, only in self._when:
            if only not in (None, purpose):
                continue
            if all(any(n in m.content for m in messages) for n in needles):
                return response
        parroted = self._parroted(purpose, messages)
        if parroted is not None:
            return parroted
        if purpose == IMPORTANCE_SCORING.name and response_schema in (
            None,
            IMPORTANCE_SCORING.response_schema,
        ):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            return json.dumps({"importance": digest_score(last_user)})
        declared = DELIVERED.get(purpose)
        schema = response_schema or (declared.response_schema if declared else None)
        if schema is not None:
            # A schema may name its own default answer (#150's beat schema: a think, a say and a
            # do from the prompt's hash).
            hook = getattr(schema, STUB_EXAMPLE, None)
            example = hook(digest, messages) if callable(hook) else conformant_example(schema)
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
        finish_reason, refused = "stop", False
        kind = self._faults[purpose].popleft() if self._faults[purpose] else None
        if kind is None and self.fault_hook is not None:
            kind = self.fault_hook(purpose)
        if kind is not None:
            error = _fault_error(kind)
            if error is not None:
                raise error
            if kind == "hang":
                await asyncio.Event().wait()
            text = {"garbage": GARBAGE, "empty": "  \n", "refusal": ""}.get(kind)
            refused = kind == "refusal"
            if kind == "length":
                finish_reason = "length"
        if text is None:
            text = self._case_answer(purpose, messages, params)
        if text is None:
            text = self._answer(purpose, messages, digest, response_schema)
            if finish_reason == "length":
                text = text[: len(text) // 2]
        return ModelResult(
            text=text,
            finish_reason=finish_reason,
            refused=refused,
            model=model,
            latency_ms=1,
            tokens_in=sum(len(m.content) for m in messages) // 4,
            tokens_out=len(text) // 4,
            cost=0.0,
            request_id=f"stub-{digest[:24]}",
        )

    async def probe(self, purpose: str) -> ProbeResult:
        """The stub speaks the protocol: it lists its configured model and completes once
        (a queued fault for ``purpose`` applies to that completion)."""
        result = await self.complete(purpose, PROBE_MESSAGES, ModelParams(max_tokens=16))
        return ProbeResult(models=(result.model,), model=result.model)

    async def aclose(self) -> None:
        return None


__all__ = [
    "FAULT_KINDS",
    "GARBAGE",
    "RETRY_AFTER",
    "STUB_EXAMPLE",
    "STUB_MODEL",
    "StubAdapter",
    "StubCall",
    "StubLatch",
    "canonical_digest",
    "conformant_example",
    "digest_score",
]
