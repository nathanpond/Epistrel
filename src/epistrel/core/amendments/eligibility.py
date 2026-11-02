"""Per-part eligibility of an amendment at a checkpoint (FR-STORE-20; #221).

The corrected history through a checkpoint ``N``/``N⁺k`` folds every eligible part in,
evaluated **per part**, never per amendment:

- a **record** part is in when its record is in (it follows the record's membership, so a
  reader that reads the record reads the part);
- a **proposition** part is in when its valid-time begins at or before the checkpoint's
  story-time (``eligible``). With both sides as clock offsets the comparison is exact, under any
  anchor. A part whose valid-time is an absolute partial date (``{absolute: "1990"}``) is
  compared with the admissible absolute range at the checkpoint (``Anchor.absolute_at``):
  certainly before (``Included``) when the period ends at or before the range's start,
  certainly after (``Excluded``) when it starts after the range's end, else ``Undecidable``.
  No valid start means valid since the story began (``Included`` everywhere); no anchor, an
  unknown year, or an unknown checkpoint is ``Undecidable``, which reads as ``Included`` until
  #240 asks the author.

Pure: the caller reads the checkpoint's story-time (``world_clock.story_time_at``) and the
anchor's range (``world_clock.anchor_at(...).absolute_at``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from epistrel.core.world_clock.anchor import AbsoluteRange, Exact, Range


class Eligibility(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNDECIDABLE = "undecidable"

    @property
    def folds(self) -> bool:
        """Whether the part is in the corrected history (``Undecidable`` reads as in, #240)."""
        return self is not Eligibility.EXCLUDED


Period = tuple[date, date]
"""A date interval ``[start, end)``."""


@dataclass(frozen=True, slots=True)
class PartTiming:
    """When a proposition part begins: a clock offset, or an absolute partial date."""

    valid_from_minutes: int | None = None
    absolute: Period | None = None

    @classmethod
    def of(cls, valid_from_minutes: int | None, valid_spec: Mapping[str, Any] | None) -> PartTiming:
        if valid_from_minutes is not None:
            return cls(valid_from_minutes=int(valid_from_minutes))
        spec = valid_spec or {}
        if isinstance(spec.get("offset_minutes"), int):
            return cls(valid_from_minutes=int(spec["offset_minutes"]))
        absolute = spec.get("absolute")
        if isinstance(absolute, str):
            return cls(absolute=partial_period(absolute))
        return cls()


def _next_month(year: int, month: int) -> date:
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def partial_period(text: str) -> Period:
    """An ISO-8601 partial date (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``) as its period;
    ``ValueError`` for anything else."""
    parts = text.strip().split("-")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not an ISO-8601 partial date: {text!r}")
    year = int(parts[0])
    if len(parts) == 1:
        return date(year, 1, 1), date(year + 1, 1, 1)
    month = int(parts[1])
    if len(parts) == 2:
        return date(year, month, 1), _next_month(year, month)
    day = date(year, month, int(parts[2]))
    return day, date.fromordinal(day.toordinal() + 1)


def _bounds(component: Any) -> tuple[int, int] | None:
    if isinstance(component, Exact) and isinstance(component.value, int):
        return component.value, component.value
    if (
        isinstance(component, Range)
        and isinstance(component.lo, int)
        and isinstance(component.hi, int)
    ):
        return component.lo, component.hi
    return None


def checkpoint_period(found: AbsoluteRange | None) -> Period | None:
    """The admissible dates at a checkpoint as one period, from what the anchor knows of the
    year (and, when exact, the month and day); ``None`` when the year is unknown."""
    if found is None:
        return None
    year = _bounds(found.year)
    if year is None:
        return None
    month = _bounds(found.month)
    day = _bounds(found.day)
    lo_year, hi_year = year
    if year[0] == year[1] and month is not None and month[0] == month[1]:
        if day is not None and day[0] == day[1]:
            start = date(lo_year, month[0], day[0])
            return start, date.fromordinal(start.toordinal() + 1)
        return date(lo_year, month[0], 1), _next_month(lo_year, month[0])
    return date(lo_year, 1, 1), date(hi_year + 1, 1, 1)


def eligible(
    timing: PartTiming, *, story_time: int | None, absolute: Period | None = None
) -> Eligibility:
    """Whether a proposition part beginning at ``timing`` is in the corrected history at a
    checkpoint whose story-time is ``story_time`` (a clock offset) and whose admissible dates
    are ``absolute`` (see the module docstring)."""
    if timing.valid_from_minutes is None and timing.absolute is None:
        return Eligibility.INCLUDED
    if timing.valid_from_minutes is not None:
        if story_time is None:
            return Eligibility.UNDECIDABLE
        if timing.valid_from_minutes <= story_time:
            return Eligibility.INCLUDED
        return Eligibility.EXCLUDED
    assert timing.absolute is not None  # noqa: S101 - one of the two is set
    if absolute is None:
        return Eligibility.UNDECIDABLE
    start, end = timing.absolute
    lo, hi = absolute
    if end <= lo:
        return Eligibility.INCLUDED
    if start >= hi:
        return Eligibility.EXCLUDED
    return Eligibility.UNDECIDABLE


__all__ = [
    "Eligibility",
    "PartTiming",
    "Period",
    "checkpoint_period",
    "eligible",
    "partial_period",
]
