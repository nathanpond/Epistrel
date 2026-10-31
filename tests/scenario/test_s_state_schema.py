"""S-STATE-SCHEMA: States are declared by the consumer and set between turns (docs/04 §7;
FR-STATE-3,7,9, FR-STORE-31; #186).

Every S-STATE-SCHEMA M6 part lives in this file: #186's API (``__api``), declaration
(``__define``), ingest (``__ingest``) and source (``__unpermitted``) parts, and #192's proposal
(``__proposals``) and rules (``__rules``) parts.

Played through the REST app with the stub adapter on a Lite story running States at
``prompt_only`` (FR-LITE-5's form; the module delivered for the test, ``states_delivered``).
A consumer write is an inter-turn slot at ``N⁺k`` after the last completed turn ``N``:
excluded from checkpoint ``N``, included in ``N⁺k``, discarded by a truncation to ``N``.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import projections
from epistrel.core.enums import OperationKind, Provenance, StateChangeSource
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.propositions import vocabulary
from tests import canon_support as c
from tests import clock_support as ck
from tests import embedding_support
from tests import provider_support as ps
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
        ledger = ps.unique_name("ledger")
        await ps.register(engine, ps.in_process_spec(ledger, {"bank": "number"}))
        await _defined(
            client,
            story,
            {"name": "bank", "type": "number", "owner": "external", "provider_id": ledger},
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


# -- #192: engine-proposed States and deterministic rules -----------------------------------------

PROPOSALS = "SELECT count(*) AS n FROM state_proposals WHERE story_id = :s"
ANALYSIS = "state_proposal_analysis"


def _quantity(value: int, unit: str, subject: str) -> dict[str, Any]:
    """A beat's declared proposition with a quantity literal (``value unit``)."""
    return {
        "proposition": {
            "subject_id": subject,
            "predicate_id": vocabulary.STATE_VALUE,
            "literal": {"kind": "quantity", "value": value, "unit": unit},
            "polarity": "affirmed",
        },
        "verdict": "novel",
    }


def _holds(subject: str, item: str) -> dict[str, Any]:
    """A beat's declared ``holds`` proposition about an object entity."""
    return {
        "proposition": {
            "subject_id": subject,
            "predicate_id": vocabulary.HOLDS,
            "object_id": item,
            "polarity": "affirmed",
        },
        "verdict": "novel",
    }


def _detected(text: str, phrase: str, category: str = "transfer") -> dict[str, Any]:
    """A detected claim (#148) over ``phrase`` in ``text``: its span and category only."""
    start = text.index(phrase)
    return {"start": start, "end": start + len(phrase), "category": category, "verdict": "novel"}


async def _beats(engine: AsyncEngine, story: str, *beats: dict[str, Any]) -> int:
    """One turn whose narration beats are ``beats`` (``StructuredBeat`` payloads); its turn."""
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=c.PRINCIPAL
    ) as op:
        op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER, beat=1)
        for index, payload in enumerate(beats, start=2):
            op.append("beat_narration", payload, beat=index)
        op.append("message_rendered", {"text": "The day goes on."})
        await op.commit()
    assert op.narrative_turn is not None
    return op.narrative_turn


async def _item(engine: AsyncEngine, story: str, name: str) -> str:
    """An object entity of the story (not a character)."""
    item = new_ulid()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO entities (id, story_id, kind, canonical_name) "
                "VALUES (:i, :s, 'object', :n)"
            ),
            {"i": item, "s": story, "n": name},
        )
    return item


async def _analyze(
    client: httpx.AsyncClient, engine: AsyncEngine, story: str, key: str | None = None
) -> Any:
    """Ask for an analysis (a keyed request is replayed once, writing nothing more) and run
    its job; the 202's body."""
    path = f"/stories/{story}/state-proposals/analyze"
    response = await client.post(path, headers=_headers(key))
    assert response.status_code == 202, response.text
    if key is not None:
        replay = await client.post(path, headers=_headers(key))
        assert replay.status_code == 202, replay.text
        assert replay.headers["Idempotent-Replay"] == "true"
        assert replay.json() == response.json()
    (outcome,) = await embedding_support.run_jobs(engine, ANALYSIS, story)
    assert str(outcome) in ("committed", "CommitOutcome.COMMITTED"), outcome
    return response.json()


async def _proposals(client: httpx.AsyncClient, story: str) -> dict[str, Any]:
    response = await client.get(f"/stories/{story}/state-proposals", headers=READ)
    assert response.status_code == 200, response.text
    return {p["name"]: p for p in response.json()["proposals"]}


@pytest.mark.part("M6", "FR-STATE-3")
async def test_s_state_schema__proposals(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    """The story begins showing recurring resources with no State: an on-demand analysis
    (202, a ``state_proposal_analysis`` job) proposes each one recurring in three or more
    turns (a declared quantity's unit, a ``holds`` object, an amount re-parsed from a detected
    claim's span), never a definition: nothing is tracked until the author approves one, which
    declares it at a slot (``created_by = proposal``); a dismissed one stays dismissed.

    Complements: 20 played turns (the respond path) write no proposal; a one-off mention and a
    resource a State already tracks (by its name or unit) are not proposed; running the
    analysis again writes no duplicate and does not re-propose the dismissed one; a decided
    proposal cannot be decided again; with States off the analysis is 409 ``module_off``."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await _story(client, engine)
        await _defined(client, story, GOLD)
        player = ids[PLAYER]
        for n in range(20):
            await played(client, story, body(player, f"I walk on, step {n}."))
        (none,) = await rows(engine, PROPOSALS, s=story)
        assert none.n == 0

        key = await _item(engine, story, "Silver Key")
        texts = [
            ("She paid two bottles of wine and kept five loaves.", "two bottles"),
            ("He trades three bottles for the map.", "three bottles"),
            ("Two bottles more change hands.", "Two bottles"),
        ]
        for n, (said, amount) in enumerate(texts):
            once = [_detected(said, "five loaves", "consumption")] if n == 0 else []
            await _beats(
                engine,
                story,
                {
                    "text": said,
                    "propositions": [
                        _quantity(12 - n, "arrows", player),
                        _quantity(3, "gold", player),
                        _holds(player, key),
                    ],
                    "detected": [_detected(said, amount), *once],
                },
            )
        (none,) = await rows(engine, PROPOSALS, s=story)
        assert none.n == 0
        tracked_before = await rows(engine, DEFINITIONS, s=story)

        # The analysis: a request operation and a job; proposals, never definitions.
        queued = await _analyze(client, engine, story, key="analyze-1")
        assert set(queued) >= {"job_id", "operation_id", "timeline_revision"}
        (jobs,) = await rows(
            engine,
            "SELECT count(*) AS n FROM jobs WHERE story_id = :s AND kind = :k",
            s=story,
            k=ANALYSIS,
        )
        assert jobs.n == 1
        (request,) = await rows(
            engine,
            "SELECT kind::text AS kind FROM operations WHERE operation_id = :o",
            o=queued["operation_id"],
        )
        assert request.kind == "maintenance"
        found = await _proposals(client, story)
        assert set(found) == {"arrows", "bottle", "silver_key"}
        arrows, bottle, silver = found["arrows"], found["bottle"], found["silver_key"]
        assert all(p["status"] == "proposed" for p in found.values())
        assert all(p["created_by_operation_id"] == queued["operation_id"] for p in found.values())
        assert (arrows["suggested_type"], arrows["suggested_unit"]) == ("number", "arrows")
        assert len({e["turn"] for e in arrows["evidence"]}) == 3
        assert silver["suggested_entity_id"] == key
        assert {e["text"] for e in bottle["evidence"]} == {
            "two bottles",
            "three bottles",
            "Two bottles",
        }
        assert all(e["span"] is not None for e in bottle["evidence"])
        assert await rows(engine, DEFINITIONS, s=story) == tracked_before
        (values,) = await rows(
            engine, "SELECT count(*) AS n FROM state_values WHERE story_id = :s", s=story
        )
        assert values.n == 0
        _refused(
            await client.get(
                f"/stories/{story}/states/arrows", params={"viewpoint": "omniscient"}, headers=READ
            ),
            404,
            "not_found",
        )

        # Approval declares the State at a slot, with the author's overrides on top.
        approval = {"definition": {"scope": "story", "initial_value": 12}}
        approve_path = f"/stories/{story}/state-proposals/{arrows['id']}/approve"
        approved = await client.post(approve_path, json=approval, headers=_headers("approve-1"))
        assert approved.status_code == 201, approved.text
        done = approved.json()
        replayed = await client.post(approve_path, json=approval, headers=_headers("approve-1"))
        assert replayed.headers["Idempotent-Replay"] == "true"
        assert replayed.json() == done
        assert done["proposal"]["status"] == "approved"
        assert done["definition"]["created_by"] == "proposal"
        assert (done["definition"]["name"], done["definition"]["unit"]) == ("arrows", "arrows")
        assert done["position"].endswith("⁺1")
        assert done["proposal"]["definition_id"] is not None
        assert await _value(client, story, "arrows") == 12

        # Dismissal; deciding twice is 404.
        dismiss_path = f"/stories/{story}/state-proposals/{bottle['id']}/dismiss"
        dismissed = await client.post(dismiss_path, headers=_headers("dismiss-1"))
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["proposal"]["status"] == "dismissed"
        replayed = await client.post(dismiss_path, headers=_headers("dismiss-1"))
        assert replayed.headers["Idempotent-Replay"] == "true"
        assert replayed.json() == dismissed.json()
        again = _refused(
            await client.post(
                f"/stories/{story}/state-proposals/{bottle['id']}/approve",
                json={},
                headers=WRITE,
            ),
            404,
            "not_found",
        )
        assert again["details"]["reason"] == "no_pending_proposal"

        # A second run: no duplicate, the dismissed one stays dismissed.
        await _beats(
            engine,
            story,
            {
                "text": "Four bottles roll away.",
                "detected": [_detected("Four bottles roll away.", "Four bottles")],
            },
        )
        await _analyze(client, engine, story)
        (count,) = await rows(engine, PROPOSALS, s=story)
        assert count.n == 3
        after = await _proposals(client, story)
        assert {n: p["status"] for n, p in after.items()} == {
            "arrows": "approved",
            "bottle": "dismissed",
            "silver_key": "proposed",
        }

        # States off: the analysis is refused, and nothing is queued.
        await st.set_level(engine, story, None)
        off = await client.post(f"/stories/{story}/state-proposals/analyze", headers=WRITE)
        _refused(off, 409, "module_off")
        assert await embedding_support.run_jobs(engine, ANALYSIS, story) == []


async def _advance(client: httpx.AsyncClient, story: str, minutes: int) -> None:
    response = await client.post(
        f"/stories/{story}/clock/advance",
        json={"minutes": minutes, "reason": "time passes"},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text


async def _rule_changes(engine: AsyncEngine, story: str, name: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT c.delta, c.valid_time_minutes, c.operation_id, c.membership::text AS membership "
        "FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
        "WHERE c.story_id = :s AND d.name = :n AND c.source = 'rule' ORDER BY c.id",
        s=story,
        n=name,
    )


HUNGER = {
    "name": "hunger",
    "type": "number",
    "initial_value": 10,
    "constraints": {"min": 0, "max": 10},
    "rules": {"kind": "elapsed", "per_minutes": 60, "delta": -3, "bounds_clamp": True},
}
SILVER = {"name": "silver", "type": "number", "initial_value": 5}
NET_WORTH = {
    "name": "net_worth",
    "type": "number",
    "rules": {"kind": "derived", "expression": "gold + silver \u00d7 2"},
}
FATIGUE = {"name": "fatigue", "type": "number", "initial_value": 0}
STANDING = {
    "name": "standing",
    "type": "number",
    "rules": {"kind": "derived", "expression": "max(net_worth - 15, 0)"},
}


@pytest.mark.part("M6", "FR-STATE-7")
async def test_s_state_schema__rules(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    clock_delivered: None,
) -> None:
    """A story-time rule updates its State deterministically: hunger (``elapsed``, -3 per 60
    minutes, clamped at its bounds) writes one ``rule`` change per whole hour the clock passes,
    in the advance's operation; ``net_worth`` (``derived``: ``gold + silver * 2``, written with
    the multiplication sign) and ``standing`` (derived from it) are recomputed in topological
    order in the transaction of any input's change.

    Complements: a derived State rejects a direct set (422 ``state_derived``, #186's guard); a
    cycle is rejected at declaration and on a definition update; a definition with rules whose
    ``authorization.sources`` omit ``rule`` is 422 ``state_unpermitted {reason: source}``; any
    other rule shape is 422; the rule changes follow their operation's membership (a truncation
    takes them away); and a replay (the projection's fold) gives identical values with no job."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await _story(client, engine)
        await ck.set_level(engine, story, "prompt_only")
        for definition in (GOLD, SILVER, HUNGER, NET_WORTH, STANDING, FATIGUE):
            await _defined(client, story, definition)
        assert await _value(client, story, "net_worth") == 20
        assert await _value(client, story, "standing") == 5
        turn = (await played(client, story, body(ids[PLAYER], "Hello.")))["turn"]

        # Elapsed: 150 minutes is two whole hours (two ticks), 60 more one tick, 120 more two
        # ticks, the second clamped at the minimum and the third holding there.
        await _advance(client, story, 150)
        assert await _value(client, story, "hunger") == 4
        ticks = await _rule_changes(engine, story, "hunger")
        assert [int(t.delta) for t in ticks] == [-3, -3]
        assert len({t.operation_id for t in ticks}) == 1
        (row,) = await rows(
            engine, "SELECT story_minute_zero AS z FROM stories WHERE id = :s", s=story
        )
        zero = int(row.z)
        assert [t.valid_time_minutes - zero for t in ticks] == [60, 120]
        await _advance(client, story, 60)
        assert await _value(client, story, "hunger") == 1
        await _advance(client, story, 120)
        assert await _value(client, story, "hunger") == 0
        assert [int(t.delta) for t in await _rule_changes(engine, story, "hunger")] == [
            -3,
            -3,
            -3,
            -1,
            0,
        ]
        await _advance(client, story, 29)  # 359 minutes: the sixth hour is not yet whole
        assert len(await _rule_changes(engine, story, "hunger")) == 5

        # Derived: folded from the inputs on every read, at every position; nothing stored.
        written = (await _put(client, story, "gold", {"delta": 5})).json()
        assert await _value(client, story, "net_worth") == 25
        assert await _value(client, story, "standing") == 10
        assert await _value(client, story, "net_worth", str(turn)) == 20
        recorded = await rows(
            engine,
            "SELECT d.name FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
            "WHERE c.story_id = :s AND d.name IN ('net_worth', 'standing')",
            s=story,
        )
        assert recorded == []
        # An input changed at an earlier position (0, before every turn): every later value of
        # the derived States follows it, at the head and at checkpoint N.
        await st.change(
            engine,
            story,
            st.delta("silver", 1, source=StateChangeSource.CONSUMER, position=Position(0)),
            kind=OperationKind.CONTROL,
        )
        assert await _value(client, story, "net_worth") == 27
        assert await _value(client, story, "standing") == 12
        assert await _value(client, story, "net_worth", str(turn)) == 22

        # The refusals write nothing.
        counted = await _count(engine, story)
        derived = _refused(
            await _put(client, story, "net_worth", {"value": 1}), 422, "state_derived"
        )
        assert derived["details"]["state"] == "net_worth"
        cycle = _refused(
            await _define(
                client,
                story,
                {
                    "name": "loop",
                    "type": "number",
                    "rules": {"kind": "derived", "expression": "loop + 1"},
                },
            ),
            422,
            "validation_error",
        )
        assert cycle["details"]["reason"] == "rules_cycle"
        patched = _refused(
            await client.patch(
                f"/stories/{story}/states/net_worth/definition",
                json={"rules": {"kind": "derived", "expression": "gold + standing"}},
                headers=WRITE,
            ),
            422,
            "validation_error",
        )
        assert patched["details"]["reason"] == "rules_cycle"
        unpermitted = _refused(
            await _define(
                client,
                story,
                {**HUNGER, "name": "thirst", "authorization": {"sources": ["consumer"]}},
            ),
            422,
            "state_unpermitted",
        )
        assert unpermitted["details"]["reason"] == "source"
        shape = _refused(
            await _define(
                client,
                story,
                {**SILVER, "name": "luck", "rules": {"kind": "script", "code": "luck += 1"}},
            ),
            422,
            "validation_error",
        )
        assert shape["details"]["reason"] == "rules_shape"
        assert await _count(engine, story) == counted

        # Replay: the projection's fold over the stored changes gives the same values, and no
        # job was ever needed.
        stored = await st.stored_values(engine, story)
        assert await st.folded_values(engine, story) == stored
        async with engine.begin() as conn:
            await projections.replace_with_fold(conn, story)
        assert await st.stored_values(engine, story) == stored
        (jobs,) = await rows(engine, "SELECT count(*) AS n FROM jobs WHERE story_id = :s", s=story)
        assert jobs.n == 0

        # Membership: truncating to the turn discards its slots' advances and sets, and the
        # rule changes with them.
        await played(client, story, body(ids[PLAYER], "And then?"))
        await _post(client, f"/stories/{story}/truncate", {"to_turn": turn})
        assert await _value(client, story, "hunger") == 10
        assert await _value(client, story, "net_worth") == 22
        assert {t.membership for t in await _rule_changes(engine, story, "hunger")} == {"discarded"}
        # An untruncation restores an input's active value, and the derived ones follow.
        await _post(client, f"/stories/{story}/untruncate", {})
        assert await _value(client, story, "net_worth") == 27
        assert await _value(client, story, "hunger") == 0

        # A changed expression governs the positions after its configuration change; the
        # positions before it keep the old one.
        changed = await client.patch(
            f"/stories/{story}/states/net_worth/definition",
            json={"rules": {"kind": "derived", "expression": "gold + silver * 3"}},
            headers=WRITE,
        )
        assert changed.status_code == 200, changed.text
        assert await _value(client, story, "net_worth") == 33
        assert await _value(client, story, "net_worth", written["position"]) == 27
        assert await _value(client, story, "net_worth", str(turn)) == 22
        later = (await _put(client, story, "silver", {"delta": 1})).json()
        assert await _value(client, story, "net_worth") == 36
        assert await _value(client, story, "net_worth", later["position"]) == 36
        # A State never becomes, or stops being, derived.
        for name, rules in (("net_worth", None), ("gold", NET_WORTH["rules"])):
            kind = _refused(
                await client.patch(
                    f"/stories/{story}/states/{name}/definition",
                    json={"rules": rules},
                    headers=WRITE,
                ),
                422,
                "validation_error",
            )
            assert kind["details"]["reason"] == "rules_kind"

        # An elapsed rule added by an update counts from the update, not the declaration: the
        # clock stands at 359 minutes, and 60 more is one tick (not six).
        added = await client.patch(
            f"/stories/{story}/states/fatigue/definition",
            json={"rules": {"kind": "elapsed", "per_minutes": 60, "delta": 1}},
            headers=WRITE,
        )
        assert added.status_code == 200, added.text
        await _advance(client, story, 60)
        assert await _value(client, story, "fatigue") == 1
        assert len(await _rule_changes(engine, story, "fatigue")) == 1
