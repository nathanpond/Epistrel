"""The one content rule every telemetry sink applies (invariant 3, NFR-SEC-4; #74).

A field whose name is in ``CONTENT_FIELDS`` or ends in ``_text`` is story content: it is dropped,
at any depth. Any other string whose field is not in ``KNOWN_FIELDS`` is cut to
``MAX_UNKNOWN_CHARS`` characters, so a value nobody vetted can carry at most a fragment. Known
fields (identifiers, enums, durations, the opaque principal) pass as they are.

Logs (``observability.logging``) and spans (``observability.tracing``) both call ``scrub``.
Metrics never take a free-form value: their labels are enums and identifiers by construction,
which ``tests/conformance/test_c_08.py::test_metrics_labels`` checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONTENT_FIELDS = frozenset(
    {
        "text",
        "say",
        "do",
        "message",
        "prompt",
        "definition",
        "pinned",
        "instructions",
        "summary",
    }
)
"""Field names that hold story content (planner's set; any ``*_text`` field also counts)."""

CONTENT_SUFFIX = "_text"
MAX_UNKNOWN_CHARS = 64
MAX_PRINCIPAL_CHARS = 256

KNOWN_FIELDS = frozenset(
    {
        # structure of a log line
        "event",
        "level",
        "logger",
        "timestamp",
        "frames",
        # the operation summary (#74)
        "operation_id",
        "story_id",
        "principal",
        "kind",
        "duration_ms",
        "outcome",
        "trace_id",
        "span_id",
        "exc_type",
        "code",
        # the request line and job lines
        "route",
        "method",
        "status",
        "job_id",
        "attempt",
        "purpose",
        "model",
        "content_logging",
    }
)
"""Fields whose values are identifiers, enums, numbers or developer-written messages."""


def is_content_field(name: str) -> bool:
    """Whether a field (a log key or the last dotted part of a span attribute) is content."""
    leaf = name.rsplit(".", 1)[-1].lower()
    return leaf in CONTENT_FIELDS or leaf.endswith(CONTENT_SUFFIX)


def _cut(name: str, value: str) -> str:
    leaf = name.rsplit(".", 1)[-1]
    if leaf == "principal":
        return value[:MAX_PRINCIPAL_CHARS]
    if leaf in KNOWN_FIELDS:
        return value
    return value[:MAX_UNKNOWN_CHARS]


def _value(name: str, value: Any) -> Any:
    if isinstance(value, str):
        return _cut(name, value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return scrub(value)
    if isinstance(value, list | tuple | set | frozenset):
        return [_value(name, item) for item in value]
    # Anything else renders through its repr, which nobody vetted: cut it like an unknown string.
    return repr(value)[:MAX_UNKNOWN_CHARS]


def scrub(fields: Mapping[str, Any]) -> dict[str, Any]:
    """``fields`` without content fields (recursively), unknown strings cut to 64 characters."""
    return {
        key: _value(key, value)
        for key, value in fields.items()
        if not (isinstance(key, str) and is_content_field(key))
    }


__all__ = [
    "CONTENT_FIELDS",
    "KNOWN_FIELDS",
    "MAX_PRINCIPAL_CHARS",
    "MAX_UNKNOWN_CHARS",
    "is_content_field",
    "scrub",
]
