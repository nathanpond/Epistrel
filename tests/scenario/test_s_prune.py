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


@pytest.mark.part("M7", "FR-STORE-18,19,28,32; FR-FB-2")
async def test_s_prune__follow_ups(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#702 (#247's not-built clauses), over S-PRUNE's tail and continuation:

    - **markers**: ``audit_view`` carries a ``prune_marker`` at the tail's first turn and the
      as-originally-recorded view lists it beside the erasures; a checkpoint inside the pruned
      range is 409 ``pruned``;
    - **replay**: refolding every projection after the pruning gives the live projections;
    - **cleanup**: an entity only the tail minted is deleted with its aliases, one an active fact
      still names is kept; a State proposal whose only evidence was a pruned event is deleted;
    - **refusals**: a feedback write to a pruned message, ``history?variant=`` of a pruned chain
      and a fork promoting it are 409 ``pruned`` (or the chain is gone); ``dormant_chains`` no
      longer lists it;
    - **guards**: ``pruned`` is terminal (the operations trigger refuses a flip back), and the
      deletes run in chunks (``CHUNK`` lowered to 1 here)."""
    from epistrel.core import prune as core_prune
    from epistrel.core.canon import facts as canon
    from epistrel.core.enums import OperationKind
    from epistrel.core.ids import new_ulid
    from epistrel.core.projections.fold import fold
    from epistrel.core.propositions import store
    from epistrel.core.propositions.model import Interval, Proposition
    from epistrel.core.propositions.vocabulary import CORE_BY_NAME
    from epistrel.core.views import Checkpoint, as_originally_recorded, audit_view
    from tests.canon_support import commit

    monkeypatch.setattr(core_prune, "CHUNK", 1)
    async with respond_client(stub_settings, engine) as (client, _):
        story, actor = await _story(client, engine)
        played = []
        for _ in range(3):
            found = await respond(client, story, respond_body(actor, FIRST))
            assert found.status_code == 200, found.text
            played.append(found.json())
        rated = played[1]["message"]["message_id"]
        assert (await rate(client, story, rated, {"rating": 2})).status_code in (200, 201)
        (tail_op,) = await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND narrative_turn = 3",
            s=story,
        )
        tail_event = played[2]["message"]["message_id"]
        async with engine.begin() as conn:
            lonely = await store.add_entity(
                conn, story, "item", "a lost glove", operation_id=str(tail_op.operation_id)
            )
            named = await store.add_entity(
                conn, story, "item", "a brass key", operation_id=str(tail_op.operation_id)
            )
            await conn.execute(
                text(
                    "INSERT INTO state_proposals (id, story_id, name, suggested_type, "
                    "suggested_scope, evidence, created_by_operation_id) VALUES (:i, :s, 'mood', "
                    "'number', 'story', CAST(:e AS jsonb), :o)"
                ),
                {
                    "i": new_ulid(),
                    "s": story,
                    "e": f'[{{"turn": 3, "event_id": "{tail_event}", "span": [0, 1], "text": ""}}]',
                    "o": str(tail_op.operation_id),
                },
            )
        await _ok(
            await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE)
        )
        # An active fact (after the truncation) still names the key.
        await commit(
            engine,
            story,
            Proposition(
                subject_id=named,
                predicate_id=CORE_BY_NAME["held_by"].id,
                object_id=actor,
                valid_time=Interval.of(None, None),
            ),
            provenance="author",
            kind=OperationKind.CONTROL,
        )
        applied = await _ok(await _prune(client, story, ignore_warnings=True))
        prune_id = applied["result"]["prune_id"]

        markers = [e for e in await audit_view(engine, story) if e.type == "prune_marker"]
        assert [(m.narrative_turn, m.payload.prune_id) for m in markers] == [(2, prune_id)]  # type: ignore[attr-defined]
        view = await as_originally_recorded(engine, story, Checkpoint(1))
        assert any(m.get("type") == "prune_marker" for m in view.erasure_markers)
        from epistrel.core.errors import Pruned

        with pytest.raises(Pruned):
            await as_originally_recorded(engine, story, Checkpoint(3))

        from datetime import datetime as _dt

        from epistrel.core.projections.base import registered_projections
        from epistrel.core.projections.fold import replace_with_fold

        async def projected() -> dict[str, list[tuple[Any, ...]]]:
            found: dict[str, list[tuple[Any, ...]]] = {}
            for projection in registered_projections():
                table = projection.table
                kept = [c.name for c in table.c]
                listed = await rows(
                    engine,
                    f"SELECT * FROM {table.name} WHERE story_id = :s",  # noqa: S608
                    s=story,
                )
                found[projection.name] = sorted(
                    tuple(
                        None if isinstance(v, _dt) else str(v)
                        for v in (getattr(r, c) for c in kept)
                    )
                    for r in listed
                )
            return found

        live = await projected()
        async with engine.begin() as conn:
            await replace_with_fold(conn, story)
        assert await projected() == live  # the replay of the post-pruning log
        del fold

        entities_left = {
            str(r.id)
            for r in await rows(engine, "SELECT id FROM entities WHERE story_id = :s", s=story)
        }
        assert lonely not in entities_left
        assert named in entities_left
        assert (
            await scalar(
                engine, "SELECT count(*) FROM entity_aliases WHERE entity_id = :e", e=lonely
            )
            == 0
        )
        assert (
            await scalar(
                engine, "SELECT count(*) FROM state_proposals WHERE story_id = :s", s=story
            )
            == 0
        )

        refused = await rate(client, story, rated, {"rating": 1})
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "pruned"
        with pytest.raises(DBAPIError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE operations SET membership = 'active' WHERE story_id = :s "
                        "AND membership = 'pruned'"
                    ),
                    {"s": story},
                )
        del canon

        # A dormant continuation, pruned: its chain is no longer readable or promotable.
        other, other_actor = await _story(client, engine)
        await _ok(await respond(client, other, respond_body(other_actor, FIRST)))
        await _ok(await client.post(f"/stories/{other}/regenerate", json={}, headers=WRITE))
        before = await _ok(await client.get(f"/stories/{other}", headers=READ))
        assert before["dormant_chains"]
        chain = before["dormant_chains"][0]
        await _ok(await _prune(client, other, ignore_warnings=True))
        after = await _ok(await client.get(f"/stories/{other}", headers=READ))
        assert after["dormant_chains"] == []
        read = await client.get(
            f"/stories/{other}/history",
            params={"variant": f"{chain['from_turn']}:{chain['variant']}"},
            headers=READ,
        )
        assert read.status_code in (404, 409), read.text
        forked = await client.post(
            f"/stories/{other}/fork",
            json={"variant_chain": {"turn": chain["from_turn"], "variant": chain["variant"]}},
            headers=WRITE,
        )
        assert forked.status_code in (404, 409), forked.text


@pytest.mark.part("M7", "FR-STORE-18,19,28,32; FR-FB-2")
async def test_s_prune__settlement_dependency(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    """#702 (#218): a tail holding a State change an active change settles
    (``settles_change_id``) is excluded whole, ``settlement_dependency`` with the settling
    change ids; complement: without the settling row the tail is a target."""
    from epistrel.core.ids import new_ulid
    from epistrel.core.states.api import StateSetRequest, set_state
    from epistrel.core.tables import state_changes
    from tests import state_support as ss

    async with respond_client(stub_settings, engine) as (client, _):
        story, actor = await _story(client, engine)
        await ss.set_level(engine, story, "prompt_only")
        await ss.define_all(engine, story, ss.gold(name="coins"))
        for _ in range(3):
            await _ok(await respond(client, story, respond_body(actor, FIRST)))
        await set_state(
            engine,
            story,
            "coins",
            StateSetRequest(value=5, scope_key=actor, reason="a purse"),
            principal="p-consumer",
        )
        (origin,) = await rows(
            engine,
            "SELECT id FROM state_changes WHERE story_id = :s ORDER BY id DESC LIMIT 1",
            s=story,
        )
        await _ok(
            await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE)
        )
        report = await _ok(await _prune(client, story))
        assert report["impact"]["to_delete"]["discarded_tails"]
        await set_state(
            engine,
            story,
            "coins",
            StateSetRequest(value=6, scope_key=actor, reason="a coin"),
            principal="p-consumer",
        )
        (active,) = await rows(
            engine,
            "SELECT id FROM state_changes WHERE story_id = :s AND membership = 'active' "
            "ORDER BY id DESC LIMIT 1",
            s=story,
        )
        columns = [c.name for c in state_changes.c if c.name not in ("id", "settles_change_id")]
        listed = ", ".join(columns)
        settling = new_ulid()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO state_changes (id, settles_change_id, {listed}) "  # noqa: S608
                    f"SELECT :n, :o, {listed} FROM state_changes WHERE id = :a"
                ),
                {"n": settling, "o": str(origin.id), "a": str(active.id)},
            )
        report = await _ok(await _prune(client, story))
        assert report["impact"]["to_delete"]["discarded_tails"] == []
        (excluded,) = report["impact"]["excluded"]
        assert excluded["reason"] == "settlement_dependency"
        assert excluded["settling_change_ids"] == [settling]
        await ss.set_level(engine, story, None)


@pytest.mark.part("M7", "FR-STORE-18,19,28,32; FR-FB-2")
async def test_s_prune__held_turn(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#702 (FR-CONC-4): a pruning is not a control action, so a turn generating while it
    commits rebases and commits (its attempt records ``rebase``), never abandoned."""
    import asyncio

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, actor = await _story(client, engine)
        for _ in range(3):
            await _ok(await respond(client, story, respond_body(actor, FIRST)))
        await _ok(
            await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE)
        )
        stub.reset()
        latch = stub.hold("generation")
        task = asyncio.ensure_future(respond(client, story, respond_body(actor, FIRST)))
        await asyncio.wait_for(latch.entered.wait(), 5)
        await _ok(await _prune(client, story, ignore_warnings=True))
        latch.release()
        played = await task
        assert played.status_code == 200, played.text
        attempts = await scalar(
            engine,
            "SELECT attempts FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND membership = 'active' ORDER BY narrative_turn DESC LIMIT 1",
            s=story,
        )
        assert [a["outcome"] for a in attempts] == ["rebase"], attempts
