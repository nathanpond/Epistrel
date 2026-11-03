"""S-EXT-DIRECTIVE (docs/04 §7; FR-EXT-8, FR-DIRECT-1; #242): an author can't make the story
say the player has gold the game says they don't.

Cal's ``gold`` is owned by an in-process provider (``tests.provider_support``); the author
directs "Cal has 500 gold." (``directive_parse`` scripted: a State set).

- **Read-only provider**: 422 ``external_value_not_overridable`` with the explanation and the
  value named, nothing written (no ``directives`` row, no slot operation).
- **Write-capable provider**: the change is routed as a write request: 200 ``routed_write`` with
  the outbox row (``intended``, ``effect.source = author``), never a ``state_changes`` row.
  Dispatched, the provider sees the row's id as its idempotency key; confirmed, the provider's
  value (scripted to 500) is what Epistrel pins next. A failed dispatch leaves the value as the
  provider has it.
- **A rewrite** ("Cal always had 500 gold"): a past change to a game-owned value is refused
  even on a write-capable provider: alone, 422 and no session; beside a story-owned change, it is
  in the preview's ``refused_changes`` and the story-owned change goes on.

In every branch: no ``state_changes`` row with ``source = author`` and no author-tier fact on
the external key.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from tests import provider_support as ps
from tests import rewrite_support as rs
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration

SET_GOLD = json.dumps(
    {
        "classification": "durable",
        "temporal_scope": "now",
        "changes": [{"kind": "state", "state": "gold", "scope": "Cal", "value": 500}],
    }
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _game(
    client: Any, engine: AsyncEngine, epistrel: Any, *, writable: bool
) -> tuple[str, dict[str, str], ps.ScriptedProvider]:
    story, ids = await structured_story(client, "Cal", "Bobby")
    name = ps.unique_name("game")
    spec = ps.write_capable_spec(name) if writable else ps.in_process_spec(name)
    await ps.register(engine, spec)
    fake = ps.ScriptedProvider(name, default=Decimal(100)).attach(epistrel)
    await ss.set_level(engine, story, "on")
    await ps.external_gold(
        engine,
        story,
        name,
        authorization={"sources": ["any_character", "author", "consumer"]},
    )
    return story, ids, fake


async def _never_authored(engine: AsyncEngine, story: str) -> None:
    authored = await rows(
        engine,
        "SELECT id FROM state_changes WHERE story_id = :s AND source = 'author'",
        s=story,
    )
    assert authored == []
    facts = await rows(
        engine,
        "SELECT id FROM facts WHERE story_id = :s AND authority = 'author'",
        s=story,
    )
    assert facts == []


def _direct(client: Any, story: str) -> Any:
    return client.post(
        f"/stories/{story}/directives",
        params=rs.OMNISCIENT,
        json={"text": "Cal has 500 gold."},
        headers=WRITE,
    )


@pytest.mark.part("M7", "FR-EXT-8; FR-DIRECT-1")
async def test_s_ext_directive(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        stub.when("", SET_GOLD, purpose="directive_parse")

        # Read-only: refused whole, nothing written.
        story, _, _ = await _game(client, engine, epistrel, writable=False)
        revision = await rs.revision(engine, story)
        refused = await _direct(client, story)
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "external_value_not_overridable"
        assert "change it in the game" in error["details"]["explanation"]
        assert [c["value_name"] for c in error["details"]["changes"]] == ["gold"]
        assert await rs.count(engine, "directives", story) == 0
        assert await rs.revision(engine, story) == revision
        await _never_authored(engine, story)

        # Write-capable: routed through the outbox, confirmed by the game.
        story, _, fake = await _game(client, engine, epistrel, writable=True)
        routed = await _direct(client, story)
        assert routed.status_code == 200, routed.text
        answer = routed.json()
        assert answer["disposition"] == "routed_write"
        (outbox_id,) = answer["applied"]["outbox_ids"]
        (row,) = await ps.outbox_rows(engine, story)
        assert str(row.id) == outbox_id
        assert str(row.status) == "intended"
        assert (row.effect["source"], row.effect["value"]) == ("author", 500)
        await _never_authored(engine, story)
        fake.script_writes("gold", "confirmed")
        fake.script("gold", Decimal(500))
        await outbox_job.dispatch(engine, outbox_id, registry=epistrel.in_process_providers)
        assert fake.idempotency_keys == [outbox_id]
        (sent,) = await ps.outbox_rows(engine, story)
        assert str(sent.status) in ("confirmed", "dispatched", "unknown")
        await _never_authored(engine, story)
        await ps.drop_outbox_jobs(engine, story)

        # A failed dispatch leaves the game's value as it is.
        story, _, fake = await _game(client, engine, epistrel, writable=True)
        failed = await _direct(client, story)
        (outbox_id,) = failed.json()["applied"]["outbox_ids"]
        fake.script_writes("gold", "failed")
        await outbox_job.dispatch(engine, outbox_id, registry=epistrel.in_process_providers)
        (sent,) = await ps.outbox_rows(engine, story)
        assert str(sent.status) == "failed"
        await _never_authored(engine, story)
        await ps.drop_outbox_jobs(engine, story)


@pytest.mark.part("M7", "FR-EXT-8; FR-DIRECT-1")
async def test_s_ext_directive__rewrite(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """A rewrite's game-owned change is refused even on a write-capable provider: alone, 422
    and no session; beside a story-owned change, listed in ``refused_changes`` while the
    story-owned change goes on."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, _, _ = await _game(client, engine, epistrel, writable=True)
        await ss.define_all(engine, story, ss.gold(name="purse", aliases=(), unit=None))
        stub = epistrel.models.stub
        gold_only = json.dumps(
            {"changes": [{"kind": "state", "state": "gold", "scope": "Cal", "value": 500}]}
        )
        stub.script(
            "rewrite_parse",
            [
                gold_only,
                json.dumps(
                    {
                        "changes": [
                            {"kind": "state", "state": "gold", "scope": "Cal", "value": 500},
                            {"kind": "state", "state": "purse", "scope": "Cal", "value": 7},
                        ]
                    }
                ),
            ],
        )
        alone = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": "Cal always had 500 gold."},
            headers=WRITE,
        )
        assert alone.status_code == 422, alone.text
        assert alone.json()["error"]["code"] == "external_value_not_overridable"
        assert alone.json()["error"]["details"]["explanation"] == (
            "a provider's past value cannot be rewritten"
        )
        assert await rs.count(engine, "rewrite_sessions", story) == 0
        mixed = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": "Cal always had 500 gold and seven coins in his purse."},
            headers=WRITE,
        )
        assert mixed.status_code == 201, mixed.text
        preview = mixed.json()["preview"]
        assert [r.get("code") for r in preview["refused_changes"]] == [
            "external_value_not_overridable"
        ]
        assert [c["state"] for c in preview["changes"]] == ["purse"]
        await _never_authored(engine, story)


@pytest.mark.part("M7", "FR-EXT-8; FR-DIRECT-1")
async def test_s_ext_directive__predicate(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#697: a fact on a predicate whose registry ``owner = external`` (#131) is the game's too:
    a durable directive setting it is 422 ``external_value_not_overridable`` (nothing written),
    and a rewrite whose only change it is is 422 with no session. Complement: the same
    predicate story-owned is applied."""
    from sqlalchemy import text

    from tests.canon_support import auto_predicate

    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, _, _ = await _game(client, engine, epistrel, writable=True)
        predicate = await auto_predicate(engine, story, "has reputation")
        async with engine.begin() as conn:
            name = await conn.scalar(
                text("SELECT name FROM predicates WHERE id = :p"), {"p": predicate}
            )
            await conn.execute(
                text("UPDATE predicates SET owner = 'external' WHERE id = :p"), {"p": predicate}
            )
        stub = epistrel.models.stub
        renown = json.dumps(
            {
                "classification": "durable",
                "temporal_scope": "now",
                "changes": [
                    {"kind": "proposition", "subject": "Cal", "predicate": name, "literal": 9}
                ],
            }
        )
        stub.when("", renown, purpose="directive_parse")
        refused = await client.post(
            f"/stories/{story}/directives",
            params=rs.OMNISCIENT,
            json={"text": "Cal's renown is nine."},
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "external_value_not_overridable"
        assert await rs.count(engine, "directives", story) == 0
        stub.when(
            "",
            json.dumps(
                {
                    "changes": [
                        {"kind": "proposition", "subject": "Cal", "predicate": name, "literal": 9}
                    ]
                }
            ),
            purpose="rewrite_parse",
        )
        alone = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": "Cal always had a renown of nine."},
            headers=WRITE,
        )
        assert alone.status_code == 422, alone.text
        assert alone.json()["error"]["code"] == "external_value_not_overridable"
        await _never_authored(engine, story)

        # Complement: story-owned again, the directive applies.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE predicates SET owner = 'story' WHERE id = :p"), {"p": predicate}
            )
        applied = await client.post(
            f"/stories/{story}/directives",
            params=rs.OMNISCIENT,
            json={"text": "Cal's renown is nine."},
            headers=WRITE,
        )
        assert applied.status_code == 200, applied.text
