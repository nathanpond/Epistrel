"""The Model Access Layer's error family and the one place provider failures are mapped
(FR-MODEL-6, NFR-REL-3).

Both adapters let the ``openai`` SDK's own exception types escape; ``ModelAccessLayer`` sends
every one through ``map_provider_error``, so a caller only ever sees a ``ModelError``:

- ``ModelUnavailable`` (503 ``model_unavailable``): no usable answer for the purpose.
  ``details``: ``purpose``, ``reason`` (see ``UnavailableReason``), and, when known,
  ``provider_status`` (the HTTP status the provider answered) and ``attempts`` (model calls
  made). A 429's ``Retry-After`` is passed through as a response header, never on other
  statuses. The provider's own message is never copied: it can echo the prompt, so the error
  names only the exception class's reason (invariant 3).
- ``EmptyCompletion`` (503 ``model_unavailable``, ``reason=empty``): an unstructured call
  answered nothing but whitespace. A caller may regenerate once (#57) before letting it out.
- ``ResponseFormatRejected``: the endpoint answered ``response_format`` with a 400; the
  structured caller falls back to parse-validate-retry. Uncaught it is an ordinary 503.
- ``BudgetExhausted``: a call the turn's budget cannot afford is not made (#69 owns the
  turn-level shape).

A turn that lets any of these escape fails as a whole: ``Operation`` rolls back, so no event is
written and the revision does not move.
"""

from __future__ import annotations

import json
import logging
from json import JSONDecodeError
from typing import Any, Literal

import openai

from epistrel.core.errors import EpistrelError

log = logging.getLogger(__name__)

type UnavailableReason = Literal[
    "not_configured",
    "no_api_key",
    "auth",
    "timeout",
    "connection",
    "http_error",
    "rate_limited",
    "garbage",
    "deadline",
    "empty",
]


class ModelError(EpistrelError):
    """Base of every Model Access Layer failure."""

    code = "model_error"
    http_status = 502


class ModelUnavailable(ModelError):
    """No usable model answer for the purpose. ``details``: ``purpose``, ``reason``, and when
    known ``provider_status`` and ``attempts``."""

    code = "model_unavailable"
    http_status = 503

    def __init__(
        self,
        purpose: str,
        reason: str,
        message: str | None = None,
        *,
        provider_status: int | None = None,
        attempts: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"purpose": purpose, "reason": reason}
        if provider_status is not None:
            details["provider_status"] = provider_status
        if attempts is not None:
            details["attempts"] = attempts
        super().__init__(
            message or f"no model is available for {purpose!r} ({reason})", details=details
        )
        self.purpose = purpose
        self.reason = reason
        self.retry_after = retry_after if reason == "rate_limited" else None

    def with_attempts(self, attempts: int) -> ModelUnavailable:
        """This error with ``details.attempts`` set (the structured caller counts its calls)."""
        self.details["attempts"] = attempts
        return self

    def response_headers(self) -> dict[str, str]:
        return {"Retry-After": self.retry_after} if self.retry_after else {}


class ResponseFormatRejected(ModelUnavailable):
    """The endpoint refused ``response_format: json_schema`` (a 400 whose body names it)."""

    def __init__(self, purpose: str) -> None:
        super().__init__(
            purpose,
            "http_error",
            f"the endpoint for {purpose!r} does not accept response_format",
            provider_status=400,
        )


class EmptyCompletion(ModelError):
    """An unstructured call came back empty or whitespace-only (``reason=empty``)."""

    code = "model_unavailable"
    http_status = 503

    def __init__(self, purpose: str) -> None:
        super().__init__(
            f"the model for {purpose!r} returned an empty completion",
            details={"purpose": purpose, "reason": "empty"},
        )
        self.purpose = purpose


class BudgetExhausted(ModelError):
    """The turn's budget cannot afford another model call; the call was not made."""

    code = "budget_exhausted"
    http_status = 503

    def __init__(self, purpose: str, used: int, max_calls: int | None) -> None:
        super().__init__(
            f"the turn's model-call budget is spent ({used} of {max_calls})",
            details={"purpose": purpose, "used": used, "max_calls": max_calls},
        )


class ProviderGarbage(Exception):
    """The provider answered 200 with a body that is not a chat completion (raised by the
    ``openai`` adapter, mapped to ``reason=garbage``)."""


def _mentions_response_format(exc: openai.APIStatusError) -> bool:
    try:
        body = json.dumps(exc.body) if exc.body is not None else ""
    except TypeError, ValueError:
        body = ""
    return "response_format" in f"{exc.message} {body}"


def map_provider_error(purpose: str, exc: BaseException) -> ModelError | None:
    """The ``ModelError`` a provider failure means, or ``None`` when ``exc`` is not one
    (an engine bug, which must propagate as itself)."""
    if isinstance(exc, ModelError):
        return exc
    error: ModelError | None
    if isinstance(exc, openai.APITimeoutError):
        error = ModelUnavailable(purpose, "timeout")
    elif isinstance(exc, openai.APIConnectionError):
        error = ModelUnavailable(purpose, "connection")
    elif isinstance(exc, openai.RateLimitError):
        error = ModelUnavailable(
            purpose,
            "rate_limited",
            provider_status=exc.status_code,
            retry_after=exc.response.headers.get("retry-after"),
        )
    elif isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        error = ModelUnavailable(purpose, "auth", provider_status=exc.status_code)
    elif isinstance(exc, openai.BadRequestError) and _mentions_response_format(exc):
        error = ResponseFormatRejected(purpose)
    elif isinstance(exc, openai.APIStatusError):
        error = ModelUnavailable(purpose, "http_error", provider_status=exc.status_code)
    elif isinstance(exc, openai.APIResponseValidationError | ProviderGarbage | JSONDecodeError):
        # The SDK raises JSONDecodeError for a body that is not JSON at all.
        error = ModelUnavailable(purpose, "garbage")
    else:
        return None
    log.warning(
        "model call failed purpose=%s reason=%s status=%s error=%s",
        purpose,
        error.details.get("reason"),
        error.details.get("provider_status"),
        type(exc).__name__,
    )
    return error


__all__ = [
    "BudgetExhausted",
    "EmptyCompletion",
    "ModelError",
    "ModelUnavailable",
    "ProviderGarbage",
    "ResponseFormatRejected",
    "UnavailableReason",
    "map_provider_error",
]
