"""The numbers a stored JSON document may hold (#108, #105; #317).

JSON has no Infinity or NaN, yet a number too large for a float (``1e400``) parses as Infinity,
and Python's and pydantic's parsers also take the non-standard ``NaN`` and ``Infinity``
literals. A free-form document that holds one cannot be stored and read back as sent: a JSON
dump writes ``null`` in its place. ``require_finite`` is the pydantic ``AfterValidator`` that
refuses such a document at its field instead (422 ``validation_error``). Its users: a scenario's
free-form structures (``core.catalog.scenario_models``) and an upload's generation parameters
(``core.media.assets``).
"""

from __future__ import annotations

import math
from typing import Any

NOT_FINITE = "numbers must be finite"
"""The refusal's message."""


def require_finite[T](value: T) -> T:
    """``value`` when every number in it (a JSON-like document: dicts, lists and tuples, at any
    depth) is finite; a ``ValueError`` otherwise."""
    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(NOT_FINITE)
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list | tuple):
            pending.extend(item)
    return value


__all__ = ["NOT_FINITE", "require_finite"]
