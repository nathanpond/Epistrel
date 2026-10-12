"""I-14: the idempotency result window (docs/04 §5; #61).

Within the window a retry returns the identical stored answer; after it (the clock injected
past ``expires_at``, then the result purged as #71's sweep will) the tombstone remains and a
retry gets a reference to the original operation, never a re-execution; a different payload
under the key is still refused.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.clock import frozen_at
from epistrel.core.idempotency import purge_expired
from tests.auth_support import auth_settings
from tests.idempotency_support import REPLAY, key_row, keyed, operations_of
from tests.respond_support import FIRST, PLAYER, body, generation_calls, new_story, respond_client
from tests.story_support import READ


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M2", "FR-CONC-6; NFR-REL-4")
async def test_i_14(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Identical replay in the window; after expiry a reference, no re-execution (the stub's
    call count is unchanged), the tombstone kept; a different payload is 409 throughout.
    Complement: the operation the reference names is the original, readable with its result
    inside the window and without it after."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await new_story(client)
        request = body(ids[PLAYER], "Where does this road lead?")
        original = await keyed(client, story, request, "i14-key")
        assert original.status_code == 200, original.text
        assert original.json()["idempotency_key"] == "i14-key"
        calls, operations = len(generation_calls(stub)), await operations_of(engine, story)

        # Within the window: the identical stored answer, nothing executed or written.
        replay = await keyed(client, story, request, "i14-key")
        assert (replay.status_code, replay.headers.get(REPLAY)) == (200, "true")
        assert replay.content == original.content
        assert (len(generation_calls(stub)), await operations_of(engine, story)) == (
            calls,
            operations,
        )
        conflict = await keyed(client, story, body(ids[PLAYER], "Elsewhere."), "i14-key")
        assert (conflict.status_code, conflict.json()["error"]["code"]) == (
            409,
            "idempotency_conflict",
        )
        # The hash covers the actor: the same words as another character are a conflict, never
        # the first character's cached reply (#61 AC3; #289).
        as_other = await keyed(
            client, story, body(ids[FIRST], "Where does this road lead?"), "i14-key"
        )
        assert as_other.status_code == 409, as_other.text
        assert as_other.json()["error"]["code"] == "idempotency_conflict"
        assert as_other.json()["error"]["details"]["reason"] == "request_hash"
        assert (len(generation_calls(stub)), await operations_of(engine, story)) == (
            calls,
            operations,
        )
        stored = await key_row(engine, "i14-key")
        operation_id = stored.operation_id
        viewpoint = original.json()["output_viewpoint"]
        read = await client.get(f"/operations/{operation_id}?viewpoint={viewpoint}", headers=READ)
        assert read.json()["result"]["body"]["message"] == original.json()["message"]

        # After the window: the stored result is no longer handed out, even before the purge.
        later = stored.expires_at + timedelta(seconds=1)
        with frozen_at(later):
            reference = await keyed(client, story, request, "i14-key")
            assert (reference.status_code, reference.headers.get(REPLAY)) == (200, "true")
            assert reference.json() == {
                "operation_id": operation_id,
                "reference": f"/operations/{operation_id}",
                "timeline_revision": original.json()["timeline_revision"],
                "idempotency_key": "i14-key",
            }
            assert await purge_expired(engine) >= 1
            tombstone = await key_row(engine, "i14-key")
            assert tombstone.result is None
            assert (tombstone.request_hash, tombstone.operation_id) == (
                stored.request_hash,
                operation_id,
            )
            again = await keyed(client, story, request, "i14-key")
            assert again.json() == reference.json()
            different = await keyed(client, story, body(ids[PLAYER], "Elsewhere."), "i14-key")
            assert different.status_code == 409
            followed = await client.get(
                f"/operations/{operation_id}?viewpoint={viewpoint}", headers=READ
            )
            assert followed.json()["status"] == "committed"
            assert followed.json()["result"] is None
            assert followed.json()["narrative_turn"] == original.json()["turn"]
        assert len(generation_calls(stub)) == calls
        assert await operations_of(engine, story) == operations
