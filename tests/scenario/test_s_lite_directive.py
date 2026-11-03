"""S-LITE-DIRECTIVE (docs/04 §7; FR-LITE-8, FR-DIRECT-1,2; #244): author directives in a Lite
story.

A Lite story plays a turn; the author then:

- **nudges** the next reply: the note is in that turn's prompt only (absent from the turn after),
  the turn still makes one generation call, nothing positioned is written and the revision does
  not move;
- sets a **lasting author fact** ("It starts raining."): recorded at the next slot, in every later
  prompt, and gone after a rewind past it (its slot operation discarded, never deleted);
- directs the **past**: 422 ``directive_requires_rewrite`` with the rewrite route, nothing
  written;
- and a **directed regeneration** applies its directive as a nudge to that regeneration only.

No response on the Lite story is ``module_off``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import FIRST, respond, respond_client
from tests.respond_support import body as respond_body
from tests.story_support import WRITE, create, story_body

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

NOW = json.dumps({"classification": "durable", "temporal_scope": "now", "changes": []})
PAST = json.dumps({"classification": "durable", "temporal_scope": "past", "changes": []})
RAIN = "It starts raining."
NUDGE = "Make Ada sound tired."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _directive(client: Any, story: str, **body: Any) -> Any:
    return client.post(
        f"/stories/{story}/directives", params=rs.OMNISCIENT, json=body, headers=WRITE
    )


def _prompt(stub: Any) -> str:
    calls = [c for c in stub.calls if c.purpose == "generation"]
    return "\n".join(m.content for m in calls[-1].messages) if calls else ""


async def _turn(client: Any, story: str, actor: str, stub: Any) -> tuple[dict[str, Any], str]:
    stub.calls.clear()
    found = await respond(client, story, respond_body(actor, FIRST))
    assert found.status_code == 200, found.text
    generations = [c for c in stub.calls if c.purpose == "generation"]
    assert len(generations) == 1  # FR-LITE-7: one generation call
    return found.json(), _prompt(stub)


@pytest.mark.part("M7", "FR-LITE-8; FR-DIRECT-1,2")
async def test_s_lite_directive(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        await _turn(client, story, actor, stub)

        # A nudge: this turn's prompt only; nothing positioned, no revision move.
        revision = await rs.revision(engine, story)
        nudged = await _directive(client, story, text=NUDGE, kind="ephemeral")
        assert nudged.status_code == 200, nudged.text
        assert nudged.json()["disposition"] == "steer_queued"
        assert await rs.revision(engine, story) == revision
        _, prompt = await _turn(client, story, actor, stub)
        assert NUDGE in prompt
        _, prompt = await _turn(client, story, actor, stub)
        assert NUDGE not in prompt

        # A lasting author fact: in every later prompt, gone after a rewind past it.
        stub.when("raining", NOW, purpose="directive_parse")
        head = (await rs.revision(engine, story), None)[0]
        fact = await _directive(client, story, text=RAIN)
        assert fact.status_code == 200, fact.text
        applied = fact.json()["applied"]["author_fact"]
        assert applied["text"] == RAIN
        assert "⁺" in applied["position"]
        assert await rs.revision(engine, story) == head + 1
        turn_before = int(applied["position"].split("⁺")[0])
        for _ in range(2):
            _, prompt = await _turn(client, story, actor, stub)
            assert RAIN in prompt
        rewound = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": turn_before}, headers=WRITE
        )
        assert rewound.status_code == 200, rewound.text
        _, prompt = await _turn(client, story, actor, stub)
        assert RAIN not in prompt
        slots = await rows(
            engine,
            "SELECT o.membership::text AS m FROM events e JOIN operations o "
            "ON o.operation_id = e.operation_id WHERE e.story_id = :s "
            "AND e.type = 'directive_applied'",
            s=story,
        )
        assert [s.m for s in slots] == ["discarded"]

        # The past: refused with the rewrite route, nothing written.
        stub.when("Ohio", PAST, purpose="directive_parse")
        count = await rs.count(engine, "directives", story)
        past = await _directive(client, story, text="Ada grew up in Ohio.")
        assert past.status_code == 422, past.text
        error = past.json()["error"]
        assert error["code"] == "directive_requires_rewrite"
        assert error["details"]["rewrite_route"].endswith("/rewrites")
        assert await rs.count(engine, "directives", story) == count

        # A directed regeneration: the directive nudges that regeneration only.
        stub.calls.clear()
        regenerated = await client.post(
            f"/stories/{story}/regenerate", json={"directive": {"text": NUDGE}}, headers=WRITE
        )
        assert regenerated.status_code == 200, regenerated.text
        assert NUDGE in _prompt(stub)
        _, prompt = await _turn(client, story, actor, stub)
        assert NUDGE not in prompt
        (row,) = await rows(
            engine,
            "SELECT disposition FROM directives WHERE story_id = :s "
            "AND disposition = 'directed_regeneration'",
            s=story,
        )
        assert row.disposition == "directed_regeneration"
