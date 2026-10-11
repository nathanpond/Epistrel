"""Per-purpose model-call accounting (FR-MODEL-12).

Every model call attempt the Model Access Layer makes is recorded once, by
``ModelAccessLayer.send``. Each attempt goes two places:

- **Prometheus metrics** in the default registry (#74 serves them at ``/metrics``):
  ``epistrel_model_calls_total{purpose,model,outcome}``,
  ``epistrel_model_tokens_total{purpose,kind}`` (``kind`` is ``input`` or ``output``, 0 when
  the endpoint reports no usage), ``epistrel_model_cost_total{purpose,model}`` (USD, only when
  the endpoint reports ``usage.cost``), and ``epistrel_model_latency_seconds{purpose}``.
- **A ``model_call`` span** (#74), a child of the current span, with purpose, model, attempt and
  outcome.
- **The call log of the operation in progress** (``recording()``): a list of ``ModelCall``
  entries the turn's response returns as ``model_calls``. #57's ``respond`` and #69's turn
  report read the same log.

``outcome`` is one of ``ok``, ``timeout`` (the call's timeout or the turn's deadline),
``http_error`` (any other transport or status failure), ``invalid_output`` (an answer that is
garbage, empty, or not the structured result asked for) and ``cancelled``. Label values are
enums, purpose names, and the configured model name reduced to ``[a-z0-9._-]``; never story
content (invariant 3).
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from prometheus_client import REGISTRY, Counter, Histogram
from pydantic import BaseModel, ConfigDict, Field

from epistrel.observability import tracing
from epistrel.orchestrator.models.errors import ModelError

Outcome = Literal["ok", "timeout", "http_error", "invalid_output", "cancelled"]
OUTCOMES: tuple[Outcome, ...] = ("ok", "timeout", "http_error", "invalid_output", "cancelled")
TOKEN_KINDS = ("input", "output")
MAX_LABEL_CHARS = 100
_LABEL_UNSAFE = re.compile(r"[^a-z0-9._-]+")
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0)

MODEL_CALLS = Counter(
    "epistrel_model_calls",
    "Model call attempts by purpose, configured model and outcome.",
    ("purpose", "model", "outcome"),
)
MODEL_TOKENS = Counter(
    "epistrel_model_tokens",
    "Tokens the endpoint reported, by purpose and kind (input or output).",
    ("purpose", "kind"),
)
MODEL_COST = Counter(
    "epistrel_model_cost",
    "Cost in USD the endpoint reported (usage.cost), by purpose and configured model.",
    ("purpose", "model"),
)
MODEL_LATENCY = Histogram(
    "epistrel_model_latency_seconds",
    "Wall-clock time of each model call attempt, by purpose.",
    ("purpose",),
    buckets=LATENCY_BUCKETS,
)

_UNAVAILABLE_OUTCOMES: dict[str, Outcome] = {
    "timeout": "timeout",
    "deadline": "timeout",
    "garbage": "invalid_output",
    "empty": "invalid_output",
}


class ModelCall(BaseModel):
    """One model call attempt as a turn's response reports it (``model_calls``)."""

    model_config = ConfigDict(frozen=True)

    purpose: str
    model: str
    attempt: int = Field(ge=1)
    outcome: Outcome
    latency_ms: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost: float | None = Field(default=None, exclude_if=lambda value: value is None)
    """USD, only when the endpoint reported it; absent from the response otherwise."""
    request_id: str | None = None
    finish_reason: str | None = None


class CallLog:
    """The model calls of one operation, in the order they were made."""

    def __init__(self) -> None:
        self.calls: list[ModelCall] = []
        self.first_started: float | None = None
        """When the operation's first model call started (``time.monotonic()``; #76)."""

    def by_purpose(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.calls:
            counts[call.purpose] = counts.get(call.purpose, 0) + 1
        return counts


CALL_LOG: ContextVar[CallLog | None] = ContextVar("CALL_LOG", default=None)


@contextmanager
def recording() -> Iterator[CallLog]:
    """Collect every model call made inside the block (in this task and the tasks it starts)."""
    log = CallLog()
    token = CALL_LOG.set(log)
    try:
        yield log
    finally:
        CALL_LOG.reset(token)


def call_started() -> None:
    """A model call is starting: the operation's log keeps the first such moment (#76's
    ``first_call_offset_ms``)."""
    log = CALL_LOG.get()
    if log is not None and log.first_started is None:
        log.first_started = time.monotonic()


def model_label(model: str | None) -> str:
    """``model`` as a metric label: lower case, ``[a-z0-9._-]`` only, at most 100 characters."""
    cleaned = _LABEL_UNSAFE.sub("_", (model or "").lower()).strip("_")[:MAX_LABEL_CHARS]
    return cleaned or "unknown"


def outcome_of(error: BaseException) -> Outcome:
    """The outcome a failed call attempt records."""
    if isinstance(error, ModelError):
        return _UNAVAILABLE_OUTCOMES.get(str(error.details.get("reason")), "http_error")
    if isinstance(error, TimeoutError):
        return "timeout"
    return "cancelled"


def record(call: ModelCall) -> None:
    """Count ``call`` in the metrics and append it to the operation's log, if one is open."""
    model = model_label(call.model)
    MODEL_CALLS.labels(call.purpose, model, call.outcome).inc()
    MODEL_TOKENS.labels(call.purpose, "input").inc(call.tokens_in)
    MODEL_TOKENS.labels(call.purpose, "output").inc(call.tokens_out)
    if call.cost is not None and call.cost >= 0:
        MODEL_COST.labels(call.purpose, model).inc(call.cost)
    MODEL_LATENCY.labels(call.purpose).observe(call.latency_ms / 1000)
    # A child span of whatever is current (the turn, the job): identifiers only (#74).
    tracing.finished_span(
        "model_call",
        call.latency_ms / 1000,
        {
            "epistrel.purpose": call.purpose,
            "epistrel.model": model,
            "epistrel.attempt": call.attempt,
            "epistrel.outcome": call.outcome,
        },
    )
    log = CALL_LOG.get()
    if log is not None:
        log.calls.append(call)


def sample(name: str, **labels: str) -> float:
    """The current value of one metric sample in the default registry (0 when unseen)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


__all__ = [
    "CALL_LOG",
    "MODEL_CALLS",
    "MODEL_COST",
    "MODEL_LATENCY",
    "MODEL_TOKENS",
    "OUTCOMES",
    "CallLog",
    "ModelCall",
    "Outcome",
    "call_started",
    "model_label",
    "outcome_of",
    "record",
    "recording",
    "sample",
]
