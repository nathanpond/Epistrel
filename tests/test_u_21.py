"""U-21: clock verification by speaker (docs/04 §3; FR-CLOCK-4,9,10,15), its M6 parts.

Every U-21 (M6) part lives in this file (traceability counts a test ID per milestone in one
file): #195's "custom calendars and time-of-day bands are honored" clause
(``test_u_21__calendar``); #200 adds the speaker clauses.

- ``test_u_21__calendar`` (pure): a custom calendar (its day length, named months and
  weekdays, its year) and author bands are validated at definition (``ClockSettings`` through
  ``validate_definition``, which genesis and ``PATCH /clock`` both run) and honored by the
  anchor: weekday names, month lengths, date shifts and band checks follow the calendar, the
  display names its months, and with the clock on M4's time grammar reads its times of day from
  the bands. The Gregorian default fixes a date only with a full date, and only a full date and
  time gives ``story_epoch``'s instant. Complement: bands that leave a gap, overlap, or stop
  short of the day's end are refused (422 ``validation_error``, ``bands``/``tiling``), as is a
  custom calendar whose year is not its months' days.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from epistrel.core.errors import InvalidRequest
from epistrel.core.resolution.time import TimeBasis, resolve_time
from epistrel.core.resolution.types import ReasonCode, ResolvedTime, Uncertain
from epistrel.core.states.model import (
    Calendar,
    ClockSettings,
    check_clock_settings,
    validate_definition,
    world_clock_definition,
)
from epistrel.core.world_clock import anchor_epoch
from epistrel.core.world_clock.anchor import (
    GREGORIAN_CLOCKWORK,
    Anchor,
    Clockwork,
    Exact,
    Outcome,
    Statement,
    band_statement,
    statement_of,
)

CUSTOM = {
    "kind": "custom",
    "day_minutes": 1200,
    "months": [{"name": "Thaw", "days": 30}, {"name": "Frost", "days": 20}],
    "weekdays": ["Moonday", "Starday", "Sunsday", "Restday", "Marketday"],
    "year_days": 50,
}
BANDS = (
    ("dark", "00:00", "06:00"),
    ("dawn", "06:00", "09:00"),
    ("day", "09:00", "18:00"),
    ("dusk", "18:00", "24:00"),
)


def _said(text: str, works: Clockwork = GREGORIAN_CLOCKWORK) -> Statement:
    """``statement_of(text)``, which must read."""
    found = statement_of(text, works)
    assert found is not None, text
    return found


def _band(name: str, works: Clockwork = GREGORIAN_CLOCKWORK) -> Statement:
    """``band_statement(name)``, which must name a band."""
    found = band_statement(name, works)
    assert found is not None, name
    return found


def _refused(settings: dict[str, object], reason: str) -> None:
    with pytest.raises(InvalidRequest) as refused:
        check_clock_settings(ClockSettings.model_validate(settings))
    assert refused.value.details["reason"] == reason, refused.value.details


@pytest.mark.part("M6", "FR-CLOCK-4")
def test_u_21__calendar() -> None:
    """See the module docstring."""
    # Validated at definition: the builtin clock with a custom calendar and author bands.
    clock = world_clock_definition()
    assert clock.clock is not None
    custom = clock.clock.model_copy(update={"calendar": Calendar.of(CUSTOM), "bands": BANDS})
    defined = validate_definition(clock.model_copy(update={"clock": custom}), builtin=True)
    assert defined.clock is not None
    assert defined.clock.calendar.document() == CUSTOM
    assert Calendar.of(defined.clock.calendar.stored()) == defined.clock.calendar
    assert Calendar.of("gregorian").document() == {"kind": "gregorian"}

    # Complement: the bands tile the day, and a custom year is its months' days.
    _refused({"bands": [["a", "00:00", "12:00"], ["b", "13:00", "24:00"]]}, "tiling")
    _refused({"bands": [["a", "00:00", "13:00"], ["b", "12:00", "24:00"]]}, "tiling")
    _refused({"bands": [["a", "00:00", "12:00"], ["b", "12:00", "23:00"]]}, "tiling")
    _refused({"bands": [["a", "05:00", "24:00"]]}, "tiling")
    _refused({"calendar": {**CUSTOM, "year_days": 51}}, "year_days")
    _refused({"calendar": {**CUSTOM, "weekdays": ["A", "a"]}}, "weekdays")
    _refused({"calendar": {"kind": "gregorian", "day_minutes": 1000}}, "shape")
    with pytest.raises(InvalidRequest):
        Clockwork.of(CUSTOM, [["a", "00:00", "10:00"]])

    # The custom calendar and bands are honored by the anchor.
    works = Clockwork.of(CUSTOM, BANDS)
    assert works.day_minutes == 1200
    assert works.band_intervals("dawn") == ((300, 450),)  # 06:00-09:00 of a 1,200-minute day
    stated = _said("it was Marketday dawn", works)
    assert stated == Statement(time=((300, 450),), weekday=4)
    anchor = Anchor.unanchored(works).refine(stated, 0)
    assert isinstance(anchor, Anchor)
    assert anchor.absolute_at(0).display() == "Marketday, between 05:00 and 07:30 (dawn)"
    assert anchor.evaluate(_band("dawn", works), 0) is Outcome.SUPPORTED
    assert anchor.evaluate(_band("dusk", works), 0) is Outcome.CONTRADICTED
    # A custom day is 1,200 minutes: a day later is the next weekday, wrapping after five.
    assert anchor.absolute_at(1200).weekday == Exact("moonday")
    assert anchor.absolute_at(1200 * 5).weekday == Exact("marketday")
    # A date in the custom calendar: the 30th of Thaw, year 3; a day later is Frost 1.
    dated = anchor.refine(Statement(year=3, month=1, day=30), 0)
    assert isinstance(dated, Anchor)
    assert (
        dated.absolute_at(0).display()
        == "Marketday, 30 Thaw, year 3, between 05:00 and 07:30 (dawn)"
    )
    following = dated.absolute_at(1200)
    assert (following.year, following.month, following.day) == (Exact(3), Exact(2), Exact(1))
    assert dated.absolute_at(1200 * 21).year == Exact(4)  # Frost has 20 days
    assert isinstance(dated.refine(Statement(month=2, day=25), 0), Anchor) is False
    assert anchor_epoch(dated) is None  # story_epoch is Gregorian, and needs the exact minute

    # The Gregorian default: a date is fixed only by a full date; the epoch only by date and time.
    gregorian = Anchor.unanchored().refine(_said("Saturday morning"), 130)
    assert isinstance(gregorian, Anchor)
    assert anchor_epoch(gregorian) is None
    full = gregorian.refine(Statement(year=2026, month=10, day=31, time=((555, 556),)), 130)
    assert isinstance(full, Anchor)
    assert anchor_epoch(full, 0) == datetime(2026, 10, 31, 7, 5, tzinfo=UTC)
    assert full.absolute_at(130 - 600).display() == "Friday, 2026-10-30, 23:15 (night)"

    # With the clock on, M4's grammar reads its times of day from the bands, on the day the
    # anchor places "now" in; with nothing anchored it is uncertain(no_calendar).
    on = TimeBasis(advances=(), bands=(("night", "00:00", "05:00"), *BANDS[1:]), anchor=full)
    found = resolve_time("this morning", 130, basis=on)
    assert isinstance(found, Uncertain)  # no band of these is named "morning"
    morning = TimeBasis(
        advances=(),
        bands=(
            ("night", "00:00", "05:00"),
            ("morning", "05:00", "10:00"),
            ("day", "10:00", "20:00"),
            ("night", "20:00", "24:00"),
        ),
        anchor=full,
    )
    found = resolve_time("this morning", 130, basis=morning)
    assert isinstance(found, ResolvedTime)
    assert found.valid_time.possible() == (300 - 555 + 130, 600 - 555 + 130)
    blank = TimeBasis(advances=(), bands=morning.bands)
    unanchored = resolve_time("this morning", 130, basis=blank)
    assert isinstance(unanchored, Uncertain)
    assert unanchored.reason.code is ReasonCode.NO_CALENDAR
    weekday = resolve_time(
        "last Monday", 130, basis=TimeBasis(advances=(), bands=(), anchor=gregorian)
    )
    assert isinstance(weekday, ResolvedTime)  # the anchor fixes Saturday, not the date
    assert weekday.valid_time.possible() == (130 - 719 - 12 * 1440, 130 - 300 - 11 * 1440)
