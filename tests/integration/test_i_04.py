"""docs/04 I-04, M2 truncate and fork parts (FR-STORE-10,11,15,18; #65, #66, #67): truncation
marks records after N discarded without deleting rows or touching valid-time, the view through N
is right, and untruncation restores them (by a swap when newer turns exist); a fork is a full,
independent copy of the history through N (no shared rows, no cross-story references) that
diverges freely; the REST surfaces, their refusals and the membership hooks."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import projections, timeline, turn_gate
from epistrel.core.config_changes import read_settings
from epistrel.core.errors import InvalidRequest, NothingToUndo, StoryNotFound, UnknownChain
from epistrel.core.ids import new_ulid
from epistrel.core.memory import records
from epistrel.core.memory.consolidation import Window
from epistrel.core.ratings import RatingFields, create_rating
from epistrel.core.stories import create_story, delete_story
from epistrel.core.variants import ChainRef
from epistrel.core.views import corrected_history
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import consolidation_support as cs
from tests.auth_support import auth_settings
from tests.concurrency_support import (
    assert_single_chain,
    chain,
    regenerate,
    reject_render,
    set_instructions,
    set_pins,
    summarise,
)
from tests.event_support import new_story, play_turn, rows, scalar, story_row
from tests.fork_support import content, fork, mentions, owned_ids, owned_rows
from tests.memory_support import derived_memory
from tests.projection_support import snapshot, truncate, untruncate
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
    Newer turns over a truncation make its undo a swap (#66: the original tail active, the
    newer turn dormant); a truncation that is not the most recent, or none, is 409
    ``nothing_to_undo``. Complement: a turn regenerated before the truncation point does not
    block the undo: the tail comes back with the variant it was built on (a swap)."""
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
    over_regenerated = await _untruncate(engine, story, second.operation_id)
    assert over_regenerated.next_turn_index == 5
    assert over_regenerated.swapped  # the tail continues variant 1 of turn 2
    assert await chain(engine, story) == [(1, 1), (2, 1), (3, 1), (4, 1)]
    plain = await _untruncate(engine, story)
    assert (plain.truncate_operation_id, plain.swapped) == (first.operation_id, False)
    assert [t for t, _ in await chain(engine, story)] == [1, 2, 3, 4, 5, 6]
    with pytest.raises(NothingToUndo) as empty:
        await _untruncate(engine, story)
    # The regenerated variant of turn 2 is dormant: a swap, not an untruncate, reaches it.
    assert empty.value.details["options"] == ["swap", "fork"]
    assert empty.value.details["dormant_chains"] == [{"turn": 2, "variant": 2}]

    third = await _truncate(engine, story, 3)
    await play_turn(engine, story, "turn 4, played differently")
    state = await timeline.undo_state(engine, story)
    assert state.undo_available
    assert [
        (c.truncate_operation_id, c.from_turn, c.variant, c.through_turn)
        for c in state.dormant_chains
    ] == [(None, 2, 2, 2), (third.operation_id, 4, 1, 6)]
    swapped = await _untruncate(engine, story)
    assert (swapped.truncate_operation_id, swapped.swapped) == (third.operation_id, True)
    assert swapped.active_chain == ChainRef(turn=4, variant=1)
    assert (swapped.restored_operations, swapped.dormant_operations) == (3, 1)
    assert await chain(engine, story) == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1)]
    state = await timeline.undo_state(engine, story)
    assert [(c.from_turn, c.variant) for c in state.dormant_chains] == [(2, 2), (4, 2)]
    with pytest.raises(InvalidRequest):
        await _truncate(engine, story, 6)  # the head
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
        assert (dormant["from_turn"], dormant["variant"], dormant["through_turn"]) == (3, 1, 3)
        assert dormant["turns"] == 1
        swap = await client.post(f"/stories/{story}/untruncate", headers=WRITE)
        assert swap.status_code == 200, swap.text
        assert swap.json()["swapped"] is True
        assert swap.json()["active_chain"] == {"turn": 3, "variant": 1}
        assert swap.json()["dormant_operations"] == 1

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


@pytest.mark.part("M2", "FR-STORE-12")
async def test_i_04__regenerate(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Regenerating the latest turn N truncates to the boundary just before it: only turn N's
    operation leaves the active timeline (``dormant``, its row and events kept), every row of
    the turns before N is unchanged, the history through N - 1 is the same, and a new variant
    of N is the one active chain. Complement: the head turn's old operation and events still
    exist, unchanged apart from their membership."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_rest_story(client)
        for n in range(1, 4):
            await played(client, story, body(ids[PLAYER], f"line {n}"))
        ops_before = await _operations(engine, story)
        events_before = {e.id: dict(e._mapping) for e in await _events(engine, story)}
        through_2 = await client.get(f"/stories/{story}/history?as_of=2", headers=READ)
        (head_op,) = [o for o, r in ops_before.items() if r["narrative_turn"] == 3]

        regenerated = await client.post(f"/stories/{story}/regenerate", headers=WRITE)
        assert regenerated.status_code == 200, regenerated.text

        ops_after = await _operations(engine, story)
        assert ops_before.keys() <= ops_after.keys()  # nothing deleted
        for op_id, row in ops_before.items():
            if op_id == head_op:
                assert ops_after[op_id]["membership"] == "dormant"
                changed = {k for k, v in row.items() if ops_after[op_id][k] != v}
                assert changed <= {"membership", "membership_tx"}
            else:
                assert ops_after[op_id] == row
        events_after = {e.id: dict(e._mapping) for e in await _events(engine, story)}
        assert all(events_after[i] == e for i, e in events_before.items())
        assert await chain(engine, story) == [(1, 1), (2, 1), (3, 2)]
        await assert_single_chain(engine, story)
        again = await client.get(f"/stories/{story}/history?as_of=2", headers=READ)
        assert again.json()["items"] == through_2.json()["items"]


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


# -- fork (#67) -------------------------------------------------------------------------------


async def _forkable(engine: AsyncEngine) -> str:
    """Five turns with everything a fork must handle: an audit event inside a turn, pinned
    memories and instructions, a summary segment, maintenance work, a regenerated turn (its old
    variant dormant) and a truncated, rewritten tail (a dormant continuation). Its content rating
    is not the deployment default, so a fork that fell back to the default would show (#296)."""
    rating = await create_rating(
        engine, RatingFields(name=f"fork-{new_ulid()}"), principal="p-test"
    )
    story = await create_story(
        engine, principal="p-test", title="a test story", content_rating_id=rating.rating.id
    )
    await play_turn(engine, story, "turn 1")
    await play_turn(engine, story, "turn 2", rejected="a rejected reply")
    await set_pins(engine, story, "the key is under the mat")
    await set_instructions(engine, story, "keep it short")
    await play_turn(engine, story, "turn 3")
    await summarise(engine, story, 3)
    await reject_render(engine, story, 3)
    await play_turn(engine, story, "turn 4")
    await regenerate(engine, story)  # 4:2 active, 4:1 dormant
    await play_turn(engine, story, "turn 5")
    await _truncate(engine, story, 4)
    await play_turn(engine, story, "turn 5, again")  # 5:2 active, 5:1 dormant
    return story


async def _rating(engine: AsyncEngine, story: str) -> tuple[str, str]:
    """The story's content rating and that rating's current version."""
    (row,) = await rows(
        engine,
        "SELECT s.content_rating_id, r.current_version_id FROM stories s "
        "JOIN content_ratings r ON r.id = s.content_rating_id WHERE s.id = :s",
        s=story,
    )
    return str(row[0]), str(row[1])


@pytest.mark.part("M2", "FR-STORE-14,15")
async def test_i_04__fork(engine: AsyncEngine) -> None:
    """A fork through N is a full copy of the corrected history through N under new ids: the
    same history, cast (with true ages), pins, instructions, settings and summary segments, a
    fresh head at N + 1 and revision 1, its own first event naming where it came from, projections
    equal to a rebuild, and no row naming the source. The source records a ``fork`` event and
    keeps its revision. Complement: what is not in the history through N (audit events,
    maintenance work, turns after N, dormant variants, the timeline edits) is not copied."""
    story = await _forkable(engine)
    revision = (await story_row(engine, story))[0]
    rating = await _rating(engine, story)
    assert rating[0] != (await read_settings(engine)).default_content_rating_id
    for at_turn, cut in ((None, 5), (3, 3), (0, 0)):
        done = await fork(engine, story, at_turn)
        assert done.forked_from.story_id == story
        assert (done.forked_from.revision, done.forked_from.at_turn) == (revision, cut)
        assert (done.timeline_revision, done.next_turn_index) == (1, cut + 1)
        assert done.name == "a test story (fork)"
        new = done.story_id
        assert await story_row(engine, new) == (1, 0, cut + 1, "active")
        mine = await content(engine, new, skip_operation=done.operation_id)
        theirs = await content(engine, story, cut)
        assert mine.history == theirs.history
        assert (mine.texts, mine.settings) == (theirs.texts, theirs.settings)
        assert mine.cast == theirs.cast
        assert mine.heads["active_variants"] == {
            str(t): v for t, v in await chain(engine, story) if t <= cut
        }
        assert await chain(engine, new) == [c for c in await chain(engine, story) if c[0] <= cut]
        await assert_single_chain(engine, new)
        types = [r.type for r in await _events(engine, new)]
        assert types[-1] == "story_created"
        assert not {"truncate", "untruncate", "swap", "fork"} & set(types)
        assert "guardrail_output_rejected" not in types
        assert ("lite_summary_segment" in types) == (cut >= 3)
        kinds = await rows(
            engine,
            "SELECT DISTINCT kind::text, membership::text, status::text FROM operations "
            "WHERE story_id = :s",
            s=new,
        )
        assert "maintenance" not in {k for k, _, _ in kinds}
        assert {(m, st) for _, m, st in kinds} == {("active", "committed")}
        before = await snapshot(engine, new)
        await projections.rebuild(engine, new)  # a maintenance operation, written after
        assert await snapshot(engine, new) == before
        origin = await scalar(
            engine,
            "SELECT payload FROM events WHERE operation_id = :o",
            o=done.operation_id,
        )
        assert origin["forked_from"] == {
            "story_id": story,
            "revision": revision,
            "at_turn": cut,
            "variant_chain": None,
        }
        assert [c["id"] for c in origin["characters"]] == [c.id for c in done.characters]
        # #48's fork clause: the fork keeps the source's rating, at its current version.
        assert await _rating(engine, new) == rating
        assert (origin["content_rating_id"], origin["rating_version_id"]) == rating

        # No shared rows, and nothing in the fork names the source, except the provenance
        # the fork's own story_created event records.
        source_ids = await owned_ids(engine, story)
        assert not source_ids & await owned_ids(engine, new)
        copied = await owned_rows(engine, new)
        provenance = {r for r in copied["events"] if done.operation_id in r}
        assert len(provenance) == 1
        copied["events"] -= provenance
        assert mentions(copied, source_ids) == {}
        assert mentions({"events": provenance}, source_ids - {story}) == {}

    assert (await story_row(engine, story))[0] == revision
    recorded = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'fork' ORDER BY seq",
        s=story,
    )
    assert [(p["at_turn"], p["revision"]) for (p,) in recorded] == [
        (5, revision),
        (3, revision),
        (0, revision),
    ]
    with pytest.raises(InvalidRequest):
        await fork(engine, story, 6)
    with pytest.raises(UnknownChain):
        await fork(engine, story, None, ChainRef(turn=2, variant=7))
    with pytest.raises(StoryNotFound):
        await fork(engine, new_ulid())


@pytest.mark.part("M2", "FR-STORE-14..16")
async def test_i_04__fork_isolation(engine: AsyncEngine) -> None:
    """After forking, a turn, an edit and a truncation on either story change nothing in the
    other (row-level diff), each story keeps one chain and the fork diverges freely; deleting
    the source removes every row of its own and leaves the fork whole. Complement: the change
    did land on its own story."""
    story = await _forkable(engine)
    done = await fork(engine, story, 4)
    new = done.story_id

    source_before = await owned_rows(engine, story)
    await play_turn(engine, new, "turn 5 of the fork")
    await play_turn(engine, new, "turn 6 of the fork")
    await set_pins(engine, new, "only in the fork")
    await _truncate(engine, new, 5)
    assert await owned_rows(engine, story) == source_before
    assert await chain(engine, new) == [(1, 1), (2, 1), (3, 1), (4, 2), (5, 1)]
    await assert_single_chain(engine, new)

    fork_before = await owned_rows(engine, new)
    await play_turn(engine, story, "turn 6 of the source")
    await set_instructions(engine, story, "only in the source")
    await _truncate(engine, story, 2)
    assert await owned_rows(engine, new) == fork_before
    assert await chain(engine, story) == [(1, 1), (2, 1)]
    await assert_single_chain(engine, story)

    await delete_story(engine, story, principal=PRINCIPAL)
    assert all(not found for found in (await owned_rows(engine, story)).values())
    assert await owned_rows(engine, new) == fork_before
    await play_turn(engine, new, "turn 6 of the fork, again")
    await assert_single_chain(engine, new)


@pytest.mark.part("M2", "FR-STORE-15")
async def test_i_04__fork_rest(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``POST /stories/{id}/fork`` over REST: 201 with ``Location``, the new story readable
    with the source's history through N, story policies and ingested dedupe rows copied and
    remapped, an idempotent retry, the post-commit hook with cause ``fork``, an archived source
    forked into an active story, and the refusals. Complement: the source's revision is
    unchanged by every fork."""
    seen: list[tuple[str, str, int]] = []

    async def after(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
        seen.append((change.cause, change.story_id, len(change.operation_ids)))

    timeline.on_membership_change(after, phase=timeline.HookPhase.POST_COMMIT)
    try:
        async with respond_client(stub_settings, engine) as (client, _):
            story, ids = await new_rest_story(client)
            for n in range(1, 4):
                await played(client, story, body(ids[PLAYER], f"line {n}"))
            ingested = await client.post(
                f"/stories/{story}/ingest",
                json={"events": [{"client_event_id": "e-1", "kind": "narration", "text": "Rain."}]},
                headers=WRITE,
            )
            assert ingested.status_code == 201, ingested.text
            own = {"name": "Mine", "rule": {"type": "wordlist", "words": ["y"]}, "message": "No."}
            mine = {**own, "name": "Theirs", "scope": "character", "scope_id": ids[PLAYER]}
            patched = await client.patch(
                f"/stories/{story}/settings", json={"policies": [own, mine]}, headers=WRITE
            )
            assert patched.status_code == 200, patched.text
            read = await client.get(f"/stories/{story}", headers=READ)
            revision = read.json()["timeline_revision"]
            history = await client.get(f"/stories/{story}/history?as_of=3", headers=READ)

            key = {**WRITE, "Idempotency-Key": new_ulid()}
            made = await client.post(
                f"/stories/{story}/fork", json={"at_turn": 3, "name": "Branch"}, headers=key
            )
            assert made.status_code == 201, made.text
            answer = made.json()
            new = answer["story_id"]
            assert made.headers["Location"] == f"/stories/{new}"
            assert answer["timeline_revision"] == 1
            assert answer["next_turn_index"] == 4
            assert answer["name"] == "Branch"
            assert answer["forked_from"] == {
                "story_id": story,
                "revision": revision,
                "at_turn": 3,
                "variant_chain": None,
            }
            assert [c["source_id"] for c in answer["characters"]] == list(ids.values())
            assert answer["counts"]["characters"] == 3
            assert answer["counts"]["client_events"] == 0  # the narration is turn 4
            again = await client.post(
                f"/stories/{story}/fork", json={"at_turn": 3, "name": "Branch"}, headers=key
            )
            assert (again.status_code, again.json()) == (201, answer)
            assert again.headers.get("Idempotent-Replay") == "true"
            assert seen == [("fork", new, answer["counts"]["operations"])]

            copy = await client.get(f"/stories/{new}", headers=READ)
            assert copy.status_code == 200
            assert copy.json()["name"] == "Branch"
            assert copy.json()["next_turn_index"] == 4
            fork_history = await client.get(f"/stories/{new}/history", headers=READ)
            renamed = {c["source_id"]: c["id"] for c in answer["characters"]}
            assert [(i["turn"], i["channel"], i["text"]) for i in fork_history.json()["items"]] == [
                (i["turn"], i["channel"], i["text"]) for i in history.json()["items"]
            ]
            assert [i["actor_character_id"] for i in fork_history.json()["items"]] == [
                renamed.get(i["actor_character_id"], i["actor_character_id"])
                for i in history.json()["items"]
            ]
            copied = await rows(
                engine,
                "SELECT scope, scope_id, name FROM policies WHERE story_id = :s ORDER BY name",
                s=new,
            )
            assert [tuple(r) for r in copied] == [
                ("story", new, "Mine"),
                ("character", renamed[ids[PLAYER]], "Theirs"),
            ]

            whole = await client.post(f"/stories/{story}/fork", headers=WRITE)
            assert whole.status_code == 201, whole.text
            dedupe = await rows(
                engine,
                "SELECT client_event_id, operation_id, result FROM client_events "
                "WHERE story_id = :s",
                s=whole.json()["story_id"],
            )
            assert [d.client_event_id for d in dedupe] == ["e-1"]
            (only,) = dedupe
            assert only.result["operation_id"] == only.operation_id
            assert (
                await scalar(
                    engine,
                    "SELECT story_id FROM operations WHERE operation_id = :o",
                    o=only.operation_id,
                )
                == whole.json()["story_id"]
            )

            for bad in ({"at_turn": 9}, {"at_turn": -1}, {"extra": 1}, {"variant_chain": "x"}):
                refused = await client.post(f"/stories/{story}/fork", json=bad, headers=WRITE)
                assert refused.status_code == 422, bad
            unknown = await client.post(
                f"/stories/{story}/fork", json={"variant_chain": "2:9"}, headers=WRITE
            )
            assert (unknown.status_code, _code(unknown)) == (409, "unknown_chain")
            stale = await client.post(
                f"/stories/{story}/fork",
                headers={**WRITE, "X-Epistrel-Expected-Revision": str(revision - 1)},
            )
            assert (stale.status_code, _code(stale)) == (409, "revision_conflict")
            missing = await client.post(f"/stories/{new_ulid()}/fork", headers=WRITE)
            assert missing.status_code == 404

            await client.post(f"/stories/{story}/archive", headers=WRITE)
            frozen = await client.post(f"/stories/{story}/fork", headers=WRITE)
            assert frozen.status_code == 201, frozen.text
            thawed = await client.get(f"/stories/{frozen.json()['story_id']}", headers=READ)
            assert thawed.json()["status"] == "active"
            read = await client.get(f"/stories/{story}", headers=READ)
            assert read.json()["timeline_revision"] == revision
    finally:
        timeline.remove_membership_hook(after)


@pytest.mark.part("M4", "FR-STORE-13")
async def test_i_04__derived(
    engine: AsyncEngine, pg_url: str, drained_embedding_jobs: None
) -> None:
    """A derived memory is in the view through N iff every one of its sources is active at or
    before N (#143). A consolidation output over turns 1-4 (positioned at 4) and a position-less
    semantic memory over the events of turns 2 and 5: through 4 the first is in and the second
    out (a source is at 5); through 5 both are in; truncating to 3 takes both out (the output
    with its window, the other with its source at 5), rows kept; untruncating brings both
    back. Complement: the episodes through 3 never leave the view."""
    models = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    models.bind(engine)
    w = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    played = await cs.bulk_turns(engine, story, 6, actor=bobby, name="Bobby", does=True)
    models.stub.script(
        "consolidation", [cs.answer(facts=[("Bobby is busy.", "observation", [0, 3])])]
    )
    made = await consolidate_window(engine, story, Window(1, 4), models=models)
    assert str(made.outcome) == "committed"
    loose = await derived_memory(
        engine, story, "Bobby keeps a log.", [played[1].event_id, played[4].event_id]
    )

    async def view(through: int | None = None) -> set[str]:
        async with engine.connect() as conn:
            return {m.text for m in await records.memories_through(conn, story, through)}

    assert {"Bobby is busy."} <= await view(4)
    assert "Bobby keeps a log." not in await view(4)
    assert {"Bobby is busy.", "Bobby keeps a log."} <= await view(5)
    await truncate(engine, story, 3)
    after = await view()
    assert {"Bobby is busy.", "Bobby keeps a log."}.isdisjoint(after)
    assert {f"Bobby does: line {n}" for n in (1, 2, 3)} <= after
    kept = await rows(engine, "SELECT id FROM memories WHERE story_id = :s", s=story)
    assert loose in {r.id for r in kept}
    await untruncate(engine, story)
    assert {"Bobby is busy.", "Bobby keeps a log."} <= await view()
