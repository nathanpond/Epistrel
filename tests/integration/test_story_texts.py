"""Pinned memories and custom instructions (#60) beyond I-18's slice: caps, the text and id
rules, the order of refusals, no-op and reorder edits, the recorded ``previous`` value, the
prompt sections, the rebuild, and the library surface."""

from __future__ import annotations

import unicodedata
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.deployment import read_story_text_caps, update_story_text_caps
from epistrel.core.story_texts import CustomInstructionsRequest, PinnedMemoriesRequest
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.respond_support import (
    AUTHOR,
    PLAYER,
    body,
    generation_calls,
    new_story,
    played,
    respond_client,
    system_of,
)
from tests.story_support import READ, WRITE

OPERATOR = "p-operator"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _pins(story: str) -> str:
    return f"/stories/{story}/pinned-memories"


def _instructions(story: str) -> str:
    return f"/stories/{story}/custom-instructions"


async def _put_pins(
    client: httpx.AsyncClient, story: str, *items: Any, headers: dict[str, str] | None = None
) -> httpx.Response:
    entries = [{"text": i} if isinstance(i, str) else i for i in items]
    return await client.put(
        _pins(story), json={"items": entries}, headers={**WRITE, **(headers or {})}
    )


async def _state(engine: AsyncEngine, story: str) -> tuple[Any, ...]:
    """The story's revision, its event count and its ``story_texts`` row."""
    return (
        await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story),
        await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story),
        await scalar(
            engine, "SELECT to_jsonb(t)::text FROM story_texts t WHERE story_id = :s", s=story
        ),
    )


def _error(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


async def test_caps_refuse_with_nothing_changed(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """51 pins → 422 with ``details.{cap, limit, actual}`` and the set unchanged; the same for
    instructions over 4,000 characters. Complement: exactly at the caps the edit is stored."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await new_story(client)
        assert (await _put_pins(client, story, "kept")).status_code == 200
        before = await _state(engine, story)

        refused = await _put_pins(client, story, *(f"pin {i}" for i in range(51)))
        assert refused.status_code == 422
        assert _error(refused)["code"] == "validation_error"
        details = _error(refused)["details"]
        assert (details["cap"], details["limit"], details["actual"]) == (
            "max_pinned_memories",
            50,
            51,
        )
        long = await client.put(_instructions(story), json={"text": "x" * 4_001}, headers=WRITE)
        assert long.status_code == 422
        assert _error(long)["details"]["cap"] == "max_custom_instructions_chars"
        assert await _state(engine, story) == before

        assert (await _put_pins(client, story, *(f"pin {i}" for i in range(50)))).status_code == 200
        at_cap = await client.put(_instructions(story), json={"text": "y" * 4_000}, headers=WRITE)
        assert at_cap.status_code == 200, at_cap.text


async def test_caps_are_deployment_settings(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A lowered cap applies to the next edit and leaves stored items alone; caps are bounded
    1 to 500 and 1 to 20,000, and each change is one prospective ``config_changes`` row."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await new_story(client)
        assert (await _put_pins(client, story, "a", "b", "c")).status_code == 200
        before = await read_story_text_caps(engine)
        await update_story_text_caps(engine, principal=OPERATOR, max_pinned_memories=2)
        try:
            assert len((await client.get(_pins(story), headers=READ)).json()["items"]) == 3
            refused = await _put_pins(client, story, "a", "b", "d")
            assert _error(refused)["details"]["limit"] == 2
            assert (await _put_pins(client, story, "a", "b")).status_code == 200
            changes = await rows(
                engine,
                "SELECT setting, old, new, class FROM config_changes "
                "WHERE setting = 'max_pinned_memories' ORDER BY config_revision DESC LIMIT 1",
            )
            assert tuple(changes[0]) == ("max_pinned_memories", 50, 2, "prospective")
        finally:
            await update_story_text_caps(
                engine,
                principal=OPERATOR,
                max_pinned_memories=before.max_pinned_memories,
                max_custom_instructions_chars=before.max_custom_instructions_chars,
            )
        for bad in ({"max_pinned_memories": 0}, {"max_custom_instructions_chars": 20_001}):
            with pytest.raises(ValidationError):
                await update_story_text_caps(engine, principal=OPERATOR, **bad)


async def test_ids_texts_and_duplicates(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Ids are minted by the server and kept: by naming them, or by an id-less item with the
    same text. Unknown or repeated ids, repeated texts (equal after NFC and trimming), blank
    and over-long items are 422 at their ``items[n]`` locator, with nothing changed."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await new_story(client)
        first = (await _put_pins(client, story, "  the well is dry  ", "the bell")).json()
        well, bell = first["items"]
        assert well["text"] == "the well is dry"
        assert len(well["id"]) == 26

        renamed = await _put_pins(
            client, story, {"id": well["id"], "text": "the well is full"}, "the bell", "new"
        )
        assert renamed.status_code == 200, renamed.text
        items = renamed.json()["items"]
        assert [i["id"] for i in items[:2]] == [well["id"], bell["id"]]
        assert items[2]["id"] not in {well["id"], bell["id"]}

        before = await _state(engine, story)
        composed = "café"
        decomposed = unicodedata.normalize("NFD", composed)
        cases: list[tuple[list[Any], str]] = [
            ([{"id": "0" * 26, "text": "x"}], "body.items[0].id"),
            (
                [{"id": well["id"], "text": "x"}, {"id": well["id"], "text": "y"}],
                "body.items[1].id",
            ),
            ([composed, f" {decomposed} "], "body.items[1].text"),
            (["   "], "body.items[0].text"),
            (["z" * 2_001], "body.items[0].text"),
        ]
        for items_in, field in cases:
            refused = await _put_pins(client, story, *items_in)
            assert refused.status_code == 422, (items_in, refused.text)
            assert _error(refused)["code"] == "validation_error"
            assert _error(refused)["details"]["field"] == field
        assert await _state(engine, story) == before
        assert (await _put_pins(client, story, "z" * 2_000)).status_code == 200


async def test_refusal_order(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Unknown story 404 → cap 422 → archived 409 → stale expected revision 409."""
    async with respond_client(stub_settings, engine) as (client, _):
        assert (await _put_pins(client, "0" * 26, "x")).status_code == 404
        assert (await client.get(_instructions("0" * 26), headers=READ)).status_code == 404
        story, _ = await new_story(client)
        stale = {"X-Epistrel-Expected-Revision": "0"}
        conflict = await _put_pins(client, story, "x", headers=stale)
        assert conflict.status_code == 409
        assert _error(conflict)["code"] == "revision_conflict"
        archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
        assert archived.status_code == 200
        too_many = await _put_pins(client, story, *(f"p{i}" for i in range(51)), headers=stale)
        assert too_many.status_code == 422
        refused = await _put_pins(client, story, "x", headers=stale)
        assert refused.status_code == 409
        assert _error(refused)["code"] == "story_archived"


async def test_edits_record_previous_and_reach_the_prompt(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Each edit records the full ordered value and the one it replaced; a reorder is an edit
    (revision +1, the new order in the next prompt), a repeat is not (nothing written), and
    ``""`` clears the instructions, which never-set stories read as ``""``. Instructions sit in
    the prompt's instructions section, pins in the pinned section, in order."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        empty = (await client.get(_instructions(story), headers=READ)).json()
        assert empty["text"] == ""
        assert (await client.get(_pins(story), headers=READ)).json()["items"] == []

        first = (await _put_pins(client, story, "PIN ONE", "PIN TWO")).json()
        swapped = await _put_pins(client, story, "PIN TWO", "PIN ONE")
        assert swapped.json()["timeline_revision"] == first["timeline_revision"] + 1
        assert [i["id"] for i in swapped.json()["items"]] == [
            first["items"][1]["id"],
            first["items"][0]["id"],
        ]
        before = await _state(engine, story)
        repeat = await _put_pins(client, story, "PIN TWO", "PIN ONE")
        assert repeat.json()["operation_id"] is None
        assert await _state(engine, story) == before

        set_once = await client.put(
            _instructions(story), json={"text": " Be brief. "}, headers=WRITE
        )
        assert set_once.json()["text"] == "Be brief."
        lib.models.stub.reset()
        await played(client, story, body(ids[PLAYER]))
        system = system_of(generation_calls(lib.models.stub)[0])
        assert system.index("Be brief.") < system.index("PIN TWO") < system.index("PIN ONE")

        cleared = await client.put(_instructions(story), json={"text": ""}, headers=WRITE)
        assert cleared.status_code == 200
        assert cleared.json()["timeline_revision"] == set_once.json()["timeline_revision"] + 2
        recorded = await rows(
            engine,
            "SELECT e.type, e.payload, e.category, o.kind, o.principal FROM events e "
            "JOIN operations o USING (operation_id) WHERE e.story_id = :s AND e.type IN "
            "('pinned_memories_set', 'custom_instructions_set') ORDER BY e.seq",
            s=story,
        )
        assert [(r.category, r.kind, r.principal) for r in recorded] == [
            ("control", "control", "p-author")
        ] * 4
        assert recorded[1].payload["previous"] == first["items"]
        assert recorded[3].payload == {"text": "", "previous": "Be brief."}
        lib.models.stub.reset()
        await played(client, story, body(ids[PLAYER]))
        assert "Be brief." not in system_of(generation_calls(lib.models.stub)[0])


async def test_rebuild_and_library(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The ``story_texts`` row is a projection: dropped and rebuilt, the reads are identical.
    The library edits with only a principal and stores the idempotency key on the operation."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _ = await new_story(client)
        keyed = await _put_pins(client, story, "one", "two", headers={"Idempotency-Key": "k-60"})
        assert keyed.status_code == 200
        key = await scalar(
            engine,
            "SELECT idempotency_key FROM operations WHERE operation_id = :o",
            o=keyed.json()["operation_id"],
        )
        assert key == "k-60"
        written = await lib.set_custom_instructions(
            AUTHOR, story, CustomInstructionsRequest(text="Library text.")
        )
        assert written.operation_id is not None
        pinned = await lib.set_pinned_memories(
            AUTHOR, story, PinnedMemoriesRequest.model_validate({"items": [{"text": "three"}]})
        )
        assert [i.text for i in pinned.items] == ["three"]

        before = (
            (await client.get(_pins(story), headers=READ)).json(),
            (await client.get(_instructions(story), headers=READ)).json(),
        )
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM story_texts WHERE story_id = :s"), {"s": story})
        rebuilt = await client.post(
            f"/admin/stories/{story}/rebuild", headers={**WRITE, "X-Epistrel-Principal": OPERATOR}
        )
        assert rebuilt.status_code == 200, rebuilt.text
        after = (
            (await client.get(_pins(story), headers=READ)).json(),
            (await client.get(_instructions(story), headers=READ)).json(),
        )
        assert after == before
