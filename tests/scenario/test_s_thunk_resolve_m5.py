"""S-THUNK-RESOLVE (M5): could-have-known and the knowledge obligations (FR-COMMIT-11,14,
FR-EPI-4; docs/04 §7; #165). One file per test ID and milestone: the M4 part is
``tests/scenario/test_s_thunk_resolve.py``.

Structured turns on the stub under per-character partitions. Jenni (the player) and Bobby, a
modeled neighbour, are on Maple Street; Ada is rendered. Jenni's porch is on Maple Street, and the
author rule ``knocked_at(x, place) ⇒ located_at(x, place)`` holds. Turn 1: someone knocks at the
porch, a salient open reference whose "who" stays an open thunk.

- **The soft flag grows with the gap.** Turn 2 binds the old knock to Bobby. He was on Maple
  Street, so nothing recorded contradicts it. He is an existing modeled character who was where
  it happened and has no record of it, so the resolution is flagged, not rejected:
  ``could_have_known_flag`` (``mild``: ten story-minutes), listed on ``/inspect/ledger`` and in
  the degradation. It commits with Bobby's backdated knowledge (``resolution`` transitions held
  from the knock) and his backdated memory of knocking (``provenance = resolution``, his own,
  anchored at turn 1). The next render, and only that one, carries the knowledge steer.
  Regenerating turn 2 after two story-days have passed flags it ``moderate``, and after forty
  ``strong``.
- **A sincere question rejects the binding.** In another story Bobby, asked in turn 2, sincerely
  asks Jenni who was at the porch. Turn 3 binding the knock to Bobby derives "Bobby knows he
  knocked", finds that line, and the model-driven binding is rejected
  (``rejected_bindings[].conflicting_lines``) and steered elsewhere: the repair binds it to the
  mailman, and nothing of Bobby's knock is committed.
- **Inert without partitions.** With ``partitions`` off, the same binding to Bobby commits with
  no flag, no knowledge and no memory (FR-EPI-4 applies only with per-character partitions).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.commit.epistemic import STORY_DAY
from epistrel.core.epistemic import partitions, replay
from epistrel.core.membership import Position
from epistrel.core.propositions import registry, store
from epistrel.orchestrator.models.registry import SalienceSketch
from epistrel.orchestrator.structured.prompt import KNOWLEDGE_STEERING
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.commit_support import salience_answer
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.story_support import READ, WRITE, override
from tests.structured_turn_support import beat, claim, render, renders_of, structured_story, text_of

pytestmark = pytest.mark.integration

KNOCK = "Someone knocks at the porch."
BOUND = "So it was Bobby at the porch all along."
ASKED = "Jenni, who was at the porch just now?"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _street(
    client: Any, engine: AsyncEngine, *, partitions: bool = True
) -> tuple[str, dict[str, str], dict[str, str]]:
    story, ids = await structured_story(client, "Jenni", "Ada", "Bobby")
    await ts.enable_testimony(engine, story, "defaulted", partitions=partitions)
    await override(client, story, {"budgets": {"model_calls": 20}})
    async with engine.begin() as conn:
        places = {
            name: await store.add_entity(conn, story, "place", name)
            for name in ("porch", "Maple Street")
        }
        places["mailman"] = await store.add_entity(conn, story, "person", "mailman")
        await registry.auto_register(conn, story, "knocked_at")
        await registry.register_author(
            conn,
            story,
            registry.ScenarioRules.parse({"rules": [{"when": "knocked_at", "then": "located_at"}]}),
        )
    await ps.inside(engine, story, places["porch"], places["Maple Street"])
    for name in ("Jenni", "Ada", "Bobby"):
        await ps.put(engine, story, ids[name], places["Maple Street"])
    return story, ids, places


async def _knock(
    client: Any, engine: AsyncEngine, epistrel: Any, story: str, ids: dict[str, str]
) -> Any:
    stub = epistrel.models.stub
    stub.script(
        "generation",
        [render(beat("do", KNOCK)), render(beat("narration", "The knocking stops."))],
    )
    stub.script(
        "salience_detection",
        [
            salience_answer(
                (2, KNOCK, SalienceSketch(subject="someone", relation="knocked_at", object="porch"))
            )
        ],
    )
    await played(client, story, body(ids["Jenni"], "Ada, did you hear that?"))
    (knock,) = await rows(engine, "SELECT * FROM thunks WHERE story_id = :s", s=story)
    assert str(knock.state) == "open"
    return knock


def _binding() -> list[str]:
    return [
        render(beat("narration", BOUND, propositions=[claim("Bobby", "knocked_at", "porch")])),
        render(beat("say", "Oh!", intent="sincere")),
    ]


async def _report(engine: AsyncEngine, story: str, turn: int) -> Any:
    """The turn report of the active variant of ``turn``."""
    (found,) = await rows(
        engine,
        "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND narrative_turn = :t AND membership = 'active'",
        s=story,
        t=turn,
    )
    return found.turn_report


async def _ledger(client: Any, story: str) -> dict[str, Any]:
    response = await client.get(
        f"/stories/{story}/inspect/ledger", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def _pass(engine: AsyncEngine, story: str, minutes: int) -> None:
    """Story-time passes (M6's world clock stand-in): the story's minute zero moves on."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE stories SET story_minute_zero = story_minute_zero + :m WHERE id = :s"),
            {"m": minutes, "s": story},
        )


async def _knowledge(engine: AsyncEngine, story: str, holder: str) -> list[Any]:
    """``holder``'s active ``resolution`` transitions: their story-time, and the predicate and
    first held turn of the belief each produced or changed."""
    return await rows(
        engine,
        "SELECT t.valid_at, q.name AS predicate, b.held_from_turn FROM belief_transitions t "
        "JOIN beliefs b ON b.id = t.belief_id JOIN propositions p ON p.id = b.proposition_id "
        "JOIN predicates q ON q.id = p.predicate_id WHERE t.story_id = :s "
        "AND t.holder_character_id = :h AND t.kind = 'resolution' AND t.membership = 'active' "
        "ORDER BY q.name",
        s=story,
        h=holder,
    )


async def _held(engine: AsyncEngine, story: str, holder: str, predicate: str) -> list[Any]:
    """``holder``'s active first-order beliefs of ``predicate`` (closed ones too): their
    ``held_from`` and ``held_to`` turns and beats, by ``held_from``."""
    return await rows(
        engine,
        "SELECT b.held_from_turn, b.held_from_beat, b.held_from_step, b.held_to_turn, "
        "b.held_to_beat, b.held_to_step, b.tx_to FROM beliefs b "
        "JOIN propositions p ON p.id = b.proposition_id JOIN predicates q ON q.id = p.predicate_id "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND q.name = :n "
        "AND b.kind = 'first_order' AND b.membership = 'active' "
        "ORDER BY b.held_from_turn, b.held_from_beat, b.held_from_step",
        s=story,
        h=holder,
        n=predicate,
    )


@pytest.mark.part("M5", "FR-COMMIT-11,14; FR-EPI-4")
async def test_s_thunk_resolve__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, _ = await _street(client, engine)
        bobby = ids["Bobby"]
        knock = await _knock(client, engine, epistrel, story, ids)

        # Turn 2 binds the knock to Bobby: flagged (mild), not rejected; his knowledge commits.
        stub.script("generation", _binding())
        await played(client, story, body(ids["Jenni"], None, do="Jenni opens the door."))
        report = await _report(engine, story, 2)
        assert report["commit"]["rejected_bindings"] == []
        (flag,) = report["commit"]["could_have_known_flags"]
        assert flag == {
            "character_id": bobby,
            "thunk_id": knock.id,
            "severity": "mild",
            "gap_minutes": 10,
        }
        assert "could_have_known_flag" in report["degradation"]
        assert report["epistemic"]["could_have_known"]["checks"] >= 1
        (resolved,) = await rows(engine, "SELECT * FROM thunks WHERE story_id = :s", s=story)
        assert str(resolved.state) == "resolved"
        # Bobby's knowledge, at the knock's story-time, held from the knock's turn on (#547):
        # he knocked (a belief his observation of the narration already held from turn 2:
        # raised in place, and its earlier span from the knock a backdated row of its own) and
        # was at the porch.
        knowledge = await _knowledge(engine, story, bobby)
        assert sorted((k.valid_at, k.predicate, k.held_from_turn) for k in knowledge) == [
            (knock.valid_time, "knocked_at", 1),
            (knock.valid_time, "knocked_at", 2),
            (knock.valid_time, "located_at", 1),
        ]
        early, held = await _held(engine, story, bobby, "knocked_at")
        assert (early.held_from_turn, held.held_from_turn, held.held_to_turn) == (1, 2, None)
        # One hold, no gap, one open row: the backdated span closes where the held one starts.
        assert (early.held_to_turn, early.held_to_beat, early.held_to_step) == (
            held.held_from_turn,
            held.held_from_beat,
            held.held_from_step,
        )
        assert early.tx_to is None
        async with engine.connect() as conn:
            knocked_at = (await registry.load(conn, story)).named("knocked_at")
            assert knocked_at is not None
            knock_predicate = knocked_at.id
            (open_row,) = [
                b
                for b in await partitions.partition(conn, story, bobby)
                if b.proposition.predicate_id == knock_predicate
            ]
            known = await replay.could_have_known(
                conn, story, bobby, open_row.proposition, Position.end_of(1)
            )
        assert isinstance(known, replay.Known), known
        assert known.path.transition_kind == "resolution"
        (memory,) = await rows(
            engine,
            "SELECT * FROM memories WHERE story_id = :s AND provenance = 'resolution' "
            "AND membership = 'active'",
            s=story,
        )
        assert (memory.holder_character_id, memory.narrative_turn) == (bobby, 1)
        assert memory.text == "Bobby knocked at porch."
        ledger = await _ledger(client, story)
        (inspected,) = ledger["thunks"]
        assert inspected["could_have_known_flags"] == [
            {"character_id": bobby, "severity": "mild", "gap_minutes": 10}
        ]

        # The next render, and only it, is steered by the flag.
        stub.script("generation", [render(beat("say", "Strange.", intent="sincere"))])
        before = len(renders_of(stub))
        await played(client, story, body(ids["Jenni"], "Ada, what now?"))
        assert KNOWLEDGE_STEERING in text_of(renders_of(stub)[before])
        stub.script("generation", [render(beat("say", "Nothing.", intent="sincere"))])
        before = len(renders_of(stub))
        await played(client, story, body(ids["Jenni"], "Ada, anything else?"))
        assert KNOWLEDGE_STEERING not in text_of(renders_of(stub)[before])

        # The flag grows with the gap: turn 2 again after two story-days, then after forty.
        for days, severity in ((2, "moderate"), (40, "strong")):
            await client.post(f"/stories/{story}/truncate", json={"to_turn": 2}, headers=WRITE)
            await _pass(engine, story, days * STORY_DAY)
            stub.script("generation", _binding())
            response = await client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
            assert response.status_code == 200, response.text
            report = await _report(engine, story, 2)
            (flag,) = report["commit"]["could_have_known_flags"]
            assert flag["severity"] == severity
            assert flag["gap_minutes"] >= days * STORY_DAY
            assert report["commit"]["rejected_bindings"] == []
            assert len(await _knowledge(engine, story, bobby)) == 3  # the earlier variant's left


@pytest.mark.part("M5", "FR-COMMIT-11,14; FR-EPI-4")
async def test_s_thunk_resolve__m5_sincere_question(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """Bobby's later sincere question rejects the binding to him, which is steered to the
    mailman; with partitions off the same binding is inert (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, places = await _street(client, engine)
        bobby = ids["Bobby"]
        knock = await _knock(client, engine, epistrel, story, ids)
        stub.script("generation", [render(beat("say", ASKED, intent="sincere"))])
        await played(
            client,
            story,
            body(ids["Jenni"], "Bobby, did you hear that?", render_character_ids=[bobby]),
        )
        (asked,) = await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'beat_say' AND character_id = :c",
            s=story,
            c=bobby,
        )
        stub.script("generation", _binding())
        stub.script(
            "regeneration",
            [
                json.dumps(
                    {
                        "text": "On the step lies a parcel: the mailman knocked.",
                        "propositions": [claim("mailman", "knocked_at", "porch")],
                    }
                )
            ],
        )
        await played(client, story, body(ids["Jenni"], None, do="Jenni opens the door."))
        report = await _report(engine, story, 3)
        (rejected,) = report["commit"]["rejected_bindings"]
        assert (rejected["bound_entity"], rejected["thunk_id"]) == (bobby, knock.id)
        assert rejected["conflicting_lines"] == [asked.id]
        assert "binding_rejected" in report["degradation"]
        assert report["commit"]["could_have_known_flags"] == []
        knocks = await rows(
            engine,
            "SELECT f.subject_id FROM facts f JOIN predicates q ON q.id = f.predicate_id "
            "WHERE f.story_id = :s AND q.name = 'knocked_at' AND f.membership = 'active'",
            s=story,
        )
        assert [k.subject_id for k in knocks] == [places["mailman"]]
        assert await _knowledge(engine, story, bobby) == []

        # Inert without partitions: the binding to Bobby commits with no flag or knowledge.
        story, ids, places = await _street(client, engine, partitions=False)
        await _knock(client, engine, epistrel, story, ids)
        stub.script("generation", _binding())
        await played(client, story, body(ids["Jenni"], None, do="Jenni opens the door."))
        report = await _report(engine, story, 2)
        assert report["commit"]["could_have_known_flags"] == []
        assert report["commit"]["rejected_bindings"] == []
        assert "epistemic" not in report or "could_have_known" not in report["epistemic"]
        assert await _knowledge(engine, story, ids["Bobby"]) == []
