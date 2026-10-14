"""A story's ad-hoc characters and their promotion to the catalog (#114): the details.

S-PROMOTE's M3 part (``test_s_promote``) is in ``tests/integration/test_personas.py``. These
tests cover the rest: the checks on a character added to a running story and their order, what
the addition records and where it shows, its input gate, the images it references, what a
truncation and a fork do with it, a promotion's refusals and options, the definition it cuts and
the images it leaves out, and the interim catalog draft fields a promotion fills.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ids import new_ulid
from epistrel.core.media.assets import referenced
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.stories import CAST_MAX
from tests.auth_support import auth_settings, bearer
from tests.character_support import complete
from tests.event_support import rows, scalar
from tests.media_support import mp4, png, uploaded
from tests.policy_support import story_item, words
from tests.respond_support import body as respond_body
from tests.respond_support import everything_sent, respond_client
from tests.story_support import character, create, story_body

AUTHOR = "p-author"
READ = bearer()
DRIVER: dict[str, Any] = {
    "name": "Dale",
    "definition": "A delivery driver in a hurry.",
    "true_age": 52,
}
STALE = {"X-Epistrel-Expected-Revision": "0"}
"""A story revision no story here is at once it exists (creation takes it to 1)."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def as_(
    principal: str = AUTHOR, key: str | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    found = {**bearer(), "X-Epistrel-Principal": principal, **(extra or {})}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


def error_of(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


async def add(
    client: httpx.AsyncClient,
    story_id: str,
    body: dict[str, Any],
    *,
    key: str | None = None,
    extra: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/characters", json=body, headers=as_(key=key, extra=extra)
    )


async def promote(
    client: httpx.AsyncClient,
    story_id: str,
    character_id: str,
    body: dict[str, Any] | None = None,
    *,
    principal: str = AUTHOR,
    key: str | None = None,
    extra: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/characters/{character_id}/promote",
        json=body if body is not None else {},
        headers=as_(principal, key, extra),
    )


async def story_counts(engine: AsyncEngine, story_id: str) -> tuple[int, ...]:
    """``(characters, operations, events, audit rows)`` of the story."""
    found = await rows(
        engine,
        "SELECT (SELECT count(*) FROM characters WHERE story_id = :s), "
        "(SELECT count(*) FROM operations WHERE story_id = :s), "
        "(SELECT count(*) FROM events WHERE story_id = :s), "
        "(SELECT count(*) FROM audit_log WHERE story_id = :s)",
        s=story_id,
    )
    return tuple(int(v) for v in found[0])


async def revision(client: httpx.AsyncClient, story_id: str) -> int:
    found = await client.get(f"/stories/{story_id}", headers=READ)
    assert found.status_code == 200, found.text
    return int(found.json()["timeline_revision"])


# -- adding a character --------------------------------------------------------------------------


async def test_refusals_come_in_order_and_write_nothing(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """An addition is refused, with nothing written, in this order: 404 for an unknown story;
    422 for its body (a missing true age, ``is_player``, a repeated image); 422 for a name the
    story has, ignoring case (``reason: duplicate``), an image no live asset has
    (``media_retired``), and a video (``reason: not_an_image``); 409 ``story_archived``; 409
    ``revision_conflict`` for a stale expected revision; 423 ``story_busy`` while a turn is in
    flight. A full cast (20 characters) is 422 naming the cap."""
    async with respond_client(stub_settings, engine) as (client, _):
        missing = await add(client, new_ulid(), DRIVER)
        assert error_of(missing)[:2] == (404, "not_found")
        story = await create(client, story_body("Wren", "Ada"))
        sid = story["story_id"]
        image = await uploaded(client, png(nonce=new_ulid()))
        video = await uploaded(
            client, mp4(), content_type="video/mp4", kind="video", poster=image["id"]
        )
        retired = await uploaded(client, png(nonce=new_ulid()))
        dropped = await client.delete(f"/media/{retired['id']}", headers=as_())
        assert dropped.status_code == 204, dropped.text
        before = await story_counts(engine, sid)
        for body, expected in (
            ({**DRIVER, "true_age": None}, (422, "validation_error", "body.true_age")),
            (
                {k: v for k, v in DRIVER.items() if k != "true_age"},
                (422, "validation_error", "body.true_age"),
            ),
            ({**DRIVER, "is_player": True}, (422, "validation_error", "body.is_player")),
            (
                {**DRIVER, "images": [image["id"], image["id"]]},
                (422, "validation_error", "body.images"),
            ),
            ({**DRIVER, "name": " ADA "}, (422, "validation_error", "body.name")),
            ({**DRIVER, "images": [new_ulid()]}, (422, "media_retired", "body.images")),
            ({**DRIVER, "images": [retired["id"]]}, (422, "media_retired", "body.images")),
            (
                {**DRIVER, "images": [image["id"], video["id"]]},
                (422, "validation_error", "body.images[1]"),
            ),
        ):
            status, code, details = error_of(await add(client, sid, body))
            assert (status, code, details["field"]) == expected, body
        stale = await add(client, sid, DRIVER, extra=STALE)
        assert error_of(stale)[:2] == (409, "revision_conflict")
        await client.post(f"/stories/{sid}/archive", headers=as_())
        archived = await add(client, sid, DRIVER)
        assert error_of(archived)[:2] == (409, "story_archived")
        await client.post(f"/stories/{sid}/unarchive", headers=as_())
        after_archive = await story_counts(engine, sid)
        async with engine.begin() as conn:
            busy_id = new_ulid()
            await conn.execute(
                text(
                    "INSERT INTO operations (operation_id, story_id, kind, principal, status) "
                    "VALUES (:o, :s, 'turn', 'p-player', 'in_flight')"
                ),
                {"o": busy_id, "s": sid},
            )
        busy = await add(client, sid, DRIVER)
        assert error_of(busy)[:2] == (423, "story_busy")
        assert error_of(busy)[2]["operation_id"] == busy_id
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM operations WHERE operation_id = :o"), {"o": busy_id}
            )
        assert await story_counts(engine, sid) == after_archive
        # The archive and unarchive wrote their own operations and events; nothing else did.
        assert after_archive[0] == before[0]
        assert after_archive[3] == before[3]

        full = await create(
            client,
            {"characters": [character(f"C{n}", 30, is_player=n == 0) for n in range(CAST_MAX)]},
        )
        capped = await add(client, full["story_id"], DRIVER)
        assert error_of(capped) == (
            422,
            "validation_error",
            {
                "field": "body",
                "cap": "cast_max",
                "limit": CAST_MAX,
                "actual": CAST_MAX + 1,
                "errors": error_of(capped)[2]["errors"],
            },
        )


async def test_an_addition_bumps_the_revision_and_joins_the_cast(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """An addition is one committed ``control`` operation by its requester that bumps the
    story's revision and records ``character_defined`` (the character's id and name, never its
    age or definition). The character joins the cast at the next ordinal, with its true age and
    safe fallback line, no catalog source and no persona; an omniscient read shows its true age
    and ``promoted_to`` (empty), a plain read neither; and the next turn's prompt carries its
    definition. The same request under its key is replayed and adds nothing more."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story = await create(client, story_body("Wren", "Ada"))
        sid, wren = story["story_id"], story["characters"][0]["id"]
        start = await revision(client, sid)
        key = new_ulid()
        body = {**DRIVER, "safe_fallback_text": "Dale has a van to move."}
        added = await add(client, sid, body, key=key)
        assert added.status_code == 201, added.text
        dale = added.json()
        assert (dale["name"], dale["ordinal"], dale["timeline_revision"]) == ("Dale", 2, start + 1)
        assert dale["idempotency_key"] == key
        replay = await add(client, sid, body, key=key)
        assert (replay.status_code, replay.headers.get("Idempotent-Replay")) == (201, "true")
        assert replay.json() == dale
        (row,) = await rows(
            engine,
            "SELECT ordinal, true_age, is_player, safe_fallback_text, source_catalog_id, "
            "persona_id, persona_snapshot, images, promoted_to FROM characters WHERE id = :c",
            c=dale["id"],
        )
        assert tuple(row) == (2, 52, False, "Dale has a van to move.", None, None, None, [], [])
        (event,) = await rows(
            engine,
            "SELECT e.type, e.category, e.payload, o.kind, o.principal, o.timeline_revision "
            "FROM events e JOIN operations o ON o.operation_id = e.operation_id "
            "WHERE o.idempotency_key = :k",
            k=key,
        )
        assert tuple(event) == (
            "character_defined",
            "control",
            {"character_id": dale["id"], "name": "Dale", "is_player": False},
            "control",
            AUTHOR,
            start + 1,
        )
        plain = (await client.get(f"/stories/{sid}", headers=READ)).json()["characters"]
        assert plain[2] == {"id": dale["id"], "name": "Dale", "is_player": False}
        omniscient = await client.get(
            f"/stories/{sid}", params={"viewpoint": "omniscient"}, headers=READ
        )
        shown = omniscient.json()["characters"][2]
        assert (shown["true_age"], shown["promoted_to"]) == (52, [])
        lib.models.stub.reset()
        played = await client.post(
            f"/stories/{sid}/respond", json=respond_body(wren), headers=as_()
        )
        assert played.status_code == 200, played.text
        assert DRIVER["definition"] in everything_sent(lib.models.stub)


async def test_the_definition_is_gated_with_the_new_age(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """The definition passes the story's input gate before anything is written: a violation is
    422 ``guardrail_violation`` at ``definition``, with an ``audit_log`` row whose entry point
    is ``story_character`` and nothing else. An age-conditioned policy is checked with the new
    character's age in the cast: the same definition is refused for a 12-year-old and accepted
    for an adult."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create(client, story_body("Wren", "Ada"))
        sid = story["story_id"]
        word = f"zq{new_ulid().lower()[:8]}"
        minors = f"mz{new_ulid().lower()[:8]}"
        policies = [
            json.loads(story_item(words(word), applies_to=["text_input"]).model_dump_json()),
            json.loads(
                story_item(
                    words(minors),
                    applies_to=["text_input"],
                    age_condition="minor_involved_or_present",
                ).model_dump_json()
            ),
        ]
        guarded = await client.patch(
            f"/stories/{sid}/settings", json={"policies": policies}, headers=as_()
        )
        assert guarded.status_code == 200, guarded.text
        before = await story_counts(engine, sid)
        refused = await add(client, sid, {**DRIVER, "definition": f"Says {word} a lot."})
        status, code, details = error_of(refused)
        assert (status, code) == (422, "guardrail_violation")
        assert [v["locator"] for v in details["violations"]] == ["definition"]
        (audit,) = await rows(
            engine,
            "SELECT details->>'entry_point' FROM audit_log WHERE story_id = :s "
            "AND kind = 'guardrail_input_rejected'",
            s=sid,
        )
        assert audit[0] == "story_character"
        child = await add(
            client, sid, {**DRIVER, "name": "Pip", "true_age": 12, "definition": f"Loves {minors}."}
        )
        assert error_of(child)[:2] == (422, "guardrail_violation")
        after = await story_counts(engine, sid)
        assert after[:3] == before[:3]
        adult = await add(client, sid, {**DRIVER, "name": "Pat", "definition": f"Loves {minors}."})
        assert adult.status_code == 201, adult.text


async def test_images_are_references_the_sweep_keeps(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """An added character's images are stored by reference, in order; an asset one of them
    names counts as referenced, so retiring it does not let the media sweep purge it while the
    story keeps it."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create(client, story_body("Wren", "Ada"))
        first = await uploaded(client, png(nonce=new_ulid()))
        second = await uploaded(client, png(nonce=new_ulid()))
        added = await add(
            client,
            story["story_id"],
            {**DRIVER, "images": [second["id"], first["id"]]},
        )
        assert added.status_code == 201, added.text
        stored = await scalar(
            engine, "SELECT images FROM characters WHERE id = :c", c=added.json()["id"]
        )
        assert stored == [second["id"], first["id"]]
        assert (await client.delete(f"/media/{first['id']}", headers=as_())).status_code == 204
        async with engine.connect() as conn:
            assert await referenced(conn, [first["id"], new_ulid()]) == {first["id"]}


async def test_an_added_character_stays_through_truncation_and_forks(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """An addition is a story-level edit with no turn position, as a pinned memory is: a
    truncation to turn 0 keeps the character, and a fork at turn 0 copies it (under a new
    id)."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create(client, story_body("Wren", "Ada"))
        sid, wren = story["story_id"], story["characters"][0]["id"]
        played = await client.post(
            f"/stories/{sid}/respond", json=respond_body(wren), headers=as_()
        )
        assert played.status_code == 200, played.text
        assert (await add(client, sid, DRIVER)).status_code == 201
        cut = await client.post(f"/stories/{sid}/truncate", json={"to_turn": 0}, headers=as_())
        assert cut.status_code == 200, cut.text
        names = [
            c["name"]
            for c in (await client.get(f"/stories/{sid}", headers=READ)).json()["characters"]
        ]
        assert names == ["Wren", "Ada", "Dale"]
        forked = await client.post(f"/stories/{sid}/fork", json={"at_turn": 0}, headers=as_())
        assert forked.status_code == 201, forked.text
        copied = (await client.get(f"/stories/{forked.json()['story_id']}", headers=READ)).json()
        assert [c["name"] for c in copied["characters"]] == ["Wren", "Ada", "Dale"]


# -- promotion -------------------------------------------------------------------------------------


async def test_promotion_refusals_and_options(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A promotion is 404 for an unknown story and for a character of another story, 422 for a
    player character (``reason: player_character``) and for a rating that cannot be assigned
    (``rating_retired``), and 409 for a stale expected revision, each with nothing written. It
    only reads the story, so it runs while a turn is in flight (the turn holds the story's turn
    slot, its generation held; the promotion is 201 at the unchanged revision, and the turn then
    commits), and on an archived story. Without an owner it is owned by its requester, not by
    the story's owner; it takes a name, an owner and a rating, records one story-less
    ``control`` operation by its requester, writes no story event, moves no revision, and is
    replayed under its key."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story = await create(client, story_body("Wren", "Ada"))
        sid, wren, ada = story["story_id"], *(c["id"] for c in story["characters"])
        other = await create(client, story_body("Kit", "Lee"))
        entries = await scalar(engine, "SELECT count(*) FROM catalog.character_catalog")
        for refused, expected in (
            (await promote(client, new_ulid(), ada), (404, "not_found")),
            (await promote(client, other["story_id"], ada), (404, "not_found")),
            (await promote(client, sid, wren), (422, "validation_error")),
            (
                await promote(client, sid, ada, {"content_rating_id": new_ulid()}),
                (422, "rating_retired"),
            ),
            (
                await promote(client, sid, ada, extra=STALE),
                (409, "revision_conflict"),
            ),
            (await promote(client, sid, ada, {"seed_memories": []}), (422, "validation_error")),
        ):
            assert error_of(refused)[:2] == expected, refused.text
        assert error_of(await promote(client, sid, wren))[2] == {
            "field": "path.character_id",
            "reason": "player_character",
            "errors": error_of(await promote(client, sid, wren))[2]["errors"],
        }
        assert await scalar(engine, "SELECT count(*) FROM catalog.character_catalog") == entries
        assert await scalar(engine, "SELECT promoted_to FROM characters WHERE id = :c", c=ada) == []

        # -- while a turn is in flight: the promotion runs; no owner named, so its requester's
        held = await revision(client, sid)
        latch = lib.models.stub.hold("generation")
        turn = asyncio.ensure_future(
            client.post(f"/stories/{sid}/respond", json=respond_body(wren), headers=as_())
        )
        try:
            await asyncio.wait_for(latch.entered.wait(), 10)
            (in_flight,) = await rows(
                engine,
                "SELECT s.in_flight_turn, o.kind, o.status FROM stories s "
                "JOIN operations o ON o.operation_id = s.in_flight_turn WHERE s.id = :s",
                s=sid,
            )
            assert (in_flight.kind, in_flight.status) == ("turn", "in_flight")
            during = await asyncio.wait_for(promote(client, sid, ada, principal="p-promoter"), 10)
        finally:
            latch.release()
        assert during.status_code == 201, during.text
        assert during.json()["timeline_revision"] == held
        assert (await turn).status_code == 200  # the turn commits after it, undisturbed
        assert await revision(client, sid) == held + 1  # the turn's bump; the promotion made none
        owned = await client.get(
            f"/catalog/characters/{during.json()['catalog_id']}",
            params={"draft": "true", "projection": "full"},
            headers=READ,
        )
        story_owner = await scalar(
            engine, "SELECT owner_principal FROM stories WHERE id = :s", s=sid
        )
        assert (owned.json()["owner_principal"], story_owner) == ("p-promoter", AUTHOR)

        start = await revision(client, sid)
        events = await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=sid)
        await client.post(f"/stories/{sid}/archive", headers=as_())
        key = new_ulid()
        body = {"name": "Ada the cook", "owner": "p-curator", "content_rating_id": EVERYONE_ID}
        done = await promote(client, sid, ada, body, principal="p-promoter", key=key)
        assert done.status_code == 201, done.text
        assert done.json()["timeline_revision"] == start  # archiving bumps no revision either
        replay = await promote(client, sid, ada, body, principal="p-promoter", key=key)
        assert (replay.json(), replay.headers.get("Idempotent-Replay")) == (done.json(), "true")
        entry = (
            await client.get(
                f"/catalog/characters/{done.json()['catalog_id']}",
                params={"draft": "true", "projection": "full"},
                headers=READ,
            )
        ).json()
        assert (entry["name"], entry["owner_principal"], entry["content_rating"]["id"]) == (
            "Ada the cook",
            "p-curator",
            EVERYONE_ID,
        )
        (operation,) = await rows(
            engine,
            "SELECT kind, principal, story_id, status FROM operations WHERE idempotency_key = :k",
            k=key,
        )
        assert tuple(operation) == ("control", "p-promoter", None, "committed")
        assert await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=sid) == (
            events + 1  # the archive's own event; the promotion wrote none
        )
        assert await revision(client, sid) == start
        promoted = await scalar(engine, "SELECT promoted_to FROM characters WHERE id = :c", c=ada)
        assert promoted == [during.json()["catalog_id"], done.json()["catalog_id"]]


async def test_promotion_cuts_the_definition_and_leaves_out_retired_images(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A definition longer than ``background_flavor`` holds is cut at 4,000 characters, with the
    warning ``definition_truncated``; an image that is no longer live is left out, with a
    ``media_retired`` warning naming it, while the live ones keep their order (the first is the
    primary image). A character created with the story promotes like an added one."""
    async with respond_client(stub_settings, engine) as (client, _):
        long = "The cook remembers every recipe. " * 150
        story = await create(
            client,
            {"characters": [character("Wren", is_player=True), character("Ada", definition=long)]},
        )
        sid, ada = story["story_id"], story["characters"][1]["id"]
        cut = await promote(client, sid, ada)
        assert cut.status_code == 201, cut.text
        (warning,) = cut.json()["warnings"]
        assert (warning["code"], warning["outcome"], warning["location"]["field"]) == (
            "definition_truncated",
            "kept",
            "definition.background_flavor",
        )
        draft = await scalar(
            engine,
            "SELECT draft FROM catalog.character_catalog WHERE id = :c",
            c=cut.json()["catalog_id"],
        )
        assert draft["definition"]["background_flavor"] == long[:4000]
        assert draft["definition"]["true_age"] == 30

        first, second, third = [await uploaded(client, png(nonce=new_ulid())) for _ in range(3)]
        added = await add(
            client, sid, {**DRIVER, "images": [first["id"], second["id"], third["id"]]}
        )
        assert added.status_code == 201, added.text
        assert (await client.delete(f"/media/{first['id']}", headers=as_())).status_code == 204
        left_out = await promote(client, sid, added.json()["id"])
        assert left_out.status_code == 201, left_out.text
        (warning,) = left_out.json()["warnings"]
        assert (warning["code"], warning["outcome"], warning["source"]) == (
            "media_retired",
            "dropped",
            first["id"],
        )
        draft = await scalar(
            engine,
            "SELECT draft FROM catalog.character_catalog WHERE id = :c",
            c=left_out.json()["catalog_id"],
        )
        assert draft["public_card"] == {
            "name": DRIVER["name"],
            "images": {"primary": second["id"], "gallery": [third["id"]]},
        }
        assert await scalar(
            engine, "SELECT images FROM characters WHERE id = :c", c=added.json()["id"]
        ) == [first["id"], second["id"], third["id"]]


# -- the catalog draft's promoted fields -----------------------------------------------------------


async def test_catalog_drafts_hold_the_promoted_fields(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A catalog character's draft takes the fields a promotion fills, at #115's paths: the
    definition's ``true_age``, ``background_flavor`` and ``safe_fallback_text``, the card's
    images (a primary image and a gallery) and the content rating. A save checks what it adds:
    an image that is not live is 422 ``media_retired`` and a rating that cannot be assigned 422
    ``rating_retired``, at their fields. The card shows the primary image and the gallery, each
    with its alt text, and the rating, never a definition field; ``full`` adds the definition; a
    publish records the rating's version, and the version's card shows it."""
    async with respond_client(stub_settings, engine) as (client, _):
        image = await uploaded(client, png(nonce=new_ulid()), alt_text="A lantern")
        extra = await uploaded(client, png(nonce=new_ulid()))
        retired = await uploaded(client, png(nonce=new_ulid()))
        await client.delete(f"/media/{retired['id']}", headers=as_())
        body = complete(
            "Mara Venn",
            images={"primary": image["id"]},
            definition_changes={"background_flavor": "Raised on Gull Rock."},
        )
        created = await client.post("/catalog/characters", json=body, headers=as_())
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        primary = created.json()["images"]["primary"]
        assert (primary["media_id"], primary["alt_text"]) == (image["id"], "A lantern")
        assert created.json()["content_rating"]["id"] == EVERYONE_ID
        assert created.json()["definition"]["true_age"] == 41
        for patch, expected in (
            (
                {"public_card": {"images": {"gallery": [retired["id"]]}}},
                (422, "media_retired", "body.public_card.images.gallery[0]"),
            ),
            (
                {"metadata": {"content_rating_id": new_ulid()}},
                (422, "rating_retired", "body.metadata.content_rating_id"),
            ),
            (
                {"definition": {"true_age": 151}},
                (422, "validation_error", "body.definition.true_age"),
            ),
            (
                {"public_card": {"images": {"gallery": [extra["id"], extra["id"]]}}},
                (422, "validation_error", "body.public_card.images.gallery"),
            ),
        ):
            refused = await client.patch(
                f"/catalog/characters/{cid}", json={"draft_revision": 1, **patch}, headers=as_()
            )
            status, code, details = error_of(refused)
            assert (status, code, details["field"]) == expected, patch
        saved = await client.patch(
            f"/catalog/characters/{cid}",
            json={"draft_revision": 1, "public_card": {"images": {"gallery": [extra["id"]]}}},
            headers=as_(),
        )
        assert saved.status_code == 200, saved.text
        card = await client.get(
            f"/catalog/characters/{cid}", params={"draft": "true"}, headers=READ
        )
        assert card.json()["images"] == {
            "primary": {"media_id": image["id"], "alt_text": "A lantern"},
            "gallery": [{"media_id": extra["id"], "alt_text": None}],
        }
        assert "definition" not in card.json()
        published = await client.post(
            f"/catalog/characters/{cid}/publish", json={"draft_revision": 2}, headers=as_()
        )
        assert published.status_code == 201, published.text
        recorded = await scalar(
            engine,
            "SELECT content_rating_version_id FROM catalog.character_versions "
            "WHERE catalog_id = :c AND version = 1",
            c=cid,
        )
        shown = (await client.get(f"/catalog/characters/{cid}", headers=READ)).json()
        assert shown["content_rating"]["version_id"] == recorded is not None
