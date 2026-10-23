"""``core.epistemic.replay.could_have_known`` over the stored records (FR-EPI-4, FR-BELIEF-2;
#165): the paths it finds and the answers it gives. COULD-HAVE-KNOWN's property over random
histories is ``tests/property/test_could_have_known.py``; this file checks the gathering.

The knock story of #146 (Cal the player; Bobby and Jenni modeled; an unmodeled mailman) under
per-character partitions.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.epistemic import partitions, replay, transitions
from epistrel.core.epistemic.replay import Known, NoPath, NotApplicable
from epistrel.core.membership import Position
from epistrel.core.propositions import registry as predicates
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from tests import commit_support as c
from tests import exposure_support as xs
from tests import partition_support as parts
from tests.canon_support import commit
from tests.event_support import rows
from tests.testimony_support import enable_testimony

pytestmark = pytest.mark.integration

LOCATED_AT = CORE_BY_NAME["located_at"].id
FOREVER = Interval.of(None, None)


def knocked(k: c.Knock, who: str) -> Proposition:
    return Proposition(
        subject_id=who, predicate_id=k.knocked_at, object_id=k.door, valid_time=FOREVER
    )


async def ask(
    engine: AsyncEngine, k: c.Knock, who: str | None, p: Proposition, at: Position, **kw: Any
) -> replay.Answer:
    async with engine.connect() as conn:
        return await replay.could_have_known(conn, k.story_id, who, p, at, **kw)


async def _story(engine: AsyncEngine) -> c.Knock:
    k = await c.knock_story(engine)
    await enable_testimony(engine, k.story_id, "defaulted")
    return k


async def test_a_held_belief_is_a_path_from_its_transition_on(
    engine: AsyncEngine, testimony_delivered: None
) -> None:
    """Bobby comes to believe the mailman knocked in turn 1 (an author transition): from then
    on that transition is his path; before it there is none, and the answer names it as the
    nearest. Jenni, who acquired nothing, has no path. A belief in the negation licenses no
    assertion. The player always knows; the narrator and a story without partitions are
    ``NotApplicable``; an unmodeled person has no partition."""
    k = await _story(engine)
    p = knocked(k, k.mailman)
    belief = await parts.believe(engine, k.story_id, k.bobby, p)
    (cause,) = await rows(
        engine, "SELECT id FROM belief_transitions WHERE belief_id = :b", b=belief.id
    )
    after = Position.end_of(belief.held_from.turn)
    found = await ask(engine, k, k.bobby, p, after)
    assert isinstance(found, Known)
    assert (found.path.kind, found.path.ref_id, found.path.transition_kind) == (
        "transition",
        cause.id,
        "author",
    )
    before = await ask(engine, k, k.bobby, p, Position(0))
    assert isinstance(before, NoPath)
    assert before.nearest is not None
    assert before.nearest.ref_id == cause.id
    assert await ask(engine, k, k.jenni, p, after) == NoPath()
    assert isinstance(await ask(engine, k, k.bobby, p.negation(), after), NoPath)
    assert await ask(engine, k, k.cal, p, after) == Known(replay.PathRef("player"))
    assert await ask(engine, k, None, p, after) == NotApplicable("narrator")
    assert await ask(engine, k, k.mailman, p, after) == NoPath()
    off = await parts.partitioned(engine, k.story_id, on=False)
    assert await ask(engine, k, k.bobby, p, after, settings=off) == NotApplicable("partitions_off")


async def test_closed_beliefs_do_not_count_and_level_matters(
    engine: AsyncEngine, testimony_delivered: None
) -> None:
    """A suspected belief licenses a reference at the default level but not at ``believed``
    (#176's); a belief corrected by its negation stops licensing at the correction."""
    k = await _story(engine)
    p = knocked(k, k.mailman)
    suspected = await parts.believe(engine, k.story_id, k.bobby, p, level=partitions.LEVELS[1])
    at = Position.end_of(suspected.held_from.turn)
    assert isinstance(await ask(engine, k, k.bobby, p, at), Known)
    assert isinstance(await ask(engine, k, k.bobby, p, at, min_level=partitions.LEVELS[2]), NoPath)
    corrected = await parts.believe(
        engine,
        k.story_id,
        k.bobby,
        p.negation(),
        cause=transitions.Kind.CORRECTION,
        level=partitions.LEVELS[3],
    )
    later = Position.end_of(corrected.held_from.turn)
    assert isinstance(await ask(engine, k, k.bobby, p, later), NoPath)
    assert isinstance(await ask(engine, k, k.bobby, p, at), Known)  # held then
    assert isinstance(await ask(engine, k, k.bobby, p.negation(), later), Known)


async def test_canon_implication_and_exposure_paths(
    engine: AsyncEngine, testimony_delivered: None, drained_embedding_jobs: None
) -> None:
    """Canon the character may read is known without a belief row; an implication of what it
    holds under the declared rules is a path; an unresolved exposure the default would accept is
    a path, dry-run in its frame with nothing written (the exposure stays unresolved, no belief
    is added)."""
    k = await _story(engine)
    seen = Proposition(
        subject_id=k.parcel, predicate_id=LOCATED_AT, object_id=k.door, valid_time=FOREVER
    )
    fact = await commit(engine, k.story_id, seen, provenance="author")
    at = Position.end_of(9)
    found = await ask(engine, k, k.jenni, seen, at)
    assert found == Known(replay.PathRef("canon", fact.id))

    async with engine.begin() as conn:
        await predicates.register_author(
            conn,
            k.story_id,
            predicates.ScenarioRules.parse(
                {"rules": [{"when": "knocked_at", "then": "located_at"}]}
            ),
        )
    belief = await parts.believe(engine, k.story_id, k.bobby, knocked(k, k.mailman))
    there = Proposition(
        subject_id=k.mailman, predicate_id=LOCATED_AT, object_id=k.door, valid_time=FOREVER
    )
    implied = await ask(engine, k, k.bobby, there, Position.end_of(belief.held_from.turn))
    assert implied == Known(replay.PathRef("implied", belief.id, belief.held_from))

    told = knocked(k, k.jenni)
    exposure_id, _ = await xs.expose(engine, k.story_id, k.bobby, told, source=k.jenni)
    before = await rows(engine, "SELECT id FROM beliefs WHERE story_id = :s", s=k.story_id)
    (exposed,) = await rows(engine, "SELECT * FROM exposures WHERE id = :e", e=exposure_id)
    found = await ask(engine, k, k.bobby, told, Position.end_of(exposed.position_turn))
    assert isinstance(found, Known)
    assert (found.path.kind, found.path.ref_id) == ("exposure", exposure_id)
    assert await rows(engine, "SELECT id FROM beliefs WHERE story_id = :s", s=k.story_id) == before
    assert await xs.state_of(engine, k.story_id, exposure_id) == "unresolved"
    # Complement: before the exposure, the hearing is no path (it is the nearest one).
    early = await ask(engine, k, k.bobby, told, Position(exposed.position_turn - 1))
    assert isinstance(early, NoPath)
    assert early.nearest is not None
    assert early.nearest.ref_id == exposure_id


async def test_a_turns_checks_are_counted_and_memoized(
    engine: AsyncEngine, testimony_delivered: None
) -> None:
    """Within ``checking()`` every answer is counted (``no_path`` apart) and a repeat of the
    same (holder, proposition, position) is answered from the memo."""
    k = await _story(engine)
    p = knocked(k, k.mailman)
    with replay.checking() as checks:
        first = await ask(engine, k, k.jenni, p, Position(3))
        again = await ask(engine, k, k.jenni, p, Position(3))
        await ask(engine, k, k.cal, p, Position(3))
    assert first == again == NoPath()
    assert checks.report() == {"checks": 2, "no_path": 1}
