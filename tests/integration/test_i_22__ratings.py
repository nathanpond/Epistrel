"""I-22 (M2 half, core resolver): a story's rating — the deployment default, a retired rating
refused for new stories while its stories keep running, and every operation recording the
rating version it ran under. The HTTP clauses that need ``POST/GET /stories`` are
``test_i_22__stories`` (#50); the in-flight re-check lands with #64."""

from __future__ import annotations

import asyncio

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
from tests.event_support import play_turn, rows, scalar
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
