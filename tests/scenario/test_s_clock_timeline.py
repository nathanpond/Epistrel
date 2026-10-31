"""S-CLOCK-TIMELINE: the clock is a State (docs/04 §7; FR-CLOCK-1,3,5,13,14, FR-STATE-4; #193).

Every S-CLOCK-TIMELINE M6 part lives in this file: #193's (``test_s_clock_timeline``), #195's
anchoring (``__anchor``) and #209's external mode (``__external``).

#193's part, played through the REST app with the stub adapter on a Lite story running the
clock at ``prompt_only`` (FR-LITE-5's form; delivered for the test):

1. Every story has its clock from creation; with the clock off it is inert: ``GET /clock``
   answers level ``off``, an advance is 409 ``module_off`` and writes nothing, ``respond``
   carries no ``clock``, and story-time is the clock-off rule.
2. ``respond`` returns the clock before and after the turn. A consumer advance between turns
   (``POST /clock/advance``, an inter-turn slot, source ``consumer``) moves it forward by
   ``minutes`` or ``to`` an offset, stored exactly (FR-CLOCK-3's offset); its replay under the
   same ``Idempotency-Key`` answers the stored result and writes nothing. A move backward is
   422 ``clock_backward``, a time of day is 422 ``clock_unanchored`` (no anchor fixes the hour),
   and neither writes a row.
3. Truncating to before a turn restores the clock as of that point; a fork has its own clock
   that diverges freely; a swap back restores the swapped continuation's clock.
4. A respond that fails before commit and an ingested ``player_turn`` or ``narration`` advance
   nothing.
5. ``manual`` (``PATCH /clock``, an interpretive configuration change; ``pacing_table`` a
   prospective one): twenty exchanges leave the clock unchanged until a skip.
6. ``world_clock`` is reserved: a declaration of it is 422 ``reserved_state``, and a set of it
   (backward or not) is 422 ``reserved_state`` naming the advance route and writes nothing; no
   route deletes it.
7. FR-CLOCK-14: in a ``queue`` story, two concurrent turns share the one clock in commit order:
   the second's ``before`` is the first's ``after``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core import states, world_clock
from epistrel.core.enums import StateChangeSource
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position
from epistrel.core.states import StateChange
from epistrel.core.states.model import world_clock_definition
from tests import canon_support as c
from tests import clock_support as ck
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import (
    FIRST,
    PLAYER,
    body,
    new_story,
    played,
    respond,
    respond_client,
    until,
)
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _at(offset: int) -> dict[str, Any]:
    hours, minutes = divmod(offset, 60)
    return {"offset": offset, "display": f"+{hours:02d}h {minutes:02d}m"}


async def _clock(client: httpx.AsyncClient, story: str) -> dict[str, Any]:
    response = await client.get(f"/stories/{story}/clock", headers=READ)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def _advance(
    client: httpx.AsyncClient, story: str, payload: dict[str, Any], key: str | None = None
) -> httpx.Response:
    headers = {**WRITE, **({"Idempotency-Key": key} if key else {})}
    return await client.post(f"/stories/{story}/clock/advance", json=payload, headers=headers)


async def _advanced(client: httpx.AsyncClient, story: str, **payload: Any) -> dict[str, Any]:
    response = await _advance(client, story, {"reason": "time passes", **payload})
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def _refused(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == code, error
    return error


async def _post(client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(path, json=payload, headers=WRITE)
    assert response.status_code in (200, 201), response.text
    found: dict[str, Any] = response.json()
    return found


async def _config_changes(engine: AsyncEngine, story: str) -> list[tuple[str, str]]:
    found = await rows(
        engine,
        "SELECT setting, class AS change_class FROM config_changes "
        "WHERE scope_id = :s AND setting LIKE 'state:world_clock.%' ORDER BY config_revision",
        s=story,
    )
    return [(r.setting, r.change_class) for r in found]


@pytest.mark.part("M6", "FR-CLOCK-1,3,5,13,14; FR-STATE-4")
async def test_s_clock_timeline(
    stub_settings: Settings, engine: AsyncEngine, clock_delivered: None
) -> None:
    """See the module docstring (each numbered case in order)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await new_story(client)

        # 1. The clock exists from creation; off, it is inert.
        off = await _clock(client, story)
        assert (off["level"], off["offset"], off["mode"], off["anchor"]) == (
            "off",
            0,
            "paced",
            None,
        )
        assert off["settings"]["pacing_table"] == [[0, 5, 0], [5, 51, 1], [51, None, 2]]
        assert off["settings"]["max_unprompted_skip_minutes"] == 1440
        _refused(await _advance(client, story, {"minutes": 30, "reason": "x"}), 409, "module_off")
        assert "clock" not in await played(client, story, body(ids[PLAYER], "Off."))
        assert await ck.clock_changes(engine, story) == 0
        async with engine.connect() as conn:
            # The clock-off rule: the story's minute zero plus ten minutes a turn.
            assert await world_clock.story_time_at(conn, story, Position(3)) == 30

        # 2. On: respond reports the clock; the consumer moves it forward between turns.
        await ck.set_level(engine, story, "prompt_only")
        assert (await _clock(client, story))["level"] == "prompt_only"
        first = await played(client, story, body(ids[PLAYER], "Hello."))
        assert first["clock"] == {"before": _at(0), "after": _at(0)}
        moved = await _advance(client, story, {"minutes": 90, "reason": "a nap"}, key="adv-1")
        assert moved.status_code == 200, moved.text
        result = moved.json()
        assert (result["before"], result["after"]) == (_at(0), _at(90))
        assert result["position"] == {
            "turn": first["turn"],
            "variant": first["variant"],
            "slot_k": 1,
            "beat": 0,
            "step": 0,
        }
        counted = await ck.clock_changes(engine, story)
        replay = await _advance(client, story, {"minutes": 90, "reason": "a nap"}, key="adv-1")
        assert replay.status_code == 200, replay.text
        assert replay.headers["Idempotent-Replay"] == "true"
        assert replay.json() == result
        assert await ck.clock_changes(engine, story) == counted
        (stored,) = await rows(
            engine,
            "SELECT c.delta, c.source::text AS source, c.slot_k FROM state_changes c "
            "WHERE c.id = :i",
            i=result["change_id"],
        )
        assert (int(stored.delta), stored.source, stored.slot_k) == (90, "consumer", 1)
        second = await played(client, story, body(ids[PLAYER], "And then?"))
        assert second["clock"] == {"before": _at(90), "after": _at(90)}
        assert (await _advanced(client, story, to=200))["after"] == _at(200)
        for payload, code in (
            ({"to": 100}, "clock_backward"),
            ({"minutes": -5}, "clock_backward"),
            ({"to": "19:40"}, "clock_unanchored"),
        ):
            _refused(await _advance(client, story, {**payload, "reason": "x"}), 422, code)
        assert await ck.clock_changes(engine, story) == counted + 1
        third = await played(client, story, body(ids[PLAYER], "Later."))
        assert third["clock"]["after"] == _at(200)
        assert (await _clock(client, story))["offset"] == 200

        # 3. Truncate, fork, swap: the clock follows the timeline.
        await _post(client, f"/stories/{story}/truncate", {"to_turn": first["turn"], "slot": 1})
        assert (await _clock(client, story))["offset"] == 90
        forked = await _post(
            client, f"/stories/{story}/fork", {"at_turn": first["turn"], "slot": 1}
        )
        branch = forked["story_id"]
        await ck.set_level(engine, branch, "prompt_only")
        assert (await _clock(client, branch))["offset"] == 90
        assert (await _advanced(client, branch, minutes=30))["after"] == _at(120)
        assert (await _clock(client, story))["offset"] == 90
        over = await played(client, story, body(ids[PLAYER], "Another way."))
        assert over["clock"] == {"before": _at(90), "after": _at(90)}
        assert (await _advanced(client, story, minutes=5))["after"] == _at(95)
        await _post(
            client,
            f"/stories/{story}/swap",
            {"to_variant_chain": {"turn": second["turn"], "variant": second["variant"]}},
        )
        assert (await _clock(client, story))["offset"] == 200
        assert (await _clock(client, branch))["offset"] == 120
        assert await ck.offset(engine, story, view=states.View.AS_RECORDED) == 205

        # 4. A respond that fails before commit, and ingested events, advance nothing.
        counted = await ck.clock_changes(engine, story)
        epistrel.models.stub.fault("generation", "http_500", n=10)
        failed = await respond(client, story, body(ids[PLAYER], "Fails."))
        assert failed.status_code >= 500 or failed.status_code == 422, failed.text
        epistrel.models.stub.reset()
        for event in (
            {
                "client_event_id": "e-1",
                "kind": "player_turn",
                "actor_character_id": ids[PLAYER],
                "say": "I wait.",
            },
            {"client_event_id": "e-2", "kind": "narration", "text": "Rain falls for hours."},
        ):
            ingested = await client.post(
                f"/stories/{story}/ingest", json={"events": [event]}, headers=WRITE
            )
            assert ingested.status_code == 201, ingested.text
        assert await ck.clock_changes(engine, story) == counted
        assert (await _clock(client, story))["offset"] == 200

        # 5. Manual mode, through the clock's settings.
        patched = await client.patch(
            f"/stories/{story}/clock", json={"mode": "manual"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["mode"] == "manual"
        again = await client.patch(
            f"/stories/{story}/clock",
            json={"pacing_table": [[0, 10, 1], [10, None, 3]]},
            headers=WRITE,
        )
        assert again.status_code == 200, again.text
        assert await _config_changes(engine, story) == [
            ("state:world_clock.mode", "interpretive"),
            ("state:world_clock.pacing_table", "prospective"),
        ]
        refused = await client.patch(
            f"/stories/{story}/clock", json={"mode": "external"}, headers=WRITE
        )
        _refused(refused, 422, "validation_error")
        for n in range(20):
            turn = await played(client, story, body(ids[PLAYER], f"Exchange {n}."))
            assert turn["clock"] == {"before": _at(200), "after": _at(200)}
        assert (await _advanced(client, story, minutes=60))["after"] == _at(260)

        # 6. The name is reserved, the State API never sets it, and nothing deletes it.
        counted = await ck.clock_changes(engine, story)
        with pytest.raises(InvalidRequest) as declared:
            await c.run(
                engine,
                story,
                lambda conn, op: states.define_state(conn, op, story, world_clock_definition()),
            )
        assert declared.value.details["reason"] == "reserved_state"
        for value in (0, 999):

            async def set_clock(conn: Any, op: Any, value: int = value) -> Any:
                change = StateChange(
                    "world_clock", "rewind", StateChangeSource.CONSUMER, value=value
                )
                return await states.apply_change(conn, op, change)

            with pytest.raises(InvalidRequest) as set_refused:
                await c.run(engine, story, set_clock)
            assert set_refused.value.details["reason"] == "reserved_state"
            assert "/clock/advance" in set_refused.value.details["use"]
        assert await ck.clock_changes(engine, story) == counted
        served = {
            (method, context.original_route.path)
            for context in iter_route_contexts(create_app(stub_settings).routes)
            if isinstance(context.original_route, APIRoute)
            for method in context.original_route.methods or ()
        }
        assert not {m for m, p in served if p.startswith("/stories/{story_id}/states")} & {"DELETE"}
        assert ("DELETE", "/stories/{story_id}/clock") not in served

    # 7. FR-CLOCK-14: one shared clock, advanced in commit order.
    async with respond_client(stub_settings, engine) as (client, epistrel):
        game, gids = await new_story(client, settings={"turn_concurrency": "queue"})
        await ck.set_level(engine, game, "prompt_only")
        await _advanced(client, game, minutes=45)
        stub = epistrel.models.stub
        latch = stub.hold("generation")
        first_turn = asyncio.ensure_future(respond(client, game, body(gids[PLAYER], "Me.")))
        await asyncio.wait_for(latch.entered.wait(), 5)
        second_turn = asyncio.ensure_future(respond(client, game, body(gids[FIRST], "Me too.")))

        async def queued() -> bool:
            found = await rows(
                engine,
                "SELECT status FROM operations WHERE story_id = :s AND kind = 'turn' "
                "AND status = 'queued'",
                s=game,
            )
            return len(found) == 1

        await until(queued, "the second turn is queued behind the first")
        latch.release()
        answered = [await first_turn, await second_turn]
        assert [r.status_code for r in answered] == [200, 200], [r.text for r in answered]
        one, two = (r.json() for r in answered)
        assert (one["turn"], two["turn"]) == (1, 2)
        assert two["clock"]["before"] == one["clock"]["after"] == _at(45)
