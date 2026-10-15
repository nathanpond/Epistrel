"""Trimmed request strings whose published ``maxLength`` is the length the server checks (#366).

``StringConstraints(strip_whitespace=True, max_length=n)`` publishes ``maxLength: n`` for the
raw string but checks the length after stripping, so the API accepted a body its own schema
rejects (``" " + 80 * "*"`` for an 80-character name) and C-01's negative-data check failed when
Hypothesis found one. ``raw_max_length`` checks the raw length first (``string_too_long``,
pydantic's own error), then strips and checks the rest as before: every such type carries
``BeforeValidator(raw_max_length(n))`` after its ``StringConstraints`` (so it runs first), and a
field a model strips with ``str_strip_whitespace`` carries it too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_core import PydanticCustomError


def raw_max_length(max_length: int) -> Callable[[Any], Any]:
    """A ``BeforeValidator`` refusing a string longer than ``max_length`` before anything
    strips it; any other value passes through to the field's own validation."""

    def check(value: Any) -> Any:
        if isinstance(value, str) and len(value) > max_length:
            raise PydanticCustomError(
                "string_too_long",
                "String should have at most {max_length} characters",
                {"max_length": max_length},
            )
        return value

    return check


__all__ = ["raw_max_length"]
