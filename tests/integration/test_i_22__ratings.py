"""I-22 (M2 half, core resolver): a story's rating — the deployment default, a retired rating
refused for new stories while its stories keep running, and every operation recording the
rating version it ran under. The HTTP clauses that need ``POST/GET /stories`` are
``test_i_22__stories`` (#50); the in-flight re-check is ``test_i_22__in_flight`` (#64)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.concurrency import LiteReadSet, touched_members
from epistrel.core.config_changes import read_settings
from epistrel.core.context import RequestContext
from epistrel.core.errors import PrincipalRequired, RatingRetired
from epistrel.core.history import read_history
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import (
    RatingFields,
    RatingReplacement,
    RatingWrite,
    create_rating,
    get_rating,
    list_ratings,
    retire_rating,
    story_guidance,
    update_rating,
)
from epistrel.core.seeds.ratings import DEFAULT_RATING_ID, TEEN_ID
from epistrel.core.stories import create_story
from epistrel.engine import Engine
from epistrel.orchestrator.policies.schemas import PolicyFields
from tests.auth_support import auth_settings
from tests.event_support import play_turn, rows, scalar
from tests.respond_support import (
    FIRST,
    PLAYER,
    body,
    generation_calls,
    new_story,
    played,
    respond,
    respond_client,
    system_of,
)
from tests.story_support import READ, WRITE, count, create, rest, story_body

OPERATOR = "p-operator"


async def _rating_version_of(engine: AsyncEngine, operation_id: str) -> str:
    return str(
        await scalar(
            engine,
            "SELECT config_revisions->>'rating_version' FROM operations WHERE operation_id = :o",
            o=operation_id,
        )
    )


@pytest.mark.part("M2", "FR-RATE-1,4")
async def test_i_22__ratings(engine: AsyncEngine) -> None:
    """A story created without a rating gets the deployment default; an edit makes a new
    version that the next operation records and the guidance follows, while the earlier
    operation keeps the version it ran under; a retired rating is hidden from listings and
    refused for a new story, while its existing story still reads and still plays."""
    default = (await read_settings(engine)).default_content_rating_id
    blank = await create_story(engine, principal="p-test")
    assert await scalar(engine, "SELECT content_rating_id FROM stories WHERE id = :s", s=blank) == (
        default
    )
    default_version = (await get_rating(engine, default)).rating.version_id
    first = await play_turn(engine, blank, "hello")
    assert await _rating_version_of(engine, first.operation_id) == default_version

    name = f"I22 {new_ulid()}"
    created = await create_rating(
        engine,
        RatingFields(
            name=name, prompt_guidance="before the edit", inherits_from=[DEFAULT_RATING_ID]
        ),
        principal=OPERATOR,
    )
    rated = await create_story(engine, principal="p-test", content_rating_id=created.rating.id)
    before = await play_turn(engine, rated, "under version 1")
    assert await _rating_version_of(engine, before.operation_id) == created.rating.version_id

    edited = await update_rating(
        engine,
        created.rating.id,
        RatingReplacement(
            name=name,
            description="",
            display_order=created.rating.display_order,
            prompt_guidance="after the edit",
            image_guidance="",
            policy_ids=[],
            inherits_from=[DEFAULT_RATING_ID],
        ),
        principal=OPERATOR,
    )
    assert edited.rating.version == 2
    assert (await get_rating(engine, created.rating.id)).rating.version_id == (
        edited.rating.version_id
    )
    after = await play_turn(engine, rated, "under version 2")
    assert await _rating_version_of(engine, after.operation_id) == edited.rating.version_id
    assert await _rating_version_of(engine, before.operation_id) == created.rating.version_id
    assert (await story_guidance(engine, rated)).prompt_guidance == "after the edit"

    await retire_rating(engine, created.rating.id, principal=OPERATOR)
    assert created.rating.id not in {r.id for r in await list_ratings(engine)}
    assert created.rating.id in {r.id for r in await list_ratings(engine, include_retired=True)}
    with pytest.raises(RatingRetired) as refused:
        await create_story(engine, principal="p-test", content_rating_id=created.rating.id)
    assert refused.value.details["reason"] == "retired"

    still = await play_turn(engine, rated, "after retirement")
    assert await _rating_version_of(engine, still.operation_id) == edited.rating.version_id
    history = await read_history(engine, rated)
    assert [item.text for item in history.items if item.kind == "player_input"] == [
        "under version 1",
        "under version 2",
        "after retirement",
    ]
    assert (await rows(engine, "SELECT content_rating_id FROM stories WHERE id = :s", s=rated))[0][
        0
    ] == created.rating.id


async def test_unknown_rating_refused_for_a_new_story(engine: AsyncEngine) -> None:
    with pytest.raises(RatingRetired) as refused:
        await create_story(engine, principal="p-test", content_rating_id="0" * 26)
    assert refused.value.details == {
        "rating_id": "0" * 26,
        "field": "content_rating_id",
        "reason": "unknown",
    }
    assert (
        await scalar(
            engine, "SELECT count(*) FROM stories WHERE content_rating_id = :r", r="0" * 26
        )
        == 0
    )


async def test_guidance_edit_touches_the_read_set(engine: AsyncEngine) -> None:
    """The Lite read set's ``guidance_text`` member is the story rating's own prompt guidance:
    editing it touches that member only (complement: an unrelated rating's edit touches
    nothing)."""
    rating = await create_rating(
        engine, RatingFields(name=f"RS {new_ulid()}", prompt_guidance="one"), principal=OPERATOR
    )
    other = await create_rating(engine, RatingFields(name=f"RS {new_ulid()}"), principal=OPERATOR)
    story = await create_story(engine, principal="p-test", content_rating_id=rating.rating.id)

    async def hashes() -> dict[str, str]:
        async with engine.connect() as conn:
            return await LiteReadSet().hashes(conn, story)

    base = await hashes()

    def replacement(write: RatingWrite, guidance: str) -> RatingReplacement:
        r = write.rating
        return RatingReplacement(
            name=r.name,
            description=r.description,
            display_order=r.display_order,
            prompt_guidance=guidance,
            image_guidance=r.image_guidance,
            policy_ids=list(r.policy_ids),
            inherits_from=list(r.inherits_from),
        )

    await update_rating(engine, other.rating.id, replacement(other, "x"), principal=OPERATOR)
    assert touched_members(base, await hashes()) == ()
    await update_rating(engine, rating.rating.id, replacement(rating, "two"), principal=OPERATOR)
    assert touched_members(base, await hashes()) == ("guidance_text",)


async def test_concurrent_writes_serialise(engine: AsyncEngine) -> None:
    """Concurrent rating writes each move the deployment revision by exactly one."""
    start = (await read_settings(engine)).config_revision
    writes = await asyncio.gather(
        *(
            create_rating(engine, RatingFields(name=f"C {new_ulid()}"), principal=OPERATOR)
            for _ in range(4)
        )
    )
    assert sorted(w.config_revision for w in writes) == [start + n for n in range(1, 5)]
    assert (await read_settings(engine)).config_revision == start + 4


async def test_library_writes_need_a_principal(engine: AsyncEngine, settings: Settings) -> None:
    library = Engine(settings, db=engine)
    with pytest.raises(PrincipalRequired):
        await library.create_content_rating(RequestContext(), RatingFields(name="no principal"))
    with pytest.raises(PrincipalRequired):
        await library.retire_content_rating(RequestContext(), DEFAULT_RATING_ID)
    listed = await library.list_content_ratings(RequestContext())
    assert DEFAULT_RATING_ID in {r.id for r in listed}


@pytest.mark.part("M2", "FR-RATE-1")
async def test_i_22__stories(engine: AsyncEngine, settings: Settings) -> None:
    """A blank story without a rating gets the deployment default; a named rating is used; a
    retired rating is refused for a new story (422 ``rating_retired``) and hidden from the
    listing, while the story already using it still answers ``GET`` and plays a turn."""
    async with rest(settings, engine) as client:
        default_id = (await client.get("/admin/settings", headers=READ)).json()[
            "default_content_rating_id"
        ]
        blank = await create(client, story_body("Ada"))
        assert blank["content_rating"]["id"] == default_id
        stored = (await client.get(f"/stories/{blank['story_id']}", headers=READ)).json()
        assert stored["content_rating"] == blank["content_rating"]

        teen = await create(client, story_body("Ada", content_rating_id=TEEN_ID))
        assert teen["content_rating"]["id"] == TEEN_ID

        made = await client.post(
            "/admin/content-ratings", json={"name": f"custom {new_ulid()}"}, headers=WRITE
        )
        assert made.status_code == 201, made.text
        rating = made.json()
        rated = await create(client, story_body("Ada", content_rating_id=rating["id"]))
        assert rated["content_rating"] == {
            "id": rating["id"],
            "version_id": rating["version_id"],
            "name": rating["name"],
        }
        retired = await client.delete(f"/admin/content-ratings/{rating['id']}", headers=WRITE)
        assert retired.status_code == 200, retired.text

        stories_before = await count(engine, "SELECT count(*) FROM stories")
        refused = await client.post(
            "/stories", json=story_body("Ada", content_rating_id=rating["id"]), headers=WRITE
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "rating_retired"
        assert refused.json()["error"]["details"]["reason"] == "retired"
        assert await count(engine, "SELECT count(*) FROM stories") == stories_before
        listed = (await client.get("/admin/content-ratings", headers=READ)).json()["items"]
        assert rating["id"] not in {r["id"] for r in listed}

        # The story already rated with it keeps running.
        still = await client.get(f"/stories/{rated['story_id']}", headers=READ)
        assert still.status_code == 200
        assert still.json()["content_rating"]["id"] == rating["id"]
        await play_turn(engine, rated["story_id"], "still here?")
        after = (await client.get(f"/stories/{rated['story_id']}", headers=READ)).json()
        assert after["timeline_revision"] == 2


@pytest.mark.part("M2", "FR-RATE-4")
async def test_i_22__prompt(pg_url: str, engine: AsyncEngine) -> None:
    """The story rating's current prompt guidance is the first section of the ``system``
    message of every Lite generation, at no extra model call; editing the guidance changes the
    next prompt (#57).

    Complement asserted: the replaced guidance is gone from the next prompt."""
    settings = auth_settings(pg_url, model_adapter="stub")
    name = f"I22P {new_ulid()}"
    created = await create_rating(
        engine,
        RatingFields(name=name, prompt_guidance="GUIDE-ONE: keep it gentle."),
        principal=OPERATOR,
    )
    async with respond_client(settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST, content_rating_id=created.rating.id)
        stub.reset()
        await played(client, story, body(ids[PLAYER]))
        (call,) = stub.calls
        assert call.purpose == "generation"
        assert system_of(call).startswith("## Content rating guidance\nGUIDE-ONE: keep it gentle.")

        await update_rating(
            engine,
            created.rating.id,
            RatingReplacement(
                name=name,
                description="",
                display_order=created.rating.display_order,
                prompt_guidance="GUIDE-TWO: be brisk.",
                image_guidance="",
                policy_ids=[],
                inherits_from=[],
            ),
            principal=OPERATOR,
        )
        stub.reset()
        await played(client, story, body(ids[PLAYER], "And now?"))
        (call,) = stub.calls
        system = system_of(call)
        assert system.startswith("## Content rating guidance\nGUIDE-TWO: be brisk.")
        assert "GUIDE-ONE" not in system
    await retire_rating(engine, created.rating.id, principal=OPERATOR)


async def _turn_row(engine: AsyncEngine, story_id: str) -> Any:
    found = await rows(
        engine,
        "SELECT operation_id, attempts, config_revisions FROM operations WHERE story_id = :s "
        "AND kind = 'turn' AND status = 'committed' ORDER BY committed_at DESC LIMIT 1",
        s=story_id,
    )
    return found[0]


@pytest.mark.part("M2", "FR-CONC-15")
async def test_i_22__in_flight(pg_url: str, engine: AsyncEngine) -> None:
    """Editing a rating's guardrails applies to every operation that commits after the edit:
    a turn in flight is re-checked under the new guardrails at commit, and its reply that now
    breaks one is regenerated before anything commits; the turn records the rating version
    and the configuration it committed under (#64, FR-CONC-16).

    Complements asserted: an edit whose new guardrails the reply passes re-checks once and
    commits that reply without a second generation (the output gate, #68, checks every reply
    under the guardrails in force when its generation started; the re-check covers the rest);
    a prospective change mid-turn (a story's ``turn_deadline``) re-checks nothing and the turn
    commits as generated; the committed history is not re-checked by a later edit."""
    settings = auth_settings(pg_url, model_adapter="stub")
    name = f"I22F {new_ulid()}"
    rating = (await create_rating(engine, RatingFields(name=name), principal=OPERATOR)).rating

    async def guardrails(*policy_ids: str) -> RatingWrite:
        return await update_rating(
            engine,
            rating.id,
            RatingReplacement(
                name=name,
                description="",
                display_order=rating.display_order,
                prompt_guidance="",
                image_guidance="",
                policy_ids=list(policy_ids),
                inherits_from=[],
            ),
            principal=OPERATOR,
        )

    async with respond_client(settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST, content_rating_id=rating.id)
        dragons = (
            await lib.policies_create(
                RequestContext(principal=OPERATOR),
                PolicyFields.model_validate(
                    {
                        "scope": "rating",
                        "scope_id": rating.id,
                        "name": f"no-dragons-{new_ulid().lower()}",
                        "rule": {"type": "wordlist", "words": ["dragon"]},
                        "message": "No dragons here.",
                    }
                ),
            )
        ).policy

        async def held_turn(*replies: str, during: Callable[[], Awaitable[Any]]) -> Any:
            stub.reset()
            stub.script("generation", replies)
            latch = stub.hold("generation")
            task = asyncio.ensure_future(respond(client, story, body(ids[PLAYER])))
            await asyncio.wait_for(latch.entered.wait(), 5)
            changed = await during()
            latch.release()
            response = await task
            assert response.status_code == 200, response.text
            return response.json(), changed

        # The guardrail lands while the dragon reply is generating: re-checked, regenerated.
        turn, edit = await held_turn(
            "A dragon lands on the roof.", "A cat purrs.", during=lambda: guardrails(dragons.id)
        )
        assert turn["message"]["text"] == "A cat purrs."
        assert len(generation_calls(stub)) == 2
        row = await _turn_row(engine, story)
        assert [(a["outcome"], a.get("recheck")) for a in row.attempts] == [
            ("recheck", "failed"),
            ("commit", None),
        ]
        assert {"rating_guardrail_set", "policy_set"} <= set(row.attempts[0]["config_changed"])
        assert row.config_revisions["rating_version"] == edit.rating.version_id
        assert row.config_revisions["deployment"] >= edit.config_revision
        replies = await rows(
            engine,
            "SELECT payload->>'text' FROM events WHERE story_id = :s AND type = 'message_rendered'",
            s=story,
        )
        assert [r[0] for r in replies] == ["A cat purrs."]  # the refused draft never committed

        # Complement: an edit mid-turn whose new guardrails the reply passes: re-checked once,
        # committed as is. (The turn starts with no guardrails: since #68 a reply that breaks the
        # guardrails in force when the turn started is caught by the output gate first.)
        cats = (
            await lib.policies_create(
                RequestContext(principal=OPERATOR),
                PolicyFields.model_validate(
                    {
                        "scope": "rating",
                        "scope_id": rating.id,
                        "name": f"no-cats-{new_ulid().lower()}",
                        "rule": {"type": "wordlist", "words": ["cat"]},
                        "message": "No cats here.",
                    }
                ),
            )
        ).policy
        await guardrails()
        turn, relaxed = await held_turn("A dragon naps.", during=lambda: guardrails(cats.id))
        assert turn["message"]["text"] == "A dragon naps."
        assert len(generation_calls(stub)) == 1
        row = await _turn_row(engine, story)
        assert [(a["outcome"], a.get("recheck")) for a in row.attempts] == [
            ("recheck", "passed"),
            ("commit", None),
        ]
        assert row.config_revisions["rating_version"] == relaxed.rating.version_id

        # Complement: a prospective change mid-turn re-checks nothing.
        before = row.config_revisions["story"]

        async def deadline() -> Any:
            return await client.patch(
                f"/stories/{story}/settings", json={"turn_deadline": 40}, headers=WRITE
            )

        turn, patched = await held_turn("Rain on the window.", during=deadline)
        assert patched.status_code == 200, patched.text
        assert len(generation_calls(stub)) == 1
        row = await _turn_row(engine, story)
        assert [a["outcome"] for a in row.attempts] == ["commit"]
        assert row.config_revisions["story"] == before
        assert patched.json()["config_revision"] == before + 1

        # Complement: committed history is not re-checked by a later edit.
        await guardrails(dragons.id)
        history = await client.get(f"/stories/{story}/history", headers=READ)
        assert "A dragon naps." in history.text
    await retire_rating(engine, rating.id, principal=OPERATOR)
