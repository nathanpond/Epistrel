"""A regression found by P-04's multi-character workload (#182): a sequential turn whose first
primary says something novel that is permitted (and so canonized by the turn's commit), heard by
the second primary.

The second primary's testimony is decided twice: while the turn stages its render (its context
and its staged testimony read canon under its viewpoint) and in the commit step, after the
turn's own canon writes. Both reads went into the attempt's read set, so the commit's read of the
same key answered differently and failed the turn (``ReadSetInconsistent``, a 500). The commit
step now reads through a read set of its own: it runs under the story lock, where nothing but the
turn itself can change what it reads."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.testing import structured_load
from tests import multi_support as ms
from tests import partition_support as parts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.key_room import key_room
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim

pytestmark = pytest.mark.integration

BOBBY_SAYS = "This lantern is mine, and so is the music box."
JENNI_SAYS = "Then keep it somewhere safe."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_a_heard_novel_claim_canonized_by_the_turn_commits(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Bobby (first) says he owns the brass lantern (canon says so) and the music box (a novel
    claim the turn permits and canonizes); Jenni (second, who already believes he owns the
    lantern, so his say is salient to her) hears both and testifies, reading
    Bobby's possessions while the turn stages her render and again in its commit, after the
    music box became canon: the turn commits.

    Complement asserted: the claim is canon after the turn, and Jenni's testimony about it is
    in her partition (a speech act from Bobby)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        items = await structured_load.plant(
            epistrel, RequestContext(principal="p-author"), room.story_id, [room.bobby]
        )
        box = items[structured_load.NOVEL_ITEM]
        # Jenni already believes something of Bobby's: his say touches her partition (salient).
        await parts.believe(
            engine,
            room.story_id,
            room.jenni,
            Proposition(
                subject_id=room.bobby,
                predicate_id=structured_load.OWNS,
                object_id=items[structured_load.PLANTED_ITEM],
                valid_time=Interval.of(None, None),
            ),
            kind=OperationKind.CONTROL,
        )
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(
                beat(
                    "say",
                    BOBBY_SAYS,
                    intent="sincere",
                    propositions=[
                        claim("I", "owns", structured_load.PLANTED_ITEM),
                        claim("I", "owns", structured_load.NOVEL_ITEM),
                    ],
                )
            ),
        )
        ms.renders_for(stub, "Jenni", ms.render(beat("say", JENNI_SAYS, intent="sincere")))
        played = await respond(
            client,
            room.story_id,
            body(room.cal, "What now?", render_character_ids=[room.bobby, room.jenni]),
        )
        assert played.status_code == 200, played.text
        assert played.json()["order"] == [room.bobby, room.jenni]
    owned = await rows(
        engine,
        "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND p.subject_id = :b AND p.object_id = :o",
        s=room.story_id,
        b=room.bobby,
        o=box,
    )
    assert len(owned) == 1
    heard = await rows(
        engine,
        "SELECT id FROM beliefs WHERE story_id = :s AND holder_character_id = :j "
        "AND kind = 'speech_act' AND speaker_character_id = :b",
        s=room.story_id,
        j=room.jenni,
        b=room.bobby,
    )
    assert heard
