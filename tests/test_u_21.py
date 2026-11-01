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
- ``test_u_21`` (#200, Postgres): the speaker clauses: narration checked against the clock, a
  character's own time claims against its time belief and classified by intent (docstring
  there), and a reading carried forward by perceived time only.
- ``test_u_21__unconscious`` (#200, through ``respond``): the ``unconscious`` fluent, its
  perception gap and Marcus's "it's still eight" (docstring there).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import testimony
from epistrel.core.errors import InvalidRequest
from epistrel.core.propositions.vocabulary import UNCONSCIOUS
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.time import TimeBasis, resolve_time
from epistrel.core.resolution.types import ReasonCode, ResolvedTime, Uncertain
from epistrel.core.states.model import (
    Calendar,
    ClockSettings,
    check_clock_settings,
    validate_definition,
    world_clock_definition,
)
from epistrel.core.verify.rules import VerdictKind
from epistrel.core.world_clock import anchor_epoch, time_belief
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
from tests import time_belief_support as tb
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import respond_client
from tests.structured_turn_support import beat

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


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


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


# -- #200: verification by speaker ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-9,10,15")
async def test_u_21(
    stub_settings: Settings,
    engine: AsyncEngine,
    time_claims_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#200's speaker clauses (FR-CLOCK-9,10,15), on a story running the clock (``manual``,
    anchored at 11:00) under ``time_awareness = exact`` with per-character partitions, each
    claim through ``Verifier.verify`` as ``verify_detected`` checks it (a character's own claim
    with its belief dimension, narration objectively):

    - narration "good evening" at 11:00 is contradicted, at 18:30 supported;
    - Jenni's meeting at 17:00 (a canon event she holds): acting on it at 11:20 is
      belief-inconsistent with no deceptive intent, a hallucination repaired; at 16:55 it is
      accepted; "I have to run to my meeting" declared deceptive (her ``think``: an excuse) is
      a lie that stands; an early start on her 15:00 errand at 14:10 is accepted;
    - "about 3:10" is consistent at 15:20 and inconsistent at 15:40; "we've been here for
      hours" is inconsistent after 20 perceived minutes and consistent after two hours; a
      joking "practically midnight" is a joke that stands;
    - Bobby read three off a stopped watch: "it's only three" at 17:30 is belief-consistent and
      objectively false, a sincere error that stands; Jenni's with no such antecedent is a
      hallucination; complement: the same "it's three" from the narrator is contradicted.
      #162's ``consistency_of`` decides a heard time against the witness's time belief through
      the same hook: Bobby's watch supports "about 3:10", Jenni's derived belief (never
      stored) contradicts it;
    - ``approximate`` (16:40): "about four" consistent, "4:52 exactly" inconsistent; ``none``
      (12:00): "must be around noon" a low-confidence guess, accepted and never anchored,
      "it's 12:07" inconsistent and repaired;
    - an unanchored clock leaves narration's "evening" undecided (#195's anchoring path); a
      character who knows the time anchors it with a sincere "it's 9:15" (a ``clock_anchor``
      request), never with a guess ("must be about nine")."""
    async with respond_client(stub_settings, engine) as (client, _):
        diner = await tb.Scene.open(client, engine, "11:00", "Jenni", "Bobby")
        place = diner.place
        meeting = await diner.event("Jenni's meeting", "17:00", "Jenni")
        errand = await diner.event("the bank errand", "15:00", "Jenni")

        # Narration at 11:00: "good evening" is contradicted.
        found, _ = await diner.narrate(
            tb.time_claim(place, "evening", await diner.now(), band=True)
        )
        assert found.kind is VerdictKind.CONTRADICTED

        # 11:20: acting on the 17:00 meeting is belief-inconsistent: a hallucination.
        await diner.at("11:20")
        due = await diner.status(meeting, "due")
        found, _ = await diner.say("Jenni", due, channel="do")
        assert tb.classified(found) == ("hallucination_repaired", "inconsistent", True)
        # Declared deceptive (her think: an excuse to leave): a lie, which stands.
        found, _ = await diner.say("Jenni", due, intent="deceptive")
        assert tb.classified(found) == ("lie", "inconsistent", False)
        # "We've been here for hours" after twenty perceived minutes: inconsistent.
        found, _ = await diner.say("Jenni", await diner.elapsed("11:00", 120))
        assert tb.classified(found) == ("hallucination_repaired", "inconsistent", True)
        # A joke is a joke.
        found, _ = await diner.say(
            "Jenni", await diner.time("practically midnight"), intent="joking"
        )
        assert tb.classified(found) == ("joke", "unknown", False)

        # 13:00: two hours on, "hours" is consistent.
        await diner.at("13:00")
        found, _ = await diner.say("Jenni", await diner.elapsed("11:00", 120))
        assert tb.classified(found) == ("sincere", "consistent", False)

        # 14:10: an early start on the 15:00 errand is accepted (not due yet, as she knows).
        await diner.at("14:10")
        found, _ = await diner.say("Jenni", await diner.status(errand, "not_due"), channel="do")
        assert tb.classified(found)[1:] == ("consistent", False)

        # 15:20 / 15:40: "about 3:10" within the tolerance, then not.
        await diner.at("15:20")
        found, _ = await diner.say("Jenni", await diner.time("about 3:10"))
        assert tb.classified(found) == ("sincere", "consistent", False)
        await diner.at("15:40")
        found, _ = await diner.say("Jenni", await diner.time("about 3:10"))
        assert tb.classified(found) == ("hallucination_repaired", "inconsistent", True)

        # 16:55: the meeting is due as she knows it.
        await diner.at("16:55")
        found, _ = await diner.say("Jenni", await diner.status(meeting, "due"), channel="do")
        assert tb.classified(found)[1:] == ("consistent", False)

        # 17:30: Bobby reads three off his watch, which stopped unnoticed.
        await diner.at("17:30")
        await diner.observe("Bobby", "15:00")
        held = await diner.belief("Bobby")
        assert (held.basis, held.times) == ("observation", ((900, 901),))
        three = await diner.time("it's only three")
        found, _ = await diner.say("Bobby", three)
        assert found.kind is VerdictKind.CONTRADICTED  # objectively false
        assert tb.classified(found) == ("sincere_error", "consistent", False)
        # No antecedent: a hallucination, repaired.
        found, _ = await diner.say("Jenni", three)
        assert tb.classified(found) == ("hallucination_repaired", "inconsistent", True)
        # Complement: the narrator's "it's three" is contradicted.
        found, _ = await diner.narrate(three)
        assert found.kind is VerdictKind.CONTRADICTED
        # #162: a heard time is decided against the witness's time belief, a derived one too.
        heard = await diner.time("about 3:10")
        async with engine.connect() as conn:
            lookup = KeyedLookup(conn, StructuredReadSet())
            for who, expected in (
                ("Bobby", VerdictKind.SUPPORTED),
                ("Jenni", VerdictKind.CONTRADICTED),
            ):
                (consistency,) = await testimony.consistency_of(
                    lookup, diner.story, diner.ids[who], [heard]
                )
                assert consistency.verdict is expected, who

        # 18:30: narration "good evening" is supported.
        await diner.at("18:30")
        found, _ = await diner.narrate(
            tb.time_claim(place, "evening", await diner.now(), band=True)
        )
        assert found.kind is VerdictKind.SUPPORTED

        # approximate: the band and the hour, never the minute.
        rough = await tb.Scene.open(client, engine, "16:40", "Jenni", awareness="approximate")
        found, _ = await rough.say("Jenni", await rough.time("about four"))
        assert tb.classified(found)[1:] == ("consistent", False)
        found, _ = await rough.say("Jenni", await rough.time("it's 4:52 exactly"))
        assert tb.classified(found)[1:] == ("inconsistent", True)

        # none: a guess is a guess; the minute is not hers to know.
        blind = await tb.Scene.open(client, engine, "12:00", "Jenni", awareness="none")
        found, verifier = await blind.say("Jenni", await blind.time("must be around noon"))
        assert tb.classified(found) == ("sincere", "consistent", False)
        assert verifier.canon_requests() == []
        found, _ = await blind.say("Jenni", await blind.time("it's 12:07"))
        assert tb.classified(found) == ("hallucination_repaired", "inconsistent", True)

        # Unanchored: narration's band is undecided until anchored; Jenni, who knows the time,
        # anchors it with a sincere statement, never with a guess.
        loose = await tb.Scene.open(client, engine, None, "Jenni")
        found, _ = await loose.narrate(
            tb.time_claim(loose.place, "evening", await loose.now(), band=True)
        )
        assert found.kind not in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED)
        found, verifier = await loose.say("Jenni", await loose.time("must be about nine"))
        assert tb.classified(found)[1:] == ("consistent", False)
        assert [r.anchors_clock for r in verifier.canon_requests()] == []
        found, verifier = await loose.say("Jenni", await loose.time("it's 9:15"))
        assert found.kind is VerdictKind.NOVEL
        assert tb.classified(found) == ("sincere", "consistent", False)
        assert [r.anchors_clock for r in verifier.canon_requests()] == [True]

    # A reading is carried forward by the time perceived since, never by the time missed: a
    # watch read "15:00" at +0, out cold from +10 to +40, is 15:30 at +60 (pure: ``derive``).
    anchored = Anchor.unanchored().refine(Statement(time=((600, 601),)), 0)
    assert isinstance(anchored, Anchor)
    carried = time_belief.derive(
        "c",
        awareness="exact",
        anchor=anchored,
        now=60,
        gaps=[(10, 40)],
        reading=time_belief.Reading("15:00", 0),
    )
    assert (carried.basis, carried.times, carried.perceived(0)) == (
        "observation",
        ((930, 931),),
        30,
    )


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-15")
async def test_u_21__unconscious(
    stub_settings: Settings,
    engine: AsyncEngine,
    time_claims_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#200's perception gap (FR-CLOCK-15; owner decision 2026-10-28), through ``respond``:
    narration "Marcus passes out" at 20:00 writes the core fluent ``unconscious(Marcus)`` (kind
    ``unconscious``) as canon (the director's, basis ``world``); from the next beat, and every
    turn while it holds, routing (``route_perception`` over ``load_scene_state``) gives Marcus
    nothing: no memory, belief transition or exposure. At 23:00 "Marcus comes to" closes it
    (the fluent's interval ends there). Marcus perceived none of the three hours: his time
    belief is the gap's (still eight), so "it's still eight" is belief-consistent although the
    clock says 23:00 and canon holds it. Complement: Bobby, awake and present throughout,
    perceives every beat and accrues the three hours; "it's still eight" from him is
    inconsistent."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        scene = await tb.Scene.open(client, engine, "20:00", "Marcus", "Bobby")
        marcus = scene.ids["Marcus"]

        first = await scene.turn()
        await tb.play(
            scene, stub, "Marcus passes out on the couch.", "Bobby", beat("say", "Is he all right?")
        )
        (fact,) = await rows(
            engine,
            "SELECT p.polarity, p.qualifiers, f.valid_from, f.valid_to "
            "FROM facts f JOIN propositions p ON p.id = f.proposition_id WHERE f.story_id = :s "
            "AND f.predicate_id = :p AND f.membership = 'active'",
            s=scene.story,
            p=UNCONSCIOUS,
        )
        assert (fact.polarity, fact.qualifiers, fact.valid_from, fact.valid_to) == (
            "affirmed",
            {"kind": "unconscious"},
            scene.zero,
            None,
        )
        # Bobby's line came after the narration: Marcus did not perceive it.
        assert (await tb.perceived(scene, "Bobby", first))["memories"] >= 2
        said = await rows(
            engine,
            "SELECT holder_character_id FROM memories WHERE story_id = :s AND narrative_turn = :t "
            "AND text LIKE '%all right%'",
            s=scene.story,
            t=first,
        )
        assert marcus not in {str(r.holder_character_id) for r in said}

        # Three hours pass; a whole turn while he is out: he is given nothing of it.
        await scene.at("23:00")
        second = await scene.turn()
        await tb.play(
            scene, stub, None, "Bobby", beat("say", "Marcus? Wake up."), do="shakes Marcus"
        )
        assert await tb.perceived(scene, "Marcus", second) == {
            "memories": 0,
            "belief_transitions": 0,
            "exposures": 0,
        }
        assert (await tb.perceived(scene, "Bobby", second))["memories"] >= 2

        # He comes to: the fluent closes at 23:00.
        await tb.play(
            scene, stub, "Marcus comes to, groggy.", "Bobby", beat("say", "There you are.")
        )
        (closed,) = await rows(
            engine,
            "SELECT f.valid_from, f.valid_to FROM facts f JOIN propositions p "
            "ON p.id = f.proposition_id WHERE f.story_id = :s AND f.predicate_id = :p "
            "AND f.membership = 'active' AND p.polarity = 'affirmed' AND f.tx_to IS NULL",
            s=scene.story,
            p=UNCONSCIOUS,
        )
        assert (closed.valid_from, closed.valid_to) == (scene.zero, scene.zero + 180)

        # Marcus perceived none of it: still eight, as far as he knows.
        held = await scene.belief("Marcus")
        assert held.basis == "gap"
        assert held.perceived(scene.zero) == 0
        assert held.times == ((1200, 1201),)
        still = await scene.time("it's still eight")
        found, _ = await scene.say("Marcus", still)
        assert tb.classified(found)[1:] == ("consistent", False)
        assert found.kind is VerdictKind.CONTRADICTED  # the clock says 23:00
        # Complement: Bobby, awake and present, accrued the three hours.
        awake = await scene.belief("Bobby")
        assert (awake.basis, awake.perceived(scene.zero), awake.times) == (
            "awareness",
            180,
            ((1380, 1381),),
        )
        found, _ = await scene.say("Bobby", still)
        assert tb.classified(found)[1:] == ("inconsistent", True)
