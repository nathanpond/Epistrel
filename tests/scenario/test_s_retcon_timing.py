"""S-RETCON-TIMING (docs/04 §7; FR-STORE-20, FR-REWRITE-12, FR-CLOCK-3; #240): placing a
correction the clock cannot.

A structured story with the clock on, anchored "Saturday afternoon" with no year, played to
turn 8 (the docs' checkpoints 40 and 80 scaled to 4 and 8). One rewrite, its parse scripted,
holds three parts:

- (a) a correction of turn 6's line: in the view at 8, out at 4 (it follows its record);
- (b) Cal lived in Ohio "before the story began": an offset comparison, in at both;
- (c) Bobby lived at the coast "in 2012": undecidable against an anchor with no year.

Under the default ``include_if_possible`` (c) is in at both checkpoints, listed in the
preview's ``timing_uncertain`` and flagged on its part and in the ``amendment_recorded`` audit
payload; a fork at 4 carries the flag; no ``timing`` question is ever asked. Under
``ask_author`` with ``best_effort`` + ``apply_directly`` (c) is a required ``timing`` question
and nothing applies until it is answered; an unreadable free-text answer is 422
``invalid_answer`` and the question stays open; "after turn 4" is stored as the part's
position bound, so (c) is out at 4 and in at 8, and a fork at 4 reads the stored bound the same
way. At 4 the amendment is partly in ((b)) and partly out ((a), and (c) under the bound).
Changing the setting afterwards leaves stored parts' eligibility unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from epistrel.core.propositions import store
from epistrel.core.propositions.vocabulary import LOCATED_AT
from epistrel.core.views import corrected_history
from epistrel.core.world_clock.anchor import statement_of
from tests import amendment_support as am
from tests import clock_support as ck
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fork_support import fork
from tests.respond_support import respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration

PARSED = json.dumps(
    {
        "changes": [
            {
                "kind": "record",
                "ref": {"turn": 6},
                "action": "replace",
                "new_text": "reply 6, as it truly went",
            },
            {
                "kind": "proposition",
                "subject": "Cal",
                "predicate": "located_at",
                "object": "Ohio",
                "valid": {"from": "before the story began", "to": "0"},
            },
            {
                "kind": "proposition",
                "subject": "Bobby",
                "predicate": "located_at",
                "object": "the coast",
                "valid": {"from": "in 2012"},
            },
        ]
    }
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _story(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], dict[int, str]]:
    story, ids = await structured_story(client, "Cal", "Bobby")
    await ck.set_level(engine, story, "on")
    statement = statement_of("Saturday afternoon")
    assert statement is not None
    await ck._set_anchor(engine, story, statement)
    async with engine.begin() as conn:
        for name in ("Ohio", "the coast"):
            await store.add_entity(conn, story, "place", name)
    replies = await am.play(engine, story, 8, start=1)
    return story, ids, replies


async def _places(engine: AsyncEngine, story: str, at: int, who: str = "Bobby") -> set[str]:
    """Where canon has ``who``, by place name, through turn ``at``."""
    names = {
        str(r.id): str(r.canonical_name)
        for r in await rows(
            engine, "SELECT id, canonical_name FROM entities WHERE story_id = :s", s=story
        )
    }
    subject = next(i for i, n in names.items() if n == who)
    async with engine.connect() as conn:
        found = await canon.canon(
            conn, story, through_position=(at, 0), subject=subject, predicate=LOCATED_AT
        )
    return {names.get(str(f.proposition.object_id), "?") for f in found}


async def _line(engine: AsyncEngine, story: str, event_id: str, at: int) -> str | None:
    found = [e for e in await corrected_history(engine, story, through_turn=at) if e.id == event_id]
    return None if not found else str(found[0].payload.model_dump()["text"])


@pytest.mark.part("M7", "FR-STORE-20; FR-REWRITE-12; FR-CLOCK-3")
async def test_s_retcon_timing(
    stub_settings: Settings, engine: AsyncEngine, clock_delivered: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, _, replies = await _story(client, engine)
        stub = epistrel.models.stub
        stub.when("", PARSED, purpose="rewrite_parse")
        opened = await rs.open_session(
            client, story, resolution_mode="best_effort", preview="apply_directly"
        )
        assert opened.status_code == 201, opened.text
        body = opened.json()
        assert [q for q in body["questions"] if q["kind"] == "timing"] == []
        uncertain = body["preview"]["timing_uncertain"]
        assert [u["valid_spec"] for u in uncertain] == [{"absolute": "2012"}]
        assert body["state"] == "applied", body
        amendment = body["apply"]["amendment_id"]
        (part,) = await rows(
            engine,
            "SELECT id, timing_uncertain, position_bound FROM amendment_parts "
            "WHERE amendment_id = :a AND valid_spec ->> 'absolute' = '2012'",
            a=amendment,
        )
        assert (part.timing_uncertain, part.position_bound) == (True, None)
        (recorded,) = await rows(
            engine,
            "SELECT e.payload FROM events e JOIN amendments a ON a.operation_id = e.operation_id "
            "WHERE a.id = :a AND e.type = 'amendment_recorded'",
            a=amendment,
        )
        assert recorded.payload["timing"] == [
            {"part_id": str(part.id), "timing_uncertain": True, "position_bound": None}
        ]
        assert "the coast" in await _places(engine, story, 4)
        assert "the coast" in await _places(engine, story, 8)
        assert "Ohio" in await _places(engine, story, 4, "Cal")
        assert "Ohio" in await _places(engine, story, 8, "Cal")
        assert await _line(engine, story, replies[6], 8) == "reply 6, as it truly went"
        assert await _line(engine, story, replies[6], 4) is None
        forked = await fork(engine, story, 4)
        (copied,) = await rows(
            engine,
            "SELECT timing_uncertain FROM amendment_parts WHERE story_id = :s "
            "AND valid_spec ->> 'absolute' = '2012'",
            s=forked.story_id,
        )
        assert copied.timing_uncertain is True
        assert "the coast" in await _places(engine, forked.story_id, 4)


@pytest.mark.part("M7", "FR-STORE-20; FR-REWRITE-12; FR-CLOCK-3")
async def test_s_retcon_timing__ask_author(
    stub_settings: Settings, engine: AsyncEngine, clock_delivered: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, _, replies = await _story(client, engine)
        patched = await client.patch(
            f"/stories/{story}/settings",
            json={"ambiguous_retcon_timing": "ask_author"},
            headers=WRITE,
        )
        assert patched.status_code == 200, patched.text
        epistrel.models.stub.when("", PARSED, purpose="rewrite_parse")
        opened = await rs.open_session(
            client, story, resolution_mode="best_effort", preview="apply_directly"
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert session["state"] == "awaiting_answers"
        (timing,) = [q for q in session["questions"] if q["kind"] == "timing"]
        assert timing["required"] is True
        assert await rs.count(engine, "amendments", story) == 0

        unreadable = await client.post(
            f"/stories/{story}/rewrites/{session['id']}/answers",
            params=rs.OMNISCIENT,
            json={"answers": [{"question_id": timing["id"], "text": "sometime in the spring"}]},
            headers=WRITE,
        )
        assert unreadable.status_code == 422, unreadable.text
        assert unreadable.json()["error"]["code"] == "invalid_answer"
        (still,) = await rows(
            engine, "SELECT answered_at FROM rewrite_questions WHERE id = :q", q=timing["id"]
        )
        assert still.answered_at is None

        answered = await client.post(
            f"/stories/{story}/rewrites/{session['id']}/answers",
            params=rs.OMNISCIENT,
            json={"answers": [{"question_id": timing["id"], "text": "after turn 4"}]},
            headers=WRITE,
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["state"] == "applied", answered.text
        (part,) = await rows(
            engine,
            "SELECT timing_uncertain, position_bound FROM amendment_parts "
            "WHERE story_id = :s AND valid_spec ->> 'absolute' = '2012'",
            s=story,
        )
        assert part.timing_uncertain is False
        assert (part.position_bound["relation"], part.position_bound["turn"]) == ("after", 4)
        at_4, at_8 = await _places(engine, story, 4), await _places(engine, story, 8)
        assert "Ohio" in await _places(engine, story, 4, "Cal")
        assert "the coast" not in at_4
        assert "the coast" in at_8
        assert await _line(engine, story, replies[6], 4) is None

        # A later fork reads the stored bound: out at its fork point 4.
        forked = await fork(engine, story, 4)
        assert "the coast" not in await _places(engine, forked.story_id, 4)
        later = await fork(engine, story, 6)
        assert "the coast" in await _places(engine, later.story_id, 6)

        # Changing the setting does not move a stored part.
        await client.patch(
            f"/stories/{story}/settings",
            json={"ambiguous_retcon_timing": "include_if_possible"},
            headers=WRITE,
        )
        assert await _places(engine, story, 4) == at_4
        assert await _places(engine, story, 8) == at_8
