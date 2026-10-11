"""docs/04 TIME, M2 slices: (b) truncation only flips membership and untruncation restores the
prior view exactly; (e) narrative position comes from turn operations only; (f) one active
variant per position, forming a single chain — through regenerate and swap too (#66: the REST
surface, the variant listing, reads along a dormant chain, and the refusals)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import timeline, variants
from epistrel.core.errors import UnknownChain
from epistrel.core.ids import new_ulid
from epistrel.core.variants import ChainRef, SwapRequest
from epistrel.core.views import audit_view, corrected_history
from tests.auth_support import auth_settings
from tests.concurrency_support import assert_single_chain, chain, regenerate
from tests.event_support import interleave, narrative, new_story, play_turn, rows, story_row
from tests.gate_support import audit_rows, input_policies
from tests.projection_support import snapshot, swap, truncate, untruncate
from tests.respond_support import PLAYER, body, generation_calls, played, respond_client
from tests.respond_support import new_story as new_rest_story
from tests.story_support import READ, WRITE

TURNS = 3


async def _rows(engine: AsyncEngine, story: str, table: str) -> list[dict[str, object]]:
    found = await rows(engine, f"SELECT * FROM {table} WHERE story_id = :s", s=story)  # noqa: S608
    return sorted((dict(r._mapping) for r in found), key=lambda r: str(r.get("operation_id")))


async def _views(engine: AsyncEngine, story: str, head: int) -> list[list[str]]:
    """The corrected history, whole and through every turn, as event ids."""
    views = [await corrected_history(engine, story)]
    views += [await corrected_history(engine, story, n) for n in range(head + 1)]
    return [[e.id for e in view] for view in views]


@pytest.mark.part("M2", "FR-STORE-10,11")
async def test_time__b(engine: AsyncEngine) -> None:
    """For every checkpoint N of a story with background work between its turns: truncation
    only flips membership (every event row persists byte for byte, valid-time included; an
    operation changes only ``membership``/``membership_tx``), and untruncation restores every
    view through every turn and every projection row exactly. Complement: while truncated,
    the views after N differ from the originals."""
    story = await new_story(engine)
    for n in range(1, TURNS + 2):
        await interleave(engine, story, after_turn=n - 1)
        await play_turn(engine, story, f"turn {n}")
    head = TURNS + 1
    for checkpoint in range(head):
        events_before = await _rows(engine, story, "events")
        ops_before = {r["operation_id"]: r for r in await _rows(engine, story, "operations")}
        views_before = await _views(engine, story, head)
        projections_before = await snapshot(engine, story)

        await truncate(engine, story, checkpoint)
        events_after = await _rows(engine, story, "events")
        known = {e["id"] for e in events_before}
        assert [e for e in events_after if e["id"] in known] == events_before
        assert [e["type"] for e in events_after if e["id"] not in known] == ["truncate"]
        for row in await _rows(engine, story, "operations"):
            old = ops_before.get(str(row["operation_id"]))
            if old is not None:
                flipped = {**row, "membership": old["membership"]}
                assert {**flipped, "membership_tx": old["membership_tx"]} == old
        truncated = await _views(engine, story, head)
        assert truncated[1 : checkpoint + 2] == views_before[1 : checkpoint + 2]
        assert truncated[0] != views_before[0]

        assert await untruncate(engine, story) is not None
        assert await _views(engine, story, head) == views_before
        assert await snapshot(engine, story) == projections_before


@pytest.mark.part("M2", "FR-STORE-8,24,25")
async def test_time__e(engine: AsyncEngine) -> None:
    """Background, audit and control events between turns never move a record's narrative
    position or change the narrative contents of any view. Complement: those events are
    recorded (seq interleaves them) and the audit view shows them."""
    quiet = await new_story(engine)
    busy = await new_story(engine)
    for n in range(1, TURNS + 1):
        await interleave(engine, busy, after_turn=n - 1)
        quiet_turn = await play_turn(engine, quiet, f"turn {n}")
        busy_turn = await play_turn(engine, busy, f"turn {n}")
        assert (quiet_turn.narrative_turn, quiet_turn.turn_variant) == (n, 1)
        assert (busy_turn.narrative_turn, busy_turn.turn_variant) == (n, 1)
    await interleave(engine, busy, after_turn=TURNS)

    # seq interleaves the extra work: the same turn content sits at different seq values.
    quiet_all = await audit_view(engine, quiet)
    busy_all = await audit_view(engine, busy)
    assert len(busy_all) > len(quiet_all)
    quiet_seq = {(e.type, e.payload): e.seq for e in quiet_all if e.narrative_turn}
    busy_seq = {(e.type, e.payload): e.seq for e in busy_all if e.category.value == "input"}
    assert any(busy_seq[k] != quiet_seq[k] for k in busy_seq)

    # Positions and narrative contents agree for every view, whole history and through each N.
    assert narrative(quiet_all) == narrative(busy_all)
    for through in (None, *range(1, TURNS + 1)):
        quiet_view = await corrected_history(engine, quiet, through)
        busy_view = await corrected_history(engine, busy, through)
        assert narrative(quiet_view) == narrative(busy_view), through
        assert {e.category.value for e in busy_view}.isdisjoint({"audit"})
        assert all(e.type != "config_changed" for e in busy_view)

    # Only turns advanced the turn index; the extra work is in the audit view.
    assert (await story_row(engine, quiet))[2] == (await story_row(engine, busy))[2] == TURNS + 1
    extra = {e.type for e in busy_all} - {e.type for e in quiet_all}
    assert extra == {"config_changed", "guardrail_output_rejected", "lite_summary_segment"}


@pytest.mark.part("M2", "FR-STORE-28")
async def test_time__f(engine: AsyncEngine) -> None:
    """After every turn, regeneration, truncation, untruncation and swap, exactly one variant
    per position is active and the active variants form a single chain (turns 1..k, next
    index k+1). Complement: the inactive variants are still there, dormant or discarded."""
    story = await new_story(engine)
    steps: list[Callable[[], Awaitable[object]]] = [
        lambda: play_turn(engine, story, "one"),
        lambda: play_turn(engine, story, "two"),
        lambda: regenerate(engine, story),
        lambda: play_turn(engine, story, "three"),
        lambda: truncate(engine, story, 1),
        lambda: untruncate(engine, story),
        lambda: truncate(engine, story, 1),
        lambda: play_turn(engine, story, "two, played differently"),
        lambda: regenerate(engine, story),
        lambda: swap(engine, story),
        lambda: regenerate(engine, story),
    ]
    for step in steps:
        await step()
        await assert_single_chain(engine, story)

    assert [t for t, _ in await chain(engine, story)] == [1, 2, 3]
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT narrative_turn, membership, count(*) FROM operations "
                "WHERE story_id = :s AND kind = 'turn' AND status = 'committed' "
                "GROUP BY 1, 2 ORDER BY 1, 2"
            ),
            {"s": story},
        )
        by_position = {(t, m): int(n) for t, m, n in found}
    assert all(by_position[(t, "active")] == 1 for t in (1, 2, 3))
    assert sum(n for (t, m), n in by_position.items() if m != "active") == 4


PRINCIPAL = "p-author"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _code(response: Any) -> str:
    code: str = response.json()["error"]["code"]
    return code


async def _membership(engine: AsyncEngine, story: str, turn: int, variant: int) -> str:
    (row,) = await rows(
        engine,
        "SELECT membership FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND narrative_turn = :t AND turn_variant = :v",
        s=story,
        t=turn,
        v=variant,
    )
    return str(row.membership)


def _replies(history: dict[str, Any]) -> list[tuple[int, int, str]]:
    return [(t["turn"], t["variant"], t["message"]["text"]) for t in history["turns"]]


@pytest.mark.part("M2", "FR-STORE-12")
async def test_time__regenerate(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``POST /regenerate`` plays the head turn again from its recorded input as variant
    ``v + 1`` (idempotent, expected revision honoured, input re-gated): the old variant is
    dormant, not deleted, and still readable with ``?variant=``; ``swap`` brings it back and
    swapping to the active chain writes nothing. ``time`` (f): one active variant per position
    at every point. Complement: a stale revision, a non-head ``turn``, a story with no turn, and
    an unknown chain are refused with nothing written."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_rest_story(client)
        empty = await client.post(f"/stories/{story}/regenerate", headers=WRITE)
        assert (empty.status_code, _code(empty)) == (422, "nothing_to_regenerate")

        await played(client, story, body(ids[PLAYER], "First words."))
        first = await played(client, story, body(ids[PLAYER], "Who goes there?"))
        await assert_single_chain(engine, story)
        before = await client.get(f"/stories/{story}", headers=READ)
        revision = before.json()["timeline_revision"]

        stale = await client.post(
            f"/stories/{story}/regenerate",
            headers={**WRITE, "X-Epistrel-Expected-Revision": str(revision - 1)},
        )
        assert (stale.status_code, _code(stale)) == (409, "revision_conflict")
        not_head = await client.post(
            f"/stories/{story}/regenerate", json={"turn": 1}, headers=WRITE
        )
        assert not_head.status_code == 422
        assert await chain(engine, story) == [(1, 1), (2, 1)]

        lib.models.stub.reset()
        key = {
            **WRITE,
            "Idempotency-Key": new_ulid(),
            "X-Epistrel-Expected-Revision": str(revision),
        }
        again = await client.post(f"/stories/{story}/regenerate", json={"turn": 2}, headers=key)
        assert again.status_code == 200, again.text
        result = again.json()
        assert (result["turn"], result["variant"], result["previous_variant"]) == (2, 2, 1)
        assert result["timeline_revision"] == revision + 1
        assert result["output_viewpoint"] == first["output_viewpoint"]
        assert result["message"]["message_id"] != first["message"]["message_id"]
        (call,) = generation_calls(lib.models.stub)
        assert "Who goes there?" in call.messages[-1].content  # the recorded input, reused
        assert "First words." in "\n".join(m.content for m in call.messages)
        assert first["message"]["text"] not in "\n".join(m.content for m in call.messages[1:])
        replay = await client.post(f"/stories/{story}/regenerate", json={"turn": 2}, headers=key)
        assert replay.json() == result
        assert replay.headers.get("Idempotent-Replay") == "true"
        assert await chain(engine, story) == [(1, 1), (2, 2)]
        assert await _membership(engine, story, 2, 1) == "dormant"
        await assert_single_chain(engine, story)

        listed = await client.get(f"/stories/{story}/variants", headers=READ)
        assert [
            (c["turn"], c["variant"], c["membership"], c["tip_turn"], c["parent"])
            for c in listed.json()["items"]
        ] == [
            (1, 1, "active", 1, None),
            (2, 1, "dormant", 2, {"turn": 1, "variant": 1}),
            (2, 2, "active", 2, {"turn": 1, "variant": 1}),
        ]
        read = (await client.get(f"/stories/{story}", headers=READ)).json()
        assert read["variant_chains"] == listed.json()["items"]
        assert [(c["from_turn"], c["variant"]) for c in read["dormant_chains"]] == [(2, 1)]

        current = await client.get(f"/stories/{story}/history", headers=READ)
        old = await client.get(f"/stories/{story}/history?variant=2:1", headers=READ)
        assert old.status_code == 200, old.text
        assert _replies(current.json())[-1][:2] == (2, 2)
        assert _replies(old.json())[-1] == (2, 1, first["message"]["text"])
        assert _replies(old.json())[0] == _replies(current.json())[0]
        bad = await client.get(f"/stories/{story}/history?variant=2", headers=READ)
        assert bad.status_code == 422
        unknown_read = await client.get(f"/stories/{story}/history?variant=2:9", headers=READ)
        assert (unknown_read.status_code, _code(unknown_read)) == (409, "unknown_chain")

        swapped = await client.post(
            f"/stories/{story}/swap",
            json={"to_variant_chain": {"turn": 2, "variant": 1}},
            headers=WRITE,
        )
        assert swapped.status_code == 200, swapped.text
        assert swapped.json()["active_chain"] == {"turn": 2, "variant": 1}
        assert (swapped.json()["activated_operations"], swapped.json()["dormant_operations"]) == (
            1,
            1,
        )
        assert await chain(engine, story) == [(1, 1), (2, 1)]
        await assert_single_chain(engine, story)
        same = await client.post(
            f"/stories/{story}/swap", json={"to_variant_chain": "2:1"}, headers=WRITE
        )
        assert same.status_code == 200
        assert same.json()["operation_id"] is None
        assert same.json()["timeline_revision"] == swapped.json()["timeline_revision"]
        unknown = await client.post(
            f"/stories/{story}/swap",
            json={"to_variant_chain": {"turn": 5, "variant": 1}},
            headers=WRITE,
        )
        assert (unknown.status_code, _code(unknown)) == (409, "unknown_chain")

        # Regenerating again takes the next free variant; overrides replace recorded values.
        third = await client.post(
            f"/stories/{story}/regenerate",
            json={"output_viewpoint": "omniscient", "prompt_params": {"length": "short"}},
            headers=WRITE,
        )
        assert third.status_code == 200, third.text
        assert (third.json()["variant"], third.json()["previous_variant"]) == (3, 1)
        assert third.json()["output_viewpoint"] == "omniscient"
        await assert_single_chain(engine, story)

        # The reused input goes through the input gate again (#56).
        await input_policies(lib, story, {"rule": {"type": "wordlist", "words": ["goes"]}})
        gated = await client.post(f"/stories/{story}/regenerate", headers=WRITE)
        assert (gated.status_code, _code(gated)) == (422, "guardrail_violation")
        assert (await audit_rows(engine, story_id=story))[-1].details["entry_point"] == (
            "regenerate"
        )
        assert await chain(engine, story) == [(1, 1), (2, 3)]


@pytest.mark.part("M2", "FR-STORE-28")
async def test_time__swap_chains(engine: AsyncEngine) -> None:
    """A chain that continues in more than one way is 409 ``unknown_chain`` with the candidate
    chains; a candidate swaps in the whole path from where it parts from the active chain, and
    swapping back restores the projections exactly. A turn written over a rewound tail takes
    the next free variant and makes the tail dormant. Complement: the membership hooks see the
    swap's changes, one per (old, new) pair."""
    story = await new_story(engine)
    for n in range(1, 4):
        await play_turn(engine, story, f"turn {n}")
    await regenerate(engine, story)  # 3.2 active, 3.1 dormant
    await truncate(engine, story, 1)  # 2.1 and 3.2 discarded
    over = await play_turn(engine, story, "turn 2, played differently")
    assert (over.narrative_turn, over.turn_variant) == (2, 2)
    assert await _membership(engine, story, 3, 2) == "dormant"
    before = await snapshot(engine, story)
    seen: list[timeline.MembershipChange] = []

    async def hook(conn: Any, change: timeline.MembershipChange) -> None:
        seen.append(change)

    timeline.on_membership_change(hook)
    try:
        with pytest.raises(UnknownChain) as ambiguous:
            await timeline.swap(
                engine,
                story,
                SwapRequest(to_variant_chain=ChainRef(turn=2, variant=1)),
                principal=PRINCIPAL,
            )
        assert ambiguous.value.details["candidates"] == [
            {"turn": 3, "variant": 1},
            {"turn": 3, "variant": 2},
        ]
        chains = {(c.turn, c.variant): c for c in await variants.list_chains(engine, story)}
        assert chains[(3, 1)].parent == ChainRef(turn=2, variant=1)
        done = await timeline.swap(
            engine,
            story,
            SwapRequest(to_variant_chain=ChainRef(turn=3, variant=2)),
            principal=PRINCIPAL,
        )
        assert done.active_chain == ChainRef(turn=2, variant=1)
        assert await chain(engine, story) == [(1, 1), (2, 1), (3, 2)]
        await assert_single_chain(engine, story)
        assert [(c.cause, c.old, c.new, len(c.operation_ids)) for c in seen] == [
            ("swap", "active", "dormant", 1),
            ("swap", "dormant", "active", 2),
        ]
        await timeline.swap(
            engine,
            story,
            SwapRequest(to_variant_chain=ChainRef(turn=2, variant=2)),
            principal=PRINCIPAL,
        )
        assert await snapshot(engine, story) == before
    finally:
        timeline.remove_membership_hook(hook)
