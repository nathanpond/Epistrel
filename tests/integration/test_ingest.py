"""``POST /stories/{id}/ingest`` (#58, FR-RESP-10): validation, the order of the checks, the
all-or-nothing batch, replays mixed with new events, normalisation before hashing, concurrent
duplicates, ``timeline_changed``, and ingested events in history and in the next prompt.
S-INPUT-INGEST's ingest half is ``test_s_input_ingest__ingest``."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import ingest as core_ingest
from epistrel.core.concurrency import RevalidationState
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation, OperationBatch
from epistrel.core.ingest import IngestRequest
from epistrel.orchestrator.policies.schemas import StoryPolicyItem
from tests.auth_support import auth_settings
from tests.concurrency_support import set_pins
from tests.event_support import rows
from tests.policy_support import unique, words
from tests.projection_support import truncate
from tests.respond_support import (
    FIRST,
    PLAYER,
    body,
    generation_calls,
    new_story,
    played,
    respond_client,
)
from tests.story_support import READ, WRITE

AUTHOR = RequestContext(principal="p-author")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def narration(cid: str, text: str = "Rain hammers the roof.") -> dict[str, Any]:
    return {"client_event_id": cid, "kind": "narration", "text": text}


def player(cid: str, actor: str, say: str = "I wait.", **fields: Any) -> dict[str, Any]:
    return {"client_event_id": cid, "kind": "player_turn", "actor_character_id": actor,
            "say": say, **fields}  # fmt: skip


async def ingest(
    client: httpx.AsyncClient,
    story_id: str,
    *events: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/ingest",
        json={"events": list(events)},
        headers={**WRITE, **(headers or {})},
    )


async def footprint(engine: AsyncEngine, story_id: str) -> tuple[Any, ...]:
    """What an ingest could write: operations, events, dedupe rows, and the story counters."""
    found = await rows(
        engine,
        "SELECT (SELECT count(*) FROM operations WHERE story_id = :s) AS ops, "
        "(SELECT count(*) FROM events WHERE story_id = :s) AS events, "
        "(SELECT count(*) FROM client_events WHERE story_id = :s) AS dedupe, "
        "(SELECT timeline_revision FROM stories WHERE id = :s) AS revision, "
        "(SELECT next_turn_index FROM stories WHERE id = :s) AS next_turn",
        s=story_id,
    )
    return tuple(found[0])


def code(response: httpx.Response) -> str:
    value: str = response.json()["error"]["code"]
    return value


async def test_body_validation(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Malformed bodies are 422 and write nothing; a kind M2 does not record is
    ``unsupported_event_kind`` naming M6."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        before = await footprint(engine, story)
        invalid = [
            [],
            [narration(f"n{i}") for i in range(51)],
            [narration("same"), narration("same", "Other.")],
            [narration("has space")],
            [narration("x" * 256)],
            [{"client_event_id": "a", "kind": "player_turn", "say": "No actor."}],
            [player("b", ids[PLAYER], say="   ")],
            [player("c", ids[PLAYER], text="Not here.")],
            [{**narration("d"), "actor_character_id": ids[PLAYER]}],
            [{"client_event_id": "e", "kind": "narration", "text": " \n "}],
            [{**narration("f"), "extra": 1}],
        ]
        for events in invalid:
            refused = await ingest(client, story, *events)
            assert refused.status_code == 422, (events, refused.text)
            assert code(refused) == "validation_error"
        for kind in ("outcome", "state_change"):
            refused = await ingest(client, story, {"client_event_id": "g", "kind": kind})
            assert refused.status_code == 422, refused.text
            assert code(refused) == "unsupported_event_kind"
            assert "M6" in refused.json()["error"]["message"]
            assert refused.json()["error"]["details"]["milestone"] == "M6"
        unknown = await ingest(client, story, {"client_event_id": "h", "kind": "dream"})
        assert code(unknown) == "unsupported_event_kind"
        assert await footprint(engine, story) == before


async def test_check_order(stub_settings: Settings, engine: AsyncEngine) -> None:
    """404 → unknown actor → replay → archived → expected revision; a replay answers even
    with a stale expected revision and on an archived story."""
    async with respond_client(stub_settings, engine) as (client, lib):
        missing = await ingest(client, "0" * 26, narration("a"))
        assert missing.status_code == 404
        story, _ = await new_story(client)
        stranger = await ingest(client, story, player("a", "0" * 26))
        assert stranger.status_code == 422
        assert stranger.json()["error"]["details"]["unknown_actor"] == {"events[0]": "0" * 26}

        first = await ingest(client, story, narration("a"))
        assert first.status_code == 201, first.text
        stale = {"X-Epistrel-Expected-Revision": "0"}
        replay = await ingest(client, story, narration("a"), headers=stale)
        assert replay.status_code == 200, replay.text
        conflict = await ingest(client, story, narration("b"), headers=stale)
        assert code(conflict) == "revision_conflict"

        await lib.archive_story(AUTHOR, story)
        assert (await ingest(client, story, narration("a"))).status_code == 200
        archived = await ingest(client, story, narration("c"))
        assert (archived.status_code, code(archived)) == (409, "story_archived")


async def test_batch_is_all_or_nothing(stub_settings: Settings, engine: AsyncEngine) -> None:
    """One event failing the input gate refuses the whole batch with nothing written (its
    locator names it); a clean batch records one turn per event, in order, each bumping the
    revision. Complement: the same events minus the violating one are all recorded. A batch with
    one conflicting ``client_event_id`` records none of its new events."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        await lib.policies_set_story(
            AUTHOR,
            story,
            [
                StoryPolicyItem.model_validate(
                    {
                        "name": unique("no-dragons"),
                        "rule": words("dragon"),
                        "applies_to": ["text_input"],
                        "message": "No dragons.",
                    }
                )
            ],
        )
        before = await footprint(engine, story)
        events = [
            player("p1", ids[PLAYER], "I light the lamp."),
            narration("n1", "Shadows move."),
            narration("n2", "A dragon lands in the yard."),
        ]
        refused = await ingest(client, story, *events)
        assert refused.status_code == 422, refused.text
        (violation,) = refused.json()["error"]["details"]["violations"]
        assert violation["locator"] == "events[2]"
        assert await footprint(engine, story) == before

        recorded = await ingest(client, story, *events[:2])
        assert recorded.status_code == 201, recorded.text
        results = recorded.json()["results"]
        assert [(r["client_event_id"], r["turn"]) for r in results] == [("p1", 1), ("n1", 2)]
        assert recorded.json()["timeline_revision"] == before[3] + 2
        ops = await rows(
            engine,
            "SELECT kind, origin, viewpoint, idempotency_key, status FROM operations "
            "WHERE story_id = :s AND origin = 'ingested' ORDER BY narrative_turn",
            s=story,
        )
        assert [(o.kind, o.status, o.viewpoint) for o in ops] == [("turn", "committed", None)] * 2
        written = await rows(
            engine,
            "SELECT type, beat, character_id, provenance, payload FROM events "
            "WHERE story_id = :s AND narrative_turn IS NOT NULL ORDER BY seq",
            s=story,
        )
        assert [(e.type, e.beat, e.character_id, e.provenance) for e in written] == [
            ("player_input", 1, ids[PLAYER], "consumer"),
            ("message_rendered", 1, None, "consumer"),
        ]
        assert written[1].payload == {
            "text": "Shadows move.",
            "render_character_ids": [],
            "model": None,
            "prompt_hash": None,
            "finish_reason": "consumer",
            "fallback_used": False,
        }

        # A batch whose second event reuses ``n1`` with a different payload is refused as a
        # whole: its new first event is not recorded either (#58 AC1; #286).
        settled = await footprint(engine, story)
        conflicting = await ingest(
            client, story, narration("new1", "One more."), narration("n1", "Different.")
        )
        assert conflicting.status_code == 409, conflicting.text
        assert code(conflicting) == "client_event_conflict"
        assert await footprint(engine, story) == settled


async def test_mixed_replay_and_new(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A batch of one replay and one new event is 200 with per-event ``replayed`` flags and no
    ``Idempotent-Replay`` header; only the new event is recorded. A replay is matched after
    NFC normalisation and trimming. The batch carries the request's idempotency key on its
    first operation only."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        first = await ingest(client, story, narration("a", "Café lights flicker."))
        original = first.json()["results"][0]
        # The same text, decomposed and padded: the same payload.
        mixed = await ingest(
            client,
            story,
            narration("a", "  Café lights flicker.\n"),
            player("b", ids[PLAYER]),
            player("c", ids[FIRST]),
            headers={"Idempotency-Key": "batch-1"},
        )
        assert mixed.status_code == 200, mixed.text
        assert "Idempotent-Replay" not in mixed.headers
        a, b, c = mixed.json()["results"]
        assert a == {**original, "replayed": True}
        assert (b["replayed"], b["turn"], c["replayed"], c["turn"]) == (False, 2, False, 3)
        assert mixed.json()["timeline_revision"] == first.json()["timeline_revision"] + 2
        keys = await rows(
            engine,
            "SELECT idempotency_key FROM operations WHERE story_id = :s AND origin = 'ingested' "
            "ORDER BY narrative_turn",
            s=story,
        )
        # The first ingest sent no key: the server generated one (#61), echoed in the header.
        assert [k.idempotency_key for k in keys] == [
            first.headers["Idempotency-Key"],
            "batch-1",
            None,
        ]
        assert (
            len(await rows(engine, "SELECT 1 FROM client_events WHERE story_id = :s", s=story)) == 3
        )


async def test_concurrent_duplicates_record_once(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Two identical ingests at once record one turn: one gets 201, the other the replay."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        for attempt in range(5):
            events = [narration(f"race-{attempt}"), player(f"race-p-{attempt}", ids[PLAYER])]
            one, two = await asyncio.gather(
                ingest(client, story, *events), ingest(client, story, *events)
            )
            assert sorted((one.status_code, two.status_code)) == [200, 201], (one.text, two.text)
            assert [r["operation_id"] for r in one.json()["results"]] == [
                r["operation_id"] for r in two.json()["results"]
            ]
        recorded = await rows(
            engine,
            "SELECT narrative_turn FROM operations WHERE story_id = :s AND origin = 'ingested' "
            "AND status = 'committed' ORDER BY narrative_turn",
            s=story,
        )
        assert [r.narrative_turn for r in recorded] == list(range(1, 11))


async def test_history_and_prompt_window(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Ingested events appear in ``history`` (origin ``ingested``) and in the next Lite
    prompt's verbatim window exactly like generated turns: the player turn as a ``user`` line,
    the narration verbatim as an ``assistant`` message. A projection rebuild reproduces them."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        await played(client, story, body(ids[PLAYER], "Anyone home?"))
        recorded = await ingest(
            client,
            story,
            player("p", ids[PLAYER], "I knock twice.", do="steps back"),
            narration("n", "The door creaks open by itself."),
        )
        assert recorded.status_code == 201, recorded.text
        history = (await client.get(f"/stories/{story}/history", headers=READ)).json()
        tail = [
            (i["turn"], i["beat"], i["kind"], i["channel"], i["origin"]) for i in history["items"]
        ][-3:]
        assert tail == [
            (2, 1, "player_input", "say", "ingested"),
            (2, 1, "player_input", "do", "ingested"),
            (3, 1, "message_rendered", "narration", "ingested"),
        ]
        assert history["items"][0]["origin"] == "generated"

        stub = lib.models.stub
        stub.reset()
        await played(client, story, body(ids[PLAYER], "Hello?"))
        (call,) = generation_calls(stub)
        window = [(m.role, m.content) for m in call.messages[1:-1]]
        assert window[-2:] == [
            ("user", f'{PLAYER}: "I knock twice." *steps back*'),
            ("assistant", "The door creaks open by itself."),
        ]

        transcript = "SELECT * FROM transcript_messages WHERE story_id = :s ORDER BY seq"
        live = await rows(engine, transcript, s=story)
        await lib.rebuild_projections(AUTHOR, story)
        assert await rows(engine, transcript, s=story) == live
        roles = {r.kind: r.role for r in live if r.operation_id in _ids(recorded)}
        assert roles == {"player_input": "player", "message_rendered": "narration"}


def _ids(response: httpx.Response) -> set[str]:
    return {r["operation_id"] for r in response.json()["results"]}


async def test_timeline_changed_abandons(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control action committed between the batch's begin and its commit makes the ingest
    409 ``timeline_changed``; its operations end ``abandoned`` and nothing is recorded.
    Complement: a truth change that is not a control action, committed in the same window,
    does not stop the ingest."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        await played(client, story, body(ids[PLAYER], "One."))
        await played(client, story, body(ids[PLAYER], "Two."))
        interloper: list[Any] = []

        class Interrupted(OperationBatch):
            async def begin(self) -> None:
                await super().begin()
                if interloper:
                    await interloper.pop()()

        monkeypatch.setattr(core_ingest, "OperationBatch", Interrupted)

        interloper.append(lambda: truncate(engine, story, 1))
        refused = await ingest(client, story, narration("t"))
        assert refused.status_code == 409, refused.text
        assert code(refused) == "timeline_changed"
        statuses = await rows(
            engine, "SELECT status FROM operations WHERE story_id = :s AND origin = 'ingested'",
            s=story,
        )  # fmt: skip
        assert [s.status for s in statuses] == ["abandoned"]
        assert await rows(engine, "SELECT 1 FROM client_events WHERE story_id = :s", s=story) == []

        # A turn cannot commit in that window (the ingest holds the turn slot, #63); a pins
        # edit moves the revision without a control action.
        interloper.append(lambda: set_pins(engine, story, "A truth change."))
        recorded = await ingest(client, story, narration("t"))
        assert recorded.status_code == 201, recorded.text
        assert recorded.json()["results"][0]["turn"] == 2


async def test_library_ingest(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``Engine.ingest`` records in process with only a principal, and replays the same way."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        request = IngestRequest.model_validate({"events": [player("lib", ids[FIRST])]})
        result = await lib.ingest(AUTHOR, story, request)
        assert (result.created, result.replayed, result.results[0].turn) == (True, False, 1)
        again = await lib.ingest(AUTHOR, story, request)
        assert (again.created, again.replayed) == (False, True)
        assert again.results[0].operation_id == result.results[0].operation_id


def test_timeline_only_decision() -> None:
    """Ingest's commit decision ignores a moved revision and abandons only after a control
    action."""
    moved = RevalidationState(
        attempt=1,
        base_revision=1,
        current_revision=3,
        control_since_base=False,
        control_operation_id=None,
        touched=("window",),
    )
    assert core_ingest._timeline_only(moved).name == "commit"
    control = RevalidationState(
        attempt=1,
        base_revision=1,
        current_revision=3,
        control_since_base=True,
        control_operation_id="op",
        touched=(),
    )
    assert core_ingest._timeline_only(control).name == "abandon"


async def test_operation_batch_guards(engine: AsyncEngine) -> None:
    """A batch needs operations, all of its own story; one over a story that does not exist
    fails at ``begin()`` (the operations row's foreign key)."""
    with pytest.raises(ValueError, match="at least one"):
        OperationBatch(engine, "s", [])
    other = Operation(engine, story_id="t", kind=OperationKind.TURN, principal="p")
    with pytest.raises(ValueError, match="belongs"):
        OperationBatch(engine, "s", [other])
    missing = "0" * 26
    ops = [Operation(engine, story_id=missing, kind=OperationKind.TURN, principal="p")]
    batch = OperationBatch(engine, missing, ops)
    with pytest.raises(IntegrityError):
        await batch.begin()
