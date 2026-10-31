"""S-EFFECT-LIFECYCLE: external effects under rewind, regeneration, fork and swap (docs/04 §7;
FR-EXT-12,13,14; #211).

Played end to end through ``respond`` with the stub adapter. Cal's gold is owned by a
write-capable, asynchronous in-process provider (#210's ``_write_capable``); "I spend 50 gold"
writes an ``intended`` outbox row in the turn's commit.

- (a) A payment queued, then rewound before dispatch: the truncation cancels the row in its own
  transaction and the dispatcher never sends it, including when the dispatcher had selected
  the row while the truncation was committing (its claim skips the row the truncation holds,
  and finds it ``cancelled`` after).
- (e) Swapped back to the continuation whose payment was cancelled: the swap lists it as
  ``cancelled_on_reactivation``, the reconciliation status lists it, and it is never sent.
- (b) The same with regeneration: a regenerate cancels the replaced variant's pending payment;
  a confirmed one comes back in the regenerate's response as ``reconciliation_required``.
- (d) A confirmed and a claimed payment, then a rewind past them: the truncation's response
  lists both with ``reconciliation_required``, the reconciliation status keeps them, and
  nothing is compensated (no control operation writes an outbox row, and nothing more is
  sent).
- (c) A fork while a payment is pending: the fork's response lists it in
  ``effects_pending_in_source`` (the source's outbox id), and the fork holds only
  non-executable copies under new ids, which nothing dispatches.

Complement: a rewind that discards only turns without effects answers an empty
``reconciliation_required``. (Erasure, S-EFFECT-LIFECYCLE (f), is asserted with
S-ERASURE-BOUNDARY's M6 outbox part, ``test_s_erasure_boundary__m6_outbox``.)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import timeline
from epistrel.core.timeline import MembershipChange
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import AUTHOR, body, respond, respond_client
from tests.scenario.test_s_ext_writes import _dispatch, _spend_turn, _write_capable
from tests.story_support import WRITE
from tests.structured_turn_support import beat

pytestmark = pytest.mark.integration

GOLD = {"kind": "change", "state": "gold", "delta": -50, "reason": "bought a sword"}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _quiet_turn(client: Any, epistrel: Any, story: str, cal: str) -> dict[str, Any]:
    """A turn whose renders declare no effect."""
    stub = epistrel.models.stub
    stub.reset()
    ms.renders_for(stub, "Mira", es.render(beat("say", "Close the door.")))
    es.narrator(stub, es.render(beat("narration", "Wind rattles the shutters.")))
    answer = await respond(client, story, body(cal, None, do="looks around"))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def _regenerate(client: Any, epistrel: Any, story: str) -> dict[str, Any]:
    """Regenerate the latest turn with renders that declare no effect."""
    stub = epistrel.models.stub
    stub.reset()
    ms.renders_for(stub, "Mira", es.render(beat("say", "Not today.")))
    es.narrator(stub, es.render(beat("narration", "The forge cools.")))
    answer = await client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def _edit(client: Any, story: str, path: str, payload: Any) -> dict[str, Any]:
    answer = await client.post(f"/stories/{story}/{path}", json=payload, headers=WRITE)
    assert answer.status_code in (200, 201), answer.text
    return dict(answer.json())


async def _status(engine: AsyncEngine, story: str) -> dict[str, str]:
    return {r.id: r.status for r in await ps.outbox_rows(engine, story)}


async def _listed(epistrel: Any, story: str) -> dict[str, str]:
    return {e.outbox_id: e.reason for e in (await epistrel.reconciliation(AUTHOR, story)).listings}


def _required(outbox_id: str, provider: str, status: str) -> dict[str, Any]:
    return {"outbox_id": outbox_id, "provider": provider, "status": status, "value": "gold"}


@pytest.mark.part("M6", "FR-EXT-12,13,14")
async def test_s_effect_lifecycle(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, fake = await _write_capable(engine, epistrel, client)
        provider = fake.name
        # Turn 1 has no effect; the rewinds go back to it (the State definitions sit before).
        await _quiet_turn(client, epistrel, story, cal)

        # (a) Queued, then rewound before dispatch: cancelled in the truncation, never sent.
        played = await _spend_turn(client, epistrel, story, cal)
        first = played["state_effects"][0]["outbox_id"]
        rewound = await _edit(client, story, "truncate", {"to_turn": 1})
        assert rewound["reconciliation_required"] == []
        assert (await _status(engine, story))[first] == "cancelled"
        assert await _dispatch(engine, epistrel, first) == outbox_job.SKIPPED
        assert first not in await _listed(epistrel, story)

        # (a) The dispatcher selected the row while the truncation was committing: the claim
        # skips the row the truncation's transaction holds, and it is cancelled after.
        played = await _spend_turn(client, epistrel, story, cal)
        raced = played["state_effects"][0]["outbox_id"]
        inside, release = asyncio.Event(), asyncio.Event()

        async def hold(conn: AsyncConnection, change: MembershipChange) -> None:
            inside.set()
            await release.wait()

        timeline.on_membership_change(hook=hold)
        try:
            truncating = asyncio.create_task(
                client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE)
            )
            await asyncio.wait_for(inside.wait(), timeout=30)
            assert await _dispatch(engine, epistrel, raced) == outbox_job.SKIPPED
            release.set()
            answer = await truncating
        finally:
            timeline.remove_membership_hook(hold)
        assert answer.status_code == 200, answer.text
        assert answer.json()["reconciliation_required"] == []
        assert (await _status(engine, story))[raced] == "cancelled"
        assert await _dispatch(engine, epistrel, raced) == outbox_job.SKIPPED
        assert fake.idempotency_keys == []

        # (e) Swap back to the continuation whose payment was cancelled: listed, never sent.
        swapped = await _edit(
            client, story, "swap", {"to_variant_chain": {"turn": 2, "variant": 1}}
        )
        assert swapped["cancelled_on_reactivation"] == [_required(first, provider, "cancelled")]
        assert swapped["reconciliation_required"] == []
        assert (await _listed(epistrel, story))[first] == "cancelled_on_reactivation"
        assert await _dispatch(engine, epistrel, first) == outbox_job.SKIPPED
        assert (await _status(engine, story))[first] == "cancelled"
        assert fake.idempotency_keys == []

        # (b) Regenerated before dispatch: the replaced variant's payment is cancelled.
        played = await _spend_turn(client, epistrel, story, cal)
        pending = played["state_effects"][0]["outbox_id"]
        regenerated = await _regenerate(client, epistrel, story)
        assert "reconciliation_required" not in regenerated
        assert (await _status(engine, story))[pending] == "cancelled"
        assert await _dispatch(engine, epistrel, pending) == outbox_job.SKIPPED

        # (b) Confirmed, then regenerated: the regenerate lists it; it stays listed.
        played = await _spend_turn(client, epistrel, story, cal)
        paid = played["state_effects"][0]["outbox_id"]
        assert await _dispatch(engine, epistrel, paid) == "confirmed"
        regenerated = await _regenerate(client, epistrel, story)
        assert regenerated["reconciliation_required"] == [_required(paid, provider, "confirmed")]
        assert (await _listed(epistrel, story))[paid] == "reconciliation_required"

        # (d) Confirmed and claimed, then rewound past: listed, never compensated.
        head = regenerated["turn"]
        played = await _spend_turn(client, epistrel, story, cal)
        confirmed = played["state_effects"][0]["outbox_id"]
        assert await _dispatch(engine, epistrel, confirmed) == "confirmed"
        played = await _spend_turn(client, epistrel, story, cal)
        claimed_id = played["state_effects"][0]["outbox_id"]
        claim = await outbox_job.claim(engine, claimed_id, registry=epistrel.in_process_providers)
        assert isinstance(claim, outbox_job.Claimed)
        rows_before, sent_before = len(await ps.outbox_rows(engine, story)), len(fake.writes)
        rewound = await _edit(client, story, "truncate", {"to_turn": head})
        assert rewound["reconciliation_required"] == sorted(
            [
                _required(confirmed, provider, "confirmed"),
                _required(claimed_id, provider, "claimed"),
            ],
            key=lambda r: r["outbox_id"],
        )
        listed = await _listed(epistrel, story)
        assert listed[confirmed] == listed[claimed_id] == "reconciliation_required"
        answer = await client.get(f"/stories/{story}/reconciliation", headers=WRITE)
        assert {e["outbox_id"]: e["reason"] for e in answer.json()["listings"]} == listed
        assert len(await ps.outbox_rows(engine, story)) == rows_before
        assert len(fake.writes) == sent_before
        assert (await _status(engine, story))[confirmed] == "confirmed"

        # (c) Fork while a payment is pending: listed in the response, never dispatched.
        played = await _spend_turn(client, epistrel, story, cal)
        waiting = played["state_effects"][0]["outbox_id"]
        rows_before = len(await ps.outbox_rows(engine, story))
        forked = await _edit(client, story, "fork", {})
        assert forked["effects_pending_in_source"] == [
            {"outbox_id": waiting, "status": "intended", "effect": GOLD}
        ]
        fork_id = forked["story_id"]
        copies = await ps.outbox_rows(engine, fork_id)
        assert copies
        assert not any(r.executable for r in copies)
        source_ids = {r.id for r in await ps.outbox_rows(engine, story)}
        assert {r.id for r in copies}.isdisjoint(source_ids)
        for row in copies:
            assert await _dispatch(engine, epistrel, row.id) == outbox_job.SKIPPED
        await outbox_job.recover(engine)
        assert await ps.outbox_jobs(engine, fork_id) == []
        assert (await epistrel.reconciliation(AUTHOR, fork_id)).listings == []
        assert len(await ps.outbox_rows(engine, story)) == rows_before
        assert await _dispatch(engine, epistrel, waiting) == "confirmed"
        assert waiting in fake.idempotency_keys
        assert {r.id for r in copies}.isdisjoint(fake.idempotency_keys)

        # Complement: a rewind over a turn without effects answers an empty list.
        quiet = await _quiet_turn(client, epistrel, story, cal)
        rewound = await _edit(client, story, "truncate", {"to_turn": quiet["turn"] - 1})
        assert rewound["reconciliation_required"] == []

        await ps.drop_outbox_jobs(engine, story)
        await ps.drop_outbox_jobs(engine, fork_id)
        await ss.set_level(engine, story, None)
