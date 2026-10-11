"""The per-call context every Engine operation receives (REST builds it from headers).

The requesting principal is opaque (FR-AUTH-4): Epistrel stores it on the operation and its
events for attribution and never validates, resolves, or interprets it.
"""

from __future__ import annotations

from dataclasses import dataclass

SYSTEM_PRINCIPAL = "system"
"""The principal background workers record (FR-AUTH-4). No REST caller may claim it."""

MAX_PRINCIPAL_LENGTH = 256


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who asked, and the request-scoped options shared by every operation.

    - ``principal``: the opaque requesting principal; required for every mutation.
    - ``idempotency_key``: the caller's key, or ``None`` (the server generates one, #61).
    - ``expected_revision``: the timeline revision the caller expects (#47), or ``None``.
    - ``viewpoint``: the requested output viewpoint (#59), or ``None`` for the default.
    """

    principal: str | None = None
    idempotency_key: str | None = None
    expected_revision: int | None = None
    viewpoint: str | None = None


def principal_problem(value: str) -> str | None:
    """Why ``value`` (already stripped) is not an acceptable principal, or ``None`` if it is."""
    if not value:
        return "must not be empty"
    if len(value) > MAX_PRINCIPAL_LENGTH:
        return f"must be at most {MAX_PRINCIPAL_LENGTH} characters"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "must not contain control characters"
    return None
