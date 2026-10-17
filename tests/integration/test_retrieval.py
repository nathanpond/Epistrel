"""I-05: the resolution pipeline end to end (docs/04 §4), its M4 parts.

#139's slice ``test_i_05__resolution`` (FR-RET-2) covers what #141's retrieval stands on: "those
guys a month ago at the game" resolves its time window against the position's story-time, its
event type, and its participants through recent context and #137's projections; a subject's
timeline is read through #136's keyed lookup. #141 adds ``test_i_05`` (episode, participants,
scoped assessments) beside it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    Interval,
    Proposition,
)
from epistrel.core.propositions.vocabulary import BORN, CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution import context as resolution
from epistrel.core.resolution.entities import resolve_mentions
from epistrel.core.resolution.events import resolve_event_type
from epistrel.core.resolution.time import (
    SubjectTimeline,
    find_time_expressions,
    resolve_time,
    story_time_at,
)
from epistrel.core.resolution.types import MentionKind, ReasonCode, ResolvedTime, Uncertain
from tests import canon_support as c

pytestmark = pytest.mark.integration

DAY = 1440


@pytest.mark.part("M4", "FR-RET-2")
async def test_i_05__resolution(engine: AsyncEngine) -> None:
    """Cal (the player) and Bobby are in the kitchen; Bobby and Ann are siblings; Bobby's
    birth is canon; the last beat says "Bobby and Ann were at the game." Cal's "Those guys a
    month ago at the game: you were there with Bobby's sister." resolves: a window around 30
    days before the position's story-time (approximate, ± 7 days), the event type ``game``,
    "those guys" a group for #141's occasion lookup, "you" Bobby (the one other present),
    "Bobby's sister" Ann (one ``relationships`` hop). "When he was seven" anchors on Bobby's
    birth read through the keyed lookup, which records the query keys. Complement: Ann has no
    birth or age in canon, so the same expression is ``uncertain(no_anchor)``; and with nothing
    resolvable the time is ``uncertain``, not a default."""
    w = await c.world(engine)
    story, cal, bobby, ann = w.story_id, w.people["Cal"], w.people["Bobby"], w.people["Ann"]
    kitchen = w.places["kitchen"]
    await c.commit(engine, story, c.at(cal, kitchen, 0, None))
    await c.commit(engine, story, c.at(bobby, kitchen, 0, None))
    sibling = Proposition(
        subject_id=bobby,
        predicate_id=CORE_BY_NAME["sibling_of"].id,
        object_id=ann,
        valid_time=Interval.of(None, None),
    )
    await c.commit(engine, story, sibling)
    birth = Instant.of(-30 * 365 * DAY, Granularity.DAY)
    await c.commit(
        engine,
        story,
        Proposition(subject_id=bobby, predicate_id=BORN, valid_time=birth),
    )
    op, _ = await c.run(
        engine, story, lambda conn, op: _nothing(), said="Bobby and Ann were at the game."
    )
    turn = int(op.narrative_turn or 0)
    position = (turn + 1, 0)
    text = "Those guys a month ago at the game: you were there with Bobby's sister."
    async with engine.connect() as conn:
        basis = await resolution.load_time_basis(conn, story)
        context = await resolution.load_context(conn, story, position=position, speaker=cal)
        read_set = StructuredReadSet()
        read_set.start_attempt()
        lookup = KeyedLookup(conn, read_set)
        bobby_timeline = await resolution.subject_timeline(lookup, story, bobby)
        ann_timeline = await resolution.subject_timeline(lookup, story, ann)
    # The time window, against the position's story-time.
    (expression,) = find_time_expressions(text)
    window = resolve_time(expression, position, basis=basis)
    assert isinstance(window, ResolvedTime)
    now = story_time_at(position, basis)
    assert window.valid_time.possible() == (now - 37 * DAY, now - 23 * DAY)
    assert window.approximate
    assert window.direction == "past"
    # The event type, then the participants.
    assert resolve_event_type(text) == "game"
    found = resolve_mentions(text, cal, context)
    assert [(m.kind, m.entity_id) for m in found] == [
        (MentionKind.GROUP, None),
        (MentionKind.SECOND_PERSON, bobby),
        (MentionKind.NAME, bobby),
        (MentionKind.DESCRIPTION, ann),
    ]
    assert context.present == frozenset({cal, bobby})
    assert any(b.text == "Bobby and Ann were at the game." for b in context.beats)
    # The subject's timeline through the keyed lookup: its reads are query keys.
    assert bobby_timeline == SubjectTimeline(born=birth)
    assert len(read_set.query_keys) == 4  # born and age, for Bobby and for Ann
    seven = resolve_time("when he was seven", position, bobby_timeline, basis=basis)
    assert isinstance(seven, ResolvedTime)
    assert seven.valid_time.possible()[0] == birth.at + 7 * 525_960
    # Complement: no anchor in canon for Ann, and an unresolvable expression.
    assert ann_timeline == SubjectTimeline()
    missing = resolve_time("when she was seven", position, ann_timeline, basis=basis)
    assert isinstance(missing, Uncertain)
    assert missing.reason.code is ReasonCode.NO_ANCHOR
    assert isinstance(resolve_time("once upon a time", position, basis=basis), Uncertain)


async def _nothing() -> None:
    return None
