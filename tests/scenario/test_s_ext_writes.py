"""S-EXT-WRITES: changes to game-owned values (docs/04 §7; FR-EXT-9, FR-RESP-7).

Every S-EXT-WRITES M6 part lives in this file: #209's read-only part (``__read_only``) and
#210's write-capable parts (``__outbox``, ``__reconciliation_api``: their own docstrings).

#209's part, played end to end through ``respond`` with the stub adapter: Cal's gold is owned by
a registered provider that is not ``write_capable`` (an in-process ``ScriptedProvider``) and
reads 300. "I spend 50 gold": Mira's render declares ``gold -50``. Epistrel never applies it:
no ``state_changes`` row is written, a ``requested_effect`` event (``input``, at the declaring
beat, ``{state, scope_key, delta, reason, effect_id, status: requested}``) is recorded, and the
response returns it with ``owner: external``, ``status: pending`` and its ``effect_id``. The
pinned value and ``external_values`` are unchanged (300), and the next turn's live read is
whatever the adapter says (here 260, as the game applied it).

Complement: the same effect on a story-owned State commits a change (300 → 250).
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.ids import new_ulid
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import AUTHOR, body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

SOLD = "Mira takes the coins and hands Cal the sword."
REASON = "bought a sword"
ULID_LENGTH = 26


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _spend(cal: str) -> str:
    return es.render(
        beat("do", SOLD, effects=[es.effect("gold", scope_key=cal, delta=-50, reason=REASON)]),
        beat("say", "Mind the edge.", intent="sincere"),
    )


async def requested(engine: AsyncEngine, story: str) -> list[Any]:
    """The story's ``requested_effect`` events, in log order."""
    return await rows(
        engine,
        "SELECT id, payload, category, beat, step, narrative_turn FROM events "
        "WHERE story_id = :s AND type = 'requested_effect' ORDER BY seq",
        s=story,
    )


@pytest.mark.part("M6", "FR-EXT-9; FR-RESP-7")
async def test_s_ext_writes__read_only(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = ps.ScriptedProvider(name).attach(epistrel)
        fake.script("gold", 300, 260)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)

        # "I spend 50 gold": returned, not applied.
        stub.reset()
        ms.renders_for(stub, "Mira", _spend(cal))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        answer = await respond(client, story, body(cal, None, do="spends 50 gold on a sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        (effect,) = played["state_effects"]
        effect_id = effect.pop("effect_id")
        assert len(effect_id) == ULID_LENGTH
        assert effect == {
            "state": "gold",
            "scope_key": cal,
            "old_value": 300,
            "new_value": None,
            "reason": REASON,
            "source": "turn",
            "contingency": "unconditional",
            "status": "pending",
            "owner": "external",
            "delta": -50,
        }
        assert await es.turn_changes(engine, story) == []
        (event,) = await requested(engine, story)
        assert event.category == "input"
        assert event.payload == {
            "effect_id": effect_id,
            "kind": "change",
            "state": "gold",
            "scope_key": cal,
            "delta": -50,
            "reason": REASON,
            "status": "requested",
        }
        do_beat = next(b for b in played["beats"] if b["text"] == SOLD)
        assert (event.narrative_turn, event.beat) == (played["turn"], do_beat["beat"])
        report = (await turn_report(engine, story))["state_effects"]
        assert (report["committed"], report["requested"]) == (0, 1)

        # The pin and external_values are unchanged: the one read, 300.
        assert [r["value"] for r in await ps.external_value_rows(engine, story)] == [300]

        # The next turn reads live again: whatever the adapter says (the game applied it).
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Wind rattles the door.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Close it.")))
        again = await respond(client, story, body(cal, None, do="looks around"))
        assert again.status_code == 200, again.text
        assert "state_effects" not in again.json()
        assert [r.payload["value"] for r in await ps.external_reads(engine, story)] == [300, 260]
        assert len(await requested(engine, story)) == 1
        assert await es.turn_changes(engine, story) == []

        # Complement: the same effect on a story-owned State commits a change.
        owned, owned_ids = await es.shop(client, engine, es.gold(), gold_of_player=300)
        stub.reset()
        ms.renders_for(stub, "Mira", _spend(owned_ids["Cal"]))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        plain = await respond(
            client, owned, body(owned_ids["Cal"], None, do="spends 50 gold on a sword")
        )
        assert plain.status_code == 200, plain.text
        assert [
            (e["old_value"], e["new_value"], e["status"]) for e in plain.json()["state_effects"]
        ] == [(300, 250, "committed")]
        assert "effect_id" not in plain.json()["state_effects"][0]
        assert await requested(engine, owned) == []
        assert len(await es.turn_changes(engine, owned)) == 1
        await ss.set_level(engine, story, None)
        await ss.set_level(engine, owned, None)


# -- #210: write-capable providers, through the outbox -------------------------------------------


async def _write_capable(
    engine: AsyncEngine, epistrel: Any, client: Any, *, dedupes: bool = True
) -> tuple[str, str, ps.ScriptedProvider]:
    """A structured story whose Cal's gold a write-capable, asynchronous in-process provider
    owns (it reads 300); answers the story, Cal's id and the fake."""
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name, dedupes=dedupes))
    fake = ps.ScriptedProvider(name, default=300).attach(epistrel)
    story, ids = await structured_story(client, "Cal", "Mira")
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    return story, ids["Cal"], fake


async def _spend_turn(
    client: Any, epistrel: Any, story: str, cal: str, *, expect: int | None = 200
) -> dict[str, Any]:
    """Mira's render declares ``gold -50`` for Cal's sword; the response's body (with
    ``expect=None``: ``{"status": <code>}``)."""
    stub = epistrel.models.stub
    stub.reset()
    ms.renders_for(stub, "Mira", _spend(cal))
    es.narrator(stub, es.render(beat("narration", "The forge glows.")))
    answer = await respond(client, story, body(cal, None, do="spends 50 gold on a sword"))
    if expect is None:
        return {"status": answer.status_code}
    assert answer.status_code == expect, answer.text
    return dict(answer.json())


async def _dispatch(engine: AsyncEngine, epistrel: Any, outbox_id: str) -> str:
    return await outbox_job.dispatch(engine, outbox_id, registry=epistrel.in_process_providers)


async def _beats(engine: AsyncEngine, story: str) -> tuple[int, int]:
    """How many events the story has, and how many of them are a turn's message."""
    found = await rows(
        engine,
        "SELECT count(*) AS n, count(*) FILTER (WHERE type = 'message_rendered') AS m "
        "FROM events WHERE story_id = :s",
        s=story,
    )
    return int(found[0].n), int(found[0].m)


@pytest.mark.part("M6", "FR-EXT-9,10; FR-RESP-7")
async def test_s_ext_writes__outbox(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#210's write part: against a write-capable provider the turn and its outbox row commit
    together (an induced failure after the row and before the turn's end leaves neither),
    dispatch carries the row id as the idempotency key, the confirmation is recorded; a failed
    or unknown answer is listed as unconfirmed and never reported as applied, and a re-send
    under the same key does not apply twice. Complement: a read-only provider writes no row."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, fake = await _write_capable(engine, epistrel, client)

        # An induced failure after the row is written and before the turn commits: neither.
        async def broken(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("induced")

        beats_before = await _beats(engine, story)
        with monkeypatch.context() as patched, contextlib.suppress(RuntimeError):
            patched.setattr(outbox_job, "enqueue", broken)
            failed = await _spend_turn(client, epistrel, story, cal, expect=None)
            assert failed["status"] >= 500
        assert await ps.outbox_rows(engine, story) == []
        assert await _beats(engine, story) == beats_before

        # The turn and its row commit together: the effect is pending, with its outbox id.
        played = await _spend_turn(client, epistrel, story, cal)
        assert (await _beats(engine, story))[1] == beats_before[1] + 1
        (effect,) = played["state_effects"]
        assert (effect["owner"], effect["status"], effect["delta"]) == ("external", "pending", -50)
        assert effect["outbox_id"] == effect["effect_id"]
        assert played["reconciliation"] == {"count": 1}
        assert await es.turn_changes(engine, story) == []
        assert await requested(engine, story) == []
        (row,) = await ps.outbox_rows(engine, story)
        do_beat = next(b for b in played["beats"] if b["text"] == SOLD)
        assert (row.id, row.status, row.member) == (effect["outbox_id"], "intended", "active")
        assert (row.narrative_turn, row.beat) == (played["turn"], do_beat["beat"])
        assert (row.value_name, row.scope_key, row.attempts) == ("gold", cal, 0)
        assert row.effect == {"kind": "change", "state": "gold", "delta": -50, "reason": REASON}
        (job,) = await ps.outbox_jobs(engine, story)
        assert (job.status, job.payload, job.coalesce_key) == (
            "queued",
            {"outbox_id": row.id},
            f"outbox:{row.id}",
        )

        # Dispatch: the idempotency key is the row id; the confirmation is recorded.
        assert await _dispatch(engine, epistrel, row.id) == "confirmed"
        assert fake.idempotency_keys == [row.id]
        assert fake.writes[0].delta == -50
        (row,) = await ps.outbox_rows(engine, story)
        assert (row.status, row.attempts) == ("confirmed", 1)
        assert row.sent_at is not None
        assert row.claimed_at is not None
        (outcome,) = await ps.outbox_outcomes(engine, story)
        assert (outcome.outbox_id, outcome.kind, outcome.received_via) == (
            row.id,
            "confirmed",
            "dispatcher",
        )
        # A second dispatch of the same row sends nothing.
        assert await _dispatch(engine, epistrel, row.id) == "skipped"
        assert fake.idempotency_keys == [row.id]
        assert (await epistrel.reconciliation(AUTHOR, story)).listings == []

        # The provider fails: listed as failed, never reported as applied.
        fake.script_writes("gold", "failed")
        played = await _spend_turn(client, epistrel, story, cal)
        assert played["state_effects"][0]["status"] == "pending"
        second = played["state_effects"][0]["outbox_id"]
        assert await _dispatch(engine, epistrel, second) == "failed"
        listed = (await epistrel.reconciliation(AUTHOR, story)).listings
        assert [(entry.outbox_id, entry.reason) for entry in listed] == [(second, "failed")]
        assert await es.turn_changes(engine, story) == []

        # An ambiguous answer (a timeout): unknown, listed; the re-send at the lease's end goes
        # under the same key and the fixture's dedupe applies it once.
        fake.script_writes("gold", "unknown")
        played = await _spend_turn(client, epistrel, story, cal)
        third = played["state_effects"][0]["outbox_id"]
        assert await _dispatch(engine, epistrel, third) == "unknown"
        assert played["reconciliation"] == {"count": 2}
        reasons = {
            e.outbox_id: e.reason for e in (await epistrel.reconciliation(AUTHOR, story)).listings
        }
        assert reasons == {second: "failed", third: "unknown"}
        with clock.frozen_at(clock.now() + outbox_job.CLAIM_LEASE + timedelta(seconds=1)):
            assert (
                await outbox_job.expire(engine, third, registry=epistrel.in_process_providers)
                == "resent:unknown"
            )
        assert fake.idempotency_keys[-2:] == [third, third]
        assert fake.deduped == 1
        assert await es.turn_changes(engine, story) == []
        await ps.drop_outbox_jobs(engine, story)

        # Complement: a read-only provider's effect writes no outbox row.
        name = ps.unique_name("ledger")
        await ps.register(engine, ps.in_process_spec(name))
        ps.ScriptedProvider(name, default=300).attach(epistrel)
        plain, plain_ids = await structured_story(client, "Cal", "Mira")
        await ss.set_level(engine, plain, "on")
        await ps.external_gold(engine, plain, name, authorization=es.ANY_CHARACTER)
        played = await _spend_turn(client, epistrel, plain, plain_ids["Cal"])
        assert "outbox_id" not in played["state_effects"][0]
        assert "reconciliation" not in played
        assert await ps.outbox_rows(engine, plain) == []
        assert len(await requested(engine, plain)) == 1
        await ss.set_level(engine, story, None)
        await ss.set_level(engine, plain, None)


@pytest.mark.part("M6", "FR-EXT-10")
async def test_s_ext_writes__reconciliation_api(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """``GET /stories/{id}/reconciliation`` (and ``Engine.reconciliation``): an undispatched
    effect is ``unconfirmed``, a failed one ``failed``, an unknown one from a provider that
    does not deduplicate ``unknown`` (its lease lapses once: never re-sent), each with its
    provider, value, status, since, effect and latest outcome; the respond response carries
    ``reconciliation: {count}``; a confirmed effect leaves the list. 404 for an unknown story."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, fake = await _write_capable(engine, epistrel, client, dedupes=False)
        played = await _spend_turn(client, epistrel, story, cal)
        assert played["reconciliation"] == {"count": 1}
        first = played["state_effects"][0]["outbox_id"]
        answer = await client.get(f"/stories/{story}/reconciliation", headers=WRITE)
        assert answer.status_code == 200, answer.text
        (entry,) = answer.json()["listings"]
        assert entry["since"]
        assert {k: v for k, v in entry.items() if k != "since"} == {
            "outbox_id": first,
            "provider": fake.name,
            "value": "gold",
            "status": "intended",
            "reason": "unconfirmed",
            "effect": {"kind": "change", "state": "gold", "delta": -50, "reason": REASON},
        }
        library = await epistrel.reconciliation(AUTHOR, story)
        assert [e.model_dump(mode="json") for e in library.listings] == answer.json()["listings"]

        fake.script_writes("gold", "failed", "unknown")
        assert await _dispatch(engine, epistrel, first) == "failed"
        played = await _spend_turn(client, epistrel, story, cal)
        second = played["state_effects"][0]["outbox_id"]
        claimed = await outbox_job.claim(engine, second, registry=epistrel.in_process_providers)
        assert isinstance(claimed, outbox_job.Claimed)
        # The worker dies before it sends: at the lease's end the row is unknown, once.
        later = clock.now() + outbox_job.CLAIM_LEASE + timedelta(seconds=1)
        with clock.frozen_at(later):
            for _ in range(2):
                await outbox_job.expire(engine, second, registry=epistrel.in_process_providers)
        assert fake.idempotency_keys == [first]
        played = await _spend_turn(client, epistrel, story, cal)
        assert played["reconciliation"] == {"count": 3}
        third = played["state_effects"][0]["outbox_id"]
        answer = await client.get(f"/stories/{story}/reconciliation", headers=WRITE)
        listed = answer.json()["listings"]
        assert [(e["outbox_id"], e["status"], e["reason"]) for e in listed] == [
            (first, "failed", "failed"),
            (second, "unknown", "unknown"),
            (third, "intended", "unconfirmed"),
        ]
        assert listed[0]["outcome"]["kind"] == "failed"
        assert listed[0]["outcome"]["received_via"] == "dispatcher"
        assert listed[1]["outcome"]["kind"] == "unknown"
        assert [o.kind for o in await ps.outbox_outcomes(engine, story)] == ["failed", "unknown"]

        # Confirmed: the effect leaves the list.
        fake.script_writes("gold", "confirmed")
        assert await _dispatch(engine, epistrel, third) == "confirmed"
        answer = await client.get(f"/stories/{story}/reconciliation", headers=WRITE)
        assert [e["outbox_id"] for e in answer.json()["listings"]] == [first, second]

        missing = await client.get(f"/stories/{new_ulid()}/reconciliation", headers=WRITE)
        assert missing.status_code == 404
        await ps.drop_outbox_jobs(engine, story)
        await ss.set_level(engine, story, None)
