"""S-PLAYER, #159's slice: the player is not modeled internally (FR-EPI-12; docs/04 §7).

The player character (``characters.is_player``) has no belief partition: a belief written for it
is refused (422 ``holder_has_no_partition``) and nothing is written, while other characters hold
beliefs about the player as usual. A viewpoint-scoped retrieval never runs for the player's
interiority: ``retrieve(viewpoint=character:<player>)`` reads ``objective`` and ``public`` records
only, so even a record scoped to the player's character is not returned and no partition is read.
#161 appends the ``think`` routing slice; #166's slice (``__verification``) is the skipped belief
verification of a player's ``respond``.

#163's modeling slice (``__modeling``): the player is the one person whose mind the engine
never writes, yet it acts on others like anyone. Wren (the player) waves to the neighbor: the
player's own ``do`` is a direct interaction, so the neighbor is modeled (a ``minor``) and holds a
memory of the wave, while the player's character stays ``major`` (``ck_characters_player_major``),
holds no belief and is never modeled; an author's weight change for it is 422 ``is_player``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import MemoryKind
from epistrel.core.epistemic import partitions
from epistrel.core.epistemic.errors import HolderHasNoPartition
from epistrel.core.events import Operation
from epistrel.core.membership import insert_derived
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.retrieval import pipeline
from epistrel.core.tables.memories import memories
from epistrel.core.visibility import Visibility
from epistrel.testing.fixtures import scale
from tests import canon_support as c
from tests import modeling_support as ms
from tests import partition_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import scalar
from tests.memory_support import use_structured
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import (
    beat,
    render,
    structured_story,
    turn_report,
    utterances,
)

pytestmark = pytest.mark.integration


@pytest.mark.part("M5", "FR-EPI-12")
async def test_s_player(engine: AsyncEngine) -> None:
    """Cal is the player. A belief held by Cal is refused before anything is written; Bobby's
    belief about Cal is stored. Retrieval as Cal returns the shared lantern memories only: not
    Bobby's private one, not the one scoped to Cal's character, no canon fact scoped to Cal, and
    no beliefs. Complements: ``is_player`` is false for Bobby, and Bobby's own retrieval does
    return his private memory and his belief."""
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


WAVE = "waves to the neighbor across the street."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M5", "FR-EPI-12")
async def test_s_player__modeling(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (#163's slice)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, _ = await ms.street(client, engine, salience=None)
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("say", "Who's that?", intent="sincere"))])
        played = await respond(client, story, body(ids["Wren"], None, do=WAVE))
        assert played.status_code == 200, played.text
        cast = await ms.characters_of(engine, story)
        neighbor = cast["the neighbor"]
        assert (neighbor.weight, neighbor.modeled_turn, neighbor.modeled_beat) == ("minor", 1, 1)
        assert (1, f"Wren does: {WAVE}") in await ms.memory_texts(engine, story, neighbor.id)
        # The player is never modeled: major, no partition, no belief.
        assert (cast["Wren"].weight, cast["Wren"].is_player) == ("major", True)
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM beliefs WHERE story_id = :s AND holder_character_id = :c",
                s=story,
                c=ids["Wren"],
            )
            == 0
        )
        refused = await client.post(
            f"/stories/{story}/characters/{ids['Wren']}/weight",
            json={"weight": "minor"},
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["reason"] == "is_player"


TOOK_KEY = "I took the key."


@pytest.mark.part("M5", "FR-EPI-12")
async def test_s_player__verification(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """#166's slice: under per-character partitions, the player's own ``say`` is checked against
    canon only. Its claim gets a verdict but no belief dimension and no classification (the
    utterance's ``classification`` is ``NULL``), and no belief of the player's is read or
    written. Complement: the same input with an NPC actor (Ada, platform-played) does run belief
    verification and is classified."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for actor in ("Wren", "Ada"):
            story, ids = await structured_story(client, "Wren", "Ada", "Brann")
            await ts.enable_testimony(engine, story, "defaulted", level="co_location")
            async with engine.begin() as conn:
                await store.add_entity(conn, story, "item", "the key")
            stub.reset()
            stub.script("generation", [render(beat("say", "Good.", intent="sincere"))])
            played = await respond(
                client,
                story,
                body(ids[actor], TOOK_KEY, render_character_ids=[ids["Brann"]]),
            )
            assert played.status_code == 200, played.text
            report = await turn_report(engine, story)
            verdicts = [v for v in report["verify"]["verdicts"] if v["claim_id"]]
            (said,) = [
                u
                for u in await utterances(engine, story)
                if u.speaker_character_id == ids[actor] and u.event_beat == 1
            ]
            if actor == "Wren":
                assert verdicts, "the player's claim is still checked against canon"
                assert all("speech" not in v for v in verdicts)
                assert "speech" not in report["verify"]
                assert said.classification is None
                assert (
                    await scalar(
                        engine,
                        "SELECT count(*) FROM beliefs WHERE story_id = :s "
                        "AND holder_character_id = :c",
                        s=story,
                        c=ids["Wren"],
                    )
                    == 0
                )
            else:
                spoken = [v["speech"] for v in verdicts if "speech" in v]
                assert spoken, "an NPC actor's say runs belief verification"
                assert said.classification == "sincere"
                assert report["verify"]["speech"]["classified"] == len(spoken)
