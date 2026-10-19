"""U-15: contradiction rules, one at a time (docs/04 §3), its M4 parts.

Every U-15 (M4) part lives in this file (traceability counts a test ID per milestone in one
file). #131's slices cover what the rules stand on: ``test_u_15__valid_time`` the proposition
shape and its valid-time relations (FR-PROP-1,2), ``test_u_15__registry`` closed sets, stored
negations and the bounded implication closure (FR-PROP-8,11); #139's
``test_u_15__resolution_time`` relative time. #140's verdict slices (supported, contradicted,
novel, uncertain) run ``core.verify.rules.check`` over records built in memory (it is pure), one
function per docs/04 clause group, each with its complement: coverage, granularity, polarity and
the functional key, key scoping, closed sets and cardinality, exclusion and implication, weak
predicates, approximate bounds, normalization and holders.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import (
    Granularity,
    Implication,
    Instant,
    Interval,
    InvalidValidTime,
    Layer,
    Polarity,
    Predicate,
    Proposition,
    TemporalKind,
    coarsely_compatible,
    covers,
    exactly_equal,
    overlaps,
    refines,
    story_minute,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.entities import (
    NARRATOR,
    EntityEntry,
    EntityIndex,
    ResolutionContext,
)
from epistrel.core.resolution.time import (
    YEAR,
    SubjectTimeline,
    TimeBasis,
    resolve_time,
    story_time_at,
)
from epistrel.core.resolution.types import ReasonCode, ResolvedTime, Uncertain
from epistrel.core.verify.normalize import ClaimSketch, RegisterPredicate, Unnormalizable, normalize
from epistrel.core.verify.rules import (
    CANON,
    ClosedSetRecord,
    Holder,
    Options,
    Rule,
    Verdict,
    VerdictKind,
    VerdictReason,
    check,
)
from tests import verify_support as vs
from tests.event_support import new_story, rows, scalar

BOBBY = "01JA0000000000000000000001"
OHIO = "01JA0000000000000000000002"
SMITH = "01JA0000000000000000000003"
TEAM = "01JA0000000000000000000004"
OSU = "01JA0000000000000000000005"
KNOCK = "01JB0000000000000000000001"


def year(y: int) -> int:
    return story_minute(datetime(y, 1, 1, tzinfo=UTC))


def minute(moment: str) -> int:
    return story_minute(datetime.fromisoformat(moment).replace(tzinfo=UTC))


def lived(start: int, end: int) -> Proposition:
    return Proposition(
        subject_id=BOBBY,
        predicate_id=CORE_BY_NAME["located_at"].id,
        object_id=OHIO,
        valid_time=Interval.of(year(start), year(end)),
    )


@pytest.mark.part("M4", "FR-PROP-1,2")
def test_u_15__valid_time() -> None:
    """Intervals ``[start, end)`` with ``start < end`` and instants with a granularity; no
    ``[t, t)`` is ever constructed; coverage and the three temporal relations under
    approximate bounds; event identity never from time alone; one viewpoint-free shape for
    every holder. Complement: overlap without coverage is not coverage."""
    canon = lived(2010, 2014)
    # A claim inside canon's interval is covered; one reaching past it only overlaps.
    assert covers(canon.valid_time, lived(2011, 2013).valid_time) is True
    wide = lived(2000, 2020).valid_time
    assert overlaps(canon.valid_time, wide) is True
    assert covers(canon.valid_time, wide) is False
    # No empty interval, ever; a literal [t, t) is refused at normalization.
    with pytest.raises(InvalidValidTime):
        Interval.of(year(2012), year(2012))
    with pytest.raises(InvalidValidTime):
        Proposition.model_validate(
            {
                "subject_id": BOBBY,
                "predicate_id": KNOCK,
                "valid_time": {
                    "kind": "interval",
                    "start": {"inner": 9, "outer": 9},
                    "end": {"inner": 9, "outer": 9},
                },
            }
        )
    # An instant at t is covered when start <= t < end; at t = end it is not.
    span = Interval.of(year(2010), year(2014))
    assert covers(span, Instant.of(year(2010), Granularity.MINUTE)) is True
    assert covers(span, Instant.of(year(2014) - 1, Granularity.MINUTE)) is True
    assert covers(span, Instant.of(year(2014), Granularity.MINUTE)) is False
    # Mixed granularity: "May 10 09:00" refines "May 10", never the reverse; two hours of one
    # day are each compatible with the day and not with each other.
    day = Instant.of(minute("2012-05-10T00:00"), Granularity.DAY)
    nine = Instant.of(minute("2012-05-10T09:00"), Granularity.HOUR)
    five = Instant.of(minute("2012-05-10T17:00"), Granularity.HOUR)
    eleventh = Instant.of(minute("2012-05-11T00:00"), Granularity.DAY)
    assert refines(nine, day)
    assert not refines(day, nine)
    assert coarsely_compatible(day, nine)
    assert coarsely_compatible(day, five)
    assert not coarsely_compatible(nine, five)
    assert not coarsely_compatible(eleventh, nine)
    assert exactly_equal(nine, Instant.of(minute("2012-05-10T09:59"), Granularity.HOUR)) is True
    assert exactly_equal(nine, day) is False
    # Approximate bounds: an overlap the bounds cannot decide is undecidable, not a verdict.
    summer = Interval.model_validate(
        {
            "start": {"inner": minute("2012-08-01T00:00"), "outer": minute("2012-06-01T00:00")},
            "end": {"inner": minute("2012-08-02T00:00"), "outer": minute("2012-09-30T00:00")},
        }
    )
    assert overlaps(summer, Interval.of(minute("2012-09-01T00:00"), year(2013))) is None
    assert covers(Interval.of(year(2012), year(2013)), summer) is True
    assert covers(Interval.of(minute("2012-07-01T00:00"), year(2013)), summer) is None
    # Event identity: two knocks on one day are two events unless an event id joins them; a
    # unique event (a birth) is one by its uniqueness key.
    knock = Predicate(
        id=KNOCK,
        story_id=TEAM,
        name="knocked",
        layer="auto",
        qualifiers=None,
        temporal_kind=TemporalKind.EVENT,
    )
    at_nine = Proposition(subject_id=SMITH, predicate_id=KNOCK, object_id=BOBBY, valid_time=nine)
    at_five = at_nine.model_copy(update={"valid_time": five})
    assert not registry.same_event(at_nine, at_five, knock)
    born = CORE_BY_NAME["born"]
    birth = Proposition(subject_id=BOBBY, predicate_id=born.id, valid_time=nine)
    assert registry.same_event(birth, birth.model_copy(update={"valid_time": five}), born)
    # Viewpoint-free: the shape names no holder, provenance, visibility or confidence, so canon
    # and an utterance hold the same proposition.
    assert not {"holder", "provenance", "visibility", "confidence"} & set(Proposition.model_fields)
    said = Proposition.model_validate(canon.model_dump())
    assert said == canon
    assert said.normalized_key() == canon.normalized_key()


@pytest.mark.integration
@pytest.mark.part("M4", "FR-PROP-8,11")
async def test_u_15__registry(engine: AsyncEngine) -> None:
    """The world is open: a closed set exists only when declared (a story whose author lists
    none has none, however many single-valued facts it holds), and an explicit negation is
    stored as a negated proposition. The implication closure applies declared rules only and
    stops at its depth. Complement: the declared closed set is stored, and the declared rule
    derives what the undeclared one cannot."""
    story = await new_story(engine)
    async with engine.begin() as conn:
        bobby = await store.add_entity(conn, story, "person", "Bobby")
        osu = await store.add_entity(conn, story, "organization", "OSU")
        florida = await store.add_entity(conn, story, "place", "Florida")
        reg = await registry.load(conn, story)
        enrolled = CORE_BY_NAME["enrolled_at"].id
        for n in range(5):
            await store.write_proposition(
                conn,
                story,
                Proposition(
                    subject_id=bobby,
                    predicate_id=enrolled,
                    object_id=osu,
                    valid_time=Interval.of(year(2010 + n), year(2011 + n)),
                ),
                reg,
            )
        never = Proposition(
            subject_id=bobby,
            predicate_id=CORE_BY_NAME["located_at"].id,
            object_id=florida,
            polarity=Polarity.NEGATED,
            valid_time=Interval.of(None, None),
        )
        negated = await store.write_proposition(conn, story, never, reg)
    assert (
        await scalar(engine, "SELECT count(*) FROM closed_sets WHERE story_id = :s", s=story) == 0
    )
    (stored,) = await rows(engine, "SELECT polarity FROM propositions WHERE id = :p", p=negated)
    assert stored.polarity == "negated"
    rules = registry.ScenarioRules.parse(
        {
            "closed_sets": [
                {
                    "predicate": "enrolled_at",
                    "subject": "bobby",
                    "members": ["osu"],
                    "valid_from": year(2010),
                    "valid_to": year(2014),
                }
            ]
        }
    )
    async with engine.begin() as conn:
        await registry.register_author(
            conn, story, rules, resolve={"bobby": bobby, "osu": osu}.__getitem__
        )
    (declared,) = await rows(engine, "SELECT * FROM closed_sets WHERE story_id = :s", s=story)
    assert declared.members == [osu]
    assert declared.asserted_by == "author"
    assert (declared.valid_from, declared.valid_to) == (year(2010), year(2014))
    # The closure: declared rules only, bounded depth.
    member = CORE_BY_NAME["member_of"].id
    part_of = "01JB0000000000000000000002"
    facts = [
        Proposition(
            subject_id=BOBBY, predicate_id=member, object_id=TEAM, valid_time=Interval.of(0, 9)
        ),
        Proposition(
            subject_id=TEAM, predicate_id=part_of, object_id=OSU, valid_time=Interval.of(0, 9)
        ),
    ]
    assert registry.imply(facts, []).derived == ()
    rule = Implication(when=member, then=enrolled, via=part_of)
    (implied,) = registry.imply(facts, [rule]).derived
    assert (implied.subject_id, implied.predicate_id, implied.object_id) == (BOBBY, enrolled, OSU)
    steps = [member, enrolled, CORE_BY_NAME["knows"].id, CORE_BY_NAME["owns"].id, part_of]
    chain = [Implication(when=a, then=b) for a, b in itertools.pairwise(steps)]
    capped = registry.imply(facts[:1], chain, max_depth=2)
    assert [p.predicate_id for p in capped.derived] == steps[1:3]
    assert capped.truncated
    assert not registry.imply(facts[:1], chain, max_depth=10).truncated


@pytest.mark.part("M4", "FR-PROP-9")
def test_u_15__resolution_time() -> None:
    """Relative time resolves against the story-time of the claim's narrative position and the
    subject's established timeline (#139): "now" is the position's minute, "last week" moves
    with the position, "when he was seven" anchors on the subject's birth or age facts, and two
    values of a functional fluent in non-overlapping intervals do not overlap. Complement: an
    expression that cannot be resolved is ``uncertain``, never a default date."""
    basis = TimeBasis(minute_zero=year(2020), epoch=datetime(2000, 1, 1, tzinfo=UTC))
    early, late = (2, 0), (2 + 7 * 144, 3)  # a week of ten-minute turns later
    assert story_time_at(late, basis) - story_time_at(early, basis) == 7 * 24 * 60
    now = resolve_time("now", late, basis=basis)
    assert isinstance(now, ResolvedTime)
    assert now.valid_time == Instant.of(story_time_at(late, basis), Granularity.MINUTE)
    # U-15: "last week resolves against the claim's position", not against the head.
    first = resolve_time("I saw him last week", early, basis=basis)
    second = resolve_time("I saw him last week", late, basis=basis)
    assert isinstance(first, ResolvedTime)
    assert isinstance(second, ResolvedTime)
    lo, hi = first.valid_time.possible()
    assert lo is not None
    assert hi is not None
    assert second.valid_time.possible() == (lo + 7 * 24 * 60, hi + 7 * 24 * 60)
    assert hi <= story_time_at(early, basis)
    # The subject's established timeline: born 2001-03-04 makes "seven" 2008-03-04 to 2009.
    born = Instant.of(minute("2001-03-04T00:00"), Granularity.DAY)
    seven = resolve_time("when he was seven", late, SubjectTimeline(born=born), basis=basis)
    assert isinstance(seven, ResolvedTime)
    assert seven.valid_time == Interval.of(born.at + 7 * YEAR, born.at + 8 * YEAR)
    assert covers(seven.valid_time, Instant.of(year(2008) + 200 * 24 * 60, Granularity.DAY))
    # An age fact alone anchors approximately (birth year +/- 1).
    aged = SubjectTimeline(ages=((19, Interval.of(year(2020), year(2021))),))
    approx = resolve_time("at the age of seven", late, aged, basis=basis)
    assert isinstance(approx, ResolvedTime)
    assert approx.approximate
    # Fluents change over time: ages 19 and 20 in non-overlapping intervals do not overlap.
    assert overlaps(Interval.of(year(2020), year(2021)), Interval.of(year(2021), year(2022))) is (
        False
    )
    # Complement: unresolvable expressions are uncertain with a reason, never a default.
    for expression, subject, code in (
        ("when he was seven", None, ReasonCode.NO_ANCHOR),
        ("once upon a time", None, ReasonCode.UNRESOLVED_TIME),
    ):
        found = resolve_time(expression, late, subject, basis=basis)
        assert isinstance(found, Uncertain)
        assert found.reason.code is code
        assert found.reason.gate == "unresolved_time"
    unanchored = resolve_time("in 2012", late, basis=TimeBasis())
    assert isinstance(unanchored, Uncertain)
    assert unanchored.reason.code is ReasonCode.NO_CALENDAR


# -- #140: the verdict slices (FR-PROP-8,10,11,12) ------------------------------------------------


def _check(
    claim: Proposition,
    *canon: Proposition,
    reg: registry.Registry = vs.REGISTRY,
    closed: tuple[ClosedSetRecord, ...] = (),
    **options: Any,
) -> Verdict:
    return check(
        claim, vs.records(*canon), registry=reg, closed_sets=closed, options=Options(**options)
    )


def _cites(verdict: Verdict, rule: Rule) -> None:
    """A ``contradicted`` verdict names its rule and the record it conflicts with."""
    assert verdict.kind is VerdictKind.CONTRADICTED, verdict
    assert rule in verdict.rules, verdict
    assert all(e.proposition_id or e.closed_set_id for e in verdict.evidence if not e.depth_capped)


def _ohio(start: int, end: int) -> Proposition:
    return vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.years(start, end))


@pytest.mark.part("M4", "FR-PROP-1,10")
def test_u_15__coverage() -> None:
    """Support needs full coverage: Ohio 2010-2014 supports a 2011-2013 claim; the coverage
    counterexample (a 2000-2020 claim) is ``uncertain(partial_coverage)`` with covered [2010,
    2014) and uncovered [2000, 2010) u [2014, 2020), each remainder checked as its own claim
    (``novel``); adjacent 2010-2012 and 2012-2014 jointly cover 2010-2014; an instant at t with
    start <= t < end is covered and one at t = end is not. Complement: overlap alone never
    supports."""
    canon = _ohio(2010, 2014)
    inside = _check(_ohio(2011, 2013), canon)
    assert inside.kind is VerdictKind.SUPPORTED
    assert inside.rules == {Rule.COVERAGE}
    wide = _check(_ohio(2000, 2020), canon)
    assert (wide.kind, wide.reason) == (VerdictKind.UNCERTAIN, VerdictReason.PARTIAL_COVERAGE)
    assert wide.covered == ((vs.year(2010), vs.year(2014)),)
    assert wide.uncovered == ((vs.year(2000), vs.year(2010)), (vs.year(2014), vs.year(2020)))
    assert [c.valid_time for c in wide.remainder_claims] == [
        vs.years(2000, 2010),
        vs.years(2014, 2020),
    ]
    assert [v.kind for v in wide.remainder_verdicts] == [VerdictKind.NOVEL, VerdictKind.NOVEL]
    assert wide.confidence == 1.0  # resolved here, never escalated
    joint = _check(_ohio(2010, 2014), _ohio(2010, 2012), _ohio(2012, 2014))
    assert joint.kind is VerdictKind.SUPPORTED
    assert len(joint.evidence) == 2
    t = vs.year(2014)
    at_start = vs.prop(
        vs.BOBBY, vs.LOCATED_AT, vs.OHIO, Instant.of(vs.year(2010), Granularity.MINUTE)
    )
    at_end = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, Instant.of(t, Granularity.MINUTE))
    assert _check(at_start, canon).kind is VerdictKind.SUPPORTED
    assert _check(at_end, canon).kind is VerdictKind.NOVEL
    # Complement: a claim that only overlaps canon is never supported.
    assert _check(_ohio(2013, 2016), canon).kind is VerdictKind.UNCERTAIN


@pytest.mark.part("M4", "FR-PROP-1,10")
def test_u_15__granularity() -> None:
    """Mixed granularity: canon "May 10 09:00" supports a claim of "May 10"; canon "May 10" does
    not support "May 10 09:00" (``granularity_refinement``, the hour a novel detail). A unique
    event (a birth): 09:00 vs 17:00 is ``contradicted``, vs "May 10" compatible, vs "May 11"
    ``contradicted``. Two non-unique knocks at 09:00 and 17:00 are two events with no
    contradiction, and "a knock on May 10" is compatible with both but identifies neither."""
    nine = Instant.of(vs.minute("2012-05-10T09:00"), Granularity.HOUR)
    five = Instant.of(vs.minute("2012-05-10T17:00"), Granularity.HOUR)
    day = Instant.of(vs.minute("2012-05-10T00:00"), Granularity.DAY)
    eleventh = Instant.of(vs.minute("2012-05-11T00:00"), Granularity.DAY)
    there = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, nine)
    assert _check(there.model_copy(update={"valid_time": day}), there).kind is VerdictKind.SUPPORTED
    coarse = _check(there, there.model_copy(update={"valid_time": day}))
    assert (coarse.kind, coarse.reason) == (
        VerdictKind.UNCERTAIN,
        VerdictReason.GRANULARITY_REFINEMENT,
    )
    assert coarse.remainder_claims == (there,)
    assert coarse.remainder_verdicts[0].kind is VerdictKind.NOVEL
    birth = vs.prop(vs.BOBBY, vs.BORN, when=nine)
    at_five = _check(birth.model_copy(update={"valid_time": five}), birth)
    _cites(at_five, Rule.INSTANT_IDENTITY)
    assert _check(birth.model_copy(update={"valid_time": day}), birth).kind is VerdictKind.SUPPORTED
    _cites(_check(birth.model_copy(update={"valid_time": eleventh}), birth), Rule.INSTANT_IDENTITY)
    knock_nine = vs.prop(vs.SMITH, vs.KNOCKED, vs.DOOR, nine)
    knock_five = knock_nine.model_copy(update={"valid_time": five})
    assert _check(knock_five, knock_nine).kind is VerdictKind.NOVEL
    either = _check(knock_nine.model_copy(update={"valid_time": day}), knock_nine, knock_five)
    assert either.kind is VerdictKind.SUPPORTED
    assert len(either.evidence) == 2  # both cited, merged into neither


@pytest.mark.part("M4", "FR-PROP-10")
def test_u_15__polarity_and_functional() -> None:
    """Opposite polarity over overlapping time is ``contradicted`` (rule ``polarity``). A
    functional predicate with the same key and different objects over overlapping time is
    ``contradicted`` (``functional_key``): the kitchen and the garden of one house, an age of 40
    and of 41. Columbus vs Ohio (Columbus ``located_in`` Ohio) is no conflict. Complement: two
    places no containment fact places are world knowledge, ``uncertain(world_knowledge)``."""
    canon = _ohio(2010, 2014)
    _cites(_check(canon.negation(), canon), Rule.POLARITY)
    never = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.FLORIDA, negated=True)
    _cites(
        _check(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.FLORIDA, vs.years(2012, 2013)), never),
        Rule.POLARITY,
    )
    house = [
        vs.prop(vs.KITCHEN, vs.LOCATED_IN, vs.HOUSE),
        vs.prop(vs.GARDEN, vs.LOCATED_IN, vs.HOUSE),
        vs.prop(vs.COLUMBUS, vs.LOCATED_IN, vs.OHIO),
    ]
    kitchen = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(0, 100))
    garden = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.GARDEN, vs.span(50, 60))
    _cites(_check(garden, kitchen, *house), Rule.FUNCTIONAL_KEY)
    columbus = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.COLUMBUS, vs.years(2011, 2012))
    assert _check(columbus, canon, *house).kind is VerdictKind.NOVEL
    assert _check(canon, columbus, *house).kind is not VerdictKind.CONTRADICTED
    unplaced = _check(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.BOSTON, vs.years(2011, 2012)), canon)
    assert (unplaced.kind, unplaced.reason) == (
        VerdictKind.UNCERTAIN,
        VerdictReason.WORLD_KNOWLEDGE,
    )
    assert unplaced.confidence == 0.5
    forty = vs.prop(vs.BOBBY, vs.AGE, literal=vs.int_literal(40), when=vs.span(0, 10))
    forty_one = vs.prop(vs.BOBBY, vs.AGE, literal=vs.int_literal(41), when=vs.span(5, 15))
    _cites(_check(forty_one, forty), Rule.FUNCTIONAL_KEY)
    # Non-overlapping fluent values (he moved) never contradict.
    moved = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.BOSTON, vs.years(2014, 2016))
    assert _check(moved, canon).kind is VerdictKind.NOVEL


@pytest.mark.part("M4", "FR-PROP-3,10")
def test_u_15__inverse_cardinality() -> None:
    """#439: an inverse predicate's cardinality applies to the claim's predicate. ``held_by``
    (the inverse of ``holds``, stored as ``holds``) is functional, so ``holds(Ann, key)`` while
    canon has ``holds(Cal, key)`` over overlapping time is ``contradicted`` (``functional_key``),
    citing Cal's record; so is the claim in its ``held_by`` form. An author's inverse with a
    maximum of 2 riders makes a third rider ``contradicted`` (``max_cardinality``).
    Complements: a holder after Cal gave the key up, Cal's own claim, a negated claim, a
    share of a stock (``quantity``, which ``held_by`` does not declare) and an inverse without a
    maximum (weak) never contradict."""
    holds, held_by = CORE_BY_NAME["holds"].id, CORE_BY_NAME["held_by"].id
    key = vs.DOOR  # any entity id serves as the item here
    cal_has_it = vs.prop(vs.CAL, holds, key, vs.span(0, 100))
    ann = vs.prop(vs.ANN, holds, key, vs.span(50, 60))
    found = _check(ann, cal_has_it)
    _cites(found, Rule.FUNCTIONAL_KEY)
    (cited,) = found.evidence
    assert cited.rule is Rule.FUNCTIONAL_KEY
    _cites(_check(vs.prop(key, held_by, vs.ANN, vs.span(50, 60)), cal_has_it), Rule.FUNCTIONAL_KEY)
    # She has it once he no longer does: no overlap, no conflict.
    after = vs.prop(vs.ANN, holds, key, vs.span(100, 200))
    assert _check(after, cal_has_it).kind is VerdictKind.NOVEL
    assert _check(vs.prop(vs.CAL, holds, key, vs.span(10, 20)), cal_has_it).kind is (
        VerdictKind.SUPPORTED
    )
    assert _check(ann.negation(), cal_has_it).kind is VerdictKind.NOVEL
    # Another item of Cal's is no conflict either (the inverse's key is the item).
    assert _check(vs.prop(vs.ANN, holds, vs.HOUSE, vs.span(50, 60)), cal_has_it).kind is (
        VerdictKind.NOVEL
    )
    # Coins split between holders: a quantity is a share, not the item in one pair of hands.
    coins = vs.prop(vs.CAL, holds, key, vs.span(0, 100), qualifiers={"quantity": "30 silver"})
    share = vs.prop(vs.ANN, holds, key, vs.span(50, 60), qualifiers={"quantity": "10 silver"})
    assert _check(share, coins).kind is VerdictKind.NOVEL
    assert _check(share, cal_has_it).kind is VerdictKind.NOVEL
    # An author's ``rides``/``ridden_by``: a horse carries at most two riders at a time.
    rides, ridden_by, weak_by = (
        "01JP000000000000000000000A",
        "01JP000000000000000000000B",
        "01JP000000000000000000000C",
    )
    extra = (
        Predicate(id=rides, story_id=vs.STORY, name="rides", layer=Layer.AUTHOR),
        Predicate(
            id=ridden_by,
            story_id=vs.STORY,
            name="ridden_by",
            layer=Layer.AUTHOR,
            inverse_of=rides,
            max_cardinality=2,
        ),
    )
    reg = vs.registry_of(extra=extra)
    horse = vs.HOUSE
    riders = [vs.prop(r, rides, horse, vs.span(0, 10)) for r in (vs.ANN, vs.CAL)]
    third = vs.prop(vs.SMITH, rides, horse, vs.span(5, 6))
    assert _check(third, riders[0], reg=reg).kind is VerdictKind.NOVEL
    _cites(_check(third, *riders, reg=reg), Rule.MAX_CARDINALITY)
    # The same pair with a weak inverse (no maximum) says nothing about the count.
    weak = (
        extra[0],
        Predicate(
            id=weak_by, story_id=vs.STORY, name="ridden_by", layer=Layer.AUTHOR, inverse_of=rides
        ),
    )
    assert _check(third, *riders, reg=vs.registry_of(extra=weak)).kind is VerdictKind.NOVEL


@pytest.mark.part("M4", "FR-PROP-8,10")
def test_u_15__key_scoping() -> None:
    """``degree_awarded`` is keyed by (person, degree): deg_A by {OSU} and deg_B by {Florida}
    do not conflict, while deg_A by {Florida} is ``contradicted``; a joint degree {OSU, Florida}
    is supported by a claim of that set and does not conflict with a second degree; a non-key
    qualifier (the field) that differs creates no key conflict."""
    when = Instant.of(vs.year(2014), Granularity.DAY)

    def degree(degree_id: str, *schools: str, **extra: Any) -> Proposition:
        return vs.prop(
            vs.BOBBY,
            vs.DEGREE,
            object_set=schools,
            when=when,
            qualifiers={"degree_id": degree_id, **extra},
        )

    deg_a = degree("deg_A", vs.OSU, field="history")
    assert _check(degree("deg_B", vs.FLORIDA), deg_a).kind is VerdictKind.NOVEL
    _cites(_check(degree("deg_A", vs.FLORIDA), deg_a), Rule.FUNCTIONAL_KEY)
    joint = degree("deg_J", vs.OSU, vs.FLORIDA)
    assert _check(degree("deg_J", vs.OSU, vs.FLORIDA), joint).kind is VerdictKind.SUPPORTED
    assert _check(degree("deg_B", vs.OSU), joint).kind is VerdictKind.NOVEL
    other_field = _check(degree("deg_A", vs.OSU, field="law"), deg_a)
    assert other_field.kind is not VerdictKind.CONTRADICTED


@pytest.mark.part("M4", "FR-PROP-8,10")
def test_u_15__closed_sets_and_cardinality() -> None:
    """A new member of a declared closed set over overlapping time is ``contradicted`` (rule
    ``closed_set``, citing the set); the same claim with no closed set is ``novel`` (the world is
    open). A ``multi`` predicate's new object is ``novel``, and one beyond its declared maximum
    is ``contradicted`` (``max_cardinality``)."""
    osu = vs.prop(vs.BOBBY, vs.ENROLLED_AT, vs.OSU, vs.years(2010, 2014))
    florida = vs.prop(vs.BOBBY, vs.ENROLLED_AT, vs.FLORIDA, vs.years(2012, 2013))
    only_osu = ClosedSetRecord(
        "01JC0000000000000000000001",
        vs.ENROLLED_AT,
        vs.BOBBY,
        (vs.OSU,),
        {},
        vs.year(2010),
        vs.year(2014),
    )
    closed = _check(florida, osu, closed=(only_osu,))
    _cites(closed, Rule.CLOSED_SET)
    assert closed.cited() == (only_osu.id,)
    assert _check(florida, osu).kind is VerdictKind.NOVEL
    # Outside the closed set's interval the set says nothing.
    later = vs.prop(vs.BOBBY, vs.ENROLLED_AT, vs.FLORIDA, vs.years(2015, 2016))
    assert _check(later, osu, closed=(only_osu,)).kind is VerdictKind.NOVEL
    friends = [vs.prop(vs.BOBBY, vs.FRIENDS_LIMITED, f, vs.span(0, 10)) for f in (vs.ANN, vs.CAL)]
    third = vs.prop(vs.BOBBY, vs.FRIENDS_LIMITED, vs.SMITH, vs.span(5, 6))
    assert _check(third, friends[0]).kind is VerdictKind.NOVEL
    _cites(_check(third, *friends), Rule.MAX_CARDINALITY)
    assert _check(third.model_copy(update={"valid_time": vs.span(20, 30)}), *friends).kind is (
        VerdictKind.NOVEL
    )


@pytest.mark.part("M4", "FR-PROP-10,11")
def test_u_15__exclusion_and_implication() -> None:
    """``alive``/``dead`` over overlapping time is ``contradicted`` (``exclusion``, either
    direction). Negating a proposition implied under the author's rule ``member_of(p, varsity
    team of X) ⇒ enrolled_at(p, X)`` is ``contradicted`` (``implication``); without the declared
    rule the same claim is ``novel``. The closure stops at the configured depth and the stop is
    flagged in the evidence."""
    alive = vs.prop(vs.BOBBY, vs.ALIVE, when=vs.span(0, None))
    dead = vs.prop(vs.BOBBY, vs.DEAD, when=vs.span(100, None))
    _cites(_check(dead, alive), Rule.EXCLUSION)
    _cites(_check(alive, dead), Rule.EXCLUSION)
    young = vs.prop(vs.BOBBY, vs.ALIVE, when=vs.span(0, 100))
    assert _check(young, dead).kind is VerdictKind.NOVEL
    member = vs.prop(vs.BOBBY, vs.MEMBER_OF, vs.TEAM, vs.span(0, 50))
    part = vs.prop(vs.TEAM, vs.PART_OF, vs.OSU)
    denied = vs.prop(vs.BOBBY, vs.ENROLLED_AT, vs.OSU, vs.span(10, 20), negated=True)
    ruled = vs.registry_of(vs.VARSITY_RULE)
    implied = _check(denied, member, part, reg=ruled)
    _cites(implied, Rule.IMPLICATION)
    assert len(implied.cited()) == 2  # the membership and the team's school
    assert _check(denied, member, part).kind is VerdictKind.NOVEL
    chain = vs.registry_of(
        Implication(when=vs.STEP_A, then=vs.STEP_B),
        Implication(when=vs.STEP_B, then=vs.STEP_C),
        Implication(when=vs.STEP_C, then=vs.ENROLLED_AT),
    )
    start = vs.prop(vs.BOBBY, vs.STEP_A, vs.OSU, vs.span(0, 50))
    capped = _check(denied, start, reg=chain, implication_max_depth=2)
    assert capped.kind is VerdictKind.NOVEL
    assert capped.depth_capped
    deep = _check(denied, start, reg=chain, implication_max_depth=3)
    _cites(deep, Rule.IMPLICATION)
    assert not deep.depth_capped


@pytest.mark.part("M4", "FR-PROP-10")
def test_u_15__weak_predicate() -> None:
    """The auto-registered ``apprenticed_with(Bobby, blacksmith_1)`` affirmed vs negated over
    overlapping time is ``contradicted`` (direct polarity, a logical conflict), while a second
    apprenticeship with a different smith is ``novel``. Asked for conflicts beyond polarity,
    the same claim is ``uncertain(weak_semantics)``, never ``contradicted``."""
    first = vs.prop(vs.BOBBY, vs.APPRENTICED, vs.SMITH, vs.span(0, 100))
    _cites(_check(first.negation(), first), Rule.POLARITY)
    second = vs.prop(vs.BOBBY, vs.APPRENTICED, vs.SMITH_2, vs.span(50, 60))
    assert _check(second, first).kind is VerdictKind.NOVEL
    strict = _check(second, first, strict_weak=True)
    assert (strict.kind, strict.reason) == (VerdictKind.UNCERTAIN, VerdictReason.WEAK_SEMANTICS)


@pytest.mark.part("M4", "FR-PROP-1,10")
def test_u_15__approximate_bounds() -> None:
    """Approximate bounds: an overlap no admissible reading can decide is ``uncertain``, and so
    is a claim whose outer bounds might extend past canon's inner bounds. Complement: a claim
    whose outer bounds sit inside canon's inner bounds is supported."""
    canon = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.approx((0, 10), (90, 100)))
    maybe_negated = vs.prop(
        vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.approx((95, 98), (110, 120)), negated=True
    )
    undecided = _check(maybe_negated, canon)
    assert (undecided.kind, undecided.reason) == (
        VerdictKind.UNCERTAIN,
        VerdictReason.APPROXIMATE_BOUNDS,
    )
    past = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.approx((5, 20), (80, 95)))
    assert _check(past, canon).kind is VerdictKind.UNCERTAIN
    inside = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.approx((10, 20), (80, 90)))
    assert _check(inside, canon).kind is VerdictKind.SUPPORTED


def _context() -> ResolutionContext:
    return ResolutionContext(
        EntityIndex(
            [
                EntityEntry(vs.BOBBY, "person", "Bobby", "masculine"),
                EntityEntry(vs.OHIO, "place", "Ohio"),
                EntityEntry(vs.SMITH, "person", "Smith", "masculine"),
                EntityEntry(vs.OSU, "organization", "Ohio State", aliases=(("OSU", "detected"),)),
            ]
        )
    )


@pytest.mark.part("M4", "FR-PROP-8,9,12")
def test_u_15__normalization() -> None:
    """Normalization (FR-PROP-12): names resolve through #139 and the relation through the
    registry or the alias table; "last week" resolves against the claim's position. An
    unregistered predicate on a detected claim, an unresolved entity, an unresolvable time and a
    value that does not fit the argument type are each ``uncertain`` with their reason, never a
    guess; a declared claim with an unregistered relation asks to auto-register it. The absence
    of any record is ``novel`` (open world)."""
    ctx, basis = (
        _context(),
        TimeBasis(minute_zero=vs.year(2020), epoch=datetime(2000, 1, 1, tzinfo=UTC)),
    )
    sketch = ClaimSketch(subject_text="Bobby", relation_text="lives in", object_text="Ohio")
    found = normalize(sketch, NARRATOR, 3, registry=vs.REGISTRY, context=ctx, basis=basis)
    assert isinstance(found, Proposition)
    assert (found.subject_id, found.predicate_id, found.object_id) == (
        vs.BOBBY,
        vs.LOCATED_AT,
        vs.OHIO,
    )
    assert found.valid_time == Interval.of(story_time_at(3, basis), None)
    assert check(found, [], registry=vs.REGISTRY).kind is VerdictKind.NOVEL
    weeks = [
        normalize(
            ClaimSketch(
                subject_text="Bobby",
                relation_text="enrolled at",
                object_text="OSU",
                time_text="last week",
            ),
            NARRATOR,
            position,
            registry=vs.REGISTRY,
            context=ctx,
            basis=basis,
        )
        for position in (2, 2 + 7 * 144)
    ]
    assert all(isinstance(w, Proposition) for w in weeks)
    first, second = (w.valid_time.possible() for w in weeks if isinstance(w, Proposition))
    assert first[0] is not None
    assert second[0] is not None
    assert second[0] - first[0] == 7 * 24 * 60
    cases = [
        (
            ClaimSketch(subject_text="Bobby", relation_text="hums near", object_text="Ohio"),
            VerdictReason.UNRESOLVED_PREDICATE,
        ),
        (
            ClaimSketch(subject_text="Zed", relation_text="lives in", object_text="Ohio"),
            VerdictReason.UNRESOLVED_ENTITY,
        ),
        (
            ClaimSketch(
                subject_text="Bobby",
                relation_text="lives in",
                object_text="Ohio",
                time_text="once upon a time",
            ),
            VerdictReason.UNRESOLVED_TIME,
        ),
        (
            ClaimSketch(subject_text="Bobby", relation_text="age", object_text="forty"),
            VerdictReason.ARGUMENT_TYPE,
        ),
        (
            ClaimSketch(subject_text="Ohio", relation_text="enrolled at", object_text="OSU"),
            VerdictReason.ARGUMENT_TYPE,
        ),
    ]
    for case, reason in cases:
        found = normalize(case, NARRATOR, 3, registry=vs.REGISTRY, context=ctx, basis=basis)
        assert isinstance(found, Unnormalizable), case
        assert found.reason is reason, (case, found)
        assert found.verdict().kind is VerdictKind.UNCERTAIN
        assert found.verdict().reason is reason
    declared = normalize(
        ClaimSketch(
            subject_text="Bobby", relation_text="hums near", object_text="Ohio", declared=True
        ),
        NARRATOR,
        3,
        registry=vs.REGISTRY,
        context=ctx,
        basis=basis,
    )
    assert declared == RegisterPredicate("hums near", "person", "place")
    # A claim whose predicate the registry does not hold is uncertain at check time too.
    stray = vs.prop(vs.BOBBY, "01JP00000000000000000000ZZ", vs.OHIO)
    assert check(stray, [], registry=vs.REGISTRY).reason is VerdictReason.UNRESOLVED_PREDICATE


@pytest.mark.part("M4", "FR-PROP-2,10")
def test_u_15__holders() -> None:
    """The same proposition shape works for canon and utterance holders: the speaker's own
    earlier utterance supports or contradicts a claim exactly as canon would."""
    said = _ohio(2010, 2014)
    for holder in (CANON, Holder.utterance(vs.ANN)):
        found = check(said.negation(), vs.records(said), registry=vs.REGISTRY, holder=holder)
        _cites(found, Rule.POLARITY)
        assert found.holder == holder
        assert check(
            _ohio(2011, 2012), vs.records(said), registry=vs.REGISTRY, holder=holder
        ).kind is (VerdictKind.SUPPORTED)
