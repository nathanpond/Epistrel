"""Relative time to story-time (FR-PROP-9, FR-RET-2; #139).

**Story-time at a position** (FR-CLOCK-2; #193) is ``story_minute_zero`` plus the world clock's
offset there, the fold of its active advances, for a story that runs the clock; for one that
does not (a story pinned to a clock-off profile version) it is #131's M4 stand-in,
``story_minute_zero + turn x story_minutes_per_turn`` (default 10 minutes a turn; the beat does
not move it). Both live in ``TimeBasis``, which only ``core.world_clock`` builds; its
``story_time_at(conn, story_id, position)`` is the one entry point, and ``resolve_time`` reads
"now" off the basis it is given.

**The grammar** is a data table, ``time_grammar.json`` (English only in M4; another language
is another table): regex rules over number words, unit words, months, weekdays, seasons
(northern hemisphere) and times of day, with each rule's parameters. Its ``coverage`` entry
lists what it covers. ``find_time_expressions(text)`` finds every expression (longest match,
no overlaps; #141 and #148 call it); ``resolve_time(expression, position, subject, basis=)``
resolves one, purely and deterministically:

- ``now`` is a minute instant; ``yesterday``/``today``/``tomorrow`` are day instants;
  ``this morning``, ``last night``, ``tonight`` exact intervals of the day's band;
- coarse spans are approximate intervals (inner and outer bounds from the table): ``last
  week`` is possibly ``[now-14d, now-7d)``, ``recently`` possibly the last 7 days, ``the other
  day`` 1-4 days ago, ``a while ago`` 30-180 days ago;
- ``N minutes/hours/days ago`` (and ``in N …``, ``… from now``, with ``direction = future``) is
  an instant of that unit; weeks, months and years are approximate around the offset (``a
  month ago`` is 30 days ± 7);
- calendar expressions (``in 2012``, ``in May``, ``May 10``, ``last summer``, ``last
  Monday``) need the story's calendar anchor (``stories.story_epoch``): without one they are
  ``uncertain(no_calendar)``; a bare date takes the position's year; a year is a year-granule
  instant;
- **with the world clock on** (#195) the basis carries the clock's partial ``anchor`` and its
  time-of-day ``bands``: its epoch is set only by an anchor that fixes the full date and time,
  so month and year granules decide nothing before that; a weekday expression resolves against
  the anchor's weekday at "now" (``uncertain(no_calendar)`` while the anchor leaves it unknown),
  and times of day (``this morning``, ``last night``, ``tonight``) are the bands' intervals on
  the day the anchor places "now" in, approximate when the anchor gives only a range of the day
  and ``uncertain(no_calendar)`` when it gives none. A clock-off story resolves as in M4;
- ``when he was seven`` uses the subject's ``born`` fact (its granule's start; a granule
  coarser than a day, or an age fact, adds ± 1 year) or an ``age`` fact's valid-time (birth ≈
  its start - age); neither is ``uncertain(no_anchor)``. The caller reads those facts through
  #136's ``KeyedLookup`` (``core.resolution.context.subject_timeline``) and passes them in;
- anything else is ``uncertain(unresolved_time)``, never a default date.

A year is 365.25 days (525,960 minutes) for offsets and ages; calendar expressions use the
calendar ``core.propositions.model`` granules use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any

from epistrel.core.membership import Position as NarrativePosition
from epistrel.core.propositions.model import (
    DEFAULT_EPOCH,
    Bound,
    Granularity,
    Instant,
    Interval,
    granule,
    story_minute,
)
from epistrel.core.resolution.types import (
    APOSTROPHE,
    Direction,
    Reason,
    ReasonCode,
    ResolvedTime,
    TimeExpression,
    TimeResolution,
    Uncertain,
)

if TYPE_CHECKING:
    from epistrel.core.world_clock.anchor import Anchor

STORY_MINUTES_PER_TURN = 10
"""M4's default story-minutes a narrative turn advances, until the world clock lands (M6)."""

DAY = 1440
YEAR = 525_960


Position = int | tuple[int, int] | NarrativePosition
"""A narrative position: a turn, ``(turn, beat)``, or the shared ``core.membership.Position``
(#164)."""

Advance = tuple[tuple[int, int, int, int], int, int]
"""One active world clock advance as a basis carries it: its position's ``key()``, its
operation's variant and its minutes."""


@dataclass(frozen=True, slots=True)
class TimeBasis:
    """The story's time basis, which ``core.world_clock`` builds (#193; no other module does):
    where the story starts (``stories.story_minute_zero``), the calendar anchor
    (``stories.story_epoch``; ``None``: no calendar, so calendar expressions are uncertain) and,
    for a story that runs the world clock, its active ``advances``. Without them (``None``: the
    clock is off) story-time is M4's stand-in, ``minutes_per_turn`` a turn (#131, #139)."""

    minute_zero: int = 0
    minutes_per_turn: int = STORY_MINUTES_PER_TURN
    epoch: datetime | None = None
    advances: tuple[Advance, ...] | None = None
    anchor: Anchor | None = None
    """#195: the clock's anchor (``None``: the clock is off, or nothing is anchored)."""
    bands: tuple[tuple[str, str, str], ...] | None = None
    """#195: the clock's time-of-day bands (``None``: the clock is off; the grammar's own)."""

    def at(self, position: Position) -> int:
        """The story-minute of ``position``, the moment it starts: ``minute_zero`` plus the
        clock's offset before it (the advances before it, leaving out another variant's of its
        own turn; an advance at a beat ends that beat, FR-CLOCK-17), or the stand-in's. A turn
        alone (or ``(turn, beat)``) is the start of that turn (or beat)."""
        return self._minutes(position, inclusive=False)

    def through(self, position: Position) -> int:
        """``minute_zero`` plus the clock's offset through ``position``: the advances at or
        before it (the clock's value there, ``core.world_clock.clock_at``)."""
        return self._minutes(position, inclusive=True)

    def _minutes(self, position: Position, *, inclusive: bool) -> int:
        if self.advances is None:
            return self.minute_zero + turn_of(position) * self.minutes_per_turn
        found = as_position(position)
        key = found.key()
        return self.minute_zero + sum(
            minutes
            for at, variant, minutes in self.advances
            if (at <= key if inclusive else at < key)
            and not (found.variant and at[0] == found.turn and variant != found.variant)
        )


def as_position(position: Position) -> NarrativePosition:
    """``position`` as a ``core.membership.Position`` (a turn alone is its start)."""
    if isinstance(position, NarrativePosition):
        return position
    if isinstance(position, int):
        return NarrativePosition(position)
    return NarrativePosition(position[0], beat=position[1])


def turn_of(position: Position) -> int:
    """The turn of ``position`` in any of its forms."""
    if isinstance(position, NarrativePosition):
        return position.turn
    return position if isinstance(position, int) else position[0]


def story_time_at(position: Position, basis: TimeBasis) -> int:
    """The story-minute of ``position`` on ``basis`` (``TimeBasis.at``). Outside this module
    story-time is read through ``core.world_clock.story_time_at`` (#193)."""
    return basis.at(position)


@dataclass(frozen=True, slots=True)
class SubjectTimeline:
    """The subject's established timeline a resolution may anchor on: its ``born`` valid-time
    and its ``age`` facts (``(years, valid_time)``), as the caller read them."""

    born: Interval | Instant | None = None
    ages: tuple[tuple[int, Interval | Instant], ...] = ()


# -- the grammar -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Rule:
    name: str
    kind: str
    regex: re.Pattern[str]
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Grammar:
    raw: Mapping[str, Any]
    rules: tuple[_Rule, ...]

    @property
    def numbers(self) -> Mapping[str, int]:
        return self.raw["numbers"]  # type: ignore[no-any-return]


def _alternatives(words: Sequence[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


@cache
def grammar() -> Grammar:
    """The English grammar (``time_grammar.json``), compiled."""
    raw = json.loads(
        resources.files("epistrel.core.resolution").joinpath("time_grammar.json").read_text("utf-8")
    )
    numbers = _alternatives(list(raw["numbers"])) + r"|\d{1,3}"
    fills = {
        "{numbers}": numbers,
        "{number}": f"(?P<number>{numbers})",
        "{unit}": f"(?P<unit>{_alternatives(list(raw['unit_words']))})",
        "{month}": f"(?P<month>{_alternatives(list(raw['months']))})",
        "{season}": f"(?P<season>{_alternatives(list(raw['seasons']))})",
        "{weekday}": f"(?P<weekday>{_alternatives(list(raw['weekdays']))})",
    }
    rules = []
    for entry in raw["rules"]:
        pattern = entry["pattern"]
        for placeholder, fill in fills.items():
            pattern = pattern.replace(placeholder, fill)
        params = {k: v for k, v in entry.items() if k not in ("rule", "kind", "pattern")}
        edge = rf"[\w'{APOSTROPHE}]"
        regex = re.compile(rf"(?<!{edge})(?:{pattern})(?!{edge})", re.IGNORECASE)
        rules.append(_Rule(entry["rule"], entry["kind"], regex, params))
    return Grammar(raw, tuple(rules))


@cache
def calendar_words() -> frozenset[str]:
    """Month, weekday and season names (never an unknown name to the mention resolver)."""
    raw = grammar().raw
    return frozenset({*raw["months"], *raw["weekdays"], *raw["seasons"]})


def find_time_expressions(text: str) -> list[TimeExpression]:
    """Every temporal expression in ``text``, in order: the longest match where two overlap."""
    found: list[TimeExpression] = []
    for rule in grammar().rules:
        for match in rule.regex.finditer(text):
            found.append(
                TimeExpression(
                    (match.start(), match.end()), rule.name, match.groupdict(), match.group()
                )
            )
    found.sort(key=lambda e: (e.span[0], -(e.span[1] - e.span[0])))
    kept: list[TimeExpression] = []
    for expression in found:
        if kept and expression.span[0] < kept[-1].span[1]:
            continue
        kept.append(expression)
    return kept


# -- resolution --------------------------------------------------------------------------------


def _uncertain(code: ReasonCode, detail: str) -> Uncertain:
    return Uncertain(Reason(code, detail))


def _direction(lo: int, hi: int, now: int) -> Direction:
    if hi <= now:
        return "past"
    if lo > now:
        return "future"
    return "present"


def _number(value: str | None) -> int | None:
    if value is None:
        return None
    word = " ".join(value.casefold().split())
    if word.isdigit():
        return int(word)
    found = grammar().numbers.get(word)
    return None if found is None else int(found)


def _approximate(center: int, half: int, tolerance: int) -> Interval:
    return Interval(
        start=Bound(inner=center - half, outer=center - tolerance),
        end=Bound(inner=center + half, outer=center + tolerance),
    )


def _range(now: int, outer: Sequence[int], inner: Sequence[int]) -> Interval:
    return Interval(
        start=Bound(inner=now + inner[0] * DAY, outer=now + outer[0] * DAY),
        end=Bound(inner=now + inner[1] * DAY, outer=now + outer[1] * DAY),
    )


def _resolved(valid_time: Interval | Instant, now: int, rule: str) -> ResolvedTime:
    lo, hi = valid_time.possible()
    approximate = isinstance(valid_time, Interval) and not valid_time.exact
    return ResolvedTime(
        valid_time,
        _direction(now if lo is None else lo, now + 1 if hi is None else hi, now),
        rule,
        approximate=approximate,
    )


def _calendar(now: int, epoch: datetime) -> datetime:
    return epoch + timedelta(minutes=now)


def _minute(moment: datetime, epoch: datetime) -> int:
    return story_minute(moment, epoch)


def _day_start(now: int, days: int, epoch: datetime | None) -> int:
    return granule(now + days * DAY, Granularity.DAY, epoch)[0]


def _anchor(subject: SubjectTimeline | None) -> tuple[int, int] | None:
    """The subject's estimated birth minute and its tolerance."""
    if subject is None:
        return None
    if subject.born is not None:
        lo, hi = subject.born.possible()
        if lo is not None:
            width = (hi - lo) if hi is not None else YEAR
            return lo, (YEAR if width > DAY else 0)
    for years, valid_time in subject.ages:
        start = valid_time.definite()[0]
        if start is not None:
            return start - years * YEAR, YEAR
    return None


def resolve_time(
    expression: str | TimeExpression,
    position: Position,
    subject: SubjectTimeline | None = None,
    *,
    basis: TimeBasis,
) -> TimeResolution:
    """``expression`` (a text, whose first temporal expression is resolved, or one that
    ``find_time_expressions`` found) as story-time from ``position`` (see the module
    docstring); ``uncertain`` when it cannot be resolved."""
    if isinstance(expression, str):
        found = find_time_expressions(expression)
        if not found:
            return _uncertain(ReasonCode.UNRESOLVED_TIME, "no_rule")
        expression = found[0]
    rules = {r.name: r for r in grammar().rules}
    rule = rules.get(expression.rule)
    if rule is None:
        return _uncertain(ReasonCode.UNRESOLVED_TIME, "no_rule")
    now = story_time_at(position, basis)
    raw = grammar().raw
    groups = {k: (v.casefold() if isinstance(v, str) else v) for k, v in expression.groups.items()}
    params = rule.params
    epoch = basis.epoch
    if rule.kind == "now":
        return _resolved(Instant.of(now, Granularity.MINUTE), now, rule.name)
    if rule.kind == "day":
        start = _day_start(now, int(params["days"]), epoch)
        return _resolved(Instant.of(start, Granularity.DAY, epoch), now, rule.name)
    if rule.kind == "time_of_day" and basis.bands is not None:
        return _clock_time_of_day(rule, groups, params, now, basis)
    if rule.kind == "weekday" and basis.bands is not None and epoch is None:
        return _clock_weekday(rule, groups, now, basis)
    if rule.kind == "time_of_day":
        day_word = params.get("day") or " ".join(str(groups.get("day") or "").split())
        days = int(raw["day_words"].get(day_word, 0))
        tod = params.get("tod") or groups.get("tod")
        band = raw["times_of_day"][str(tod)]
        if tod == "night" and day_word in ("this", "today", "in the"):
            days = 0
        start = _day_start(now, days, epoch)
        valid = Interval.of(start + band[0] * 60, start + band[1] * 60)
        return _resolved(valid, now, rule.name)
    if rule.kind == "range":
        return _resolved(_range(now, params["outer"], params["inner"]), now, rule.name)
    if rule.kind == "offset":
        count = _number(groups.get("number"))
        unit = raw["unit_words"].get(str(groups.get("unit")))
        if count is None or unit is None:
            return _uncertain(ReasonCode.UNRESOLVED_TIME, rule.name)
        minutes = int(raw["minutes"][unit])
        at = now + int(params["sign"]) * count * minutes
        spec = raw["units"][unit]
        if spec["granularity"] is not None:
            granularity = Granularity(spec["granularity"])
            return _resolved(Instant.of(at, granularity, epoch), now, rule.name)
        return _resolved(_approximate(at, DAY // 2, int(spec["tolerance_minutes"])), now, rule.name)
    if rule.kind == "age":
        years = _number(groups.get("number"))
        anchor = _anchor(subject)
        if years is None:
            return _uncertain(ReasonCode.UNRESOLVED_TIME, rule.name)
        if anchor is None:
            return _uncertain(ReasonCode.NO_ANCHOR, rule.name)
        birth, tolerance = anchor
        start, end = birth + years * YEAR, birth + (years + 1) * YEAR
        valid = Interval(
            start=Bound(inner=start, outer=start - tolerance),
            end=Bound(inner=end, outer=end + tolerance),
        )
        return _resolved(valid, now, rule.name)
    # Calendar expressions: they need the story's calendar.
    if epoch is None:
        return _uncertain(ReasonCode.NO_CALENDAR, rule.name)
    return _calendar_time(rule, groups, now, epoch)


def _day_starts(now: int, basis: TimeBasis) -> tuple[int, int, int] | None:
    """With the clock on: the earliest and latest story-minute "now"'s day may start at, and
    the day's length (``None`` when the anchor places "now" nowhere in the day)."""
    from epistrel.core.world_clock.anchor import Anchor

    anchor = basis.anchor
    if not isinstance(anchor, Anchor):
        return None
    found = anchor.absolute_at(now - basis.minute_zero)
    day = anchor.clockwork.day_minutes
    minutes = [m for s in found.segments for m in (s.lo, s.hi - 1)]
    if not minutes or (
        len(found.segments) == 1 and found.segments[0].hi - found.segments[0].lo >= day
    ):
        return None
    days = {s.day for s in found.segments}
    if len(days) > 1:
        # Across midnight: count the earlier day's minutes back from midnight.
        first = min(days)
        minutes = [
            m - (day if s.day == first else 0) for s in found.segments for m in (s.lo, s.hi - 1)
        ]
    return now - max(minutes), now - min(minutes), day


def _span(lo_start: int, hi_start: int, begin: int, end: int) -> Interval | None:
    """``[day start + begin, day start + end)`` over the admissible day starts: exact when the
    day start is, else approximate (``None`` when no minute is certainly inside)."""
    if lo_start == hi_start:
        return Interval.of(lo_start + begin, lo_start + end)
    if hi_start + begin >= lo_start + end:
        return None
    return Interval(
        start=Bound(inner=hi_start + begin, outer=lo_start + begin),
        end=Bound(inner=lo_start + end, outer=hi_start + end),
    )


def _band_span(
    name: str, bands: Sequence[tuple[str, str, str]], day: int
) -> tuple[int, int] | None:
    """Band ``name``'s span on a day of ``day`` minutes; a band that wraps past midnight (the
    night) runs from its late tile into the next day."""
    tiles = sorted(
        (int(lo[:2]) * 60 + int(lo[3:]), int(hi[:2]) * 60 + int(hi[3:]))
        for n, lo, hi in bands
        if n.casefold() == name
    )
    if not tiles:
        return None
    tiles = [(lo * day // 1440, hi * day // 1440) for lo, hi in tiles]
    late = tiles[-1]
    if late[1] == day and tiles[0][0] == 0 and len(tiles) > 1:
        return late[0], day + tiles[0][1]
    return late


def _clock_time_of_day(
    rule: _Rule, groups: Mapping[str, Any], params: Mapping[str, Any], now: int, basis: TimeBasis
) -> TimeResolution:
    raw = grammar().raw
    day_word = params.get("day") or " ".join(str(groups.get("day") or "").split())
    days = int(raw["day_words"].get(day_word, 0))
    tod = str(params.get("tod") or groups.get("tod"))
    starts = _day_starts(now, basis)
    if starts is None:
        return _uncertain(ReasonCode.NO_CALENDAR, rule.name)
    lo_start, hi_start, day = starts
    bands = basis.bands or ()
    if tod == "tonight":
        evening = _band_span("evening", bands, day)
        night = _band_span("night", bands, day)
        span = None if evening is None or night is None else (evening[0], max(night[1], evening[1]))
    else:
        span = _band_span(tod, bands, day)
        if tod == "night" and day_word in ("this", "today", "in the"):
            days = 0
    if span is None:
        return _uncertain(ReasonCode.UNRESOLVED_TIME, rule.name)
    shift = days * day
    valid = _span(lo_start + shift, hi_start + shift, span[0], span[1])
    if valid is None:
        return _uncertain(ReasonCode.NO_CALENDAR, rule.name)
    return _resolved(valid, now, rule.name)


def _clock_weekday(
    rule: _Rule, groups: Mapping[str, Any], now: int, basis: TimeBasis
) -> TimeResolution:
    from epistrel.core.world_clock.anchor import Anchor, Exact

    anchor = basis.anchor
    starts = _day_starts(now, basis)
    if not isinstance(anchor, Anchor) or starts is None:
        return _uncertain(ReasonCode.NO_CALENDAR, rule.name)
    found = anchor.absolute_at(now - basis.minute_zero)
    weekday = found.weekday
    names = anchor.clockwork.weekdays
    target_name = str(groups["weekday"])
    if not isinstance(weekday, Exact) or target_name not in names:
        return _uncertain(ReasonCode.NO_CALENDAR, rule.name)
    today = names.index(str(weekday.value))
    target = names.index(target_name)
    week = {"last": -1, "next": 1}.get(str(groups.get("which") or ""), 0)
    delta = len(names) * week + target - today
    lo_start, hi_start, day = starts
    valid = _span(lo_start + delta * day, hi_start + delta * day, 0, day)
    if valid is None:
        return _uncertain(ReasonCode.NO_CALENDAR, rule.name)
    return _resolved(valid, now, rule.name)


def _calendar_time(
    rule: _Rule, groups: Mapping[str, Any], now: int, epoch: datetime
) -> TimeResolution:
    raw = grammar().raw
    today = _calendar(now, epoch)
    year = int(groups["year"]) if groups.get("year") else today.year
    try:
        if rule.kind == "year":
            moment = datetime(year, 1, 1, tzinfo=UTC)
            return _resolved(
                Instant.of(_minute(moment, epoch), Granularity.YEAR, epoch), now, rule.name
            )
        if rule.kind in ("month", "date"):
            month = int(raw["months"][str(groups["month"])])
            if rule.kind == "date":
                moment = datetime(year, month, int(groups["day"]), tzinfo=UTC)
                granularity = Granularity.DAY
            else:
                moment = datetime(year, month, 1, tzinfo=UTC)
                granularity = Granularity.MONTH
            return _resolved(Instant.of(_minute(moment, epoch), granularity, epoch), now, rule.name)
    except ValueError:
        return _uncertain(ReasonCode.UNRESOLVED_TIME, rule.name)
    if rule.kind == "season":
        first, last = raw["seasons"][str(groups["season"])]
        which = " ".join(str(groups.get("which") or "").split())

        def occurrence(y: int) -> tuple[int, int]:
            start = datetime(y, first, 1, tzinfo=UTC)
            end_year, end_month = y + (last - 1) // 12, (last - 1) % 12 + 1
            end = datetime(end_year, end_month, 1, tzinfo=UTC)
            return _minute(start, epoch), _minute(end, epoch)

        spans = [occurrence(y) for y in range(today.year - 2, today.year + 2)]
        if which == "last":
            chosen = [s for s in spans if s[1] <= now][-1]
        elif which == "next":
            chosen = next(s for s in spans if s[0] > now)
        else:
            current = [s for s in spans if s[0] <= now < s[1]]
            chosen = current[0] if current else occurrence(today.year)
        return _resolved(Interval.of(*chosen), now, rule.name)
    if rule.kind == "weekday":
        target = int(raw["weekdays"][str(groups["weekday"])])
        which = str(groups.get("which") or "")
        monday = today.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=today.weekday()
        )
        week = {"last": -1, "next": 1}.get(which, 0)
        moment = monday + timedelta(days=7 * week + target)
        return _resolved(Instant.of(_minute(moment, epoch), Granularity.DAY, epoch), now, rule.name)
    return _uncertain(ReasonCode.UNRESOLVED_TIME, rule.name)


__all__ = [
    "DAY",
    "DEFAULT_EPOCH",
    "STORY_MINUTES_PER_TURN",
    "YEAR",
    "Advance",
    "Grammar",
    "Position",
    "SubjectTimeline",
    "TimeBasis",
    "as_position",
    "calendar_words",
    "find_time_expressions",
    "grammar",
    "resolve_time",
    "story_time_at",
    "turn_of",
]
