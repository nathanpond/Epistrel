"""S-KNOCK: determined-but-unknown vs a global thunk (docs/04 §7; FR-COMMIT-6,13, FR-EPI-8;
#169).

Structured turns on the stub under per-character partitions (perception ``full``). Wren (the
player) and Jenni are in the parlor; Bobby, a modeled character, is on the porch beyond the
closed front door. The S-KNOCK fixture's auto predicate ``knocked_at(x, the door)`` (who knocked
at the door) is #154's test vocabulary, not core vocabulary.

- **Someone knocked.** Turn 1 narrates a salient knock: "a knock occurred" commits for the
  hearers (``knocked_at_occurred``, the minimal commit discharging the knock's obligation) and
  the turn closes with "who" an open thunk (FR-COMMIT-13's granularity).
- **The modeled knocker** (FR-COMMIT-6, FR-EPI-8). Turn 2 renders Bobby knocking with intent
  ("Bobby knocks at the door again."): his ``do`` resolves the thunk, and the knocker is
  committed on his side, scoped to him (``characters: [Bobby]``: through the closed door the
  parlor caught only the sound, an envelope). The identity is determined and known to Bobby,
  who holds a committed memory of knocking, and unknown to the hearers: absent from their reads
  and partitions. In turn 3 Jenni settles it for herself ("the mailman knocked"): a second value
  for a fixed reference, ``contradicted`` without citing the hidden one, and canon keeps one
  knocker. Viewpoints cannot assign truth either: ``POST /admin/stories/{id}/facts`` naming
  Jenni as the holder is 422 ``viewpoint_cannot_assign_truth``.
- **Undetermined** (another story). Nobody decided who knocked: a global thunk, genuinely
  undetermined for everyone (no fact for any reader, the omniscient one included, and no
  belief in anyone's partition). When a later turn makes the knocker's identity salient
  ("Jenni opens the door to see who knocked"), identity becomes an obligation and the turn's
  follow-up settles it then: the mailman.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from epistrel.core.propositions import registry, store
from epistrel.orchestrator.models.registry import SalienceSketch
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.commit_support import salience_answer
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.story_support import WRITE, override
from tests.structured_turn_support import beat, claim, render, structured_story, turn_report

pytestmark = pytest.mark.integration

KNOCK = "Someone knocks at the door."
WHO = SalienceSketch(subject="someone", relation="knocked_at", object="the door")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _house(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str]]:
    """The parlor (Wren, Jenni), the porch (Bobby) beyond the closed door, the door, the
    mailman and ``knocked_at``."""
    story, ids = await structured_story(client, "Wren", "Jenni", "Bobby")
    await ts.enable_testimony(engine, story, "defaulted")
    await override(client, story, {"budgets": {"model_calls": 20}})
    async with engine.begin() as conn:
        for name, kind in (("parlor", "place"), ("porch", "place"), ("the door", "item")):
            ids[name] = await store.add_entity(conn, story, kind, name)
        ids["the mailman"] = await store.add_entity(conn, story, "person", "the mailman")
        ids["knocked_at"] = (await registry.auto_register(conn, story, "knocked_at")).id
    await ps.adjoin(engine, story, ids["parlor"], ids["porch"], "door_closed")
    for name, place in (("Wren", "parlor"), ("Jenni", "parlor"), ("Bobby", "porch")):
        await ps.put(engine, story, ids[name], ids[place])
    return story, ids


async def _knock(client: Any, engine: AsyncEngine, stub: Any, story: str, ids: Any) -> Any:
    """Turn 1: the salient knock; "a knock occurred" commits and "who" stays open."""
    stub.script(
        "generation",
        [render(beat("do", KNOCK)), render(beat("narration", "The knocking stops."))],
    )
    stub.script("salience_detection", [salience_answer((2, KNOCK, WHO))])
    await played(client, story, body(ids["Wren"], "Did you hear that?"))
    (knock,) = await rows(engine, "SELECT * FROM thunks WHERE story_id = :s", s=story)
    assert str(knock.state) == "open"
    report = await turn_report(engine, story)
    assert report["commit"]["minimal_commits"] == 1
    return knock


async def _knockers(engine: AsyncEngine, story: str, ids: dict[str, str]) -> list[Any]:
    """The current ``knocked_at`` facts: knocker and scope."""
    return await rows(
        engine,
        "SELECT f.id, p.subject_id, f.visibility FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id WHERE f.story_id = :s "
        "AND f.predicate_id = :p AND f.tx_to IS NULL AND f.membership = 'active' ORDER BY f.id",
        s=story,
        p=ids["knocked_at"],
    )


async def _occurred(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.subject_id, f.visibility FROM facts f JOIN predicates q "
        "ON q.id = f.predicate_id WHERE f.story_id = :s AND q.name = 'knocked_at_occurred'",
        s=story,
    )


async def _held(engine: AsyncEngine, story: str, holder: str, predicate: str) -> list[Any]:
    """``holder``'s current first-order beliefs of ``predicate``."""
    return [
        b
        for b in ts.first_order(await ts.beliefs_of(engine, story, holder))
        if b.predicate_id == predicate
    ]


@pytest.mark.part("M5", "FR-COMMIT-6,13; FR-EPI-8")
async def test_s_knock(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _house(client, engine)
        jenni, bobby, wren = ids["Jenni"], ids["Bobby"], ids["Wren"]
        knock = await _knock(client, engine, stub, story, ids)
        # "Someone knocked" committed for the hearers; who knocked is no fact yet.
        assert [o.subject_id for o in await _occurred(engine, story)] == [ids["the door"]]
        assert await _knockers(engine, story, ids) == []

        # -- turn 2: Bobby knocks with intent; the knocker is committed on his side
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "Bobby knocks at the door again.",
                        propositions=[claim("Bobby", "knocked_at", "the door")],
                    )
                )
            ],
        )
        await played(client, story, body(wren, "Who's there?", render_character_ids=[bobby]))
        (resolved,) = await rows(engine, "SELECT * FROM thunks WHERE story_id = :s", s=story)
        assert (str(resolved.state), resolved.id) == ("resolved", knock.id)
        (knocker,) = await _knockers(engine, story, ids)
        assert knocker.subject_id == bobby
        assert knocker.visibility == {"scope": "characters", "characters": [bobby]}
        assert resolved.resolved_fact_id == knocker.id
        # Known to Bobby, with a committed memory of knocking; unknown to the hearers.
        assert [b.subject_id for b in await _held(engine, story, bobby, ids["knocked_at"])] == [
            bobby
        ]
        knocking = await rows(
            engine,
            "SELECT text FROM memories WHERE story_id = :s AND holder_character_id = :h "
            "AND membership = 'active' AND text ILIKE '%knock%'",
            s=story,
            h=bobby,
        )
        assert knocking
        assert await _held(engine, story, jenni, ids["knocked_at"]) == []
        async with engine.connect() as conn:
            for viewpoint, seen in (
                (f"character:{bobby}", [knocker.id]),
                (f"character:{jenni}", []),
                (f"character:{wren}", []),
                ("narrator", []),
                ("omniscient", [knocker.id]),
            ):
                found = await canon.canon(
                    conn, story, predicate=ids["knocked_at"], viewpoint=viewpoint
                )
                assert [f.id for f in found] == seen, viewpoint

        # -- turn 3: Jenni settles it for herself; a second knocker is contradicted
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "Jenni decides the mailman knocked.",
                        propositions=[claim("the mailman", "knocked_at", "the door")],
                    )
                )
            ],
        )
        await played(client, story, body(wren, "Jenni, who was it?"))
        assert [k.id for k in await _knockers(engine, story, ids)] == [knocker.id]
        (rejected,) = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND narrative_turn = 3 "
            "AND type = 'render_rejected'",
            s=story,
        )
        (verdict,) = rejected.payload["verdicts"]
        assert (verdict["kind"], verdict["cause"], verdict["evidence_ids"]) == (
            "contradicted",
            "hidden_conflict",
            [],
        )
        # The repair's constraint is generic: it names nothing of the hidden knocker.
        (constraint,) = rejected.payload["constraints"]
        assert "Bobby" not in constraint["text"]
        assert constraint["cites"] == []
        # A viewpoint cannot assign the knocker's truth.
        refused = await client.post(
            f"/admin/stories/{story}/facts",
            json={
                "proposition": {
                    "subject_id": ids["the mailman"],
                    "predicate_id": ids["knocked_at"],
                    "object_id": ids["the door"],
                    "valid_time": {"kind": "interval", "start": None, "end": None},
                },
                "holder": jenni,
            },
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "viewpoint_cannot_assign_truth"
        assert [k.id for k in await _knockers(engine, story, ids)] == [knocker.id]

        # -- undetermined: nobody decided who knocked (another story)
        stub.reset()
        story, ids = await _house(client, engine)
        knock = await _knock(client, engine, stub, story, ids)
        async with engine.connect() as conn:
            for viewpoint in (
                "omniscient",
                "narrator",
                *(f"character:{ids[n]}" for n in ("Wren", "Jenni", "Bobby")),
            ):
                found = await canon.canon(
                    conn, story, predicate=ids["knocked_at"], viewpoint=viewpoint
                )
                assert found == [], viewpoint
        for name in ("Jenni", "Bobby"):
            assert await _held(engine, story, ids[name], ids["knocked_at"]) == []

        # A later turn makes identity salient: it becomes an obligation and resolves then.
        stub.reset()
        opens = "Jenni opens the door: it is the mailman."
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        opens,
                        propositions=[claim("the mailman", "knocked_at", "the door")],
                    )
                )
            ],
        )
        stub.script("salience_detection", [salience_answer((2, opens, WHO))])
        await played(client, story, body(ids["Wren"], "Go on, open it."))
        (settled,) = await rows(engine, "SELECT * FROM thunks WHERE story_id = :s", s=story)
        assert str(settled.state) == "resolved"
        (knocker,) = await _knockers(engine, story, ids)
        assert knocker.subject_id == ids["the mailman"]
        report = await turn_report(engine, story)
        assert report["commit"]["minimal_commits"] == 0
        assert report["commit"]["dependences"] == 1
