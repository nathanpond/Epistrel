"""Partial anchors: the clock's absolute time, never more than the story established
(FR-CLOCK-3,4,10; docs/02 §7.1; #195). Pure: nothing here reads or writes the database.

**The offset and the anchor.** The clock is always an exact offset in minutes from the story's
start (#193). Its absolute date and time is an ``Anchor``: per-component constraints (year,
month, day, weekday, time-of-day band, hour, minute), each ``Exact``, a ``Range`` or ``UNKNOWN``,
established at a position (``anchored_at``, at clock offset ``offset_at_anchor``). The
components are held jointly where they interact, as the planner's note asks: a small set of
admissible ``Segment`` s, each a day (an index relative to the anchor's *reference day*) and a
half-open minute-of-day range on it, plus the reference day's year, month, day and weekday. So
"Saturday morning" is the reference day's weekday Saturday and the one segment ``(0, [05:00,
12:00))``; the date stays unknown.

**Shifting** (``absolute_at(offset)``). The absolute time at any other position is the anchor's
segments shifted by ``offset - offset_at_anchor`` and cut at midnight, so a range that crosses
midnight widens the day and the weekday: 130 minutes before "Saturday morning" at +130 is
[02:50, 09:50) Saturday, and 600 minutes before it is Friday 19:00 to Saturday 02:00 (Friday or
Saturday). A date moves with the day only as far as the calendar decides it (a full date is
shifted exactly; a lone day of the month only while no month can end in between).

**Refining** (``refine(statement, offset, position)``). A later ``Statement`` (a time, a band,
a weekday, a date) intersects the admissible segments at its own offset; the anchor is then held
at the statement's position. An empty intersection, or a stated component the anchor already
fixes otherwise, is a ``Contradiction`` and leaves the anchor as it was (the caller keeps the
old one). "It's 9:15" narrows the minute and back-fills every earlier position exactly.

**Evaluating** (``evaluate(check, offset)``). A check decides only when every admissible value
agrees: ``supported`` when it holds on every segment for every value of every unknown component,
``contradicted`` when it holds on none, ``uncertain`` otherwise. No check ever resolves by
picking a value inside a range (U-24's never-picks property).

**Calendars and bands** (FR-CLOCK-4). The Gregorian calendar (``core.states.model.Calendar``) has
1,440-minute days, its months and leap years, and Monday-first weekdays. A custom calendar names
its day length, months (each with its days), weekdays and year length; a custom date's weekday
is known only when the story states it. The time-of-day bands (``(name, from, to)`` on a 24-hour
clock, tiling the day; ``check_bands``) are scaled to a custom day's length.

**Display** (``AbsoluteRange.display``): the known components and ranges only ("Saturday,
between 05:00 and 12:00 (morning)"; "Friday or Saturday, between 19:00 and 02:00"); ``None``
when nothing is known, so the caller shows the offset.
"""

from __future__ import annotations

import calendar as gregorian_calendar
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from functools import cached_property
from typing import Any, Final

from epistrel.core.membership import Position
from epistrel.core.states.model import (
    DEFAULT_BANDS,
    Calendar,
    CalendarMonth,
    check_bands,
    check_calendar,
    hhmm_minutes,
)

Band = tuple[str, str, str]
Interval = tuple[int, int]
"""A half-open minute-of-day range ``[lo, hi)``."""

GREGORIAN_WEEKDAYS: Final = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
GREGORIAN_MONTHS: Final = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


# -- components -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Exact:
    """A component known to be ``value``."""

    value: int | str


@dataclass(frozen=True, slots=True)
class Range:
    """A component known to lie in ``lo`` … ``hi`` (inclusive; a weekday, hour or band range
    may wrap around: Saturday … Monday)."""

    lo: int | str
    hi: int | str


class UnknownType:
    """A component the story has not established."""

    _instance: UnknownType | None = None

    def __new__(cls) -> UnknownType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Unknown"


UNKNOWN: Final = UnknownType()
Component = Exact | Range | UnknownType
COMPONENTS: Final = ("year", "month", "day", "weekday", "band", "hour", "minute")


class Outcome(StrEnum):
    """``evaluate``'s answer."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class Contradiction:
    """``refine``'s answer to a statement the anchor rules out: which component (``time``,
    ``weekday``, ``year``, ``month`` or ``day``)."""

    component: str
    detail: str = ""


# -- the calendar ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clockwork:
    """A calendar and its time-of-day bands, as the arithmetic needs them."""

    calendar: Calendar = field(default_factory=Calendar)
    bands: tuple[Band, ...] = DEFAULT_BANDS

    @classmethod
    def of(cls, calendar: Any = None, bands: Iterable[Sequence[str]] | None = None) -> Clockwork:
        """A validated clockwork (422 ``validation_error`` for a bad calendar or bands)."""
        found = check_calendar(Calendar.of(calendar))
        chosen = DEFAULT_BANDS if bands is None else tuple((b[0], b[1], b[2]) for b in bands)
        check_bands(chosen)
        return cls(found, chosen)

    @property
    def day_minutes(self) -> int:
        return self.calendar.day_minutes or 1440

    @property
    def weekdays(self) -> tuple[str, ...]:
        if self.calendar.gregorian:
            return GREGORIAN_WEEKDAYS
        return tuple(w.casefold() for w in self.calendar.weekdays or ())

    @property
    def months(self) -> tuple[str, ...]:
        if self.calendar.gregorian:
            return GREGORIAN_MONTHS
        return tuple(m.name.casefold() for m in self.calendar.months or ())

    @cached_property
    def band_tiles(self) -> tuple[tuple[str, int, int], ...]:
        """The bands as ``(name, lo, hi)`` minute-of-day tiles of this calendar's day."""
        day = self.day_minutes
        return tuple(
            (name.casefold(), hhmm_minutes(lo) * day // 1440, hhmm_minutes(hi) * day // 1440)
            for name, lo, hi in self.bands
        )

    def band_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for name, _, _ in self.band_tiles))

    def band_intervals(self, name: str) -> tuple[Interval, ...]:
        """Every tile of band ``name`` (the night is two)."""
        wanted = name.casefold()
        return tuple((lo, hi) for n, lo, hi in self.band_tiles if n == wanted and lo < hi)

    def bands_of(self, lo: int, hi: int) -> list[str]:
        """The bands ``[lo, hi)`` meets, in day order."""
        return [n for n, b_lo, b_hi in self.band_tiles if b_lo < hi and lo < b_hi]

    def min_month_days(self) -> int:
        if self.calendar.gregorian:
            return 28
        return min(m.days for m in self.calendar.months or (CalendarMonth(name="x", days=1),))

    def month_count(self) -> int:
        return len(self.months)

    def month_days(self, year: int, month: int) -> int:
        if self.calendar.gregorian:
            return gregorian_calendar.monthrange(year, month)[1]
        return (self.calendar.months or ())[month - 1].days

    def valid_date(self, year: int, month: int, day: int) -> bool:
        if not 1 <= month <= self.month_count():
            return False
        if self.calendar.gregorian and not 1 <= year <= 9999:
            return False
        return 1 <= day <= self.month_days(year, month)

    def ordinal(self, year: int, month: int, day: int) -> int:
        """The date's day number (Gregorian: ``date.toordinal``)."""
        if self.calendar.gregorian:
            return date(year, month, day).toordinal()
        months = self.calendar.months or ()
        year_days = self.calendar.year_days or 1
        return (year - 1) * year_days + sum(m.days for m in months[: month - 1]) + day

    def from_ordinal(self, ordinal: int) -> tuple[int, int, int] | None:
        """The ``(year, month, day)`` of a day number (``None`` outside the calendar)."""
        if self.calendar.gregorian:
            if not 1 <= ordinal <= date.max.toordinal():
                return None
            found = date.fromordinal(ordinal)
            return found.year, found.month, found.day
        year_days = self.calendar.year_days or 1
        year, rest = divmod(ordinal - 1, year_days)
        for index, month in enumerate(self.calendar.months or (), start=1):
            if rest < month.days:
                return year + 1, index, rest + 1
            rest -= month.days
        return None  # pragma: no cover - year_days is the months' days (check_calendar)

    def weekday_of(self, year: int, month: int, day: int) -> int | None:
        """A Gregorian date's weekday (Monday 0); ``None`` for a custom calendar."""
        if self.calendar.gregorian:
            return date(year, month, day).weekday()
        return None

    def clock(self, minute: int) -> str:
        """A minute of the day as ``HH:MM`` (the day's end as ``24:00``)."""
        hours, minutes = divmod(minute, 60)
        return f"{hours:02d}:{minutes:02d}"


GREGORIAN_CLOCKWORK: Final = Clockwork()


# -- statements and checks ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Statement:
    """What a sentence establishes or claims about the absolute time at one position: the
    admissible minutes of the day (``time``: half-open ranges; ``None``: no time stated), a
    weekday (an index of the calendar's), and a date's year, month and day. Every stated part
    must hold (a check of all of them; a refinement by all of them)."""

    time: tuple[Interval, ...] | None = None
    weekday: int | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    approximate: bool = False
    """Said with "about", "around" …: widened by the time-statement tolerance even when it
    refines the anchor."""

    @property
    def empty(self) -> bool:
        return (
            self.time is None
            and self.weekday is None
            and self.year is None
            and self.month is None
            and self.day is None
        )

    def widened(self, tolerance: int, day_minutes: int = 1440) -> Statement:
        """This statement with each stated time widened by ``tolerance`` minutes either side
        (wrapping around midnight)."""
        if self.time is None or tolerance <= 0:
            return self
        found: list[Interval] = []
        for lo, hi in self.time:
            if hi - lo >= day_minutes - 2 * tolerance:
                return replace(self, time=((0, day_minutes),))
            found += _wrap(lo - tolerance, hi + tolerance, day_minutes)
        return replace(self, time=_merge(found))

    def document(self) -> dict[str, Any]:
        """The statement as an anchor fact stores it."""
        found: dict[str, Any] = {}
        if self.time is not None:
            found["time"] = [list(i) for i in self.time]
        for name in ("weekday", "year", "month", "day"):
            value = getattr(self, name)
            if value is not None:
                found[name] = value
        if self.approximate:
            found["approximate"] = True
        return found

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> Statement:
        time = raw.get("time")
        return cls(
            time=None if time is None else tuple((int(a), int(b)) for a, b in time),
            weekday=raw.get("weekday"),
            year=raw.get("year"),
            month=raw.get("month"),
            day=raw.get("day"),
            approximate=bool(raw.get("approximate", False)),
        )


@dataclass(frozen=True, slots=True)
class Elapsed:
    """``elapsed_since``: ``minutes`` have passed since clock offset ``since``, within a
    factor of ``factor`` either way. The clock's offset is exact, so this needs no anchor."""

    since: int
    minutes: int
    factor: float = 2.0


@dataclass(frozen=True, slots=True)
class Scheduled:
    """``scheduled_status``: an event's status (``due``, ``not_due`` …). Uncertain until #196
    keeps scheduled events."""

    event_id: str
    status: str


Check = Statement | Elapsed | Scheduled


def _wrap(lo: int, hi: int, day: int) -> list[Interval]:
    """``[lo, hi)`` (which may run past either end of the day) folded into the day."""
    if hi - lo >= day:
        return [(0, day)]
    lo_m, hi_m = lo % day, hi % day
    if hi_m == 0:
        hi_m = day
    if lo_m < hi_m:
        return [(lo_m, hi_m)]
    return [(lo_m, day), (0, hi_m)]


def _merge(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    found: list[list[int]] = []
    for lo, hi in sorted(i for i in intervals if i[0] < i[1]):
        if found and lo <= found[-1][1]:
            found[-1][1] = max(found[-1][1], hi)
        else:
            found.append([lo, hi])
    return tuple((a, b) for a, b in found)


def _intersect(lo: int, hi: int, with_: Sequence[Interval]) -> list[Interval]:
    return [(max(lo, a), min(hi, b)) for a, b in with_ if max(lo, a) < min(hi, b)]


def _covered(lo: int, hi: int, by: Sequence[Interval]) -> bool:
    """``[lo, hi)`` lies wholly inside the union ``by`` (merged)."""
    return any(a <= lo and hi <= b for a, b in _merge(by))


# -- the anchor -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    """Admissible minutes ``[lo, hi)`` of day ``day`` (an index relative to the reference
    day)."""

    day: int
    lo: int
    hi: int


@dataclass(frozen=True, slots=True)
class DayDate:
    """What is known of one day's date and weekday (``None``: unknown)."""

    year: int | None = None
    month: int | None = None
    day: int | None = None
    weekday: int | None = None


@dataclass(frozen=True)
class AbsoluteRange:
    """The absolute time at one clock offset (``Anchor.absolute_at``): the admissible segments
    and what is known of each day's date."""

    clockwork: Clockwork
    segments: tuple[Segment, ...]
    days: Mapping[int, DayDate]

    # -- components -------------------------------------------------------------------------

    def _component(self, values: Sequence[int | None], cyclic: int | None = None) -> Component:
        if not values or any(v is None for v in values):
            return UNKNOWN
        found = sorted({v for v in values if v is not None})
        if len(found) == 1:
            return Exact(found[0])
        if cyclic is None:
            return Range(found[0], found[-1])
        if len(found) >= cyclic:
            return UNKNOWN
        # The smallest cyclic run holding every value: start after the largest gap.
        gaps = [(found[(i + 1) % len(found)] - v) % cyclic for i, v in enumerate(found)]
        widest = max(range(len(found)), key=lambda i: gaps[i])
        if sum(g for i, g in enumerate(gaps) if i != widest) != len(found) - 1:
            return UNKNOWN  # not one run (it cannot be, for consecutive days)
        return Range(found[(widest + 1) % len(found)], found[widest])

    @property
    def year(self) -> Component:
        return self._component([self.days[s.day].year for s in self.segments])

    @property
    def month(self) -> Component:
        return self._component([self.days[s.day].month for s in self.segments])

    @property
    def day(self) -> Component:
        return self._component([self.days[s.day].day for s in self.segments])

    @property
    def weekday(self) -> Component:
        found = self._component(
            [self.days[s.day].weekday for s in self.segments], len(self.clockwork.weekdays)
        )
        names = self.clockwork.weekdays
        if isinstance(found, Exact):
            return Exact(names[int(found.value)])
        if isinstance(found, Range):
            return Range(names[int(found.lo)], names[int(found.hi)])
        return found

    def _whole_day(self) -> bool:
        day = self.clockwork.day_minutes
        merged = _merge((s.lo, s.hi) for s in self.segments)
        return merged == ((0, day),)

    @property
    def hour(self) -> Component:
        if self._whole_day():
            return UNKNOWN
        hours = sorted({h for s in self.segments for h in range(s.lo // 60, (s.hi - 1) // 60 + 1)})
        return self._component(hours, cyclic=max(1, self.clockwork.day_minutes // 60))

    @property
    def minute(self) -> Component:
        """The minute of the hour: known only while every admissible minute shares its
        hour."""
        if not self.segments or isinstance(self.hour, UnknownType | Range):
            return UNKNOWN
        lo = min(s.lo for s in self.segments) % 60
        hi = (max(s.hi for s in self.segments) - 1) % 60
        return Exact(lo) if lo == hi else Range(lo, hi)

    @property
    def band(self) -> Component:
        if self._whole_day():
            return UNKNOWN
        names: list[str] = []
        for s in sorted(self.segments, key=lambda x: (x.day, x.lo)):
            for name in self.clockwork.bands_of(s.lo, s.hi):
                if name not in names:
                    names.append(name)
        if len(names) == 1:
            return Exact(names[0])
        return Range(names[0], names[-1]) if names else UNKNOWN

    def components(self) -> dict[str, Component]:
        """Every component: year, month, day, weekday, band, hour, minute."""
        return {name: getattr(self, name) for name in COMPONENTS}

    # -- display -----------------------------------------------------------------------------

    def _date_text(self, d: DayDate) -> str | None:
        cw = self.clockwork
        if d.year is not None and d.month is not None and d.day is not None:
            if cw.calendar.gregorian:
                return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
            return f"{d.day} {cw.calendar.months[d.month - 1].name}, year {d.year}"  # type: ignore[index]
        parts: list[str] = []
        if d.day is not None:
            parts.append(f"the {_ordinal_text(d.day)}")
        if d.month is not None:
            month = (
                cw.months[d.month - 1].title()
                if cw.calendar.gregorian
                else (cw.calendar.months or ())[d.month - 1].name
            )
            parts.append(f"of {month}" if parts else month)
        if d.year is not None:
            parts.append(f"{'in ' if parts else 'year '}{d.year}")
        return " ".join(parts) or None

    def _day_text(self, index: int) -> str | None:
        d = self.days[index]
        found: list[str] = []
        if d.weekday is not None:
            found.append(self.clockwork.weekdays[d.weekday].title())
        date_text = self._date_text(d)
        if date_text is not None:
            found.append(date_text)
        return ", ".join(found) or None

    def _time_text(self, lo: int, hi: int) -> str | None:
        cw = self.clockwork
        if lo == 0 and hi == cw.day_minutes:
            return None
        text = cw.clock(lo) if hi - lo == 1 else f"between {cw.clock(lo)} and {cw.clock(hi)}"
        bands = cw.bands_of(lo, hi)
        return f"{text} ({bands[0]})" if len(bands) == 1 else text

    def display(self) -> str | None:
        """The known components and ranges (see the module docstring); ``None`` when nothing
        is known."""
        if not self.segments:
            return None
        ordered = sorted(self.segments, key=lambda s: (s.day, s.lo))
        texts = [(self._day_text(s.day), self._time_text(s.lo, s.hi)) for s in ordered]
        if len(ordered) == 2 and ordered[0].hi == self.clockwork.day_minutes and ordered[1].lo == 0:
            # One range across midnight.
            first, second = ordered
            days = [t for t in (texts[0][0], texts[1][0]) if t]
            day_text = " or ".join(dict.fromkeys(days)) or None
            time_text = (
                None
                if first.lo <= second.hi
                else f"between {self.clockwork.clock(first.lo)} and "
                f"{self.clockwork.clock(second.hi)}"
            )
            joined = ", ".join(t for t in (day_text, time_text) if t)
            return joined or None
        pieces = [", ".join(t for t in pair if t) for pair in texts]
        pieces = [p for p in pieces if p]
        if not pieces:
            return None
        return " or ".join(dict.fromkeys(pieces))

    def document(self) -> dict[str, Any]:
        """The components as JSON (``Exact``: the value; ``Range``: ``[lo, hi]``; unknown:
        ``null``) with the display."""

        def plain(c: Component) -> Any:
            if isinstance(c, Exact):
                return c.value
            if isinstance(c, Range):
                return [c.lo, c.hi]
            return None

        return {
            **{name: plain(c) for name, c in self.components().items()},
            "display": self.display(),
        }


def _ordinal_text(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@dataclass(frozen=True)
class Anchor:
    """The clock's absolute time (see the module docstring). ``segments`` are relative to the
    reference day, whose ``year``, ``month``, ``day`` and ``weekday`` are what is known of it;
    ``revision`` is how many anchor statements built it (#197 keys on it). ``unanchored``:
    nothing is established yet (no position, no segments)."""

    clockwork: Clockwork = field(default_factory=Clockwork)
    segments: tuple[Segment, ...] = ()
    anchored_at: Position | None = None
    offset_at_anchor: int = 0
    year: int | None = None
    month: int | None = None
    day: int | None = None
    weekday: int | None = None
    revision: int = 0

    @classmethod
    def unanchored(cls, clockwork: Clockwork | None = None) -> Anchor:
        return cls(clockwork or Clockwork())

    @property
    def is_unanchored(self) -> bool:
        return not self.segments

    # -- dates -------------------------------------------------------------------------------

    def _full(self) -> bool:
        return self.year is not None and self.month is not None and self.day is not None

    def _date_of(self, index: int) -> DayDate:
        """What is known of day ``index``'s date and weekday."""
        cw = self.clockwork
        n = len(cw.weekdays)
        weekday = None if self.weekday is None else (self.weekday + index) % n
        if self._full():
            at = cw.ordinal(int(self.year or 0), int(self.month or 0), int(self.day or 0))
            found = cw.from_ordinal(at + index)
            if found is None:
                return DayDate(weekday=weekday)
            y, m, d = found
            if weekday is None:
                weekday = cw.weekday_of(y, m, d)
            return DayDate(y, m, d, weekday)
        if index == 0:
            return DayDate(self.year, self.month, self.day, weekday)
        day = None
        same_month = False
        if self.day is not None and 1 <= self.day + index <= cw.min_month_days():
            day, same_month = self.day + index, True
        month = self.month if same_month else None
        year = self.year if same_month else None
        if (
            not same_month
            and self.year is not None
            and self.month is not None
            and 1 < self.month < cw.month_count()
            and abs(index) <= cw.min_month_days()
        ):
            year = self.year  # a month's neighbours lie in its year
        return DayDate(year, month, day, weekday)

    # -- shifting ----------------------------------------------------------------------------

    def _shifted(self, shift: int) -> tuple[Segment, ...]:
        day = self.clockwork.day_minutes
        found: list[Segment] = []
        for s in self.segments:
            lo, hi = s.day * day + s.lo + shift, s.day * day + s.hi + shift
            while lo < hi:
                index = lo // day
                end = min(hi, (index + 1) * day)
                found.append(Segment(index, lo - index * day, end - index * day))
                lo = end
        return tuple(found)

    def _range(self, segments: Sequence[Segment]) -> AbsoluteRange:
        days = {s.day: self._date_of(s.day) for s in segments}
        return AbsoluteRange(self.clockwork, tuple(segments), days)

    def absolute_at(self, offset: int) -> AbsoluteRange:
        """The absolute time at clock offset ``offset`` (see the module docstring)."""
        if self.is_unanchored:
            return AbsoluteRange(
                self.clockwork, (Segment(0, 0, self.clockwork.day_minutes),), {0: DayDate()}
            )
        return self._range(self._shifted(offset - self.offset_at_anchor))

    def display(self, offset: int | None = None) -> str | None:
        """The absolute time at ``offset`` (the anchoring offset by default) as a reader sees
        it: its known components and ranges; ``None`` when nothing is known."""
        return self.absolute_at(self.offset_at_anchor if offset is None else offset).display()

    # -- evaluating --------------------------------------------------------------------------

    def _holds(self, check: Statement, segment: Segment, known: DayDate) -> tuple[bool, bool]:
        """``(every admissible value satisfies it, some does)`` on ``segment``."""
        every, some = True, True
        if check.time is not None:
            if not _intersect(segment.lo, segment.hi, check.time):
                return False, False
            every = _covered(segment.lo, segment.hi, check.time)
        for name, stated, n in (
            ("weekday", check.weekday, len(self.clockwork.weekdays)),
            ("year", check.year, 0),
            ("month", check.month, 0),
            ("day", check.day, 0),
        ):
            if stated is None:
                continue
            value = getattr(known, name)
            if value is None:
                every = every and n == 1
                continue
            if value != stated:
                return False, False
        return every, some

    def evaluate(self, check: Check, offset: int) -> Outcome:
        """``check`` at clock offset ``offset`` (see the module docstring)."""
        if isinstance(check, Scheduled):
            return Outcome.UNCERTAIN
        if isinstance(check, Elapsed):
            actual = offset - check.since
            if actual < 0 or check.minutes < 0:
                return Outcome.UNCERTAIN
            factor = max(1.0, check.factor)
            low, high = check.minutes / factor, check.minutes * factor
            return Outcome.SUPPORTED if low <= actual <= high else Outcome.CONTRADICTED
        if check.empty:
            return Outcome.UNCERTAIN
        found = self.absolute_at(offset)
        results = [self._holds(check, s, found.days[s.day]) for s in found.segments]
        if all(every for every, _ in results):
            return Outcome.SUPPORTED
        if not any(some for _, some in results):
            return Outcome.CONTRADICTED
        return Outcome.UNCERTAIN

    # -- refining ----------------------------------------------------------------------------

    def refine(
        self, statement: Statement, offset: int, position: Position | None = None
    ) -> Anchor | Contradiction:
        """This anchor narrowed by ``statement`` made at clock offset ``offset`` (see the module
        docstring), held at ``position``; a ``Contradiction`` when nothing admissible is
        left."""
        if statement.empty:
            return self
        cw = self.clockwork
        if self.is_unanchored:
            base: tuple[Segment, ...] = (Segment(0, 0, cw.day_minutes),)
            anchor = replace(self, segments=base, offset_at_anchor=offset)
        else:
            base = self._shifted(offset - self.offset_at_anchor)
            anchor = replace(self, segments=base, offset_at_anchor=offset)
        kept: list[Segment] = []
        for s in base:
            pieces = (
                [(s.lo, s.hi)] if statement.time is None else _intersect(s.lo, s.hi, statement.time)
            )
            known = anchor._date_of(s.day)
            if any(
                stated is not None and getattr(known, name) not in (None, stated)
                for name, stated in (
                    ("weekday", statement.weekday),
                    ("year", statement.year),
                    ("month", statement.month),
                    ("day", statement.day),
                )
            ):
                continue
            kept += [Segment(s.day, lo, hi) for lo, hi in pieces]
        if not kept:
            component = "time" if statement.time is not None else "date"
            return Contradiction(component, "no admissible time is left")
        days = sorted({s.day for s in kept})
        updates: dict[str, Any] = {}
        if len(days) == 1:
            index = days[0]
            n = len(cw.weekdays)
            if statement.weekday is not None and self.weekday is None:
                updates["weekday"] = (statement.weekday - index) % n
            stated = (statement.year, statement.month, statement.day)
            if all(v is not None for v in stated) and not anchor._full():
                y, m, d = (int(v) for v in stated)  # type: ignore[arg-type]
                if not cw.valid_date(y, m, d):
                    return Contradiction("date", "no such date")
                back = cw.from_ordinal(cw.ordinal(y, m, d) - index)
                if back is not None:
                    updates.update(year=back[0], month=back[1], day=back[2])
            elif index == 0:
                for name, value in zip(("year", "month", "day"), stated, strict=True):
                    if value is not None and getattr(self, name) is None:
                        updates[name] = value
        refined = replace(
            anchor,
            segments=tuple(sorted(kept, key=lambda s: (s.day, s.lo))),
            anchored_at=position if position is not None else self.anchored_at,
            revision=self.revision + 1,
            **updates,
        )
        if refined._full():
            year, month, day = (
                int(refined.year or 0),
                int(refined.month or 0),
                int(refined.day or 0),
            )
            if not cw.valid_date(year, month, day):
                return Contradiction("date", "no such date")
            weekday = cw.weekday_of(year, month, day)
            if weekday is not None:
                if refined.weekday is not None and refined.weekday != weekday:
                    return Contradiction("weekday", "the date falls on another weekday")
                refined = replace(refined, weekday=weekday)
        return refined

    # -- the full date and time ---------------------------------------------------------------

    def fixes_date_time(self) -> bool:
        """The anchor fixes the full date and the exact minute (``story_epoch``'s condition)."""
        return (
            self._full()
            and len(self.segments) == 1
            and self.segments[0].hi - self.segments[0].lo == 1
        )

    def epoch_minutes(self) -> tuple[int, int, int, int] | None:
        """With a full date and time: ``(year, month, day, minute of day)`` at clock offset 0,
        the instant ``stories.story_epoch`` is for a Gregorian story (``None`` otherwise)."""
        if not self.fixes_date_time():
            return None
        found = self.absolute_at(0)
        (segment,) = found.segments
        known = found.days[segment.day]
        if known.year is None or known.month is None or known.day is None:
            return None
        return known.year, known.month, known.day, segment.lo

    # -- storage -----------------------------------------------------------------------------

    def document(self) -> dict[str, Any] | None:
        """The anchor as JSON (``GET /clock``'s ``anchor``; ``None`` while unanchored): its
        components at the anchoring offset, the reference day and segments, and where it was
        established."""
        if self.is_unanchored:
            return None
        found = self.absolute_at(self.offset_at_anchor)
        return {
            **found.document(),
            "offset": self.offset_at_anchor,
            "revision": self.revision,
            "anchored_at": None
            if self.anchored_at is None
            else {
                "turn": self.anchored_at.turn,
                "variant": self.anchored_at.variant,
                "slot_k": self.anchored_at.slot_k,
                "beat": self.anchored_at.beat,
            },
            "reference": {
                "year": self.year,
                "month": self.month,
                "day": self.day,
                "weekday": self.weekday,
            },
            "segments": [[s.day, s.lo, s.hi] for s in self.segments],
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, Any], clockwork: Clockwork | None = None) -> Anchor:
        """An anchor ``document`` wrote (an author's ``anchor`` setting takes the same form,
        ``segments`` and ``reference``, at ``offset``)."""
        reference = raw.get("reference") or {}
        at = raw.get("anchored_at")
        return cls(
            clockwork or Clockwork(),
            tuple(Segment(int(d), int(lo), int(hi)) for d, lo, hi in raw.get("segments") or ()),
            None
            if not at
            else Position(int(at["turn"]), int(at["variant"]), int(at["slot_k"]), int(at["beat"])),
            int(raw.get("offset", 0)),
            reference.get("year"),
            reference.get("month"),
            reference.get("day"),
            reference.get("weekday"),
            int(raw.get("revision", 0)),
        )


def fold(
    statements: Iterable[tuple[Statement, int, Position | None]],
    base: Anchor,
) -> Anchor:
    """The anchor ``statements`` (each with its clock offset and position, in position order)
    build on ``base``; a statement that contradicts what came before is skipped (it was never
    meant to commit)."""
    current = base
    for statement, offset, position in statements:
        found = current.refine(statement, offset, position)
        if isinstance(found, Anchor):
            current = found
    return current


# -- reading statements -----------------------------------------------------------------------

ABOUT: Final = frozenset({"about", "around", "roughly", "nearly", "almost", "close to"})
"""Approximators that widen a stated time by the tolerance; any other ("after", "until" …)
states no time this module will decide on."""
APPROXIMATORS: Final = (
    "shortly after",
    "shortly before",
    "just after",
    "just before",
    "just past",
    "well past",
    "close to",
    "nearly",
    "almost",
    "about",
    "around",
    "roughly",
    "past",
    "after",
    "before",
    "gone",
    "until",
    "till",
)
_HOURS: Final = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_FRACTIONS: Final = {
    "half past": 30,
    "quarter past": 15,
    "a quarter past": 15,
    "quarter to": -15,
    "a quarter to": -15,
}
_AM: Final = frozenset({"am", "a.m.", "in the morning"})
_PM: Final = frozenset({"pm", "p.m.", "in the evening", "at night", "in the afternoon"})
_NAMED: Final = {"noon": 720, "midday": 720, "midnight": 0}
_LEAD = re.compile(r"^(?:it\s+(?:is|was)|it['\u2019]s)\s+(?:now\s+)?(?:a\s+)?", re.IGNORECASE)
_DAY_OF_MONTH = re.compile(r"^(?:the\s+)?(\d{1,3})(?:st|nd|rd|th)$")


def _hour(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    return _HOURS.get(word)


def _points(hour: int, minute: int, meridian: str | None, day: int) -> tuple[Interval, ...] | None:
    """The minutes of the day ``hour:minute`` may be: one with a meridian or a 24-hour hour
    (13 and on), else both halves of the day."""
    if not 0 <= minute < 60:
        return None
    if meridian is not None:
        if not 1 <= hour <= 12:
            return None
        base = hour % 12 + (12 if meridian in _PM else 0)
        found = [base * 60 + minute]
    elif hour == 0 or hour > 12:
        found = [hour * 60 + minute]
    else:
        found = [(hour % 12) * 60 + minute, (hour % 12 + 12) * 60 + minute]
    found = [m % day for m in found if m < day or m == day]
    if not found:
        return None
    return _merge((m, m + 1) for m in found)


def _clock_time(text: str, day: int) -> tuple[Interval, ...] | None:
    meridian = None
    for word in sorted((*_AM, *_PM), key=len, reverse=True):
        if text.endswith(" " + word) or (text.endswith(word) and text[: -len(word)][-1:].isdigit()):
            meridian = word
            text = text[: -len(word)].strip()
            break
    if meridian is None and text in _NAMED:
        m = _NAMED[text]
        return ((m, m + 1),)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match:
        return _points(int(match.group(1)), int(match.group(2)), meridian, day)
    match = re.fullmatch(r"(\w+)\s+o['\u2019]clock", text)
    if match:
        hour = _hour(match.group(1))
        return None if hour is None else _points(hour, 0, meridian, day)
    for fraction, minutes in sorted(_FRACTIONS.items(), key=lambda f: -len(f[0])):
        if text.startswith(fraction + " "):
            hour = _hour(text[len(fraction) + 1 :].strip())
            if hour is None:
                return None
            if minutes < 0:
                return _points((hour - 1) % 12 or 12, 60 + minutes, meridian, day)
            return _points(hour, minutes, meridian, day)
    hour = _hour(text)
    if hour is not None and meridian is not None:
        return _points(hour, 0, meridian, day)
    return None


def statement_of(text: str, clockwork: Clockwork = GREGORIAN_CLOCKWORK) -> Statement | None:
    """What ``text`` (a time as said: "9:15", "half past nine", "Saturday morning", "the 14th",
    "about noon") states, or ``None`` when it states nothing this module decides on (a vague
    "after nine", a named time such as "dusk")."""
    said = " ".join(_LEAD.sub("", text.strip()).casefold().replace(",", " ").split())
    said = said.rstrip(".!?")
    approximate = False
    for word in APPROXIMATORS:
        if said.startswith(word + " "):
            if word not in ABOUT:
                return None
            approximate = True
            said = said[len(word) + 1 :]
            break
    day = clockwork.day_minutes
    clocked = _clock_time(said, day)
    if clocked is not None:
        return Statement(time=clocked, approximate=approximate)
    match = _DAY_OF_MONTH.fullmatch(said)
    if match and not approximate:
        return Statement(day=int(match.group(1)))
    words = said.split()
    weekday: int | None = None
    if words and words[0] in clockwork.weekdays:
        weekday = clockwork.weekdays.index(words[0])
        words = words[1:]
    band = " ".join(words)
    if band.startswith("in the "):
        band = band[len("in the ") :]
    if band in ("", "day") and weekday is not None:
        return Statement(weekday=weekday)
    tiles = clockwork.band_intervals(band) if band else ()
    if tiles and not approximate:
        return Statement(time=_merge(tiles), weekday=weekday)
    return None


def band_statement(name: str, clockwork: Clockwork = GREGORIAN_CLOCKWORK) -> Statement | None:
    """The time of day band ``name`` covers (``None``: no such band)."""
    tiles = clockwork.band_intervals(name.strip())
    return Statement(time=_merge(tiles)) if tiles else None


__all__ = [
    "ABOUT",
    "COMPONENTS",
    "GREGORIAN_CLOCKWORK",
    "UNKNOWN",
    "AbsoluteRange",
    "Anchor",
    "Band",
    "Check",
    "Clockwork",
    "Component",
    "Contradiction",
    "DayDate",
    "Elapsed",
    "Exact",
    "Outcome",
    "Range",
    "Scheduled",
    "Segment",
    "Statement",
    "UnknownType",
    "band_statement",
    "fold",
    "statement_of",
]
