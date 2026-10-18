"""Relative time to story-time (FR-PROP-9, FR-RET-2; #139).

**Story-time at a position.** Until the world clock lands (M6, FR-CLOCK-2) the story-time of a
narrative position ``(turn, beat)`` is ``story_minute_zero + turn x story_minutes_per_turn``
(#131's M4 rule; ``TimeBasis``, default 10 minutes a turn; the beat does not move it):
``story_time_at``. "Now" is that minute.

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
from typing import Any

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

STORY_MINUTES_PER_TURN = 10
"""M4's default story-minutes a narrative turn advances, until the world clock lands (M6)."""

DAY = 1440
YEAR = 525_960


@dataclass(frozen=True, slots=True)
class TimeBasis:
    """M4's stand-in for the world clock (M6): where the story starts
    (``stories.story_minute_zero``), how far a turn moves it, and the calendar anchor
    (``stories.story_epoch``; ``None``: no calendar, so calendar expressions are uncertain)."""

    minute_zero: int = 0
    minutes_per_turn: int = STORY_MINUTES_PER_TURN
    epoch: datetime | None = None


Position = int | tuple[int, int]
"""A narrative position: a turn, or ``(turn, beat)``."""


def story_time_at(position: Position, basis: TimeBasis) -> int:
    """The story-minute of ``position`` (see the module docstring)."""
    turn = position if isinstance(position, int) else position[0]
    return basis.minute_zero + turn * basis.minutes_per_turn


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
    "Grammar",
    "Position",
    "SubjectTimeline",
    "TimeBasis",
    "calendar_words",
    "find_time_expressions",
    "grammar",
    "resolve_time",
    "story_time_at",
]
