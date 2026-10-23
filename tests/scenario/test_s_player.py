"""S-PLAYER, #159's slice: the player is not modeled internally (FR-EPI-12; docs/04 §7).

The player character (``characters.is_player``) has no belief partition: a belief written for it
is refused (422 ``holder_has_no_partition``) and nothing is written, while other characters hold
beliefs about the player as usual. A viewpoint-scoped retrieval never runs for the player's
interiority: ``retrieve(viewpoint=character:<player>)`` reads ``objective`` and ``public`` records
only, so even a record scoped to the player's character is not returned and no partition is read.
#161 appends the ``think`` routing slice, #166 the skipped belief verification of a player's
``respond``, #163 the modeling slice.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import MemoryKind
from epistrel.core.epistemic import partitions
from epistrel.core.epistemic.errors import HolderHasNoPartition
from epistrel.core.events import Operation
from epistrel.core.membership import insert_derived
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.retrieval import pipeline
from epistrel.core.tables.memories import memories
from epistrel.core.visibility import Visibility
from epistrel.testing.fixtures import scale
from tests import canon_support as c
from tests import partition_support as ps
from tests.event_support import scalar
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration


@pytest.mark.part("M5", "FR-EPI-12")
async def test_s_player(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cal is the player. A belief held by Cal is refused before anything is written; Bobby's
    belief about Cal is stored. Retrieval as Cal returns the shared lantern memories only: not
    Bobby's private one, not the one scoped to Cal's character, no canon fact scoped to Cal, and
    no beliefs. Complements: ``is_player`` is false for Bobby, and Bobby's own retrieval does
    return his private memory and his belief."""
    ps.deliver_partitions(monkeypatch)
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    cal, bobby = w.characters["Cal"], w.characters["Bobby"]
    people = w.people

    async with engine.connect() as conn:
        assert await partitions.is_player(conn, story, cal)
        assert not await partitions.is_player(conn, story, bobby)

    in_kitchen = c.at(people["Cal"], w.places["kitchen"], 0, None)
    with pytest.raises(HolderHasNoPartition) as refused:
        await ps.believe(engine, story, cal, in_kitchen)
    assert (refused.value.code, refused.value.http_status) == ("holder_has_no_partition", 422)
    assert refused.value.details["holder"] == cal
    assert await ps.belief_rows(engine, story) == []
    events = "SELECT count(*) FROM events WHERE story_id = :s AND type = 'belief_committed'"
    assert await scalar(engine, events, s=story) == 0

    # Others hold beliefs about the player.
    about_cal = await ps.believe(engine, story, bobby, in_kitchen)
    assert about_cal.holder_character_id == bobby
    assert about_cal.proposition.subject_id == people["Cal"]

    scoped = Visibility(scope="characters", characters=(cal,))
    await c.commit(
        engine,
        story,
        Proposition(
            subject_id=people["Bobby"],
            predicate_id=c.KNOWS,
            object_id=people["Ann"],
            valid_time=Interval.of(None, None),
        ),
        visibility=scoped,
    )
    ids: dict[str, str] = {}

    async def remember(conn: AsyncConnection, op: Operation) -> None:
        for name, line, visibility in (
            ("shared", "Bobby says: the lantern is lit.", Visibility(scope="public")),
            ("narration", "The lantern swings beside Bobby.", Visibility()),
            (
                "bobby",
                "Bobby thinks: Cal must not find the lantern.",
                Visibility(scope="characters", characters=(bobby,)),
            ),
            ("cal", "Cal glimpses the lantern behind Bobby.", scoped),
        ):
            row = scale.memory_row(
                story_id=story,
                operation_id=op.operation_id,
                line=line,
                turn=op.narrative_turn,
                kind=MemoryKind.EPISODIC,
                participants=[people["Bobby"], people["Cal"]],
            )
            row["visibility"] = visibility.document()
            await insert_derived(conn, memories, [row])
            ids[name] = str(row["id"])

    await c.run(engine, story, remember)
    settings = await ps.partitioned(engine, story)

    async def ask(character: str) -> pipeline.RetrievalResult:
        async with engine.begin() as conn:
            return await pipeline.retrieve(
                conn,
                story,
                pipeline.RetrievalQuery("Is Cal near Bobby's lantern?", actor=character),
                viewpoint=f"character:{character}",
                settings=settings,
                touch=False,
            )

    as_player = await ask(cal)
    returned = {m.id for m in (*as_player.episodes, *as_player.assessments, *as_player.memories)}
    assert returned == {ids["shared"], ids["narration"]}
    assert all(f.visibility.scope in ("objective", "public") for f in as_player.canon)
    assert as_player.beliefs == ()

    as_bobby = await ask(bobby)
    seen = {m.id for m in (*as_bobby.episodes, *as_bobby.assessments, *as_bobby.memories)}
    assert ids["bobby"] in seen
    assert ids["cal"] not in seen
    assert [b.id for b in as_bobby.beliefs] == [about_cal.id]
