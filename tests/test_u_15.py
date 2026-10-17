"""U-15: contradiction rules, one at a time (docs/04 §3), its M4 parts.

Every U-15 (M4) part lives in this file (traceability counts a test ID per milestone in one
file). #131's slices cover what the rules stand on: ``test_u_15__valid_time`` the proposition
shape and its valid-time relations (FR-PROP-1,2), ``test_u_15__registry`` closed sets, stored
negations and the bounded implication closure (FR-PROP-8,11). #140 adds the verdict slices
(supported, contradicted, novel, uncertain) and #139 ``test_u_15__resolution_time``.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import (
    Granularity,
    Implication,
    Instant,
    Interval,
    InvalidValidTime,
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
