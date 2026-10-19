"""The adapter contract every model transport implements (FR-MODEL-1,11, NFR-PORT-2).

``ModelAdapter.complete(purpose, messages, params, response_schema=None)`` sends one chat
completion for a purpose and returns a ``ModelResult``. Two implementations ship:
``OpenAICompatibleAdapter`` (the ``openai`` SDK against any OpenAI-compatible endpoint) and
``StubAdapter`` (deterministic, for tests). ``EPISTREL_MODEL_ADAPTER`` picks one; nothing in
``epistrel.core`` knows which.

Transport failures surface as the SDK's own exception types from both adapters;
``ModelAccessLayer`` maps every one to a ``ModelError`` (``errors.map_provider_error``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

from epistrel.core.errors import InvalidRequest
from epistrel.orchestrator.models.errors import ModelError, ModelUnavailable

DEFAULT_TIMEOUT_S = 60.0
"""Per-call timeout when neither the purpose's config nor the call sets one."""

MAX_COMPLETION_TOKENS = 4_000
MAX_SEED = 2**31 - 1

EVAL_CASE_HEADER = "X-Eval-Case"
"""The line the Model Access Layer appends to a call's system message inside an eval scope
(``access.eval_scope``, #134): ``X-Eval-Case: <case id>``. It names an eval case, never story
content, so the stub can answer per case (``StubAdapter.case``)."""
EVAL_CASE_PATTERN = re.compile(rf"^{EVAL_CASE_HEADER}: (\S+)$", re.MULTILINE)


class Message(BaseModel):
    """One chat message sent to a model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class ModelParams(BaseModel):
    """Generation parameters; ``None`` inherits (call → purpose row → ``default`` row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: StrictFloat | None = Field(default=None, ge=0.0, le=2.0)
    top_p: StrictFloat | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: StrictInt | None = Field(default=None, ge=1, le=MAX_COMPLETION_TOKENS)
    timeout: StrictFloat | None = Field(default=None, gt=0.0, le=600.0)
    stop: tuple[str, ...] | None = Field(default=None, max_length=4)
    seed: StrictInt | None = Field(default=None, ge=0, le=MAX_SEED)
    """The provider's sampling seed (OpenAI ``seed``), for reproducible runs; the eval harness
    sets one per run seed (#134). Providers treat it as best effort."""

    def merged(self, override: ModelParams | None) -> ModelParams:
        """These params with every key ``override`` sets replacing ours."""
        if override is None:
            return self
        return self.model_copy(update=override.model_dump(exclude_none=True))

    def wire(self) -> dict[str, Any]:
        """The set keys, JSON-ready (``stop`` as a list)."""
        values = self.model_dump(exclude_none=True)
        if "stop" in values:
            values["stop"] = list(values["stop"])
        return values


class ModelResult(BaseModel):
    """What one completion returned, plus what it cost (FR-MODEL-12 counts these)."""

    model_config = ConfigDict(frozen=True)

    text: str
    finish_reason: str | None
    model: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    request_id: str | None = None
    refused: bool = False
    """The provider set the message's ``refusal`` instead of answering (structured calls treat
    it as unparseable)."""


class UnknownPurpose(InvalidRequest):
    """A call named a purpose that is not delivered. ``details.reason``: ``unknown`` or
    ``reserved`` (with ``milestone``)."""


class UndeclaredInput(InvalidRequest):
    """A payload carried a field its purpose does not declare (FR-MODEL-13)."""


class ProbeResult(BaseModel):
    """A protocol probe's answer: the endpoint's model list and the model that completed."""

    model_config = ConfigDict(frozen=True)

    models: tuple[str, ...]
    model: str


PROBE_MESSAGES = (Message(role="user", content="Reply with the single word: ready"),)
"""The protocol probe's one completion: no story content, nothing to validate."""
PROBE_TIMEOUT_S = 30.0


REQUEST_FIELDS: ContextVar[tuple[str, ...] | None] = ContextVar("REQUEST_FIELDS", default=None)
"""The declared input field paths of the call in progress, set by ``ModelAccessLayer.call`` so
the stub can record them; never holds values."""


def eval_case_of(messages: Sequence[Message]) -> str | None:
    """The eval case a request names (its ``X-Eval-Case`` line), or ``None``."""
    for message in messages:
        found = EVAL_CASE_PATTERN.search(message.content)
        if found is not None:
            return found.group(1)
    return None


@runtime_checkable
class ModelAdapter(Protocol):
    """One model transport."""

    name: str

    async def complete(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResult: ...

    async def probe(self, purpose: str) -> ProbeResult:
        """Protocol compatibility for ``purpose``'s endpoint: ``/models`` plus one completion."""
        ...

    async def aclose(self) -> None: ...


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "EVAL_CASE_HEADER",
    "MAX_SEED",
    "PROBE_MESSAGES",
    "PROBE_TIMEOUT_S",
    "REQUEST_FIELDS",
    "Message",
    "ModelAdapter",
    "ModelError",
    "ModelParams",
    "ModelResult",
    "ModelUnavailable",
    "ProbeResult",
    "UndeclaredInput",
    "UnknownPurpose",
    "eval_case_of",
]
