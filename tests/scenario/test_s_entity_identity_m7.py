"""S-ENTITY-IDENTITY, the M7 part (docs/04 §7; FR-PROP-7, FR-DIRECT-8; #225): the author makes
the reveal through the side-channel.

The M5 house (``test_s_entity_identity_m5._house``): Wren and Jenni in the parlor, Bobby on the
porch, "the stranger" an entity of its own; canon holds that Jenni knows Bobby and Jenni holds
that she does not know the stranger. Jenni says "I've never met that stranger." (sincere,
consistent with her beliefs, unresolved in canon).

``POST /stories/{id}/entities/merge`` with Bobby and the stranger, ``best_effort`` +
``apply_directly``: the merge is an amendment, applied at once. Canon now holds
``same_as(Bobby, stranger)`` (written by the amendment's part), and the pair's identity
proposal is approved. What Jenni said keeps its text, intent and belief relation, while its
objective relation is ``false`` in canon (she knows Bobby), a sincere error, and the author is
notified. Complements: Jenni's belief partition gains no ``same_as``; merging the pair again is
422 ``merge_invalid`` (``already_same``), and one entity alone is ``too_few``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import rewrite_support as rs
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.scenario.test_s_entity_identity_m5 import NEVER, SAME_AS, _house
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, render

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _merge(client: Any, story: str, ids: list[str]) -> Any:
    return await client.post(
        f"/stories/{story}/entities/merge",
        params=rs.OMNISCIENT,
        json={"entity_ids": ids, "resolution_mode": "best_effort", "preview": "apply_directly"},
        headers=WRITE,
    )


@pytest.mark.part("M7", "FR-PROP-7; FR-DIRECT-8")
async def test_s_entity_identity__m7(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    try:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            story, ids = await _house(client, engine)
            jenni, bobby, stranger = ids["Jenni"], ids["Bobby"], ids["the stranger"]
            stub = epistrel.models.stub
            stub.reset()
            stub.script(
                "generation",
                [
                    render(
                        beat(
                            "say",
                            NEVER,
                            intent="sincere",
                            propositions=[claim("I", "knows", "the stranger", negated=True)],
                        )
                    )
                ],
            )
            await played(
                client,
                story,
                body(ids["Wren"], "Jenni, who was that?", render_character_ids=[jenni]),
            )
            (line,) = await rows(
                engine,
                "SELECT u.id, u.intent, u.belief_relation, u.objective_relation, e.payload "
                "FROM utterances u JOIN events e ON e.id = u.event_id "
                "WHERE u.story_id = :s AND u.speaker_character_id = :j",
                s=story,
                j=jenni,
            )
            assert line.objective_relation != "false"

            merged = await _merge(client, story, [bobby, stranger])
            assert merged.status_code == 201, merged.text
            body_ = merged.json()
            assert (body_["kind"], body_["state"]) == ("entity_merge", "applied")
            report = body_["apply"]["report"]

            identity = await rows(
                engine,
                "SELECT f.amendment_part_id FROM facts f JOIN propositions p "
                "ON p.id = f.proposition_id WHERE f.story_id = :s AND p.predicate_id = :sa "
                "AND p.subject_id = :b AND p.object_id = :x AND f.superseded_by IS NULL",
                s=story,
                sa=SAME_AS,
                b=bobby,
                x=stranger,
            )
            assert identity
            assert identity[0].amendment_part_id is not None
            approved = await rows(
                engine,
                "SELECT status FROM predicate_proposals WHERE story_id = :s AND kind = 'identity'",
                s=story,
            )
            assert {r.status for r in approved} == {"approved"}

            (after,) = await rows(
                engine,
                "SELECT u.intent, u.belief_relation, u.objective_relation, u.classification, "
                "e.payload FROM utterances u JOIN events e ON e.id = u.event_id WHERE u.id = :u",
                u=line.id,
            )
            assert (after.intent, after.belief_relation) == (line.intent, line.belief_relation)
            assert after.payload["text"] == line.payload["text"] == NEVER
            assert (after.objective_relation, after.classification) == ("false", "sincere_error")
            noticed = [
                n["utterance_id"]
                for n in report["notifications"]
                if n["kind"] == "scene_meaning_may_change"
            ]
            assert noticed == [str(line.id)]

            # Complements: Jenni holds no identity; the pair cannot merge twice.
            held = await rows(
                engine,
                "SELECT b.id FROM beliefs b JOIN propositions p ON p.id = b.proposition_id "
                "WHERE b.story_id = :s AND b.holder_character_id = :j AND p.predicate_id = :sa",
                s=story,
                j=jenni,
                sa=SAME_AS,
            )
            assert held == []
            again = await _merge(client, story, [bobby, stranger])
            assert again.status_code == 422, again.text
            assert again.json()["error"]["details"]["reason"] == "already_same"
            alone = await _merge(client, story, [bobby])
            assert alone.json()["error"]["details"]["reason"] == "too_few"
    finally:
        await ts.forget(engine)


@pytest.mark.part("M7", "FR-PROP-7; FR-DIRECT-8")
async def test_s_entity_identity__m7_places(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#481: "the tavern" and, a turn later, "the inn" share no word and are minted as two
    places; the second turn's report names "the inn" a duplicate candidate (the scene's location
    is untracked; a notice: nothing merged). The author merges them (``best_effort``, applied at
    once): canon then reads one place, each name answering for the other's facts, and undoing
    the merge restores the two places' facts exactly."""
    from epistrel.core.canon import facts as canon
    from epistrel.core.propositions.vocabulary import LOCATED_AT
    from tests.detection_support import claims_answer
    from tests.integration.test_introduced_entities import extracted
    from tests.structured_turn_support import structured_story, turn_report

    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren", "Ada")
        stub = epistrel.models.stub
        for who, text, place in (
            ("Maren", "Maren steps into the tavern.", "the tavern"),
            ("Ada", "Ada waits by the door of the inn.", "the inn"),
        ):
            stub.reset()
            stub.script("generation", [render(beat("do", text))])
            stub.script(
                "extraction_backstop",
                [claims_answer(extracted(text, who, "located_at", place))],
            )
            await played(
                client, story, body(ids["Wren"], "And then?", render_character_ids=[ids[who]])
            )
        places = {
            r.canonical_name: str(r.id)
            for r in await rows(
                engine,
                "SELECT id, canonical_name FROM entities WHERE story_id = :s AND kind = 'place'",
                s=story,
            )
        }
        inn, tavern = places["the inn"], places["the tavern"]
        notices = (await turn_report(engine, story))["commit"].get("duplicate_candidates", [])
        # The scene's location is untracked (Wren's place is unknown): "the inn" may be it.
        assert {(n["entity_id"], n["reason"]) for n in notices} >= {
            (inn, "untracked_scene_location")
        }

        async def at(place: str) -> set[str]:
            async with engine.connect() as conn:
                found = await canon.canon(conn, story, predicate=LOCATED_AT, object=place)
            return {f.proposition.subject_id for f in found if f.valid_to is None}

        before = (await at(inn), await at(tavern))
        assert before[0] != before[1]
        merged = await _merge(client, story, [tavern, inn])
        assert merged.status_code == 201, merged.text
        amendment = merged.json()["apply"]["amendment_id"]
        assert await at(inn) == await at(tavern) == before[0] | before[1]
        undone = await client.post(
            f"/stories/{story}/amendments/{amendment}/undo",
            params=rs.OMNISCIENT,
            json={"resolution_mode": "best_effort", "preview": "apply_directly"},
            headers=WRITE,
        )
        assert undone.status_code == 201, undone.text
        assert (await at(inn), await at(tavern)) == before
