"""S-CLOCK-TIMELINE: the clock is a State (docs/04 §7; FR-CLOCK-1,3,5,13,14, FR-STATE-4; #193).

Every S-CLOCK-TIMELINE M6 part lives in this file: #193's (``test_s_clock_timeline``), #195's
anchoring (``__anchor``, docstring there) and #209's external mode (``__external``).

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
import json
import re
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core import states, world_clock
from epistrel.core.canon import facts
from epistrel.core.enums import StateChangeSource
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position
from epistrel.core.propositions.vocabulary import CLOCK_ANCHOR
from epistrel.core.states import StateChange
from epistrel.core.states.model import world_clock_definition
from epistrel.core.world_clock.anchor import UNKNOWN, Anchor, Exact
from tests import canon_support as c
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import provider_support as pv
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
from tests.structured_turn_support import beat, structured_story, turn_report

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
        # Replies too short to pace (#203: a Lite turn paces its input and reply, under 5
        # words: 0 minutes), so only the consumer's advances move the clock here.
        epistrel.models.stub.when("", "Mm.", purpose="generation")

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


async def _anchor_facts(engine: AsyncEngine, story: str) -> list[Any]:
    """The story's ``clock_anchor`` facts, active or not."""
    return await rows(
        engine,
        "SELECT f.id, f.visibility, f.provenance::text AS provenance, f.membership::text AS m "
        "FROM facts f WHERE f.story_id = :s AND f.predicate_id = :p ORDER BY f.id",
        s=story,
        p=CLOCK_ANCHOR,
    )


async def _anchor_at(engine: AsyncEngine, story: str) -> Anchor:
    async with engine.connect() as conn:
        return await world_clock.anchor_at(conn, story)


async def _narrate(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    stub: Any,
    story: str,
    cal: str,
    narration: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A turn whose narration is ``narration`` (Mira says something timeless); the response
    and the turn report."""
    stub.reset()
    es.narrator(stub, es.render(beat("narration", narration)))
    ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the step.")))
    answer = await respond(client, story, body(cal, None, do="looks around"))
    assert answer.status_code == 200, answer.text
    return answer.json(), await turn_report(engine, story)


@pytest.mark.part("M6", "FR-CLOCK-3")
async def test_s_clock_timeline__anchor(
    stub_settings: Settings,
    engine: AsyncEngine,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#195's clause: an unanchored story is partially anchored when the first render
    establishes "Saturday morning" (weekday and a time range only, no minute or date chosen);
    the anchor is a commitment (a ``clock_anchor`` fact, ``visibility = engine``, never in a
    character's canon or prompt), a later "it's 13:00" is contradicted and leaves it as it
    was, "it's 9:15" narrows it to the minute, a truncation un-anchors and a fork copies.
    Complement: no date component is ever reported."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal, mira = ids["Cal"], ids["Mira"]
        market = await ps.place_entity(engine, story, "the market")
        for who in (cal, mira):
            await ps.put(engine, story, who, market)
        await ck.set_level(engine, story, "on")
        # #194: manual mode, so no pacing moves the clock between the offsets asserted here.
        manual = await client.patch(
            f"/stories/{story}/clock", json={"mode": "manual"}, headers=WRITE
        )
        assert manual.status_code == 200, manual.text
        assert (await ck.advance(engine, story, 130)).after.offset_minutes == 130
        assert (await _anchor_at(engine, story)).is_unanchored

        # The first render establishes "Saturday morning" at +130.
        first, report = await _narrate(
            client, engine, stub, story, cal, "It was Saturday morning, and the market was waking."
        )
        anchor = await _anchor_at(engine, story)
        here = anchor.absolute_at(130)
        assert here.weekday == Exact("saturday")
        assert [(s.lo, s.hi) for s in here.segments] == [(300, 720)]
        assert (here.year, here.month, here.day, here.minute) == (UNKNOWN,) * 4
        (fact,) = await _anchor_facts(engine, story)
        assert fact.visibility == {"scope": "engine"}
        assert fact.provenance == "director"
        clock = await _clock(client, story)
        assert clock["anchor"]["weekday"] == "saturday"
        assert clock["anchor"]["year"] is None
        assert clock["anchor"]["day"] is None
        assert clock["display"] == "+02h 10m (Saturday, between 05:00 and 12:00 (morning))"
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\b\d{1,2}(st|nd|rd|th)\b", clock["display"])
        # Engine-only: no character reads the anchor, and no prompt carries it.
        async with engine.connect() as conn:
            for viewpoint in (f"character:{mira}", f"character:{cal}"):
                assert not await facts.canon(
                    conn, story, predicate=CLOCK_ANCHOR, viewpoint=viewpoint
                )
            assert await facts.canon(conn, story, predicate=CLOCK_ANCHOR)

        # "It's 13:00" under the anchor is contradicted, and the anchor is unchanged.
        _, report = await _narrate(
            client, engine, stub, story, cal, "It's 13:00 and the bells ring."
        )
        kinds = [v.get("kind") for v in report["verify"]["verdicts"]]
        assert "contradicted" in kinds, report["verify"]
        assert await _anchor_at(engine, story) == anchor
        assert len(await _anchor_facts(engine, story)) == 1
        for call in stub.calls:
            sent = "\n".join(m.content for m in call.messages)
            assert '"statement"' not in sent
            assert "clock_anchor" not in sent

        # "It's 9:15" narrows the anchor to the minute and back-fills the start exactly.
        _, report = await _narrate(
            client, engine, stub, story, cal, "It's 9:15, said the town clock."
        )
        narrowed = await _anchor_at(engine, story)
        assert [(s.lo, s.hi) for s in narrowed.absolute_at(130).segments] == [(555, 556)]
        assert narrowed.display(0) == "Saturday, 07:05 (morning)"
        assert narrowed.revision == 2
        assert (await _clock(client, story))["anchor"]["day"] is None

        # The exact minute places a time of day: "10:00" is the next 10:00, 45 minutes on.
        head = (await _clock(client, story))["offset"]
        moved = await _advanced(client, story, to="10:00")
        assert moved["after"]["offset"] == head + 45
        assert moved["after"]["display"].endswith("(Saturday, 10:00 (morning))")
        # A fork copies the anchor; a truncation to before the first render un-anchors.
        forked = await _post(client, f"/stories/{story}/fork", {"at_turn": first["turn"]})
        assert (await _anchor_at(engine, forked["story_id"])).absolute_at(130).weekday == Exact(
            "saturday"
        )
        await _post(client, f"/stories/{story}/truncate", {"to_turn": first["turn"] - 1})
        assert (await _anchor_at(engine, story)).is_unanchored
        assert (await _clock(client, story))["anchor"] is None

        # The calendar and bands are set with PATCH /clock (on the fork, which keeps its
        # anchor); bands that do not tile are refused. A stored anchor names its weekday, so a
        # weekday the new calendar lacks is unknown again, never another day.
        branch = forked["story_id"]
        custom = {
            "kind": "custom",
            "day_minutes": 1440,
            "months": [{"name": "Thaw", "days": 30}],
            "weekdays": ["Moonday", "Sunsday"],
            "year_days": 30,
        }
        patched = await client.patch(
            f"/stories/{branch}/clock", json={"calendar": custom}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        assert (await _clock(client, branch))["settings"]["calendar"] == custom
        assert (await _anchor_at(engine, branch)).absolute_at(130).weekday is UNKNOWN
        gap = [["night", "00:00", "05:00"], ["day", "06:00", "24:00"]]
        refused = await client.patch(f"/stories/{branch}/clock", json={"bands": gap}, headers=WRITE)
        _refused(refused, 422, "validation_error")


# -- #209: the external clock -------------------------------------------------------------------


def _skip(minutes: int, reason: str = "they wait") -> dict[str, Any]:
    return {"kind": "time_advance", "state": "world_clock", "minutes": minutes, "reason": reason}


def _words(n: int) -> str:
    return " ".join(["word"] * (n - 1) + ["end."])


async def _external_turn(
    client: httpx.AsyncClient,
    stub: Any,
    story: str,
    cal: str,
    *mira: dict[str, Any],
    narration: str = "Rain taps.",
    regenerated: str | None = None,
) -> dict[str, Any]:
    stub.reset()
    es.narrator(stub, es.render(beat("narration", narration)))
    ms.renders_for(stub, "Mira", es.render(*mira))
    if regenerated is not None:
        stub.script("regeneration", [json.dumps({"text": regenerated})])
    answer = await respond(client, story, body(cal, None, do="waits"))
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


def _proposed(played: dict[str, Any]) -> list[int]:
    """The turn's requested clock advances' minutes (each pending, owned by the consumer)."""
    found = [e for e in played.get("state_effects", []) if e.get("kind") == "time_advance"]
    for e in found:
        assert (e["state"], e["owner"], e["status"]) == ("world_clock", "external", "pending")
        assert e["effect_id"]
    return [int(e["minutes"]) for e in found]


@pytest.mark.part("M6", "FR-CLOCK-5")
async def test_s_clock_timeline__external(
    stub_settings: Settings,
    engine: AsyncEngine,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#209's clause (owner decision 2026-10-30): ``PATCH /clock`` with ``mode = external`` and
    ``provider_value`` makes the clock a bound provider's ``number`` of story-minutes on the
    offset scale (unbound: 422 ``provider_unbound``; a text value: 422 ``validation_error``).
    The offset is read and pinned per turn (600, then 700), ``respond``'s ``clock`` and
    ``GET /clock`` read the pin, the pacing increments and the declared skip are requested
    (``time_advance`` in ``state_effects``, ``requested_effect`` events) and never written,
    ``/clock/advance`` is 422 ``clock_external``, and a due event's stop shortens the proposed
    advance (from 700 with the bell at 720: 20 minutes). Complement: back in ``paced`` mode
    the clock keeps its last pinned offset and a turn commits its advance."""
    name = pv.unique_name("gameclock")
    await pv.register(engine, pv.in_process_spec(name, {"elapsed": "number", "label": "text"}))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = pv.ScriptedProvider(name).attach(epistrel)
        fake.script("elapsed", 600, 700)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal, mira = ids["Cal"], ids["Mira"]
        square = await ps.place_entity(engine, story, "the square")
        for who in (cal, mira):
            await ps.put(engine, story, who, square)
        await ck.set_clock(engine, story, "on")
        path = f"/stories/{story}/clock"
        external = {"mode": "external", "provider_value": f"{name}.elapsed"}

        # The provider must be bound, and its value a number.
        _refused(await client.patch(path, json=external, headers=WRITE), 422, "provider_unbound")
        await pv.bind(engine, story, name, {"player": "p-1"})
        text_value = {"mode": "external", "provider_value": f"{name}.label"}
        _refused(await client.patch(path, json=text_value, headers=WRITE), 422, "validation_error")
        assert (await _clock(client, story))["mode"] == "paced"
        patched = await client.patch(path, json=external, headers=WRITE)
        assert patched.status_code == 200, patched.text
        found = patched.json()["settings"]
        assert (found["mode"], found["provider_id"], found["external_value_name"]) == (
            "external",
            name,
            "elapsed",
        )
        changes = await _config_changes(engine, story)
        for member in ("mode", "owner", "provider_id", "external_value_name"):
            assert (f"state:world_clock.{member}", "interpretive") in changes

        # The consumer's clock: Epistrel never advances it.
        _refused(
            await _advance(client, story, {"minutes": 30, "reason": "x"}), 422, "clock_external"
        )
        assert await ck.clock_changes(engine, story) == 0

        # A turn: the offset pinned at 600; pacing and the skip proposed, never written.
        played = await _external_turn(
            client,
            stub,
            story,
            cal,
            beat("say", "We could wait here a while.", effects=[_skip(30)]),
            narration=_words(60),
        )
        assert fake.reads[0][0] == "elapsed"
        assert (played["clock"]["before"]["offset"], played["clock"]["after"]["offset"]) == (
            600,
            600,
        )
        proposed = _proposed(played)
        assert 30 in proposed
        assert sum(proposed) > 30  # the renders' pacing increments too
        assert (await turn_report(engine, story))["clock"]["start"] == 600
        assert await ck.clock_changes(engine, story) == 0
        events = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'requested_effect' "
            "ORDER BY seq",
            s=story,
        )
        assert sorted(int(e.payload["minutes"]) for e in events) == sorted(proposed)
        assert {(e.payload["kind"], e.payload["state"]) for e in events} == {
            ("time_advance", "world_clock")
        }
        assert (await _clock(client, story))["offset"] == 600

        # The next turn reads the provider again: 700.
        played = await _external_turn(client, stub, story, cal, beat("say", "Hm."))
        assert played["clock"]["before"]["offset"] == 700
        assert (await _clock(client, story))["offset"] == 700

        # A due event's stop shortens the proposed advance: the bell at 720, a 60-minute skip.
        created = await client.post(
            f"/stories/{story}/scheduled-events",
            json={
                "proposition": "the bell",
                "planned": 720,
                "participants": [mira],
                "owner": "world",
            },
            headers=WRITE,
        )
        assert created.status_code == 201, created.text
        played = await _external_turn(
            client,
            stub,
            story,
            cal,
            beat("say", "Let us wait.", effects=[_skip(60)]),
            regenerated="The bell rings out across the square.",
        )
        assert sum(_proposed(played)) == 20
        assert await ck.clock_changes(engine, story) == 0

        # Complement: paced again, the clock keeps its last pinned offset and commits a turn.
        back = await client.patch(path, json={"mode": "paced"}, headers=WRITE)
        assert back.status_code == 200, back.text
        assert back.json()["settings"]["provider_id"] is None
        assert (await _clock(client, story))["offset"] == 700
        played = await _external_turn(
            client, stub, story, cal, beat("say", "Hm."), narration=_words(60)
        )
        assert _proposed(played) == []
        assert played["clock"]["after"]["offset"] > played["clock"]["before"]["offset"] == 700
        assert await ck.clock_changes(engine, story) > 0
