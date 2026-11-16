"""The eval harness's per-call trace hook (#737): what one model call sent and got back.

Only the eval harness turns it on (``evals.trace``): it sets ``CALL_TRACE`` to a sink for the
calls of its run, and every ``ModelAccessLayer.send`` made in that context hands the sink one
record per HTTP request the adapter made (``Exchange``). Outside an eval run the variable is
``None``: ``send`` and the adapter skip every step here, and nothing is kept. No setting turns
it on, so a deployed engine cannot (invariant 3: a trace carries prompts and replies, and is
eval-harness output, never engine logging).

``send`` opens an exchange list for its adapter call (``EXCHANGES``); the OpenAI-compatible
adapter appends one ``Exchange`` per request it sends (a retry with the legacy ``max_tokens``
or without sampling is a second request) with the request body as sent and the response or the
provider's error. ``send`` then adds what it knows (purpose, attempt, latency, the error it
raised) and passes each to the sink. The stub adapter sends nothing, so a stub run's calls leave
no record. The body never holds the API key: the SDK sends that as a header, which is never
read here.

``STRUCTURED_SCHEMA`` names the schema a structured call is for (``structured_messages`` sets
it), so a parse-validate-retry call, which sends no ``response_format``, still says which schema
it asked for. The endpoint's protocol probe (``probe``) does not go through ``send`` and is not
traced.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Exchange:
    """One HTTP request a model call made, and what came back (module docstring)."""

    body: dict[str, Any]
    """The Chat Completions request body as sent (no key: the key is a header)."""
    response: dict[str, Any] | None = None
    """``text``, ``finish_reason``, ``refusal``, ``tokens_in``/``tokens_out``,
    ``completion_tokens_details``, ``request_id``, ``reported_version``, ``model``."""
    error: dict[str, Any] | None = None
    """``type``, ``status``, ``body`` and ``request_id`` of the failure the request ended in."""
    latency_ms: int | None = None


class CallTraceSink(Protocol):
    """Where ``send`` hands a call's exchanges (``evals.trace.CallTrace``)."""

    def record(self, call: dict[str, Any], exchanges: list[Exchange]) -> None:
        """One model call: ``call`` (purpose, attempt, schema, latency, error) and the HTTP
        requests it made, in order."""


CALL_TRACE: ContextVar[CallTraceSink | None] = ContextVar("CALL_TRACE", default=None)
EXCHANGES: ContextVar[list[Exchange] | None] = ContextVar("EXCHANGES", default=None)
STRUCTURED_SCHEMA: ContextVar[str | None] = ContextVar("STRUCTURED_SCHEMA", default=None)


def exchange(body: dict[str, Any]) -> Exchange | None:
    """A new exchange for a request about to be sent, when ``send`` opened a list."""
    opened = EXCHANGES.get()
    if opened is None:
        return None
    found = Exchange(body=dict(body))
    opened.append(found)
    return found


def error_of(exc: BaseException) -> dict[str, Any]:
    """What a trace keeps of a failure: its type, HTTP status, the provider's error body and
    request id when the SDK exception carries them."""
    found: dict[str, Any] = {"type": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        found["status"] = status
    body = getattr(exc, "body", None)
    if body is not None:
        found["body"] = body
    request_id = getattr(exc, "request_id", None)
    if request_id:
        found["request_id"] = str(request_id)
    return found


__all__ = [
    "CALL_TRACE",
    "EXCHANGES",
    "STRUCTURED_SCHEMA",
    "CallTraceSink",
    "Exchange",
    "error_of",
    "exchange",
]
