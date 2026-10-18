"""The per-call context every Engine operation receives (REST builds it from headers).

The requesting principal is opaque (FR-AUTH-4): Epistrel stores it on the operation and its
events for attribution and never validates, resolves, or interprets it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SYSTEM_PRINCIPAL = "system"
"""The principal background workers record (FR-AUTH-4). No REST caller may claim it."""

MAX_PRINCIPAL_LENGTH = 256

CONTROL_CHARACTERS = r"\x00-\x1f\x7f-\x9f"
"""Every control character (Unicode ``Cc``: C0, DEL and C1), as a regular expression range.
The one definition the identifier rules share: a principal, a relation filter's principal
(#321) and a listing status value (#110)."""

_CONTROL = re.compile(f"[{CONTROL_CHARACTERS}]")


def has_control(value: str) -> bool:
    """Whether ``value`` holds a control character (``CONTROL_CHARACTERS``)."""
    return _CONTROL.search(value) is not None


PRINCIPAL_PATTERN = rf"^[\t ]*[^{CONTROL_CHARACTERS}]{{1,256}}[\t ]*$"
"""A principal as sent: 1 to 256 characters without control characters (C0, DEL or C1; #321),
between optional spaces and tabs (which are trimmed). ``principal_problem`` is the full rule (it
also refuses a blank one); ``system`` is reserved besides."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who asked, and the request-scoped options shared by every operation.

    - ``principal``: the opaque requesting principal; required for every mutation.
    - ``idempotency_key``: the caller's key, or ``None`` (the server generates one, #61).
    - ``expected_revision``: the timeline revision the caller expects (#47), or ``None``.
    - ``viewpoint``: the requested output viewpoint (#59), or ``None`` for the default.
    - ``received_at``: when the request arrived (``time.monotonic()``, stamped by the REST
      app's first middleware); a turn's deadline and ``elapsed_ms`` start here (#69). ``None``
      (a library call): they start when the Engine method is entered.
    """

    principal: str | None = None
    idempotency_key: str | None = None
    expected_revision: int | None = None
    viewpoint: str | None = None
    received_at: float | None = None


def principal_problem(value: str) -> str | None:
    """Why ``value`` (already stripped) is not an acceptable principal, or ``None`` if it is."""
    if not value:
        return "must not be empty"
    if len(value) > MAX_PRINCIPAL_LENGTH:
        return f"must be at most {MAX_PRINCIPAL_LENGTH} characters"
    if has_control(value):
        return "must not contain control characters"
    return None
