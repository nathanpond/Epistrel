"""Trimmed request strings whose published ``maxLength`` is the length the server checks (#366).

``StringConstraints(strip_whitespace=True, max_length=n)`` publishes ``maxLength: n`` for the
raw string but checks the length after stripping, so the API accepted a body its own schema
rejects (``" " + 80 * "*"`` for an 80-character name) and C-01's negative-data check failed when
Hypothesis found one. ``trim_as_sent`` checks the raw length first (``string_too_long``,
pydantic's own error), then strips as ``str.strip`` does and checks the rest as before. Every
such type carries ``BeforeValidator(trim_as_sent(n))`` after its ``StringConstraints`` (so it
runs first), and so does each field of a model stripping with ``str_strip_whitespace``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_core import PydanticCustomError


def trim_as_sent(max_length: int) -> Callable[[Any], Any]:
    """A ``BeforeValidator`` refusing a string longer than ``max_length`` as sent, then
    stripping it with ``str.strip`` (the server's own notion of blank, ``_non_blank`` and the
    like: pydantic's ``strip_whitespace`` keeps ``\\x1c`` to ``\\x1f``, so ``"\\x1f"`` passed a
    name and failed later as a 500). Any other value passes through to the field's own
    validation."""

    def check(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if len(value) > max_length:
            raise PydanticCustomError(
                "string_too_long",
                "String should have at most {max_length} characters",
                {"max_length": max_length},
            )
        return value.strip()

    return check


__all__ = ["trim_as_sent"]
