"""U-24: partial anchors (docs/04 §3; FR-CLOCK-3,10,18), its M6 parts.

Every U-24 (M6) part lives in this file (traceability counts a test ID per milestone in one
file): #195's anchor clauses (``test_u_24__anchors``) and its never-picks property
(``test_u_24__never_picks``); #197 adds the ``possibly_due`` clauses (``__possibly_due``).

- ``test_u_24__anchors`` (pure, ``core.world_clock.anchor`` and the clock checks' owner lookup
  through #140's ``check``): an unanchored clock at +130 anchored by "Saturday morning" is
  weekday Saturday, time [05:00, 12:00), date unknown; 130 minutes earlier is [02:50, 09:50)
  Saturday, 600 minutes earlier Friday-or-Saturday across midnight. "Good morning" at the anchor
  is supported, "good evening" contradicted; "it's 13:00" is contradicted (and refining by it is
  a ``Contradiction`` that leaves the anchor as it was); "it's 9:15" is undecided, narrows the
  anchor to the minute and back-fills earlier positions exactly; "it's the 14th" is uncertain
  until a date is established. With the clock off the checks are ``uncertain(no_clock)`` and
  ``time_is`` is the M4 fact-backed predicate. Complement: an anchor that fixes the full date
  and time decides every check.
- ``test_u_24__never_picks`` (Hypothesis): over random anchors (built by random refinements at
  random offsets) and random checks, ``evaluate`` is exactly the brute-force answer over every
  admissible (day, minute) value: ``supported`` only when all agree, ``contradicted`` only
  when none holds, and ``uncertain`` whenever two admissible values disagree.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    LiteralKind,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.registry import core_registry
from epistrel.core.propositions.vocabulary import (
    ELAPSED_SINCE,
    SCHEDULED_STATUS,
    TIME_IS,
    TIME_OF_DAY_BAND,
)
from epistrel.core.verify import rules
from epistrel.core.verify.rules import VerdictKind, VerdictReason
from epistrel.core.world_clock.anchor import (
    GREGORIAN_CLOCKWORK,
    UNKNOWN,
    Anchor,
    Clockwork,
    Contradiction,
    Elapsed,
    Exact,
    Outcome,
    Range,
    Scheduled,
    Segment,
    Statement,
    band_statement,
    fold,
    statement_of,
)
from epistrel.core.world_clock.verify import owned_value

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 2_000 if NIGHTLY else 200

CLOCK = "01JD0000000000000000000C10"
PLACE = "01JD0000000000000000000C11"
EVENT = "01JD0000000000000000000C12"
SATURDAY = 5
REGISTRY = core_registry()


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


def _claim(predicate: str, value: str | int, **qualifiers: int) -> Proposition:
    kind = LiteralKind.DURATION if isinstance(value, int) else LiteralKind.STR
    return Proposition(
        subject_id=EVENT if predicate == SCHEDULED_STATUS else PLACE,
        predicate_id=predicate,
        literal=TypedLiteral(kind=kind, value=value),
        qualifiers=qualifiers,
        valid_time=Instant.of(130, Granularity.MINUTE),
    )


def _verdict(anchor: Anchor, claim: Proposition, offset: int = 130) -> rules.Verdict:
    """#140's ``check`` of a canon holder's clock claim, with the clock as the lookup reads it."""
    return rules.check(
        claim, registry=REGISTRY, owned=[owned_value(CLOCK, anchor, offset=offset, tolerance=15)]
    )


def _saturday_morning() -> Anchor:
    found = Anchor.unanchored().refine(_said("Saturday morning"), 130)
    assert isinstance(found, Anchor)
    return found


@pytest.mark.part("M6", "FR-CLOCK-3,10")
def test_u_24__anchors() -> None:
    """See the module docstring."""
    unanchored = Anchor.unanchored()
    assert unanchored.absolute_at(130).display() is None
    assert unanchored.evaluate(_band("morning"), 130) is Outcome.UNCERTAIN

    # "Saturday morning" at +130: weekday and a time range only.
    anchor = _saturday_morning()
    here = anchor.absolute_at(130)
    assert here.weekday == Exact("saturday")
    assert here.band == Exact("morning")
    assert here.hour == Range(5, 11)
    assert (here.year, here.month, here.day, here.minute) == (UNKNOWN,) * 4
    assert [(s.lo, s.hi) for s in here.segments] == [(300, 720)]
    assert here.display() == "Saturday, between 05:00 and 12:00 (morning)"

    # Applied backward by the offset difference; across midnight the weekday widens.
    earlier = anchor.absolute_at(0)
    assert [(s.lo, s.hi) for s in earlier.segments] == [(170, 590)]
    assert earlier.weekday == Exact("saturday")
    across = anchor.absolute_at(130 - 600)
    assert across.weekday == Range("friday", "saturday")
    assert [(s.day, s.lo, s.hi) for s in across.segments] == [(-1, 1140, 1440), (0, 0, 120)]
    assert across.display() == "Friday or Saturday, between 19:00 and 02:00"

    # Clock checks through #140's check and the owner lookup.
    morning = _verdict(anchor, _claim(TIME_OF_DAY_BAND, "morning"))
    assert morning.kind is VerdictKind.SUPPORTED
    assert [e.owner_ref for e in morning.evidence] == [f"state:{CLOCK}"]
    evening = _verdict(anchor, _claim(TIME_OF_DAY_BAND, "evening"))
    assert evening.kind is VerdictKind.CONTRADICTED
    assert [(e.rule, e.owner_ref) for e in evening.evidence] == [
        (rules.Rule.FUNCTIONAL_KEY, f"state:{CLOCK}")
    ]
    assert _verdict(anchor, _claim(TIME_IS, "13:00")).kind is VerdictKind.CONTRADICTED
    # "it's 9:15" is undecided under the range, so it may narrow the anchor (novel).
    assert anchor.evaluate(_said("9:15").widened(15), 130) is Outcome.UNCERTAIN
    assert _verdict(anchor, _claim(TIME_IS, "9:15")).kind is VerdictKind.NOVEL

    # Refining: 13:00 is a Contradiction and the anchor is as it was.
    refused = anchor.refine(_said("13:00"), 130)
    assert isinstance(refused, Contradiction)
    assert anchor.absolute_at(130).display() == "Saturday, between 05:00 and 12:00 (morning)"
    assert fold([(_said("13:00"), 130, None)], anchor) == anchor
    # 9:15 narrows to the exact minute and back-fills the earlier positions exactly.
    exact = anchor.refine(_said("it's 9:15"), 130)
    assert isinstance(exact, Anchor)
    assert exact.revision == 2
    assert exact.absolute_at(130).minute == Exact(15)
    assert exact.absolute_at(130).hour == Exact(9)
    assert exact.absolute_at(0).display() == "Saturday, 07:05 (morning)"
    assert exact.absolute_at(130 - 600).display() == "Friday, 23:15 (night)"
    # A refinement at a later offset is held there and still applies backward.
    later = anchor.refine(_said("10:00"), 190)
    assert isinstance(later, Anchor)
    assert later.offset_at_anchor == 190
    assert later.absolute_at(130).display() == "Saturday, 09:00 (morning)"

    # "It's the 14th" is uncertain until a date is established.
    fourteenth = _said("it's the 14th")
    assert fourteenth == Statement(day=14)
    assert anchor.evaluate(fourteenth, 130) is Outcome.UNCERTAIN
    dated = exact.refine(Statement(year=2026, month=11, day=14), 130)
    assert isinstance(dated, Anchor)
    assert dated.evaluate(fourteenth, 130) is Outcome.SUPPORTED
    assert dated.evaluate(Statement(day=15), 130) is Outcome.CONTRADICTED
    assert dated.absolute_at(130 - 600).day == Exact(13)
    # A date on another weekday than the one established contradicts.
    assert isinstance(exact.refine(Statement(year=2026, month=11, day=15), 130), Contradiction)

    # Complement: an anchor that fixes the full date and time decides every check.
    assert dated.fixes_date_time()
    for check in (
        _band("morning"),
        _band("evening"),
        _said("9:15"),
        _said("13:00"),
        _said("Saturday"),
        _said("Sunday morning"),
        Statement(year=2026),
        Statement(month=12),
    ):
        assert check is not None
        assert dated.evaluate(check, 130) is not Outcome.UNCERTAIN, check
        assert dated.evaluate(check, 130 + 999) is not Outcome.UNCERTAIN, check

    # Elapsed time is exact on the offset; a scheduled status waits for #196.
    assert anchor.evaluate(Elapsed(since=100, minutes=20), 130) is Outcome.SUPPORTED
    assert anchor.evaluate(Elapsed(since=0, minutes=600), 130) is Outcome.CONTRADICTED
    elapsed = _verdict(anchor, _claim(ELAPSED_SINCE, 120, since=10))
    assert elapsed.kind is VerdictKind.SUPPORTED
    assert anchor.evaluate(Scheduled(EVENT, "due"), 130) is Outcome.UNCERTAIN
    assert _verdict(anchor, _claim(SCHEDULED_STATUS, "due")).kind is VerdictKind.UNCERTAIN

    # The clock off: the checks are uncertain(no_clock); time_is is M4's fact-backed predicate.
    for predicate, value in ((TIME_OF_DAY_BAND, "morning"), (SCHEDULED_STATUS, "due")):
        off = rules.check(_claim(predicate, value), registry=REGISTRY)
        assert (off.kind, off.reason) == (VerdictKind.UNCERTAIN, VerdictReason.NO_CLOCK)
    assert rules.check(_claim(TIME_IS, "9:15"), registry=REGISTRY).kind is VerdictKind.NOVEL
    assert rules.check(_claim(TIME_IS, "13:00"), registry=REGISTRY).kind is VerdictKind.NOVEL


# -- the never-picks property --------------------------------------------------------------

DAY = 1440
intervals = st.tuples(st.integers(0, DAY - 1), st.integers(1, 600)).map(
    lambda t: ((t[0], min(DAY, t[0] + t[1])),)
)
statements = st.builds(
    Statement,
    time=st.one_of(st.none(), intervals, st.sampled_from([((300, 720),), ((0, 300), (1260, DAY))])),
    weekday=st.one_of(st.none(), st.integers(0, 6)),
    day=st.one_of(st.none(), st.integers(1, 28)),
)
dated = st.builds(
    Statement,
    year=st.integers(2000, 2030),
    month=st.integers(1, 12),
    day=st.integers(1, 28),
)


def _expected(anchor: Anchor, check: Statement, offset: int) -> Outcome:
    """The brute-force answer: every admissible (day, minute) value, each unknown component
    taking every value."""
    found = anchor.absolute_at(offset)
    seen: set[bool | None] = set()
    for segment in found.segments:
        known = found.days[segment.day]
        for minute in range(segment.lo, segment.hi):
            truth: bool | None = True
            if check.time is not None and not any(lo <= minute < hi for lo, hi in check.time):
                truth = False
            for name in ("weekday", "year", "month", "day"):
                stated = getattr(check, name)
                if stated is None or truth is False:
                    continue
                value = getattr(known, name)
                if value is None:
                    truth = None  # both: some admissible value agrees, some does not
                elif value != stated:
                    truth = False
            seen.add(truth)
    if seen == {True}:
        return Outcome.SUPPORTED
    if seen == {False}:
        return Outcome.CONTRADICTED
    return Outcome.UNCERTAIN


@pytest.mark.part("M6", "FR-CLOCK-3")
@settings(max_examples=EXAMPLES, deadline=None)
@given(
    st.lists(
        st.tuples(st.one_of(statements, dated), st.integers(-3000, 3000)), min_size=0, max_size=4
    ),
    statements,
    st.integers(-3000, 3000),
)
def test_u_24__never_picks(
    refinements: list[tuple[Statement, int]], check: Statement, offset: int
) -> None:
    """See the module docstring."""
    assume(not check.empty)  # a statement of nothing is uncertain by definition
    anchor = fold(((s, o, None) for s, o in refinements), Anchor.unanchored())
    found = anchor.evaluate(check, offset)
    assert found is _expected(anchor, check, offset), (anchor, check, offset)
    # Never more than the story established: the components are only what every admissible
    # value shares.
    at = anchor.absolute_at(offset)
    if len({s.day for s in at.segments}) > 1 and anchor.weekday is not None:
        assert not isinstance(at.weekday, Exact)
    minutes = {m % 60 for s in at.segments for m in range(s.lo, s.hi)}
    if isinstance(at.minute, Exact):
        assert minutes == {at.minute.value}  # a range never reads as one minute
    for s in at.segments:
        assert isinstance(s, Segment)
        assert 0 <= s.lo < s.hi <= DAY
