"""S-MODELING: interaction-triggered modeling (docs/04 §7; FR-EPI-9,10; #163).

Wren (the player) and Jenni (the rendered character) are on the street, played end to end
through ``respond`` with the stub adapter under per-character partitions (perception ``full``,
model salience):

- turn 1: Jenni waves to the neighbor (an interaction: the neighbor is modeled, a ``minor``
  with an empty partition that the wave lands in), watches a jogger run past and passes the
  jogger on the path (mere appearance, the motion sense of "passes": no ``characters`` row, no
  entity, no partition; #543) and notices the old man on the bench, whom
  the salience step marks as a character (modeled, not yet interacted with: his partition stays
  empty). Each modeled character's true age comes from the turn's age repair
  (``true_age_source = repaired``), its ``definition`` is the triggering beat and it is placed
  in the scene; the turn's report lists them by trigger, and ``character_modeled`` events record
  them;
- turns 2 and 3: Jenni hands the delivery driver a tip (modeled), then thanks him again (a
  ``say`` addressed to him): one persistent entity (one ``characters`` row) whose sparse
  partition accrues a memory from each interaction and nothing else (not Jenni's coffee);
- sparse, low-weight machinery: every modeled character is ``minor``, the omniscient story read
  shows its weight and ``modeled_at`` (Jenni ``major`` at position 0), a minor holds no memory
  of a beat it did not take part in, and the author can promote one to ``major``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.structured.render import DEFAULT_REPAIRED_AGE
from tests import modeling_support as ms
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, render, turn_report

pytestmark = pytest.mark.integration

WAVE = "Jenni waves to the neighbor across the street."
JOGGER = "Jenni watches a jogger run past."
BENCH = "Jenni notices the old man on the bench."
PASSES = "Jenni passes the jogger on the path."
TIP = "Jenni hands the delivery driver a tip."
COFFEE = "Jenni sips her coffee."
AGAIN = "Thanks again, see you tomorrow!"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _turn(
    client: Any, epistrel: Any, story: str, ids: dict[str, str], answer: str, sal: str
) -> None:
    stub = epistrel.models.stub
    stub.reset()
    stub.script("generation", [answer])
    stub.script("salience_detection", [sal])
    response = await respond(client, story, body(ids["Wren"], "Morning!"))
    assert response.status_code == 200, response.text


@pytest.mark.part("M5", "FR-EPI-9,10")
async def test_s_modeling(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring. Complement: the merely-seen jogger has no ``characters`` row,
    no entity and no partition."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, place = await ms.street(client, engine)
        await _turn(
            client,
            epistrel,
            story,
            ids,
            render(beat("do", WAVE), beat("do", JOGGER), beat("do", BENCH), beat("do", PASSES)),
            ms.salient_characters("the old man on the bench"),
        )
        cast = await ms.characters_of(engine, story)
        assert set(cast) == {"Wren", "Jenni", "the neighbor", "the old man on the bench"}
        neighbor, old_man = cast["the neighbor"], cast["the old man on the bench"]
        for row, beat_no, text in ((neighbor, 2, WAVE), (old_man, 4, BENCH)):
            assert row.weight == "minor"
            assert (row.true_age, row.true_age_source) == (DEFAULT_REPAIRED_AGE, "repaired")
            assert (row.modeled_turn, row.modeled_beat) == (1, beat_no)
            assert row.definition == text
        assert cast["Jenni"].weight == cast["Wren"].weight == "major"
        report = await turn_report(engine, story)
        assert [(m["character_id"], m["trigger"]) for m in report["modeling"]["modeled"]] == [
            (neighbor.id, "interaction"),
            (old_man.id, "salience"),
        ]
        assert report["modeling"]["deferred"] == []
        events = await ms.modeled_events(engine, story)
        assert [(e.payload["trigger"], e.payload["entity_id"], e.category) for e in events] == [
            ("interaction", neighbor.id, "derived"),
            ("salience", old_man.id, "derived"),
        ]
        # Each is placed in the scene and is an ordinary character's entity.
        placed = await rows(
            engine,
            "SELECT p.subject_id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.predicate_id = '01JPRED0000000000000000002' "
            "AND p.object_id = :place",
            s=story,
            place=place,
        )
        assert {neighbor.id, old_man.id} <= {str(r.subject_id) for r in placed}
        # The wave landed in the neighbor's new partition; the jogger and the bench did not.
        assert await ms.memory_texts(engine, story, neighbor.id) == [(1, f"Jenni does: {WAVE}")]
        assert await ms.memory_texts(engine, story, old_man.id) == []
        jenni = [t for _, t in await ms.memory_texts(engine, story, ids["Jenni"])]
        assert {f"Jenni does: {t}" for t in (WAVE, JOGGER, BENCH, PASSES)} <= set(jenni)
        # Complement: appearance models nothing.
        assert not [n for n in cast if "jogger" in n]
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM entities WHERE story_id = :s AND canonical_name ILIKE "
                "'%jogger%'",
                s=story,
            )
            == 0
        )
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM memories m JOIN characters c "
                "ON c.id = m.holder_character_id WHERE m.story_id = :s AND c.name ILIKE "
                "'%jogger%'",
                s=story,
            )
            == 0
        )

        # The recurring driver: modeled at the tip, the same character at the thanks.
        await _turn(
            client,
            epistrel,
            story,
            ids,
            render(beat("do", TIP), beat("do", COFFEE)),
            ms.salient_characters(),
        )
        driver_id = (await ms.characters_of(engine, story))["the delivery driver"].id
        await _turn(
            client,
            epistrel,
            story,
            ids,
            render(beat("say", AGAIN, intent="sincere", addressee=driver_id)),
            ms.salient_characters(),
        )
        cast = await ms.characters_of(engine, story)
        drivers = [r for n, r in cast.items() if "driver" in n]
        assert [r.name for r in drivers] == ["the delivery driver"]
        driver = drivers[0]
        assert (driver.weight, driver.modeled_turn, driver.modeled_beat) == ("minor", 2, 2)
        assert await ms.memory_texts(engine, story, driver.id) == [
            (2, f"Jenni does: {TIP}"),
            (3, f"Jenni says: {AGAIN}"),
        ]
        # The neighbor took part in nothing since: its partition did not grow.
        assert await ms.memory_texts(engine, story, neighbor.id) == [(1, f"Jenni does: {WAVE}")]

        # The omniscient read shows weight and modeled_at; the author promotes the driver.
        read = await client.get(
            f"/stories/{story}", params={"viewpoint": "omniscient"}, headers=WRITE
        )
        assert read.status_code == 200, read.text
        listed = {c["name"]: c for c in read.json()["characters"]}
        assert listed["the delivery driver"]["weight"] == "minor"
        assert listed["the delivery driver"]["modeled_at"] == {
            "turn": 2,
            "variant": 1,
            "slot_k": 0,
            "beat": 2,
            "step": 0,
        }
        assert listed["Jenni"]["weight"] == "major"
        assert listed["Jenni"]["modeled_at"]["turn"] == 0
        promoted = await client.post(
            f"/stories/{story}/characters/{driver.id}/weight",
            json={"weight": "major"},
            headers=WRITE,
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["character"]["weight"] == "major"
        assert (await ms.characters_of(engine, story))["the delivery driver"].weight == "major"
