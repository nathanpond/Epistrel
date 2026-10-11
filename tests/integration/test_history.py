"""``GET /stories/{id}/history`` (#59) beyond C-12: the window (``as_of``, paging by whole
turns), the per-turn reply summary, the refusal order, and the request-model checks."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest
from epistrel.core.viewpoint import validate_default_viewpoint, with_requested
from tests.auth_support import auth_settings
from tests.respond_support import FIRST, PLAYER, body, new_story, played, respond_client
from tests.story_support import READ, WRITE


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _history(client: httpx.AsyncClient, story: str, **params: Any) -> httpx.Response:
    return await client.get(f"/stories/{story}/history", params=params, headers=READ)


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def _code(response: httpx.Response) -> tuple[int, str, Any]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"].get("field")


async def test_window_and_paging(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``as_of`` reads through a turn (0: genesis only); pages hold whole turns, newest first,
    ascending within a page, and ``next_before_turn`` walks back to the first turn; a turn
    larger than ``limit`` is returned whole."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        for n in range(1, 5):
            await played(client, story, body(ids[PLAYER], f"line {n}", do=f"act {n}"))
        whole = _ok(await _history(client, story))
        assert (whole["as_of"], whole["next_before_turn"]) == (4, None)
        assert [i["turn"] for i in whole["items"]] == [t for t in (1, 2, 3, 4) for _ in range(3)]
        assert [t["turn"] for t in whole["turns"]] == [1, 2, 3, 4]

        through_two = _ok(await _history(client, story, as_of=2))
        assert {i["turn"] for i in through_two["items"]} == {1, 2}
        assert _ok(await _history(client, story, as_of=0))["items"] == []

        pages, before = [], None
        while True:
            params: dict[str, Any] = {"limit": 5}
            if before is not None:
                params["before_turn"] = before
            page = _ok(await _history(client, story, **params))
            pages.append(sorted({i["turn"] for i in page["items"]}))
            before = page["next_before_turn"]
            if before is None:
                break
        assert pages == [[4], [3], [2], [1]]
        two_turns = _ok(await _history(client, story, limit=6))
        assert (sorted({i["turn"] for i in two_turns["items"]}), two_turns["next_before_turn"]) == (
            [3, 4],
            3,
        )
        tiny = _ok(await _history(client, story, limit=1))
        assert [i["channel"] for i in tiny["items"]] == ["say", "do", "message"]


async def test_reply_less_turn_has_a_null_message(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """An ingested player turn has no reply: its turn summary's ``message`` is null, while a
    played turn's names its reply (#58's hand-off)."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        played_turn = await played(client, story, body(ids[PLAYER], "Hello."))
        recorded = await client.post(
            f"/stories/{story}/ingest",
            json={
                "events": [
                    {
                        "client_event_id": "h-1",
                        "kind": "player_turn",
                        "actor_character_id": ids[FIRST],
                        "say": "I say nothing more.",
                    }
                ]
            },
            headers=WRITE,
        )
        assert recorded.status_code == 201, recorded.text
        turns = _ok(await _history(client, story))["turns"]
        assert turns == [
            {"turn": 1, "variant": 1, "message": played_turn["message"]},
            {"turn": 2, "variant": 1, "message": None},
        ]


async def test_refusals_and_their_order(stub_settings: Settings, engine: AsyncEngine) -> None:
    """404 for an unknown story; 422 for an impossible window or an unknown actor, before the
    viewpoint; every 422 before the 403 for audit records under a character viewpoint."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        await played(client, story, body(ids[PLAYER]))
        unknown = await _history(client, "0" * 26)
        assert unknown.status_code == 404
        cases: list[tuple[dict[str, Any], tuple[int, str, str]]] = [
            ({"as_of": 2}, (422, "validation_error", "query.as_of")),
            ({"as_of": -1}, (422, "validation_error", "query.as_of")),
            ({"limit": 0}, (422, "validation_error", "query.limit")),
            ({"limit": 1001}, (422, "validation_error", "query.limit")),
            ({"before_turn": 0}, (422, "validation_error", "query.before_turn")),
            ({"actor_character_id": "x"}, (422, "validation_error", "query.actor_character_id")),
            ({"as_of": "x"}, (422, "validation_error", "query.as_of")),
            (
                {"viewpoint": "nobody", "include_audit": 1},
                (422, "viewpoint_unresolved", "query.viewpoint"),
            ),
            (
                {"as_of": 9, "viewpoint": f"character:{ids[FIRST]}", "include_audit": 1},
                (422, "validation_error", "query.as_of"),
            ),
            (
                {"viewpoint": f"character:{ids[FIRST]}", "include_audit": 1},
                (403, "omniscient_required", "query.include_audit"),
            ),
        ]
        for params, expected in cases:
            assert _code(await _history(client, story, **params)) == expected, params
        assert (
            _ok(await _history(client, story, viewpoint="omniscient", include_audit=1))["audit"]
            == []
        )


async def test_two_named_viewpoints_must_agree(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """The body's ``output_viewpoint`` and ``?viewpoint=`` (or the library keyword) name one
    viewpoint: the same value is accepted, two different ones are 422."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        vp = f"character:{ids[FIRST]}"
        agreed = await client.post(
            f"/stories/{story}/respond",
            json=body(ids[PLAYER], output_viewpoint=vp),
            params={"viewpoint": vp},
            headers=WRITE,
        )
        assert agreed.status_code == 200, agreed.text
        assert agreed.json()["output_viewpoint"] == vp
        clash = await client.post(
            f"/stories/{story}/respond",
            json=body(ids[PLAYER], output_viewpoint=vp),
            params={"viewpoint": "omniscient"},
            headers=WRITE,
        )
        assert _code(clash) == (422, "validation_error", "body.output_viewpoint")
        with pytest.raises(InvalidRequest):
            await lib.history(RequestContext(viewpoint=vp), story, viewpoint="omniscient")


def test_viewpoint_helpers() -> None:
    """The pure rules: a default may only be a cast member's character viewpoint; a context's
    viewpoint is replaced only when a named value fills it."""
    assert validate_default_viewpoint(None, ["a"]) is None
    assert validate_default_viewpoint(" character:a ", ["a"]) == "character:a"
    for bad in ("a", "character:b"):
        with pytest.raises(Exception, match="names no character") as refused:
            validate_default_viewpoint(bad, ["a"])
        assert getattr(refused.value, "code", None) == "viewpoint_unresolved"
    ctx = RequestContext(viewpoint="omniscient")
    assert with_requested(ctx) is ctx
    assert with_requested(ctx, None, " omniscient") is ctx
    padded = with_requested(RequestContext(viewpoint=" omniscient"), "omniscient")
    assert padded.viewpoint == "omniscient"
    assert with_requested(RequestContext(), "character:a").viewpoint == "character:a"
