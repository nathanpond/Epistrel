"""S-LITE-DIRECTIVE (docs/04 §7; FR-LITE-8, FR-DIRECT-1,2; #244): author directives in a Lite
story.

A Lite story plays a turn; the author then:

- **nudges** the next reply: the note is in that turn's prompt only (absent from the turn after),
  the turn still makes one generation call, nothing positioned is written and the revision does
  not move;
- sets a **lasting author fact** ("It starts raining."): recorded at the next slot, in every later
  prompt, and gone after a rewind past it (its slot operation discarded, never deleted);
- directs the **past**: routed into a Lite rewrite session (#245; with no line to fix, 422
  ``rewrite_noop`` and nothing written);
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

        # The past: a Lite rewrite session (#245); with no line to fix, a no-op.
        stub.when("Ohio", PAST, purpose="directive_parse")
        stub.when("", json.dumps({"changes": []}), purpose="rewrite_parse")
        stub.when("", json.dumps({"hits": []}), purpose="lite_rewrite_reach")
        count = await rs.count(engine, "directives", story)
        past = await _directive(client, story, text="Ada grew up in Ohio.")
        assert past.status_code == 422, past.text
        assert past.json()["error"]["code"] == "rewrite_noop"
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


async def _lite_story(client: Any, engine: AsyncEngine, **overrides: Any) -> tuple[str, str]:
    from tests.respond_support import PLAYER, new_story

    story, ids = await new_story(client, PLAYER, FIRST, overrides=overrides or None)
    return story, ids[PLAYER]


def _clock_parse(**clock: Any) -> str:
    return json.dumps(
        {
            "classification": "durable",
            "temporal_scope": "now",
            "changes": [{"kind": "clock", **clock}],
            "steer_text": "",
        }
    )


@pytest.mark.part("M7", "FR-LITE-8; FR-DIRECT-1,2")
async def test_s_lite_directive__dispatch(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#699 (#244's dispatch, clock, cap and hint clauses), on Lite stories:

    - a character viewpoint is 403 ``omniscient_required``; an idempotent replay of a lasting
      fact answers the same ``author_fact.id`` and writes nothing more;
    - standing guidance ("always …") is a nudge with ``hints: [use_custom_instructions]``;
    - with the prompt-only clock on, a time-only directive moves the clock at the slot and
      records no author fact (``applied.author_fact: null``); a time of day the unanchored clock
      cannot place is 422 ``clock_unanchored`` with nothing written; with the clock off the same
      parse is a lasting fact holding the author's text, and no clock change is written;
    - past ``lite_author_facts_max`` active facts, one more is 409 ``author_fact_limit``."""
    from tests import clock_support as ck

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, actor = await _lite_story(client, engine)
        await _turn(client, story, actor, stub)

        forbidden = await client.post(
            f"/stories/{story}/directives",
            params={"viewpoint": f"character:{actor}"},
            json={"text": RAIN},
            headers=WRITE,
        )
        assert forbidden.status_code == 403, forbidden.text
        stub.when("raining", NOW, purpose="directive_parse")
        keyed = {**WRITE, "Idempotency-Key": "lite-fact-1"}
        first = await client.post(
            f"/stories/{story}/directives", params=rs.OMNISCIENT, json={"text": RAIN}, headers=keyed
        )
        assert first.status_code == 200, first.text
        count = await rs.count(engine, "directives", story)
        again = await client.post(
            f"/stories/{story}/directives", params=rs.OMNISCIENT, json={"text": RAIN}, headers=keyed
        )
        assert again.status_code == 200, again.text
        assert (
            again.json()["applied"]["author_fact"]["id"]
            == (first.json()["applied"]["author_fact"]["id"])
        )
        assert await rs.count(engine, "directives", story) == count

        stub.when(
            "Always",
            json.dumps(
                {
                    "classification": "ephemeral",
                    "steer_text": "Describe the weather.",
                    "standing": True,
                }
            ),
            purpose="directive_parse",
        )
        standing = await _directive(client, story, text="Always describe the weather.")
        assert standing.status_code == 200, standing.text
        assert standing.json()["disposition"] == "steer_queued"
        assert standing.json()["hints"] == ["use_custom_instructions"]

        # The prompt-only clock: a time-only directive moves it; no author fact.
        clocked, clocked_actor = await _lite_story(client, engine, clock={"level": "prompt_only"})
        await _turn(client, clocked, clocked_actor, stub)
        stub.when("hour", _clock_parse(minutes=60), purpose="directive_parse")
        moved = await _directive(client, clocked, text="An hour passes.")
        assert moved.status_code == 200, moved.text
        assert moved.json()["applied"]["author_fact"] is None
        assert len(moved.json()["applied"]["clock"]) == 1
        stub.when("noon", _clock_parse(to="12:00"), purpose="directive_parse")
        revision = await rs.revision(engine, clocked)
        unplaced = await _directive(client, clocked, text="It is noon.")
        assert unplaced.status_code == 422, unplaced.text
        assert unplaced.json()["error"]["code"] == "clock_unanchored"
        assert await rs.revision(engine, clocked) == revision
        await ck.set_level(engine, clocked, None)
        off = await _directive(client, clocked, text="An hour passes.")
        assert off.status_code == 200, off.text
        assert off.json()["applied"]["author_fact"]["text"] == "An hour passes."
        assert off.json()["applied"]["clock"] == []

        # The cap: lite_author_facts_max active facts.
        put = await client.put("/admin/settings", json={"lite_author_facts_max": 2}, headers=WRITE)
        assert put.status_code == 200, put.text
        try:
            stub.when("snow", NOW, purpose="directive_parse")
            second = await _directive(client, story, text="It starts to snow.")
            assert second.status_code == 200, second.text
            capped = await _directive(client, story, text="More snow falls.")
            assert capped.status_code == 409, capped.text
            assert capped.json()["error"]["details"] == {"active": 2, "max": 2}
        finally:
            await client.put("/admin/settings", json={"lite_author_facts_max": 50}, headers=WRITE)


@pytest.mark.part("M7", "FR-LITE-8; FR-DIRECT-1,2")
async def test_s_lite_directive__regeneration(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#699 (#244's directed-regeneration clause): the directive is a nudge for that
    regeneration only, with no parse and no reconciliation; naming ``resolution_mode`` or
    ``preview`` answers the warning ``lite_no_reconciliation`` (none without them)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, actor = await _lite_story(client, engine)
        await _turn(client, story, actor, stub)
        stub.calls.clear()
        plain = await client.post(
            f"/stories/{story}/regenerate", json={"directive": {"text": NUDGE}}, headers=WRITE
        )
        assert plain.status_code == 200, plain.text
        assert plain.json().get("warnings", []) == []
        assert NUDGE in _prompt(stub)
        assert not [c for c in stub.calls if c.purpose == "directive_parse"]
        warned = await client.post(
            f"/stories/{story}/regenerate",
            json={"directive": {"text": NUDGE}, "resolution_mode": "best_effort"},
            headers=WRITE,
        )
        assert warned.status_code == 200, warned.text
        assert [w["kind"] for w in warned.json()["warnings"]] == ["lite_no_reconciliation"]
        assert await rs.count(engine, "rewrite_sessions", story) == 0
