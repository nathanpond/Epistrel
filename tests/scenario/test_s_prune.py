"""S-PRUNE (docs/04 §7; FR-STORE-18,19,28,32, FR-FB-2; #247): manual pruning in two stages.

A Lite story plays three turns, rates its second reply, and truncates to turn 1 (a discarded
tail); it plays a turn and regenerates it (a dormant continuation). Pruning without
``ignore_warnings`` reports both targets and changes nothing; with it, one control operation
deletes their records (the operations stay as ``pruned`` markers, the rating is kept and marked
``pruned``, audit events are kept), writes a ``pruning`` marker and moves the revision by one.
Afterwards the untruncate and the swap back are 409 ``pruned``. A story with nothing to prune
writes nothing. The events guard refuses a pruning delete outside its own conditions, and every
table with an operation reference is classified. The policy slice (#248): a story that opts
into ``prune_policy`` has its tails older than the policy pruned once by the daily run, listed
at ``prune-runs``; a story with the policy off, or a younger tail, is left alone.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.prune import PRUNABLE_TABLES, PRUNE_RETAINED_TABLES, run_policy
from epistrel.core.schema import metadata
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows, scalar
from tests.feedback_support import rate
from tests.respond_support import FIRST, respond, respond_client
from tests.respond_support import body as respond_body
from tests.story_support import WRITE, create, story_body

pytestmark = pytest.mark.integration

READ = bearer()


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json()
    return found


async def _story(client: Any, engine: AsyncEngine) -> tuple[str, str]:
    story = (await create(client, story_body("Wren", "Ada")))["story_id"]
    cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
    return story, next(str(c.id) for c in cast if c.name == "Wren")


async def _counts(engine: AsyncEngine, story: str) -> tuple[int, ...]:
    return tuple(
        [
            await scalar(engine, f"SELECT count(*) FROM {t} WHERE story_id = :s", s=story)  # noqa: S608
            for t in ("events", "operations", "transcript_messages", "message_feedback")
        ]
        + [await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)]
    )


def _prune(client: Any, story: str, **body: Any) -> Any:
    return client.post(f"/stories/{story}/prune", json=body, headers=WRITE)


@pytest.mark.part("M7", "FR-STORE-18,19,28,32; FR-FB-2")
async def test_s_prune__manual(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, _):
        # A discarded tail: three turns, the second reply rated, truncated to turn 1.
        story, actor = await _story(client, engine)
        played = []
        for _ in range(3):
            found = await respond(client, story, respond_body(actor, FIRST))
            assert found.status_code == 200, found.text
            played.append(found.json())
        rated = played[1]["message"]["message_id"]
        rated_answer = await rate(client, story, rated, {"rating": 2})
        assert rated_answer.status_code in (200, 201), rated_answer.text
        await _ok(
            await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE)
        )

        before = await _counts(engine, story)
        report = await _ok(await _prune(client, story))
        assert report["applied"] is False
        assert report["result"] is None
        impact = report["impact"]
        (tail,) = impact["to_delete"]["discarded_tails"]
        assert (tail["from"]["turn"], tail["to"]["turn"]) == (2, 3)
        assert {o["operation"] for o in impact["lost_operations"]} == {"untruncate"}
        assert impact["retained"]["feedback_marked"] == 1
        assert await _counts(engine, story) == before

        applied = await _ok(await _prune(client, story, ignore_warnings=True))
        assert applied["applied"] is True
        result = applied["result"]
        assert result["timeline_revision"] == before[-1] + 1
        assert result["deleted"]["events"] > 0
        pruned = await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND membership = 'pruned'",
            s=story,
        )
        assert len(pruned) == result["deleted"]["operations"]
        left = await rows(
            engine,
            "SELECT e.category FROM events e JOIN operations o ON o.operation_id = e.operation_id "
            "WHERE e.story_id = :s AND o.membership = 'pruned'",
            s=story,
        )
        assert {r.category for r in left} <= {"audit", "amendment"}
        (marker,) = await rows(
            engine, "SELECT payload FROM events WHERE story_id = :s AND type = 'pruning'", s=story
        )
        assert marker.payload["prune_id"] == result["prune_id"]
        (feedback,) = await rows(
            engine,
            "SELECT pruned_at FROM message_feedback WHERE story_id = :s AND message_id = :m",
            s=story,
            m=rated,
        )
        assert feedback.pruned_at is not None
        untruncate = await client.post(f"/stories/{story}/untruncate", json={}, headers=WRITE)
        assert untruncate.status_code == 409, untruncate.text
        assert untruncate.json()["error"]["code"] == "pruned"
        # The active chain still plays.
        await _ok(await respond(client, story, respond_body(actor, FIRST)))

        # A dormant continuation: a regenerated turn's first variant.
        other, other_actor = await _story(client, engine)
        await _ok(await respond(client, other, respond_body(other_actor, FIRST)))
        await _ok(await client.post(f"/stories/{other}/regenerate", json={}, headers=WRITE))
        report = await _ok(await _prune(client, other))
        (dormant,) = report["impact"]["to_delete"]["dormant_continuations"]
        assert report["impact"]["suggestions"][0]["code"] == "promote_before_pruning"
        await _ok(await _prune(client, other, ignore_warnings=True))
        back = await client.post(
            f"/stories/{other}/swap",
            json={"to_variant_chain": dict(dormant["from"])},
            headers=WRITE,
        )
        assert back.status_code in (404, 409, 422), back.text


async def test_s_prune__nothing_eligible(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A story with no inactive record: pruning with ``ignore_warnings`` writes nothing."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, actor = await _story(client, engine)
        await _ok(await respond(client, story, respond_body(actor, FIRST)))
        before = await _counts(engine, story)
        found = await _ok(await _prune(client, story, ignore_warnings=True))
        assert found["applied"] is False
        assert await _counts(engine, story) == before


async def test_prune_trigger_guard(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The events guard admits a pruning delete only under ``epistrel.allow_prune`` for the
    story and only for a non-audit event of a ``pruned`` operation."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, actor = await _story(client, engine)
        await _ok(await respond(client, story, respond_body(actor, FIRST)))
    (event,) = await rows(
        engine,
        "SELECT e.id FROM events e JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND o.kind = 'turn' AND e.category <> 'audit' LIMIT 1",
        s=story,
    )

    async def attempt(guc: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('epistrel.allow_prune', :g, true)"), {"g": guc}
            )
            await conn.execute(text("DELETE FROM events WHERE id = :e"), {"e": event.id})

    for guc in ("", story):
        with pytest.raises(DBAPIError):
            await attempt(guc)
    assert await scalar(engine, "SELECT count(*) FROM events WHERE id = :e", e=event.id) == 1


def test_prune_table_coverage() -> None:
    """Every table with an operation reference is either pruned or retained."""
    pruned = {t.name for t in PRUNABLE_TABLES}
    assert not pruned & PRUNE_RETAINED_TABLES
    referencing = {
        t.name
        for t in metadata.tables.values()
        if any(c.name == "operation_id" or c.name.endswith("_operation_id") for c in t.c)
    }
    unclassified = referencing - pruned - PRUNE_RETAINED_TABLES
    assert unclassified == set(), sorted(unclassified)


async def _aged_tail(client: Any, engine: AsyncEngine, age_days: int) -> str:
    """A story with three turns truncated to turn 1, the tail flipped ``age_days`` ago."""
    story = (await create(client, story_body("Wren", "Ada")))["story_id"]
    cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
    actor = next(str(c.id) for c in cast if c.name == "Wren")
    for _ in range(3):
        found = await respond(client, story, respond_body(actor, FIRST))
        assert found.status_code == 200, found.text
    cut = await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE)
    assert cut.status_code == 200, cut.text
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE operations SET membership_tx = membership_tx - make_interval(days => :d) "
                "WHERE story_id = :s AND membership = 'discarded'"
            ),
            {"s": story, "d": age_days},
        )
    return str(story)


async def _discarded(engine: AsyncEngine, story: str) -> int:
    return int(
        await scalar(
            engine,
            "SELECT count(*) FROM operations WHERE story_id = :s AND membership = 'discarded'",
            s=story,
        )
    )


@pytest.mark.part("M7", "FR-STORE-30,32")
async def test_s_prune__policy(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, _):
        opted = await _aged_tail(client, engine, 10)
        kept = await _aged_tail(client, engine, 10)
        young = await _aged_tail(client, engine, 2)
        revision = await scalar(
            engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=opted
        )
        for story in (opted, young):
            patched = await client.patch(
                f"/stories/{story}/settings",
                json={"prune_policy": {"older_than_days": 7}},
                headers=WRITE,
            )
            assert patched.status_code == 200, patched.text
        # Setting the policy deletes nothing and moves no revision.
        assert await _discarded(engine, opted) > 0
        assert (
            await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=opted)
            == revision
        )
        bad = await client.patch(
            f"/stories/{opted}/settings",
            json={"prune_policy": {"older_than_days": 0}},
            headers=WRITE,
        )
        assert bad.status_code == 422, bad.text

        first = await run_policy(engine, clock.now())
        assert first["pruned"] >= 1
        assert await _discarded(engine, opted) == 0
        assert await _discarded(engine, kept) > 0  # the policy is off
        assert await _discarded(engine, young) > 0  # younger than the policy
        again = await run_policy(engine, clock.now() + timedelta(minutes=1))
        assert again["pruned"] == 0 or await _discarded(engine, opted) == 0

        runs = await client.get(f"/stories/{opted}/prune-runs", headers=READ)
        assert runs.status_code == 200, runs.text
        (run,) = runs.json()["prune_runs"]
        assert (run["trigger"], run["policy"]) == ("policy", {"older_than_days": 7})
        assert run["principal"] == "system"
        none = await client.get(f"/stories/{kept}/prune-runs", headers=READ)
        assert none.json()["prune_runs"] == []
