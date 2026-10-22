"""S-AUDIT-CATEGORY: audit records stay out of story views (docs/04 §7; FR-STORE-25).

M2 part (#68): the first reply of a Lite turn trips a story output policy and is regenerated;
the rejected draft is recorded as a ``guardrail_output_rejected`` event (category ``audit``)
with the turn. ``test_s_audit_category__prereq`` is #45's event-level slice (moved here from
``tests/integration`` so the scenario has one home).

M4 part (#151, ``test_s_audit_category__structured``): a structured turn's contradicted say is
rejected, its one regeneration is rejected too and the safe fallback is released: two
``render_rejected`` events, the beat's ``verify_verdict`` and a repair attempt, all ``audit``.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.events.types import RenderRejected
from epistrel.core.views import audit_view, corrected_history
from tests import structured_turn_support as st
from tests.auth_support import auth_settings
from tests.event_support import new_story as new_event_story
from tests.event_support import play_turn, rows
from tests.policy_support import AUTHOR, story_item, words
from tests.respond_support import (
    FIRST,
    PLAYER,
    body,
    everything_sent,
    new_story,
    played,
    respond_client,
)
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration

REJECTED = "The basilisk uncoils beneath the stair."
RELEASED = "The cellar is quiet."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M2", "FR-STORE-25")
async def test_s_audit_category(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The rejected output is absent from every story view (each character's and the
    omniscient history, the corrected history) and from the next turn's captured prompt;
    it is present in the audit view (omniscient, ``include_audit``) in its turn; truncating
    the turn discards it with the turn. Complement asserted: the released reply of the same
    turn is in the views and in the next prompt."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST)
        await lib.policies_set_story(AUTHOR, story, [story_item(words("basilisk"))])

        stub.reset()
        stub.script("generation", [REJECTED, RELEASED])
        turn = await played(client, story, body(ids[PLAYER], "What's down there?"))
        assert turn["message"]["text"] == RELEASED

        async def history(viewpoint: str, **params: int) -> str:
            response = await client.get(
                f"/stories/{story}/history",
                params={"viewpoint": viewpoint, **params},
                headers=READ,
            )
            assert response.status_code == 200, response.text
            return response.text

        for viewpoint in (f"character:{ids[PLAYER]}", f"character:{ids[FIRST]}", "omniscient"):
            text = await history(viewpoint)
            assert "basilisk" not in text
            assert RELEASED in text
        corrected = await corrected_history(engine, story)
        assert "guardrail_output_rejected" not in {e.type for e in corrected}
        assert all("basilisk" not in str(e.payload) for e in corrected)

        audited = await history("omniscient", include_audit=1)
        assert "basilisk" in audited
        audit_items = [
            e for e in await audit_view(engine, story) if e.type == "guardrail_output_rejected"
        ]
        assert [(e.category.value, e.narrative_turn) for e in audit_items] == [
            ("audit", turn["turn"])
        ]

        # The next prompt carries the released reply, never the rejected one.
        stub.reset()
        await played(client, story, body(ids[PLAYER], "And now?"))
        sent = everything_sent(stub)
        assert RELEASED in sent
        assert "basilisk" not in sent

        # Truncating the turn discards its audit record with it.
        truncated = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": 0}, headers=WRITE
        )
        assert truncated.status_code == 200, truncated.text
        assert "basilisk" not in await history("omniscient", include_audit=1)
        memberships = await rows(
            engine,
            "SELECT o.membership FROM events e JOIN operations o USING (operation_id) "
            "WHERE e.story_id = :s AND e.type = 'guardrail_output_rejected'",
            s=story,
        )
        assert [m.membership for m in memberships] == ["discarded"]


@pytest.mark.part("M2", "FR-STORE-25")
async def test_s_audit_category__prereq(engine: AsyncEngine) -> None:
    """A rejected output recorded inside an active turn is absent from the corrected history at
    every checkpoint. Complement: the audit view returns it, in its turn's position."""
    story = await new_event_story(engine)
    turn = await play_turn(engine, story, "hello", rejected="not this")

    for through in (None, 1):
        view = await corrected_history(engine, story, through)
        assert "guardrail_output_rejected" not in {e.type for e in view}
        assert [e.type for e in view if e.operation_id == turn.operation_id] == [
            "player_input",
            "message_rendered",
        ]

    audit = [e for e in await audit_view(engine, story) if e.operation_id == turn.operation_id]
    assert [(e.type, e.category.value) for e in audit] == [
        ("player_input", "input"),
        ("guardrail_output_rejected", "audit"),
        ("message_rendered", "narrative_state"),
    ]
    assert {(e.narrative_turn, e.turn_variant) for e in audit} == {(1, 1)}


FIRST_REJECTED = "I am seventy years old, truly."
SECOND_REJECTED = "I'm seventy-one, I swear it."
REJECTED_WORD = "seventy"


@pytest.mark.part("M4", "FR-STORE-25")
async def test_s_audit_category__structured(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Two rejected renders, the verify verdicts and the repair attempt of a structured turn
    appear in no story view (every viewpoint's history, the corrected history), no retrieval
    (no memory holds them) and no generation context (the next turn's every model call), and all
    appear in the audit view, in their turn; truncating the turn discards them with it.
    Complement: the released fallback line is in the views."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await st.structured_story(client, "Wren", "Ada")
        await st.age_fact(engine, story, ids["Ada"], 64)
        stub.script(
            "generation",
            [
                st.render(
                    st.beat(
                        "say",
                        FIRST_REJECTED,
                        intent="sincere",
                        propositions=[st.claim("I", "age", "70")],
                    )
                )
            ],
        )
        stub.script(
            "regeneration",
            [json.dumps({"text": SECOND_REJECTED, "propositions": [st.claim("I", "age", "71")]})],
        )
        turn = await played(client, story, body(ids["Wren"], "How old are you, Ada?"))
        released = "Ada changes the subject."
        assert turn["message"]["text"] == f'Ada: "{released}"'

        audited = [
            e
            for e in await audit_view(engine, story)
            if e.type in ("render_rejected", "verify_verdict")
        ]
        assert sorted((e.type, e.category.value) for e in audited) == [
            ("render_rejected", "audit"),
            ("render_rejected", "audit"),
            ("verify_verdict", "audit"),
        ]
        assert {e.narrative_turn for e in audited} == {turn["turn"]}
        renders = [e.payload for e in audited if isinstance(e.payload, RenderRejected)]
        assert [r.text for r in renders] == [FIRST_REJECTED, SECOND_REJECTED]
        assert [r.attempt for r in renders] == [0, 1]  # the original render and the repair attempt

        async def history(viewpoint: str, **params: int) -> str:
            response = await client.get(
                f"/stories/{story}/history",
                params={"viewpoint": viewpoint, **params},
                headers=READ,
            )
            assert response.status_code == 200, response.text
            return response.text

        for viewpoint in (f"character:{ids['Wren']}", f"character:{ids['Ada']}", "omniscient"):
            text = await history(viewpoint)
            assert REJECTED_WORD not in text
            assert released in text
        corrected = await corrected_history(engine, story)
        assert not {"render_rejected", "verify_verdict"} & {e.type for e in corrected}
        assert all(REJECTED_WORD not in str(e.payload) for e in corrected)
        assert REJECTED_WORD in await history("omniscient", include_audit=1)
        # No retrieval reads them: no memory of the story holds the rejected text.
        memories = await rows(engine, "SELECT text FROM memories WHERE story_id = :s", s=story)
        assert memories
        assert all(REJECTED_WORD not in m.text for m in memories)

        # The next turn's every model call carries the released line, never a rejected one.
        stub.reset()
        await played(client, story, body(ids["Wren"], "Fair enough."))
        sent = everything_sent(stub)
        assert released in sent
        assert REJECTED_WORD not in sent

        # Truncating the turn discards its audit records with it.
        truncated = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": 0}, headers=WRITE
        )
        assert truncated.status_code == 200, truncated.text
        assert REJECTED_WORD not in await history("omniscient", include_audit=1)
        memberships = await rows(
            engine,
            "SELECT DISTINCT o.membership FROM events e JOIN operations o USING (operation_id) "
            "WHERE e.story_id = :s AND e.type IN ('render_rejected', 'verify_verdict')",
            s=story,
        )
        assert [m.membership for m in memberships] == ["discarded"]
