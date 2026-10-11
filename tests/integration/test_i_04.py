"""docs/04 I-04, M2 truncate parts (FR-STORE-10,11,18; #65): truncation marks records after N
discarded without deleting rows or touching valid-time, the view through N is right, and
untruncation restores them; the REST surface, its refusals and the membership hooks."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import timeline, turn_gate
from epistrel.core.errors import InvalidRequest, NothingToUndo, SwapRequired
from epistrel.core.ids import new_ulid
from epistrel.core.views import corrected_history
from tests.auth_support import auth_settings
from tests.concurrency_support import assert_single_chain, chain, regenerate
from tests.event_support import new_story, play_turn, rows, scalar, story_row
from tests.projection_support import snapshot
from tests.respond_support import PLAYER, body, played, respond_client
from tests.respond_support import new_story as new_rest_story
from tests.story_support import READ, WRITE

PRINCIPAL = "p-author"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _truncate(engine: AsyncEngine, story: str, to_turn: int) -> timeline.Truncation:
    return await timeline.truncate(
        engine, story, timeline.TruncateRequest(to_turn=to_turn), principal=PRINCIPAL
    )


async def _untruncate(
    engine: AsyncEngine, story: str, named: str | None = None
) -> timeline.Untruncation:
    return await timeline.untruncate(
        engine,
        story,
        timeline.UntruncateRequest(truncate_operation_id=named),
        principal=PRINCIPAL,
    )


async def _operations(engine: AsyncEngine, story: str) -> dict[str, Any]:
    found = await rows(
        engine,
        "SELECT * FROM operations WHERE story_id = :s AND status = 'committed' "
        "AND kind <> 'control'",
        s=story,
    )
    return {r.operation_id: dict(r._mapping) for r in found}


async def _events(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(engine, "SELECT * FROM events WHERE story_id = :s ORDER BY seq", s=story)


def _texts(view: list[Any]) -> list[str]:
    return [str(e.payload.model_dump()["text"]) for e in view if e.narrative_turn]


@pytest.mark.part("M2", "FR-STORE-10,11,18")
async def test_i_04__truncate(engine: AsyncEngine) -> None:
    """Truncating to N flips only the operations after N to ``discarded`` (stamped with the
    truncation's transaction time); no row is deleted and no valid-time moves; the view
    through N and the whole view are the history through N; untruncation restores the prior
    view, chain and projections. Complement: the operations through N keep their stamp."""
    story = await new_story(engine)
    for n in range(1, 6):
        await play_turn(engine, story, f"turn {n}")
    before_ops = await _operations(engine, story)
    before_events = await _events(engine, story)
    before_view = await corrected_history(engine, story)
    before_projections = await snapshot(engine, story)
    revision = (await story_row(engine, story))[0]

    done = await _truncate(engine, story, 2)
    assert (done.to_turn, done.next_turn_index, done.discarded_operations) == (2, 3, 3)
    assert done.timeline_revision == revision + 1
    tx = await scalar(
        engine, "SELECT committed_at FROM operations WHERE operation_id = :o", o=done.operation_id
    )
    after_ops = await _operations(engine, story)
    assert after_ops.keys() == before_ops.keys()
    for op_id, row in after_ops.items():
        old = before_ops[op_id]
        if (row["narrative_turn"] or 0) > 2:
            assert (row["membership"], row["membership_tx"]) == ("discarded", tx)
        else:
            assert row == old
        assert {
            **row,
            "membership": old["membership"],
            "membership_tx": old["membership_tx"],
        } == old
    events_after = await _events(engine, story)
    assert [tuple(e) for e in events_after[: len(before_events)]] == [
        tuple(e) for e in before_events
    ]
    assert [e.type for e in events_after[len(before_events) :]] == ["truncate"]
    assert _texts(await corrected_history(engine, story)) == _texts(
        await corrected_history(engine, story, 2)
    )
    assert "turn 3" not in _texts(await corrected_history(engine, story))
    assert await chain(engine, story) == [(1, 1), (2, 1)]
    heads = await scalar(
        engine, "SELECT to_jsonb(h) FROM story_heads h WHERE story_id = :s", s=story
    )
    assert heads["next_turn_index"] == 3
    assert heads["active_variants"] == {"1": 1, "2": 1}
    transcript = await rows(
        engine,
        "SELECT narrative_turn, membership FROM transcript_messages WHERE story_id = :s",
        s=story,
    )
    assert {m for t, m in transcript if t > 2} == {"discarded"}
    assert {m for t, m in transcript if t <= 2} == {"active"}

    restored = await _untruncate(engine, story)
    assert restored.truncate_operation_id == done.operation_id
    assert (restored.next_turn_index, restored.restored_operations) == (6, 3)
    assert [e.id for e in await corrected_history(engine, story)] == [e.id for e in before_view]
    assert await snapshot(engine, story) == before_projections
    untx = await scalar(
        engine,
        "SELECT committed_at FROM operations WHERE operation_id = :o",
        o=restored.operation_id,
    )
    for op_id, row in (await _operations(engine, story)).items():
        old = before_ops[op_id]
        assert {**row, "membership_tx": old["membership_tx"]} == old
        moved = (row["narrative_turn"] or 0) > 2
        assert row["membership_tx"] == (untx if moved else old["membership_tx"])
    await assert_single_chain(engine, story)


@pytest.mark.part("M2", "FR-STORE-10,18")
async def test_i_04__undo_stack(engine: AsyncEngine) -> None:
    """Undo is last in, first out: a stacked truncation discards only what is still active
    (earlier discards keep their stamp), and two untruncations restore both in reverse order.
    Newer turns over a truncation make its undo a swap (409 ``swap_required``); a truncation
    that is not the most recent, or none, is 409 ``nothing_to_undo``. Complement: a turn
    regenerated before the truncation point does not block the reactivation."""
    story = await new_story(engine)
    for n in range(1, 7):
        await play_turn(engine, story, f"turn {n}")
    first = await _truncate(engine, story, 4)
    stamped = await rows(
        engine,
        "SELECT operation_id, membership_tx FROM operations WHERE story_id = :s "
        "AND narrative_turn > 4 ORDER BY 1",
        s=story,
    )
    second = await _truncate(engine, story, 2)
    assert second.discarded_operations == 2
    assert (
        await rows(
            engine,
            "SELECT operation_id, membership_tx FROM operations WHERE story_id = :s "
            "AND narrative_turn > 4 ORDER BY 1",
            s=story,
        )
        == stamped
    )

    with pytest.raises(NothingToUndo) as stale:
        await _untruncate(engine, story, first.operation_id)
    assert stale.value.http_status == 409
    await regenerate(engine, story)  # turn 2 gets a new variant; nothing after 2 is active
    assert (await _untruncate(engine, story, second.operation_id)).next_turn_index == 5
    assert [t for t, _ in await chain(engine, story)] == [1, 2, 3, 4]
    assert (await _untruncate(engine, story)).truncate_operation_id == first.operation_id
    assert [t for t, _ in await chain(engine, story)] == [1, 2, 3, 4, 5, 6]
    with pytest.raises(NothingToUndo):
        await _untruncate(engine, story)

    third = await _truncate(engine, story, 3)
    await play_turn(engine, story, "turn 4, played differently")
    state = await timeline.undo_state(engine, story)
    assert state.undo_available
    assert [
        (c.truncate_operation_id, c.from_turn, c.through_turn) for c in state.dormant_chains
    ] == [(third.operation_id, 4, 6)]
    with pytest.raises(SwapRequired) as swap:
        await _untruncate(engine, story)
    assert swap.value.details == {
        "truncate_operation_id": third.operation_id,
        "to_turn": 3,
        "options": ["swap", "fork"],
    }
    with pytest.raises(InvalidRequest):
        await _truncate(engine, story, 4)  # the head
    with pytest.raises(InvalidRequest):
        await _truncate(engine, story, -1)
    await _truncate(engine, story, 0)
    assert await chain(engine, story) == []
    assert (await story_row(engine, story))[2] == 1


@pytest.mark.part("M2", "FR-STORE-10")
async def test_i_04__queued_turns_and_hooks(engine: AsyncEngine) -> None:
    """A turn waiting in the story's queue fails with ``timeline_changed`` in the truncation's
    transaction, and its queue row goes; registered membership hooks see each change, in the
    transaction and after it, and a failing post-commit hook does not undo the edit.
    Complement: a failing in-transaction hook rolls the truncation back."""
    story = await new_story(engine)
    for n in range(1, 4):
        await play_turn(engine, story, f"turn {n}")
    queued = new_ulid()
    await turn_gate.enqueue(
        engine,
        story,
        operation_id=queued,
        kind=turn_gate.RESPOND,
        request={},
        principal=PRINCIPAL,
        idempotency_key=None,
        viewpoint=None,
        origin="generated",
        queue_max=5,
    )
    seen: list[tuple[str, str, int, bool]] = []

    async def inside(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
        seen.append((change.cause, change.new.value, len(change.operation_ids), True))

    async def after(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
        seen.append((change.cause, change.new.value, len(change.operation_ids), False))
        raise RuntimeError("a hook bug")

    timeline.on_membership_change(inside)
    timeline.on_membership_change(after, phase=timeline.HookPhase.POST_COMMIT)
    try:
        done = await _truncate(engine, story, 1)
        await _untruncate(engine, story)
    finally:
        timeline.remove_membership_hook(inside)
        timeline.remove_membership_hook(after)
    assert seen == [
        ("truncate", "discarded", 2, True),
        ("truncate", "discarded", 2, False),
        ("untruncate", "active", 2, True),
        ("untruncate", "active", 2, False),
    ]
    outcome = await turn_gate.outcome(engine, story, queued)
    assert (outcome.done, outcome.status, outcome.error_code) == (
        True,
        "failed",
        "timeline_changed",
    )
    assert outcome.error is not None
    assert outcome.error["details"]["control_operation_id"] == done.operation_id
    assert await rows(engine, "SELECT * FROM turn_queue WHERE story_id = :s", s=story) == []

    async def broken(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
        raise RuntimeError("a hook bug")

    timeline.on_membership_change(broken)
    revision = (await story_row(engine, story))[0]
    try:
        with pytest.raises(RuntimeError):
            await _truncate(engine, story, 1)
    finally:
        timeline.remove_membership_hook(broken)
    assert (await story_row(engine, story))[0] == revision
    assert [t for t, _ in await chain(engine, story)] == [1, 2, 3]


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


@pytest.mark.part("M2", "FR-STORE-10,11,18")
async def test_i_04__rest(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``POST /stories/{id}/truncate`` and ``…/untruncate`` over REST: the answers, the
    history read as of a turn, ``GET /stories/{id}``'s ``undo_available`` and
    ``dormant_chains``, the refusals, and an idempotent retry. Complement: a read as of a turn
    past the truncated head is 422, and the same read before the truncation returned it."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_rest_story(client)
        for n in range(1, 4):
            await played(client, story, body(ids[PLAYER], f"line {n}"))
        read = await client.get(f"/stories/{story}", headers=READ)
        assert (read.json()["undo_available"], read.json()["dormant_chains"]) == (False, [])
        as_of_3 = await client.get(f"/stories/{story}/history?as_of=3", headers=READ)
        assert as_of_3.status_code == 200
        through_1 = await client.get(f"/stories/{story}/history?as_of=1", headers=READ)

        for bad in ({"to_turn": 3}, {"to_turn": -1}, {"to_turn": 1, "extra": 1}, {}):
            refused = await client.post(f"/stories/{story}/truncate", json=bad, headers=WRITE)
            assert refused.status_code == 422, bad
        nothing = await client.post(f"/stories/{story}/untruncate", headers=WRITE)
        assert (nothing.status_code, _code(nothing)) == (409, "nothing_to_undo")

        key = {**WRITE, "Idempotency-Key": new_ulid()}
        cut = await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=key)
        assert cut.status_code == 200, cut.text
        answer = cut.json()
        assert answer["next_turn_index"] == 2
        assert answer["to_turn"] == 1
        assert answer["discarded_operations"] == 2
        assert answer["idempotency_key"] == key["Idempotency-Key"]
        again = await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=key)
        assert again.json() == answer
        assert again.headers.get("Idempotent-Replay") == "true"

        history = await client.get(f"/stories/{story}/history", headers=READ)
        assert history.json()["as_of"] == 1
        assert history.json()["items"] == through_1.json()["items"]
        past = await client.get(f"/stories/{story}/history?as_of=3", headers=READ)
        assert past.status_code == 422
        read = await client.get(f"/stories/{story}", headers=READ)
        assert (read.json()["undo_available"], read.json()["dormant_chains"]) == (True, [])
        assert read.json()["next_turn_index"] == 2

        stale = await client.post(
            f"/stories/{story}/untruncate",
            json={"truncate_operation_id": new_ulid()},
            headers=WRITE,
        )
        assert (stale.status_code, _code(stale)) == (409, "nothing_to_undo")
        back = await client.post(
            f"/stories/{story}/untruncate",
            json={"truncate_operation_id": answer["operation_id"]},
            headers=WRITE,
        )
        assert back.status_code == 200, back.text
        assert back.json()["next_turn_index"] == 4
        assert back.json()["restored_operations"] == 2
        restored = await client.get(f"/stories/{story}/history?as_of=3", headers=READ)
        assert restored.json()["items"] == as_of_3.json()["items"]

        await client.post(f"/stories/{story}/truncate", json={"to_turn": 2}, headers=WRITE)
        await played(client, story, body(ids[PLAYER], "line 3, again"))
        read = await client.get(f"/stories/{story}", headers=READ)
        assert read.json()["undo_available"] is True
        (dormant,) = read.json()["dormant_chains"]
        assert (dormant["from_turn"], dormant["through_turn"]) == (3, 3)
        assert dormant["operations"] >= 1
        swap = await client.post(f"/stories/{story}/untruncate", headers=WRITE)
        assert (swap.status_code, _code(swap)) == (409, "swap_required")
        assert swap.json()["error"]["details"]["options"] == ["swap", "fork"]

        archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
        assert archived.status_code == 200
        refused = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE
        )
        assert (refused.status_code, _code(refused)) == (409, "story_archived")
        missing = await client.post(
            f"/stories/{new_ulid()}/truncate", json={"to_turn": 0}, headers=WRITE
        )
        assert missing.status_code == 404


@pytest.mark.part("M2", "FR-STORE-10")
async def test_i_04__timeline_moved(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that commits between a truncation's read and its commit is discarded too: the
    plan is recomputed under the story lock and the same operation re-reads and restages.
    Complement: the attempt is recorded with reason ``timeline_moved``."""
    story = await new_story(engine)
    for n in range(1, 4):
        await play_turn(engine, story, f"turn {n}")
    original = timeline._plan_truncate
    interleaved: list[str] = []

    async def plan_then_play(conn: AsyncConnection, story_id: str, to_turn: int) -> Any:
        planned = await original(conn, story_id, to_turn)
        if not interleaved:
            interleaved.append((await play_turn(engine, story, "turn 4")).operation_id)
        return planned

    monkeypatch.setattr(timeline, "_plan_truncate", plan_then_play)
    done = await _truncate(engine, story, 1)
    assert done.discarded_operations == 3
    attempts = await scalar(
        engine, "SELECT attempts FROM operations WHERE operation_id = :o", o=done.operation_id
    )
    assert [a["reason"] for a in attempts] == ["timeline_moved"]
    membership = await scalar(
        engine, "SELECT membership FROM operations WHERE operation_id = :o", o=interleaved[0]
    )
    assert membership == "discarded"
    assert await chain(engine, story) == [(1, 1)]
