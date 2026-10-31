"""Projection rebuild: a maintenance operation that bumps nothing, restores reads, and is served
at ``POST /admin/stories/{id}/rebuild``; the interim ``GET /stories/{id}/history`` reads the
transcript projection."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.errors import StoryNotFound
from epistrel.core.events import Operation
from epistrel.core.projections import rebuild
from tests.auth_support import auth_settings, bearer
from tests.event_support import execute_in_tx, new_story, play_turn, rows, scalar, story_row
from tests.projection_support import snapshot, truncate, untruncate

PRINCIPAL = {"X-Epistrel-Principal": "p-operator"}
COUNT_EVENTS = "SELECT count(*) FROM events WHERE story_id = :s"


async def _story_with_history(engine: AsyncEngine) -> str:
    story_id = await new_story(engine)
    for n in range(1, 4):
        await play_turn(engine, story_id, f"turn {n}", rejected=f"draft {n}")
    await truncate(engine, story_id, 1)
    await untruncate(engine, story_id)
    await truncate(engine, story_id, 2)
    return story_id


def _drop_projection_rows(story_id: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    return (
        ("DELETE FROM transcript_messages WHERE story_id = :s", {"s": story_id}),
        ("DELETE FROM story_heads WHERE story_id = :s", {"s": story_id}),
    )


def _with_engine[T](url: str, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    """Run ``work`` on its own engine and event loop (the TestClient runs the app on another)."""

    async def run() -> T:
        engine = make_engine(auth_settings(url))
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def test_rebuild_no_revision_bump(engine: AsyncEngine) -> None:
    """A rebuild leaves `timeline_revision` (and every story counter) as it was, records one
    committed `maintenance` operation with no event. Complement: a turn does bump it."""
    story_id = await _story_with_history(engine)
    before = await story_row(engine, story_id)
    events_before = await scalar(engine, COUNT_EVENTS, s=story_id)

    result = await rebuild(engine, story_id, principal="p-operator")

    assert await story_row(engine, story_id) == before
    assert await scalar(engine, COUNT_EVENTS, s=story_id) == events_before
    (op,) = await rows(
        engine,
        "SELECT kind, status, principal, narrative_turn FROM operations WHERE operation_id = :o",
        o=result.operation_id,
    )
    assert tuple(op) == ("maintenance", "committed", "p-operator", None)
    assert result.counts == {
        "transcript_messages": 3 * 2,
        "story_heads": 1,
        "story_texts": 1,
        "lite_summary_segments": 0,
        # The structured projections over canon (#137): a Lite story has none.
        "relationships": 0,
        "placement": 0,
        "scene_state": 0,
        # The belief projection (#159): no partition in a Lite story.
        "belief_state": 0,
        "nested_belief_state": 0,
        "state_values": 0,
    }

    await play_turn(engine, story_id, "turn 3, again")
    assert (await story_row(engine, story_id))[0] == before[0] + 1


async def test_rebuild_unknown_story(engine: AsyncEngine) -> None:
    with pytest.raises(StoryNotFound):
        await rebuild(engine, "0" * 26)
    async with engine.connect() as conn:
        with pytest.raises(StoryNotFound):
            await rebuild(conn, "0" * 26)


async def test_rebuild_accepts_a_connection(engine: AsyncEngine) -> None:
    story_id = await _story_with_history(engine)
    incremental = await snapshot(engine, story_id)
    await execute_in_tx(engine, *_drop_projection_rows(story_id))
    async with engine.connect() as conn:
        await rebuild(conn, story_id)
    assert await snapshot(engine, story_id) == incremental


async def test_heads_follow_turns_and_truncation(engine: AsyncEngine) -> None:
    """Heads start empty at genesis, record each turn's variant (a regenerated variant replaces
    the earlier one), and drop the turns a truncation discards."""
    story_id = await new_story(engine)
    head = "SELECT next_turn_index, active_variants FROM story_heads WHERE story_id = :s"
    assert tuple((await rows(engine, head, s=story_id))[0]) == (1, {})
    for n in range(1, 4):
        await play_turn(engine, story_id, f"turn {n}")
    await play_turn(engine, story_id, "turn 3, regenerated", turn=3)
    assert tuple((await rows(engine, head, s=story_id))[0]) == (4, {"1": 1, "2": 1, "3": 2})
    await truncate(engine, story_id, 1)
    assert tuple((await rows(engine, head, s=story_id))[0]) == (2, {"1": 1})


async def test_story_less_and_empty_operations_touch_no_projection(engine: AsyncEngine) -> None:
    story_id = await new_story(engine)
    before = await snapshot(engine, story_id)
    async with Operation(
        engine, story_id=None, kind=OperationKind.CONTROL, principal="p-test"
    ) as op:
        op.append("config_changed", {"setting": "s"}, provenance=Provenance.SYSTEM)
        await op.commit()
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.CONTROL, principal="p-test"
    ) as op:
        await op.commit()
    assert await snapshot(engine, story_id) == before


def test_history_survives_dropping_and_rebuilding_projections(pg_url: str) -> None:
    """With both projections' rows dropped, history has nothing to read; after the rebuild
    endpoint runs, `GET /stories/{id}/history` answers exactly as before."""
    story_id = _with_engine(pg_url, _story_with_history)
    with TestClient(create_app(auth_settings(pg_url))) as client:
        url = f"/stories/{story_id}/history"
        before = client.get(url, headers=bearer())
        assert before.status_code == 200
        body = before.json()
        assert [(i["turn"], i["kind"], i["text"]) for i in body["items"]] == [
            (1, "player_input", "turn 1"),
            (1, "message_rendered", "reply to turn 1"),
            (2, "player_input", "turn 2"),
            (2, "message_rendered", "reply to turn 2"),
        ]
        assert {i["channel"] for i in body["items"]} == {"message"}

        _with_engine(pg_url, lambda engine: execute_in_tx(engine, *_drop_projection_rows(story_id)))
        assert client.get(url, headers=bearer()).json()["items"] == []

        rebuilt = client.post(
            f"/admin/stories/{story_id}/rebuild", headers={**bearer(), **PRINCIPAL}
        )
        assert rebuilt.status_code == 200
        assert rebuilt.json()["counts"] == {
            "transcript_messages": 6,
            "story_heads": 1,
            "story_texts": 1,
            "lite_summary_segments": 0,
            # The structured projections over canon (#137): a Lite story has none.
            "relationships": 0,
            "placement": 0,
            "scene_state": 0,
            # The belief projection (#159): no partition in a Lite story.
            "belief_state": 0,
            "nested_belief_state": 0,
            "state_values": 0,
        }
        assert len(rebuilt.json()["operation_id"]) == 26

        after = client.get(url, headers=bearer())
    assert after.status_code == 200
    assert after.json() == body


def test_rebuild_endpoint_refusals(pg_url: str) -> None:
    """The rebuild is a mutation: no principal → 400 before anything runs; no credential → 401;
    an unknown story → 404 for the rebuild and for history."""
    story_id = _with_engine(pg_url, new_story)
    with TestClient(create_app(auth_settings(pg_url))) as client:
        path = f"/admin/stories/{story_id}/rebuild"
        assert client.post(path, headers=bearer()).status_code == 400
        assert client.post(path, headers=PRINCIPAL).status_code == 401
        missing = client.post(
            f"/admin/stories/{'0' * 26}/rebuild", headers={**bearer(), **PRINCIPAL}
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        assert client.get(f"/stories/{'0' * 26}/history", headers=bearer()).status_code == 404
    maintenance = _with_engine(
        pg_url,
        lambda engine: scalar(
            engine,
            "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'maintenance'",
            s=story_id,
        ),
    )
    assert maintenance == 0
