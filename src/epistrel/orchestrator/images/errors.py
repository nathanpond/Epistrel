"""What an image adapter raises when a provider call fails (FR-IMG-1, NFR-SEC-4; #255).

Every error is an ``ImageAdapterError`` with two flags the job reads (#256, #268):

- ``retryable``: the job may try again within its budget;
- ``ambiguous``: the request may have reached the provider and been charged, so the send's
  outcome is unknown (a timeout after the body went out).

A message carries the error's code, the HTTP status and the provider's error code, and nothing
else: never the provider's own text, which can echo the prompt (NFR-SEC-4). Adapters raise
these ``from None``, so no chained transport exception carries the request either.
"""

from __future__ import annotations

import re

PROVIDER_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
"""What a provider's error code must look like to be repeated: a short token, never text."""


def provider_code_of(value: object) -> str | None:
    """``value`` as a provider error code worth repeating (a short token or an integer), or
    ``None`` (absent, free text, or anything else)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and PROVIDER_CODE_PATTERN.fullmatch(value):
        return value
    return None


class ImageAdapterError(Exception):
    """A failed provider call (module docs)."""

    code = "image_adapter_error"
    retryable = False
    ambiguous = False

    def __init__(
        self,
        *,
        status: int | None = None,
        provider_code: object = None,
        reason: str | None = None,
        ambiguous: bool | None = None,
    ) -> None:
        self.status = status
        self.provider_code = provider_code_of(provider_code)
        self.reason = reason
        if ambiguous is not None:
            self.ambiguous = ambiguous
        parts = [self.code]
        if reason is not None:
            parts.append(reason)
        if status is not None:
            parts.append(f"HTTP {status}")
        if self.provider_code is not None:
            parts.append(f"provider code {self.provider_code}")
        super().__init__(": ".join(parts))


class ImageRequestInvalid(ImageAdapterError):
    """The provider refused the request as it is (400, 404, 413, 422, other 4xx), or the
    request cannot be routed (no endpoints, an unresolvable reference). Not retried."""

    code = "image_request_invalid"


class ImageAdapterAuth(ImageAdapterError):
    """The provider refused the credential (401, 403). Not retried; the job fails naming it."""

    code = "image_adapter_auth"


class ImageAdapterCredits(ImageAdapterError):
    """The provider's account has no credit left (402). Not retried."""

    code = "image_adapter_credits"


class ImageProviderUnavailable(ImageAdapterError):
    """The provider is overloaded, rate-limiting or failing (409, 429, 5xx, 524, 529), or
    could not be reached. Retryable within the job's budget."""

    code = "image_provider_unavailable"
    retryable = True


class ImageProviderTimeout(ImageAdapterError):
    """No answer within the request timeout (or a 408). Retryable, and ambiguous: the request
    may have been generated and charged (#268)."""

    code = "image_provider_timeout"
    retryable = True
    ambiguous = True


class ImageProviderMalformed(ImageAdapterError):
    """The provider answered with something that is not a usable result: a redirect, malformed
    JSON, no image, undecodable bytes, a format outside PNG/JPEG/WebP, or a pixel bomb.
    Retryable."""

    code = "image_provider_malformed"
    retryable = True


__all__ = [
    "PROVIDER_CODE_PATTERN",
    "ImageAdapterAuth",
    "ImageAdapterCredits",
    "ImageAdapterError",
    "ImageProviderMalformed",
    "ImageProviderTimeout",
    "ImageProviderUnavailable",
    "ImageRequestInvalid",
    "provider_code_of",
]
