"""S-DUET: multi-character composition (docs/04 §7; FR-MULTI-1,2,3,4; #177).

The key room (``tests.fixtures.key_room``): Cal (the player), Bobby and Jenni in the study, a
structured story with per-character renders under per-character partitions. Cal asks; the
request names Jenni then Bobby, both react (``sequential``), each render scripted by its name:

- Jenni holds a secret in her ``think``. Each primary renders against its own viewpoint: Bobby's
  prompt holds Jenni's ``say`` and none of her thought, and his reply never repeats it; the
  composed message for Cal never holds it either (FR-MULTI-1,2).
- Concurrent commitments are reconciled on one timeline (FR-MULTI-3): Jenni's ``latent`` puts
  the brass key in the study (on the mantel), then Bobby's ``do`` pockets it (``holds``), which no
  verification contradicts and which still cannot stand beside her earlier commitment (the
  possession/placement pair). Her earlier beat stands (``earlier_beat``) and his ``do`` is
  rewritten as an attempt ("Bobby reaches for the brass key, but it is already gone."); the
  pair is on ``turn_report.reconcile``, the response's ``reconcile`` and a ``turn_reconciled``
  audit record. Both ``latent`` effects (hers and Bobby's own, the drawer in the study) land once.
- One composed message, returned with an ordered, attributed beat list (FR-MULTI-4): the beats
  in order with their actors, the narrative composed from exactly them.

Complement (the story's test plan): the contradictory pair leaves exactly one canon value of the
key's whereabouts, and the loser is an attempt. ``test_s_duet__simultaneous`` (#175 AC5) runs the
reconciliation after a ``simultaneous`` turn's adjudication: a pair adjudication never sees (one
character's own two commitments) is reconciled like a sequential turn's.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import multi_support as ms
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.key_room import KeyRoom, key_room
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, turn_report

pytestmark = pytest.mark.integration

SECRET = "S-DUET-SECRET Jenni burned the ledger herself."
GREETING = "Evening, Bobby. Cal wants to know about the lamp."
HIDES = "The brass key lies on the study's mantel."
REPLY = "Evening, Jenni."
POCKETS = "Bobby slips the brass key into his pocket."
ATTEMPT = "Bobby reaches for the brass key, but it is already gone."
LAMP = "The desk drawer stands in the study."
TAKES = "Jenni slips the brass key into her pocket."
JENNI_ATTEMPT = "Jenni reaches for the brass key, but it is already gone."
STRETCHES = "Bobby stretches by the window."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _ask(room: KeyRoom, **fields: Any) -> dict[str, Any]:
    return body(
        room.cal, "What happened here?", render_character_ids=[room.jenni, room.bobby], **fields
    )


async def _key_facts(engine: AsyncEngine, story: str, key: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT p.predicate_id, p.subject_id, p.object_id FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND (p.subject_id = :k OR p.object_id = :k) "
        "AND f.membership = 'active'",
        s=story,
        k=key,
    )


@pytest.mark.part("M5", "FR-MULTI-1,2,3,4")
async def test_s_duet(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    multi_delivered: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        stub.reset()
        ms.renders_for(
            stub,
            "Jenni",
            ms.render(
                beat("think", SECRET),
                beat("say", GREETING, intent="sincere"),
                beat(
                    "latent",
                    HIDES,
                    propositions=[claim("the brass key", "located_at", "study")],
                ),
            ),
        )
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(
                beat("say", REPLY, intent="sincere"),
                beat("do", POCKETS, propositions=[claim("Bobby", "holds", "the brass key")]),
                beat(
                    "latent", LAMP, propositions=[claim("the desk drawer", "located_at", "study")]
                ),
            ),
        )
        answer = await respond(client, room.story_id, _ask(room))
        assert answer.status_code == 200, answer.text
        played = answer.json()

        # Each against its own viewpoint: Bobby never saw Jenni's secret.
        (bobby_prompt,) = ms.render_prompts(stub, "Bobby")
        assert f"Jenni says: {GREETING}" in bobby_prompt
        assert "S-DUET-SECRET" not in bobby_prompt
        assert "S-DUET-SECRET" not in str(played)

        # Reconciled on one timeline: her earlier commitment stands, his do is an attempt.
        report = await turn_report(engine, room.story_id)
        pairs = report["reconcile"]["pairs"]
        (pair,) = [p for p in pairs if p["rule"] != "dedupe"]
        jenni_beats = [b for b in played["beats"] if b["actor_character_id"] == room.jenni]
        bobby_beats = [b for b in played["beats"] if b["actor_character_id"] == room.bobby]
        assert (pair["rule"], pair["action"]) == ("earlier_beat", "attempt")
        assert pair["loser_beat"] in {b["beat"] for b in bobby_beats}
        assert played["reconcile"] == [{k: v for k, v in pair.items() if k != "action"}]
        assert ("do", ATTEMPT) in [(b["channel"], b["text"]) for b in bobby_beats]
        assert POCKETS not in played["message"]["text"]
        audit = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'turn_reconciled'",
            s=room.story_id,
        )
        assert [a.payload["pairs"] for a in audit] == [pairs]
        # Complement: one canon value of the key's whereabouts, the loser's never written.
        facts = await _key_facts(engine, room.story_id, room.key)
        assert len(facts) == 1
        assert facts[0].object_id == room.study
        # Both latents' effects landed, once each.
        assert len(await _key_facts(engine, room.story_id, room.drawer)) == 1

        # One composed message with ordered, attributed beats.
        assert played["order"] == [room.jenni, room.bobby]
        numbers = [b["beat"] for b in played["beats"] if b["channel"] != "message"]
        assert numbers == sorted(numbers)
        actors = [b["actor_character_id"] for b in played["beats"] if b["channel"] != "message"]
        assert actors == [room.cal, room.jenni, room.bobby, room.bobby]
        assert all("fidelity" in b for b in played["beats"] if b["channel"] != "message")
        text = played["message"]["text"]
        assert text.index(GREETING) < text.index(REPLY) < text.index(ATTEMPT)
        assert played["message"]["narrative"] == text
        assert HIDES not in text
        assert LAMP not in text  # latents are never a character's output
        assert jenni_beats
        assert bobby_beats


@pytest.mark.part("M5", "FR-MULTI-1,2,3,4")
async def test_s_duet__simultaneous(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    multi_delivered: None,
) -> None:
    """#175 AC5: after a ``simultaneous`` turn's adjudication, the surviving commitments go
    through the same reconciliation. Adjudication settles only two characters' clashing
    actions, so Jenni's own ``latent`` (the key on the mantel) and her later ``do`` (she
    pockets it) survive it; reconciliation keeps her earlier commitment and turns the ``do`` into
    an attempt. Bobby's unrelated action stands."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        stub.reset()
        ms.renders_for(
            stub,
            "Jenni",
            ms.render(
                beat(
                    "latent",
                    HIDES,
                    propositions=[claim("the brass key", "located_at", "study")],
                ),
                beat("do", TAKES, propositions=[claim("Jenni", "holds", "the brass key")]),
            ),
        )
        ms.renders_for(stub, "Bobby", ms.render(beat("do", STRETCHES)))
        answer = await respond(client, room.story_id, _ask(room, interaction_mode="simultaneous"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        report = await turn_report(engine, room.story_id)
        assert played["interaction_mode"] == "simultaneous"
        assert "adjudications" not in played  # nothing two characters both acted on
        (pair,) = report["reconcile"]["pairs"]
        assert (pair["rule"], pair["action"]) == ("earlier_beat", "attempt")
        texts = [(b["channel"], b["text"]) for b in played["beats"]]
        assert ("do", JENNI_ATTEMPT) in texts
        assert ("do", STRETCHES) in texts
        facts = await _key_facts(engine, room.story_id, room.key)
        assert [f.object_id for f in facts] == [room.study]
