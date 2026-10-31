"""S-STATE-SCHEMA: States are declared by the consumer and set between turns (docs/04 §7;
FR-STATE-3,7,9, FR-STORE-31; #186).

Every S-STATE-SCHEMA M6 part lives in this file: #186's API (``__api``), declaration
(``__define``), ingest (``__ingest``) and source (``__unpermitted``) parts; #192 adds the rules
part and #202 the proposal part.

Played through the REST app with the stub adapter on a Lite story running States at
``prompt_only`` (FR-LITE-5's form; the module delivered for the test, ``states_delivered``).
A consumer write is an inter-turn slot at ``N⁺k`` after the last completed turn ``N``:
excluded from checkpoint ``N``, included in ``N⁺k``, discarded by a truncation to ``N``.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import state_support as st
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import PLAYER, body, new_story, played, respond_client
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration

GOLD = {
    "name": "gold",
    "type": "number",
    "constraints": {"non_negative": True},
    "initial_value": 10,
    "unit": "coins",
}
CHANGES = "SELECT count(*) AS n FROM state_changes WHERE story_id = :s"
DEFINITIONS = (
    "SELECT name, membership::text AS membership FROM state_definitions "
    "WHERE story_id = :s AND name <> 'world_clock' ORDER BY id"
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _headers(key: str | None = None, revision: int | None = None) -> dict[str, str]:
    found = dict(WRITE)
    if key is not None:
        found["Idempotency-Key"] = key
    if revision is not None:
        found["X-Epistrel-Expected-Revision"] = str(revision)
    return found


async def _define(
    client: httpx.AsyncClient, story: str, definition: dict[str, Any], key: str | None = None
) -> httpx.Response:
    return await client.post(
        f"/stories/{story}/states", json={"definition": definition}, headers=_headers(key)
    )


async def _defined(client: httpx.AsyncClient, story: str, definition: dict[str, Any]) -> Any:
    response = await _define(client, story, definition)
    assert response.status_code == 201, response.text
    return response.json()


async def _put(
    client: httpx.AsyncClient,
    story: str,
    name: str,
    payload: dict[str, Any],
    *,
    key: str | None = None,
    revision: int | None = None,
) -> httpx.Response:
    return await client.put(
        f"/stories/{story}/states/{name}",
        json={"reason": "the consumer says so", **payload},
        headers=_headers(key, revision),
    )


async def _value(client: httpx.AsyncClient, story: str, name: str, as_of: str | None = None) -> Any:
    params = {"viewpoint": "omniscient", **({"as_of": as_of} if as_of is not None else {})}
    response = await client.get(f"/stories/{story}/states/{name}", params=params, headers=READ)
    assert response.status_code == 200, response.text
    return response.json()["state"]["value"]


def _refused(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == code, error
    return error


async def _post(client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> Any:
    response = await client.post(path, json=payload, headers=WRITE)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _count(engine: AsyncEngine, story: str) -> int:
    (found,) = await rows(engine, CHANGES, s=story)
    return int(found.n)


async def _committed(engine: AsyncEngine, story: str) -> int:
    (found,) = await rows(
        engine,
        "SELECT count(*) AS n FROM operations WHERE story_id = :s AND status = 'committed'",
        s=story,
    )
    return int(found.n)


async def _revisions(engine: AsyncEngine, story: str) -> tuple[int, int]:
    (found,) = await rows(
        engine,
        "SELECT timeline_revision, config_revision FROM stories WHERE id = :s",
        s=story,
    )
    return int(found.timeline_revision), int(found.config_revision)


async def _story(client: httpx.AsyncClient, engine: AsyncEngine) -> tuple[str, dict[str, str]]:
    story, ids = await new_story(client)
    await st.set_level(engine, story, "prompt_only")
    return story, ids


@pytest.mark.part("M6", "FR-STATE-9")
async def test_s_state_schema__api(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    """A consumer set (``PUT /states/{name}``) is an inter-turn slot after the last turn: its
    idempotent replay returns the same ``position`` and writes nothing, and a stale
    ``X-Epistrel-Expected-Revision`` is 409 with nothing written.

    Complement: checkpoint ``N`` lacks the set and ``N⁺1`` has it; truncating to ``N``
    discards it and untruncating restores it; a fork at ``N⁺1`` copies it (its slot recorded
    an event). The refusals write nothing: a broken constraint, an externally owned value,
    ``world_clock`` and States off."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await _story(client, engine)
        await _defined(client, story, GOLD)
        turn = (await played(client, story, body(ids[PLAYER], "Hello.")))["turn"]
        revision, config = await _revisions(engine, story)

        # The set: the next slot after turn N, the revision +1; a replay answers the same.
        first = await _put(client, story, "gold", {"delta": -3}, key="set-1", revision=revision)
        assert first.status_code == 200, first.text
        written = first.json()
        assert (written["old_value"], written["new_value"]) == (10, 7)
        assert written["position"] == f"{turn}⁺1"
        assert written["slot"] == {"turn": turn, "variant": 1, "slot_k": 1, "beat": 0, "step": 0}
        assert written["timeline_revision"] == revision + 1
        assert await _revisions(engine, story) == (revision + 1, config)
        counted = await _count(engine, story)
        replay = await _put(client, story, "gold", {"delta": -3}, key="set-1", revision=revision)
        assert replay.status_code == 200, replay.text
        assert replay.headers["Idempotent-Replay"] == "true"
        assert replay.json()["position"] == written["position"]
        assert replay.json() == written
        assert await _count(engine, story) == counted
        stale = await _put(client, story, "gold", {"delta": -1}, revision=revision)
        _refused(stale, 409, "revision_conflict")
        assert await _count(engine, story) == counted
        (op,) = await rows(
            engine,
            "SELECT kind::text AS kind, slot_k, narrative_turn FROM operations "
            "WHERE operation_id = :o",
            o=written["operation_id"],
        )
        assert (op.kind, op.slot_k, op.narrative_turn) == ("inter_turn", 1, turn)
        (change,) = await rows(
            engine,
            "SELECT source::text AS source FROM state_changes WHERE id = :i",
            i=written["change_id"],
        )
        assert change.source == "consumer"

        # Checkpoints: N excludes the slot, N⁺1 includes it (+ is sent as %2B or ⁺).
        assert await _value(client, story, "gold", str(turn)) == 10
        assert await _value(client, story, "gold", f"{turn}+1") == 7
        assert await _value(client, story, "gold", f"{turn}⁺1") == 7
        assert await _value(client, story, "gold") == 7

        # A fork at N⁺1 copies the slot's set.
        forked = await _post(client, f"/stories/{story}/fork", {"at_turn": turn, "slot": 1})
        branch = forked["story_id"]
        await st.set_level(engine, branch, "prompt_only")
        assert await _value(client, branch, "gold") == 7

        # Truncating to N (a turn later) discards the set; untruncating restores it.
        await played(client, story, body(ids[PLAYER], "And then?"))
        await _post(client, f"/stories/{story}/truncate", {"to_turn": turn})
        assert await _value(client, story, "gold") == 10
        await _post(client, f"/stories/{story}/untruncate", {})
        assert await _value(client, story, "gold") == 7

        # The refusals write nothing.
        counted = await _count(engine, story)
        before = await _revisions(engine, story)
        broken = _refused(
            await _put(client, story, "gold", {"delta": -8}), 422, "state_constraint_violation"
        )
        assert {k: broken["details"][k] for k in ("constraint", "attempted", "current")} == {
            "constraint": "non_negative",
            "attempted": -1,
            "current": 7,
        }
        await _defined(
            client,
            story,
            {"name": "bank", "type": "number", "owner": "external", "provider_id": "ledger"},
        )
        before = await _revisions(engine, story)
        _refused(await _put(client, story, "bank", {"value": 5}), 422, "state_readonly_external")
        clock = _refused(
            await _put(client, story, "world_clock", {"value": 5}), 422, "validation_error"
        )
        assert clock["details"]["reason"] == "reserved_state"
        _refused(await _put(client, story, "nothing", {"value": 5}), 404, "not_found")
        assert await _count(engine, story) == counted
        assert await _revisions(engine, story) == before
        await st.set_level(engine, story, None)
        _refused(await _put(client, story, "gold", {"delta": 1}), 409, "module_off")
        _refused(
            await client.get(
                f"/stories/{story}/states", params={"viewpoint": "omniscient"}, headers=READ
            ),
            409,
            "module_off",
        )
        assert await _count(engine, story) == counted


@pytest.mark.part("M6", "FR-STATE-3")
async def test_s_state_schema__define(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    """A consumer ``POST /states`` declares a State at a slot (201, ``created_by =
    consumer``), and a truncation before it discards it, freeing its name: the State is gone
    and declaring it again succeeds. ``PATCH …/definition`` moves ``config_revision`` and
    records a ``config_changes`` row, but not ``timeline_revision``; an untruncation that
    would restore the discarded declaration over the new one is 409 ``state_name_taken``.

    Complement: ``world_clock``, a taken name, a shape error and an immutable field are
    refused, and nothing is written."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await _story(client, engine)
        turn = (await played(client, story, body(ids[PLAYER], "Hello.")))["turn"]
        revision, config = await _revisions(engine, story)
        declared = await _defined(client, story, GOLD)
        assert declared["position"] == f"{turn}⁺1"
        assert declared["timeline_revision"] == revision + 1
        assert declared["definition"]["created_by"] == "consumer"
        assert declared["definition"]["constraints"] == {"non_negative": True}
        assert await _value(client, story, "gold") == 10

        # A definition change: configuration, not timeline.
        patched = await client.patch(
            f"/stories/{story}/states/gold/definition",
            json={"unit": "crowns", "constraints": {"non_negative": True, "max": 500}},
            headers=WRITE,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["config_revision"] == config + 1
        assert patched.json()["timeline_revision"] == revision + 1
        assert patched.json()["definition"]["unit"] == "crowns"
        assert await _revisions(engine, story) == (revision + 1, config + 1)
        settings = await rows(
            engine,
            "SELECT setting, class::text AS change_class FROM config_changes "
            "WHERE scope_id = :s AND setting LIKE 'state:gold.%' ORDER BY setting",
            s=story,
        )
        assert [(r.setting, r.change_class) for r in settings] == [
            ("state:gold.constraints", "interpretive"),
            ("state:gold.unit", "prospective"),
        ]

        # The refusals write nothing.
        before = await _revisions(engine, story)
        reserved = _refused(
            await _define(client, story, {"name": "world_clock", "type": "number"}),
            422,
            "validation_error",
        )
        assert reserved["details"]["reason"] == "reserved_state"
        _refused(await _define(client, story, GOLD), 409, "state_name_taken")
        _refused(
            await _define(client, story, {"name": "flag", "type": "enum"}), 422, "validation_error"
        )
        immutable = _refused(
            await client.patch(
                f"/stories/{story}/states/gold/definition", json={"type": "text"}, headers=WRITE
            ),
            422,
            "validation_error",
        )
        assert immutable["details"]["reason"] == "immutable_field"
        assert await _revisions(engine, story) == before

        # A truncation to N (a turn later) discards the declaration and frees its name.
        await played(client, story, body(ids[PLAYER], "And then?"))
        await _post(client, f"/stories/{story}/truncate", {"to_turn": turn})
        gone = await client.get(
            f"/stories/{story}/states/gold", params={"viewpoint": "omniscient"}, headers=READ
        )
        _refused(gone, 404, "not_found")
        again = await _defined(client, story, {**GOLD, "initial_value": 3})
        assert again["position"] == f"{turn}⁺2"
        assert await _value(client, story, "gold") == 3
        assert [(r.name, r.membership) for r in await rows(engine, DEFINITIONS, s=story)] == [
            ("gold", "discarded"),
            ("gold", "active"),
        ]
        undo = await client.post(f"/stories/{story}/untruncate", json={}, headers=WRITE)
        _refused(undo, 409, "state_name_taken")
        assert await _value(client, story, "gold") == 3


@pytest.mark.part("M6", "FR-STATE-9")
async def test_s_state_schema__ingest(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    """Ingest records ``kind: state_change`` as a consumer slot operation: deduplicated by
    ``client_event_id``, and in a mixed batch ``[player_turn, state_change]`` the set takes
    the ingested turn's ``⁺1`` (batch order is position order); a batch of a set alone takes
    the slot after the story's last turn. No ingested event advances the clock.

    Complement: the ``outcome`` kind is still 422 ``unsupported_event_kind`` (#217 updates
    this test), and a set its constraint refuses writes nothing of the batch."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await _story(client, engine)
        await _defined(client, story, GOLD)
        batch = {
            "events": [
                {
                    "client_event_id": "e-1",
                    "kind": "player_turn",
                    "actor_character_id": ids[PLAYER],
                    "say": "I count my coins.",
                },
                {
                    "client_event_id": "e-2",
                    "kind": "state_change",
                    "state": "gold",
                    "delta": 5,
                    "reason": "found a purse",
                },
            ]
        }
        ingested = await client.post(f"/stories/{story}/ingest", json=batch, headers=WRITE)
        assert ingested.status_code == 201, ingested.text
        turn_result, set_result = ingested.json()["results"]
        assert set_result["turn"] == turn_result["turn"]
        slots = await rows(
            engine,
            "SELECT operation_id, kind::text AS kind, narrative_turn, slot_k FROM operations "
            "WHERE operation_id IN (:a, :b)",
            a=turn_result["operation_id"],
            b=set_result["operation_id"],
        )
        by_id = {r.operation_id: r for r in slots}
        assert by_id[turn_result["operation_id"]].kind == "turn"
        assert (
            by_id[set_result["operation_id"]].kind,
            by_id[set_result["operation_id"]].narrative_turn,
            by_id[set_result["operation_id"]].slot_k,
        ) == ("inter_turn", turn_result["turn"], 1)
        assert await _value(client, story, "gold") == 15
        assert await _value(client, story, "gold", str(turn_result["turn"])) == 10

        counted = await _count(engine, story)
        again = await client.post(f"/stories/{story}/ingest", json=batch, headers=WRITE)
        assert again.status_code == 200, again.text
        assert all(r["replayed"] for r in again.json()["results"])
        assert await _count(engine, story) == counted

        alone = {
            "events": [
                {
                    "client_event_id": "e-3",
                    "kind": "state_change",
                    "state": "gold",
                    "value": 40,
                    "reason": "a reward",
                }
            ]
        }
        ingested = await client.post(f"/stories/{story}/ingest", json=alone, headers=WRITE)
        assert ingested.status_code == 201, ingested.text
        (only,) = ingested.json()["results"]
        (slot,) = await rows(
            engine,
            "SELECT narrative_turn, slot_k FROM operations WHERE operation_id = :o",
            o=only["operation_id"],
        )
        assert (slot.narrative_turn, slot.slot_k) == (turn_result["turn"], 2)
        assert await _value(client, story, "gold") == 40
        (advances,) = await rows(
            engine,
            "SELECT count(*) AS n FROM state_changes c JOIN state_definitions d "
            "ON d.id = c.state_id WHERE c.story_id = :s AND d.name = 'world_clock'",
            s=story,
        )
        assert advances.n == 0

        counted = await _count(engine, story)
        committed = await _committed(engine, story)
        broken = {
            "events": [
                {
                    "client_event_id": "e-4",
                    "kind": "narration",
                    "text": "The purse is empty.",
                },
                {
                    "client_event_id": "e-5",
                    "kind": "state_change",
                    "state": "gold",
                    "delta": -100,
                    "reason": "a theft",
                },
            ]
        }
        refused = await client.post(f"/stories/{story}/ingest", json=broken, headers=WRITE)
        _refused(refused, 422, "state_constraint_violation")
        unknown = await client.post(
            f"/stories/{story}/ingest",
            json={"events": [{"client_event_id": "e-6", "kind": "outcome"}]},
            headers=WRITE,
        )
        _refused(unknown, 422, "unsupported_event_kind")
        assert await _count(engine, story) == counted
        assert await _committed(engine, story) == committed


@pytest.mark.part("M6", "FR-STATE-9")
async def test_s_state_schema__unpermitted(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    """A State whose ``authorization.sources`` omit ``consumer`` refuses an API set and an
    ingested ``state_change`` with 422 ``state_unpermitted {reason: source}``, and nothing is
    written.

    Complement: a State with the default sources (none named) accepts the same set."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ids = await _story(client, engine)
        await _defined(
            client,
            story,
            {**GOLD, "name": "favor", "authorization": {"sources": ["director", "rule"]}},
        )
        await _defined(client, story, GOLD)
        counted = await _count(engine, story)
        before = await _revisions(engine, story)
        refused = _refused(
            await _put(client, story, "favor", {"delta": 1}), 422, "state_unpermitted"
        )
        assert (refused["details"]["reason"], refused["details"]["source"]) == (
            "source",
            "consumer",
        )
        ingested = await client.post(
            f"/stories/{story}/ingest",
            json={
                "events": [
                    {
                        "client_event_id": "u-1",
                        "kind": "state_change",
                        "state": "favor",
                        "delta": 1,
                        "reason": "a gift",
                    }
                ]
            },
            headers=WRITE,
        )
        assert _refused(ingested, 422, "state_unpermitted")["details"]["reason"] == "source"
        assert await _count(engine, story) == counted
        assert await _revisions(engine, story) == before
        assert await _value(client, story, "favor") == 10

        accepted = await _put(client, story, "gold", {"delta": 1})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["new_value"] == 11
