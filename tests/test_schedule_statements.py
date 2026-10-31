"""Unit tests of ``core.world_clock.schedule_statements`` (FR-CLOCK-19; #198): ``classify`` over
the matrix of a matching event, who speaks and change language, the change lexicon, a report's
objective relation and the placement of a time of day through the clock's anchor."""

from __future__ import annotations

import pytest

from epistrel.core.enums import ScheduledStatus
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.world_clock import schedule_statements as ss
from epistrel.core.world_clock import stops
from epistrel.core.world_clock.anchor import Anchor, statement_of

JENNI, CAL, BOSS = (
    "01J9Z8Q4N5V6W7X8Y9Z0A1B2J1",
    "01J9Z8Q4N5V6W7X8Y9Z0A1B2C1",
    "01J9Z8Q4N5V6W7X8Y9Z0A1B2B1",
)
MEETING, GATE = "01J9Z8Q4N5V6W7X8Y9Z0A1B2M1", "01J9Z8Q4N5V6W7X8Y9Z0A1B2G1"
ZERO = 1_000


def said(subject: str, at: int, *, participants: tuple[str, ...] = ()) -> Proposition:
    """``subject``'s ``scheduled`` claim about the meeting at clock offset ``at``."""
    return Proposition(
        subject_id=subject,
        predicate_id=SCHEDULED,
        object_id=MEETING,
        qualifiers={"participants": list(participants)} if participants else {},
        valid_time=Instant.of(ZERO + at, Granularity.MINUTE),
    )


def meeting(owner: str, *, shared: tuple[str, ...] = ()) -> stops.ScheduledEvent:
    return stops.ScheduledEvent(
        MEETING,
        ScheduledStatus.PENDING,
        420,
        owner=owner,
        participants=(JENNI,),
        shared_owners=shared,
    )


@pytest.mark.parametrize(
    ("event", "speaker", "change", "expected"),
    [
        # No match: the speaker's own new plan is a create; anyone else's is not.
        (None, JENNI, False, "create"),
        (None, JENNI, True, "create"),
        (None, CAL, False, "report"),
        (None, "world", False, "create"),
        # A match, by its owner: change language changes it, a bare statement reports.
        (JENNI, JENNI, True, "change"),
        (JENNI, JENNI, False, "report"),
        # By a non-owner: a change is refused (a report), a bare statement a report.
        ("world", JENNI, True, "refused"),
        ("world", JENNI, False, "report"),
        (BOSS, JENNI, True, "refused"),
        # The world and the author may change any event.
        (JENNI, "world", True, "change"),
        (JENNI, "author", True, "change"),
        # A shared owner may change it.
        ("shared", JENNI, True, "change"),
    ],
)
def test_classify_schedule(event: str | None, speaker: str, change: bool, expected: str) -> None:
    resolved = None
    if event == "shared":
        resolved = meeting(BOSS, shared=(JENNI,))
    elif event is not None:
        resolved = meeting(event)
    claim = said(JENNI, 6 * 60)
    found = ss.classify(claim, speaker, resolved, change)
    if expected == "create":
        assert isinstance(found, ss.Create)
        assert found.owner == speaker
    elif expected == "change":
        assert isinstance(found, ss.Change)
        assert (found.event, found.planned_at) == (resolved, ZERO + 6 * 60)
    elif expected == "refused":
        assert found == ss.Report(resolved, refused_change=True)
    else:
        assert found == ss.Report(resolved)


def test_classify_schedule__participants_and_world_claims() -> None:
    # A participant who is not the subject creates too (the plan names her).
    found = ss.classify(said(CAL, 60, participants=(JENNI,)), JENNI, None, False)
    assert found == ss.Create(JENNI, (CAL, JENNI))
    # A character's world claim ("the gate closes at 15:10") is never a create.
    gate = Proposition(
        subject_id=GATE,
        predicate_id=SCHEDULED,
        object_id=MEETING,
        valid_time=Instant.of(ZERO + 20, Granularity.MINUTE),
    )
    assert ss.classify(gate, JENNI, None, False) == ss.Report(None)
    assert ss.classify(gate, "world", None, False) == ss.Create("world", ())


def test_change_language() -> None:
    for text in (
        "I pushed it to seven.",
        "I'll go at four instead.",
        "I moved my meeting to six.",
        "It's rescheduled.",
        "It's changed to eight.",
        "The dinner is now at nine.",
    ):
        assert ss.explicit_change(text), text
    for text in ("My meeting's at five.", "The bank's at four.", "Let's go now."):
        assert not ss.explicit_change(text), text
    assert ss.explicit_change("Fine.", declared=True)


def test_report_objective_relation() -> None:
    event = meeting("world")
    assert ss.same_time(event, said(JENNI, 420), ZERO)
    assert not ss.same_time(event, said(JENNI, 360), ZERO)


def basis(anchor: Anchor | None) -> TimeBasis:
    return TimeBasis(minute_zero=ZERO, advances=(), anchor=anchor)


def test_place_time() -> None:
    statement = statement_of("11:00 am")
    assert statement is not None
    exact = Anchor.unanchored().refine(statement, 0, Position(0))
    assert isinstance(exact, Anchor)
    at = basis(exact)
    # The next occurrence at or after 11:00.
    assert ss.place_time("3 pm", at, Position(1)) == ZERO + 4 * 60
    assert ss.place_time("15:10", at, Position(1)) == ZERO + 4 * 60 + 10
    # Two readings: the sooner one (17:00 today, not 05:00 tomorrow).
    assert ss.place_time("five o'clock", at, Position(1)) == ZERO + 6 * 60
    # Earlier in the day: tomorrow.
    assert ss.place_time("10 am", at, Position(1)) == ZERO + 23 * 60
    # Complements: no anchor, a partial anchor, and no time of day.
    assert ss.place_time("3 pm", basis(None), Position(1)) is None
    morning = statement_of("morning")
    assert morning is not None
    partial = Anchor.unanchored().refine(morning, 0, Position(0))
    assert isinstance(partial, Anchor)
    assert ss.place_time("3 pm", basis(partial), Position(1)) is None
    assert ss.place_time("soon", at, Position(1)) is None
