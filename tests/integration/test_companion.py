"""Companion scenarios (#116): S-COMPANION's M3 part and the detailed tests.

- ``test_s_companion`` (S-COMPANION's M3 part): the one-to-one scenario every catalog
  character is created with, its fixed slots, its edits, its versions pinned to the character's,
  and its archiving with the character, catalog side. Its story-creation clauses (a story from a
  companion is a one-to-one chat; another scenario casting the character brings none of the
  companion's world) are #119's ``test_i_22__creation``.

I-20's M3 part, the publishing rules (alone, together, atomic, concurrent, rating difference),
is ``test_i_20__companion`` in ``tests/integration/test_i_20.py``, beside #111's slice, because
traceability counts a test ID per milestone in one file. The detailed tests below name no
requirement ID, so they are not parts. The rating write-through needs no policy (the ids
differ); the rating difference that warns is I-20's, on a scratch database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine, RequestContext
from epistrel.config import Settings
from epistrel.core.catalog import companion as companions
from epistrel.core.catalog.characters import CharacterPublishRequest
from epistrel.core.catalog.companion_slots import defaults, pin_of
from epistrel.core.errors import CharacterNotFound, InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.seeds.ratings import EVERYONE_ID, TEEN_ID
from epistrel.core.stories import STORY_TABLES
from epistrel.core.tables import stories
from tests import scenario_support as scenarios
from tests.catalog_support import BASE, character, headers, read_companion
from tests.event_support import rows, scalar
from tests.story_support import create as create_story
from tests.story_support import rest, story_body
from tests.validation_support import entries

CHARACTERS = BASE
SCENARIOS = scenarios.BASE
READ = scenarios.READ
WRITE = headers()
REPLAY = "Idempotent-Replay"
SECRET = "Linda had an affair, known only to her."
WORLD = {"description": "A terraced house on a wet street in Leeds."}
OPENING = {"opening": {"narration": "Linda pours two cups of tea and sits down."}}

Snapshot = dict[str, list[str]]


@pytest.fixture
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with rest(settings, engine) as found:
        yield found


# -- helpers ------------------------------------------------------------------------------------


async def _ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


async def _character(
    client: httpx.AsyncClient, name: str, *, owner: str | None = None, **fields: Any
) -> dict[str, Any]:
    """A complete catalog character (it publishes as it is), created: the ``full`` draft."""
    body = character(name, **fields)
    if owner is not None:
        body["owner"] = owner
    return await _ok(await client.post(CHARACTERS, json=body, headers=WRITE), 201)


async def _publish(
    client: httpx.AsyncClient,
    character_id: str,
    revision: int,
    *,
    companion: int | None = None,
    principal: str = "p-catalog",
    key: str | None = None,
) -> httpx.Response:
    """``POST /catalog/characters/{id}/publish``; with ``companion``, its companion's draft
    (``with_companion=true``) at that revision too."""
    body: dict[str, Any] = {"draft_revision": revision}
    params: dict[str, str] = {}
    if companion is not None:
        body["companion_draft_revision"] = companion
        params["with_companion"] = "true"
    return await client.post(
        f"{CHARACTERS}/{character_id}/publish",
        json=body,
        params=params,
        headers=headers(key, principal),
    )


async def _save(
    client: httpx.AsyncClient,
    scenario_id: str,
    revision: int,
    *,
    principal: str = "p-catalog",
    **groups: Any,
) -> httpx.Response:
    return await client.patch(
        f"{SCENARIOS}/{scenario_id}",
        json={"draft_revision": revision, **groups},
        headers=headers(principal=principal),
    )


async def _save_character(
    client: httpx.AsyncClient, character_id: str, revision: int, **groups: Any
) -> dict[str, Any]:
    response = await client.patch(
        f"{CHARACTERS}/{character_id}", json={"draft_revision": revision, **groups}, headers=WRITE
    )
    return await _ok(response)


async def _versions(engine: AsyncEngine, scenario_id: str) -> list[tuple[Any, ...]]:
    """Each companion version: ``(version, pinned_character_id, pinned_character_version, the
    pin its content holds)``, oldest first."""
    found = await rows(
        engine,
        "SELECT version, pinned_character_id, pinned_character_version, content "
        "FROM catalog.scenario_versions WHERE catalog_id = :s ORDER BY version",
        s=scenario_id,
    )
    return [
        (r.version, r.pinned_character_id, r.pinned_character_version, pin_of(r.content))
        for r in found
    ]


async def _character_versions(engine: AsyncEngine, character_id: str) -> int:
    count = await scalar(
        engine,
        "SELECT count(*) FROM catalog.character_versions WHERE catalog_id = :c",
        c=character_id,
    )
    return int(count)


def _slots(document: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    """Each role's slot fields as a ``full`` read shows them."""
    return [
        (role["id"], role["kind"], role["fill"], role["character"])
        for role in document["cast"]["roles"]
    ]


def _rows_sql(table: Table) -> str:
    column = "id" if table.name == "stories" else "story_id"
    return f'SELECT row_to_json(t)::text FROM "{table.name}" t WHERE "{column}" = :s'  # noqa: S608


async def _snapshot(engine: AsyncEngine, story_id: str) -> Snapshot:
    """Every row of the story (per story table, and its ``stories`` row)."""
    found: Snapshot = {}
    async with engine.connect() as conn:
        for table in (*STORY_TABLES, stories):
            found[table.name] = sorted(await conn.scalars(text(_rows_sql(table)), {"s": story_id}))
    return found


# -- S-COMPANION (M3) -----------------------------------------------------------------------------


@pytest.mark.part("M3", "FR-CAT-5,10")
async def test_s_companion(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """Creating catalog character Linda creates her companion scenario: one NPC slot locked to
    her and one player slot, owned and listed as she is. Publishing her (version 1) publishes
    the companion's first version, pinned to it. Adding a slot, or reassigning the locked one,
    is refused (422 ``companion_slots_fixed``) and changes nothing. Another principal edits the
    companion's world description and opening, seeds Linda's default world ("had an affair,
    known only to her") and publishes it alone: a version pinned to Linda's version 1.
    Publishing Linda's version 2 makes a companion version pinned to version 2 with the previous
    companion content, the seed included. Archiving Linda archives her companion in the same
    write, and a story made from Linda stays byte-identical.

    Complements asserted: the refused slot changes left the draft and its revision as they were;
    the companion's own unarchive is refused; the story snapshot does see the story's own
    archive."""
    linda = await _character(client, "Linda Okafor", owner="p-author")
    draft = await read_companion(client, linda["id"])
    sid = draft["id"]
    assert linda["companion_id"] == sid
    assert (draft["companion_of"], draft["owner_principal"], draft["status"]) == (
        linda["id"],
        "p-author",
        "draft",
    )
    assert draft["listing_status"] == linda["listing_status"]
    assert _slots(draft) == [
        ("npc", "npc", "locked", {"catalog_id": linda["id"], "version": None}),
        ("player", "player", "open", None),
    ]

    first = await _ok(await _publish(client, linda["id"], 1), 201)
    assert (first["version"], first["companion"]["id"], first["companion"]["version"]) == (
        1,
        sid,
        1,
    )
    assert await _versions(engine, sid) == [(1, linda["id"], 1, 1)]

    draft = await read_companion(client, linda["id"])
    revision = draft["draft_revision"]
    roles = draft["cast"]["roles"]
    other = await _character(client, "Ruth Okafor")
    reassigned = [{**roles[0], "character": {"catalog_id": other["id"]}}, roles[1]]
    for changed, slot in (
        ([*roles, scenarios.role("sister")], ("sister", "body.cast.roles[2].id")),
        (reassigned, ("npc", "body.cast.roles[0].character")),
    ):
        refused = await _save(client, sid, revision, cast={"roles": changed})
        status, code, details = _error(refused)
        assert (status, code, details["role"], details["field"]) == (
            422,
            "companion_slots_fixed",
            *slot,
        )
    unchanged = await read_companion(client, linda["id"])
    assert (unchanged["draft_revision"], unchanged["cast"]) == (revision, draft["cast"])

    seed = scenarios.seed(SECRET, holder="npc")
    edited = await _ok(
        await _save(
            client,
            sid,
            revision,
            principal="p-editor",
            world=WORLD,
            starting_state={**OPENING, "seeded_memories": [seed]},
        )
    )
    assert edited["world"]["description"] == WORLD["description"]
    alone = await _ok(
        await client.post(
            f"{SCENARIOS}/{sid}/publish",
            json={"draft_revision": edited["draft_revision"]},
            headers=headers(principal="p-editor"),
        ),
        201,
    )
    assert alone["version"] == 2
    assert await _versions(engine, sid) == [(1, linda["id"], 1, 1), (2, linda["id"], 1, 1)]

    saved = await _save_character(
        client,
        linda["id"],
        first["draft_revision"],
        public_card={"public_description": "Retired, and restless with it."},
    )
    second = await _ok(await _publish(client, linda["id"], saved["draft_revision"]), 201)
    assert (second["version"], second["companion"]["version"]) == (2, 3)
    assert await _versions(engine, sid) == [
        (1, linda["id"], 1, 1),
        (2, linda["id"], 1, 1),
        (3, linda["id"], 2, 2),
    ]
    copied = await _ok(
        await client.get(f"{SCENARIOS}/{sid}", params={"projection": "full"}, headers=READ)
    )
    assert (copied["version"], copied["pinned_character_version"]) == (3, 2)
    assert [s["text"] for s in copied["starting_state"]["seeded_memories"]] == [SECRET]
    assert copied["world"]["description"] == WORLD["description"]

    story = await create_story(client, story_body("Wren", "Linda"))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE characters SET source_catalog_id = :x, source_catalog_version = 2 "
                "WHERE id = :c"
            ),
            {"x": linda["id"], "c": story["characters"][1]["id"]},
        )
    before = await _snapshot(engine, story["story_id"])
    archived = await _ok(await client.post(f"{CHARACTERS}/{linda['id']}/archive", headers=WRITE))
    assert (archived["status"], archived["companion_id"]) == ("archived", sid)
    shown = await _ok(await client.get(f"{SCENARIOS}/{sid}", headers=READ))
    assert shown["status"] == "archived"
    refused_alone = await client.post(f"{SCENARIOS}/{sid}/unarchive", headers=WRITE)
    assert _error(refused_alone)[:2] == (422, "companion_follows_character")
    assert await _snapshot(engine, story["story_id"]) == before
    archived_story = await client.post(f"/stories/{story['story_id']}/archive", headers=WRITE)
    assert archived_story.status_code == 200, archived_story.text
    assert await _snapshot(engine, story["story_id"]) != before


# -- creation and reads ---------------------------------------------------------------------------


async def test_a_companion_is_created_with_its_character(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A character's creation creates its companion in the same transaction: the defaults (the
    character's name and content rating, two roles, no world, a starting cast of 2), its
    character's owner and listing status, status ``draft`` at draft revision 1, no version, its
    rating not set on it. The character's answers and ``full`` reads name it
    (``companion_id``); its card does not. A character created without a name gets a companion
    whose locked role is labelled ``Companion``, and no rating. One control operation records the
    creation, and the companion's draft is indexed under the character its locked role names. A
    scenario's creation cannot name ``companion_of`` (422 at ``body.companion_of``)."""
    created = await _character(client, "Mara Venn", owner="p-owner")
    sid = created["companion_id"]
    (row,) = await rows(
        engine,
        "SELECT owner_principal, listing_status, status, current_version, draft_revision, "
        "companion_of, content_rating_explicit, draft FROM catalog.scenario_catalog "
        "WHERE id = :s",
        s=sid,
    )
    assert tuple(row)[:7] == (
        "p-owner",
        created["listing_status"],
        "draft",
        None,
        1,
        created["id"],
        False,
    )
    assert row.draft == defaults(created["id"], name="Mara Venn", rating_id=EVERYONE_ID, pin=None)
    assert row.draft["cast"]["max_characters_at_start"] == 2
    assert "description" not in row.draft["world"]
    assert await _versions(engine, sid) == []
    full = await _ok(
        await client.get(
            f"{CHARACTERS}/{created['id']}",
            params={"draft": "true", "projection": "full"},
            headers=READ,
        )
    )
    assert full["companion_id"] == sid
    card = await _ok(
        await client.get(f"{CHARACTERS}/{created['id']}", params={"draft": "true"}, headers=READ)
    )
    assert "companion_id" not in card
    operations = await scalar(
        engine,
        "SELECT count(*) FROM operations WHERE idempotency_key = :k",
        k=created["idempotency_key"],
    )
    assert operations == 1
    indexed = await rows(
        engine,
        "SELECT field, ref_kind FROM subject_references "
        "WHERE record_table = 'scenario_catalog' AND record_id = :s AND subject_id = :c",
        s=sid,
        c=created["id"],
    )
    assert ("cast.roles[0].character.catalog_id", "declared") in {tuple(r) for r in indexed}

    planted = await client.post(
        SCENARIOS,
        json={"public_card": {"name": "Not a companion"}, "companion_of": created["id"]},
        headers=WRITE,
    )
    assert _error(planted)[:2] == (422, "validation_error")
    assert _error(planted)[2]["field"] == "body.companion_of"

    nameless = await _ok(
        await client.post(CHARACTERS, json={"definition": {"true_age": 30}}, headers=WRITE), 201
    )
    unnamed = await read_companion(client, nameless["id"])
    assert unnamed["public_card"]["name"] is None
    assert [r["public_label"] for r in unnamed["cast"]["roles"]] == ["Companion", "You"]
    assert unnamed["metadata"]["content_rating_id"] is None


async def test_the_companion_reads_through_its_character(
    client: httpx.AsyncClient, engine: AsyncEngine, settings: Settings
) -> None:
    """``GET /catalog/characters/{id}/companion`` reads the companion as a scenario read does:
    its draft while it was never published (with its ``draft_revision``), then its latest
    version; ``version``, ``draft`` and ``projection`` pass through. ``full`` shows
    ``companion_of`` and ``pinned_character_version`` (``null`` before the character's first
    publish), and the locked role's ``character.version`` holds the same. An unknown character,
    or an unknown version, is 404; ``version`` with ``draft`` is 422. The library answers the
    same."""
    created = await _character(client, "Ivo Brand")
    url = f"{CHARACTERS}/{created['id']}/companion"
    never = await _ok(await client.get(url, headers=READ))
    assert (never["id"], never["version"], never["companion_of"]) == (
        created["companion_id"],
        None,
        created["id"],
    )
    full = await _ok(await client.get(url, params={"projection": "full"}, headers=READ))
    assert (full["draft_revision"], full["pinned_character_version"]) == (1, None)
    assert full["cast"]["roles"][0]["character"] == {"catalog_id": created["id"], "version": None}

    await _ok(await _publish(client, created["id"], 1), 201)
    latest = await _ok(await client.get(url, params={"projection": "full"}, headers=READ))
    assert (latest["version"], latest["pinned_character_version"]) == (1, 1)
    assert "draft_revision" not in latest
    assert latest["cast"]["roles"][0]["character"] == {"catalog_id": created["id"], "version": 1}
    first = await _ok(await client.get(url, params={"version": "1"}, headers=READ))
    assert (first["version"], frozenset(first)) == (1, scenarios.CARD_KEYS)
    drafted = await _ok(
        await client.get(url, params={"draft": "true", "projection": "full"}, headers=READ)
    )
    assert (drafted["draft_revision"], drafted["pinned_character_version"]) == (2, 1)

    unknown = await client.get(f"{CHARACTERS}/{new_ulid()}/companion", headers=READ)
    assert _error(unknown)[:2] == (404, "not_found")
    assert "character_id" in _error(unknown)[2]
    missing = await client.get(url, params={"version": "9"}, headers=READ)
    assert _error(missing)[:2] == (404, "not_found")
    both = await client.get(url, params={"version": "1", "draft": "true"}, headers=READ)
    assert _error(both)[:2] == (422, "validation_error")

    lib = Engine(settings, db=engine)
    try:
        shown = await lib.get_companion(RequestContext(), created["id"])
        assert shown.model_dump(mode="json") == await _ok(await client.get(url, headers=READ))
        with pytest.raises(CharacterNotFound):
            await lib.get_companion(RequestContext(), new_ulid())
    finally:
        await lib.models.aclose()


# -- the slots ------------------------------------------------------------------------------------


async def test_a_companion_keeps_its_slots(client: httpx.AsyncClient) -> None:
    """A save that adds, removes, renames or repeats a role, or changes a role's ``kind``,
    ``fill`` or ``character`` (its id, or its pin), is 422 ``companion_slots_fixed`` naming the
    role and the field, before any other rule (a slot change beside an invalid field is still
    ``companion_slots_fixed``), and changes nothing. Every other field is editable: the slots
    echoed unchanged (the pin ``null`` before the character's first publish, then its version)
    with new labels, setup descriptions, participation and seeds; the roles reordered; the other
    groups."""
    created = await _character(client, "Nell Hart")
    draft = await read_companion(client, created["id"])
    sid, revision = draft["id"], draft["draft_revision"]
    npc, player = draft["cast"]["roles"]
    unpinned = {k: v for k, v in npc.items() if k != "character"}
    refusals: list[tuple[Any, str | None, str]] = [
        ([npc], "player", "body.cast.roles"),
        ([npc, player, scenarios.role("cook")], "cook", "body.cast.roles[2].id"),
        ([{**npc, "id": "linda"}, player], "linda", "body.cast.roles[0].id"),
        ([npc, npc], "npc", "body.cast.roles[1].id"),
        ([{**npc, "kind": "player"}, player], "npc", "body.cast.roles[0].kind"),
        ([{**npc, "fill": "default"}, player], "npc", "body.cast.roles[0].fill"),
        ([npc, {**player, "fill": "locked"}], "player", "body.cast.roles[1].fill"),
        ([{**npc, "character": None}, player], "npc", "body.cast.roles[0].character"),
        ([unpinned, player], "npc", "body.cast.roles[0].character"),
        (
            [{**npc, "character": {"catalog_id": created["id"], "version": 1}}, player],
            "npc",
            "body.cast.roles[0].character",
        ),
        (
            [{**npc, "fill": "default", "public_label": ""}, player],
            "npc",
            "body.cast.roles[0].fill",
        ),
        ([{"id": 7}, npc, player], None, "body.cast.roles[0].id"),
    ]
    for roles, role, field in refusals:
        refused = await _save(client, sid, revision, cast={"roles": roles})
        status, code, details = _error(refused)
        assert (status, code) == (422, "companion_slots_fixed"), roles
        assert (details["role"], details["field"], details["character_id"]) == (
            role,
            field,
            created["id"],
        ), roles
    removed = await _save(client, sid, revision, cast=None)
    assert _error(removed)[1:] == (
        "companion_slots_fixed",
        {
            "scenario_id": sid,
            "character_id": created["id"],
            "role": "npc",
            "field": "body.cast.roles",
        },
    )
    assert (await read_companion(client, created["id"]))["draft_revision"] == revision

    renamed = [
        {**npc, "public_label": "Nell", "participation": "silent", "required": False},
        {**player, "setup_description": "You call on Nell after a long week."},
    ]
    kept = await _ok(await _save(client, sid, revision, cast={"roles": renamed}))
    assert [r["public_label"] for r in kept["cast"]["roles"]] == ["Nell", "You"]
    reordered = await _ok(
        await _save(client, sid, kept["draft_revision"], cast={"roles": renamed[::-1]})
    )
    assert [r["id"] for r in reordered["cast"]["roles"]] == ["player", "npc"]
    other_groups = await _ok(
        await _save(
            client,
            sid,
            reordered["draft_revision"],
            metadata={"labels": ["Quiet"]},
            world=WORLD,
            starting_state=OPENING,
        )
    )
    assert other_groups["metadata"]["labels"] == ["quiet"]

    published = await _ok(await _publish(client, created["id"], 1), 201)
    assert published["companion"]["version"] == 1
    draft = await read_companion(client, created["id"])
    player, npc = draft["cast"]["roles"]
    assert npc["character"] == {"catalog_id": created["id"], "version": 1}
    stale_pin = await _save(
        client,
        sid,
        draft["draft_revision"],
        cast={"roles": [player, {**npc, "character": {"catalog_id": created["id"]}}]},
    )
    assert _error(stale_pin)[1] == "companion_slots_fixed"
    echoed = await _save(client, sid, draft["draft_revision"], cast={"roles": [player, npc]})
    assert echoed.status_code == 200, echoed.text


async def test_the_slots_check_refuses_a_planted_draft(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A companion draft whose slots were changed some other way than a save (here SQL drops the
    player's slot) fails the validation's companion check: the dry run reports
    ``companion_slots_fixed`` at ``cast.roles``, and publishing the draft, alone or with the
    character, is 422 ``publish_invalid`` with it (naming the companion), writing nothing. A
    character-alone publish copies the last published content, which keeps its slots, and
    publishes."""
    created = await _character(client, "Otto Ring")
    sid = created["companion_id"]
    await _ok(await _publish(client, created["id"], 1), 201)
    draft = await read_companion(client, created["id"])
    saved = await _ok(
        await _save(client, sid, draft["draft_revision"], world=WORLD, starting_state=OPENING)
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE catalog.scenario_catalog SET draft = draft #- '{cast,roles,1}' "
                "WHERE id = :s"
            ),
            {"s": sid},
        )
    checked = await _ok(await client.post(f"{SCENARIOS}/{sid}/validate", headers=WRITE))
    found = entries(checked["report"], "companion_slots_fixed")
    assert [(e["location"]["field"], e["location"]["role"], e["outcome"]) for e in found] == [
        ("cast.roles", "player", "rejected")
    ]
    assert checked["valid"] is False
    alone = await client.post(
        f"{SCENARIOS}/{sid}/publish",
        json={"draft_revision": saved["draft_revision"]},
        headers=WRITE,
    )
    assert _error(alone)[:2] == (422, "publish_invalid")
    together = await _publish(client, created["id"], 2, companion=saved["draft_revision"])
    status, code, details = _error(together)
    assert (status, code, details["entity"], details["scenario_id"]) == (
        422,
        "publish_invalid",
        "companion",
        sid,
    )
    assert [e["code"] for e in details["report"]["errors"]] == ["companion_slots_fixed"]
    assert len(await _versions(engine, sid)) == 1
    assert await _character_versions(engine, created["id"]) == 1
    copied = await _ok(await _publish(client, created["id"], 2), 201)
    assert copied["companion"]["version"] == 2


# -- following the character ---------------------------------------------------------------------


async def test_the_companion_takes_its_characters_rating_until_set(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A companion starts with its character's content rating. While its rating was never set on
    it, each character save that changes the character's rating gives the companion's draft the
    same rating, at its next draft revision (a removed rating is removed); a save that leaves
    the rating alone changes nothing on the companion, and neither does a companion save of
    other metadata. A companion save that names its rating, even to the value it has, sets it
    explicitly: from then on the character's changes leave it alone."""
    created = await _character(client, "Ora Pell")
    sid, cid = created["companion_id"], created["id"]

    async def companion() -> tuple[str | None, int]:
        draft = await read_companion(client, cid)
        return draft["metadata"]["content_rating_id"], draft["draft_revision"]

    explicit = "SELECT content_rating_explicit FROM catalog.scenario_catalog WHERE id = :s"
    assert await companion() == (EVERYONE_ID, 1)
    saved = await _save_character(client, cid, 1, metadata={"content_rating_id": TEEN_ID})
    assert await companion() == (TEEN_ID, 2)
    saved = await _save_character(
        client, cid, saved["draft_revision"], public_card={"public_description": "Later."}
    )
    assert await companion() == (TEEN_ID, 2)
    labelled = await _ok(await _save(client, sid, 2, metadata={"labels": ["calm"]}))
    assert await scalar(engine, explicit, s=sid) is False
    saved = await _save_character(
        client, cid, saved["draft_revision"], metadata={"content_rating_id": None}
    )
    assert await companion() == (None, labelled["draft_revision"] + 1)
    saved = await _save_character(
        client, cid, saved["draft_revision"], metadata={"content_rating_id": EVERYONE_ID}
    )
    rating, revision = await companion()
    assert rating == EVERYONE_ID

    same = await _ok(
        await _save(client, sid, revision, metadata={"content_rating_id": EVERYONE_ID})
    )
    assert await scalar(engine, explicit, s=sid) is True
    saved = await _save_character(
        client, cid, saved["draft_revision"], metadata={"content_rating_id": TEEN_ID}
    )
    assert await companion() == (EVERYONE_ID, same["draft_revision"])


async def test_archive_unarchive_and_delete_go_with_the_character(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Archiving a character archives its companion in the same write, and unarchiving it
    restores the companion (``published`` when it has a version, ``draft`` otherwise); the
    archive and unarchive answers name the companion. The companion's own archive, unarchive and
    delete are 422 ``companion_follows_character`` and change nothing, and while its character is
    archived it cannot be published alone (422 ``entry_archived``). Deleting the character
    deletes the companion and its versions in the same transaction: each gets its own
    ``deleted`` ownership event and ``catalog_entry_deleted`` audit row, both rows naming the one
    operation, and the companion's subject-reference rows go."""
    published = await _character(client, "Pia Lund")
    drafted = await _character(client, "Quin Hale")
    pid, psid = published["id"], published["companion_id"]
    await _ok(await _publish(client, pid, 1), 201)
    status = "SELECT status FROM catalog.scenario_catalog WHERE id = :s"
    for character_id, scenario_id, restored in (
        (pid, psid, "published"),
        (drafted["id"], drafted["companion_id"], "draft"),
    ):
        archived = await _ok(
            await client.post(f"{CHARACTERS}/{character_id}/archive", headers=WRITE)
        )
        assert archived["companion_id"] == scenario_id
        assert await scalar(engine, status, s=scenario_id) == "archived"
        for action in ("archive", "unarchive"):
            refused = await client.post(f"{SCENARIOS}/{scenario_id}/{action}", headers=WRITE)
            assert _error(refused) == (
                422,
                "companion_follows_character",
                {"entity_type": "scenario", "entity_id": scenario_id, "character_id": character_id},
            )
        await _ok(await client.post(f"{CHARACTERS}/{character_id}/unarchive", headers=WRITE))
        assert await scalar(engine, status, s=scenario_id) == restored
    refused = await client.delete(f"{SCENARIOS}/{psid}", headers=WRITE)
    assert _error(refused)[:2] == (422, "companion_follows_character")
    assert len(await _versions(engine, psid)) == 1
    await _ok(await client.post(f"{CHARACTERS}/{pid}/archive", headers=WRITE))
    draft = await read_companion(client, pid)
    shelved = await client.post(
        f"{SCENARIOS}/{psid}/publish",
        json={"draft_revision": draft["draft_revision"]},
        headers=WRITE,
    )
    assert _error(shelved)[:2] == (422, "entry_archived")
    await _ok(await client.post(f"{CHARACTERS}/{pid}/unarchive", headers=WRITE))

    key = f"delete-{pid}"
    deleted = await client.delete(f"{CHARACTERS}/{pid}", headers=headers(key))
    assert deleted.status_code == 204, deleted.text
    assert (
        await scalar(engine, "SELECT count(*) FROM catalog.scenario_catalog WHERE id = :s", s=psid)
        == 0
    )
    assert await _versions(engine, psid) == []
    assert await _character_versions(engine, pid) == 0
    (operation,) = await rows(
        engine, "SELECT operation_id FROM operations WHERE idempotency_key = :k", k=key
    )
    audits = await rows(
        engine,
        "SELECT details->>'entity_type' AS entity_type, details->>'entity_id' AS entity_id, "
        "details->>'operation_id' AS operation_id FROM audit_log "
        "WHERE kind = 'catalog_entry_deleted' AND details->>'entity_id' IN (:c, :s) "
        "ORDER BY 1",
        c=pid,
        s=psid,
    )
    assert [tuple(a) for a in audits] == [
        ("character", pid, operation.operation_id),
        ("scenario", psid, operation.operation_id),
    ]
    events = await rows(
        engine,
        "SELECT entity_type, entity_id FROM ownership_events WHERE kind = 'deleted' "
        "AND entity_id IN (:c, :s) ORDER BY entity_type",
        c=pid,
        s=psid,
    )
    assert [tuple(e) for e in events] == [("character", pid), ("scenario", psid)]
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM subject_references WHERE record_id = :s OR record_id LIKE :v",
            s=psid,
            v=f"{psid}@%",
        )
        == 0
    )


# -- publishing -----------------------------------------------------------------------------------


async def test_a_character_publish_names_the_side_it_refuses(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """``with_companion=true`` needs ``companion_draft_revision``, and the revision is refused
    without it (422 at ``body.companion_draft_revision``, before the ``Idempotency-Key`` is
    claimed: a used key answers the 422, not a conflict). A stale character revision is 409
    ``draft_stale`` naming the character (``details.entity``), a stale companion revision 409
    naming the companion, and an archived companion (only SQL can archive it alone) 422
    ``entry_archived`` naming it; a character draft that cannot be published is 422
    ``publish_invalid`` naming the character. Nothing is written."""
    created = await _character(client, "Ruth Vale")
    cid, sid = created["id"], created["companion_id"]
    key = f"publish-{cid}"
    for params, body in (
        ({"with_companion": "true"}, {"draft_revision": 1}),
        ({}, {"draft_revision": 1, "companion_draft_revision": 1}),
    ):
        refused = await client.post(
            f"{CHARACTERS}/{cid}/publish", json=body, params=params, headers=headers(key)
        )
        status, code, details = _error(refused)
        assert (status, code, details["field"]) == (
            422,
            "validation_error",
            "body.companion_draft_revision",
        )
    stale = await _publish(client, cid, 9)
    assert _error(stale)[:2] == (409, "draft_stale")
    assert (_error(stale)[2]["entity"], _error(stale)[2]["character_id"]) == ("character", cid)
    stale_companion = await _publish(client, cid, 1, companion=9)
    status, code, details = _error(stale_companion)
    assert (status, code, details["entity"], details["scenario_id"]) == (
        409,
        "draft_stale",
        "companion",
        sid,
    )
    assert details["current_draft_revision"] == 1
    async with engine.begin() as conn:  # out of step with its character: only SQL can do that
        await conn.execute(
            text("UPDATE catalog.scenario_catalog SET status = 'archived' WHERE id = :s"),
            {"s": sid},
        )
    shelved = await _publish(client, cid, 1, companion=1)
    assert _error(shelved) == (
        422,
        "entry_archived",
        {"entity": "companion", "scenario_id": sid},
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE catalog.scenario_catalog SET status = 'draft' WHERE id = :s"),
            {"s": sid},
        )
    saved = await _save_character(client, cid, 1, definition={"true_age": None})
    invalid = await _publish(client, cid, saved["draft_revision"])
    status, code, details = _error(invalid)
    assert (status, code, details["entity"], details["character_id"]) == (
        422,
        "publish_invalid",
        "character",
        cid,
    )
    assert "true_age_missing" in [e["code"] for e in details["report"]["errors"]]
    assert await _character_versions(engine, cid) == 0
    assert await _versions(engine, sid) == []


async def test_a_companion_published_meanwhile_is_stale(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A character-alone publish copies the companion's latest version as it read it. When the
    companion is published alone between that read and the publish's row locks, the copy would
    no longer be the latest: the publish is 409 ``draft_stale`` naming the companion, and writes
    nothing. The retry copies the new version."""
    created = await _character(client, "Sam Reed")
    cid, sid = created["id"], created["companion_id"]
    await _ok(await _publish(client, cid, 1), 201)
    draft = await read_companion(client, cid)
    saved = await _ok(
        await _save(client, sid, draft["draft_revision"], world=WORLD, starting_state=OPENING)
    )
    original = companions.SCENARIOS.prepare_for
    published: list[int] = []
    started: list[bool] = []

    async def meanwhile(*args: Any, **kwargs: Any) -> Any:
        if not started:  # once: the companion's own publish prepares through here too
            started.append(True)
            answer = await client.post(
                f"{SCENARIOS}/{sid}/publish",
                json={"draft_revision": saved["draft_revision"]},
                headers=WRITE,
            )
            published.append(answer.status_code)
        return await original(*args, **kwargs)

    monkeypatch.setattr(companions.SCENARIOS, "prepare_for", meanwhile)
    refused = await _publish(client, cid, 2)
    monkeypatch.undo()
    assert published == [201]
    status, code, details = _error(refused)
    assert (status, code, details["entity"], details["scenario_id"]) == (
        409,
        "draft_stale",
        "companion",
        sid,
    )
    assert await _character_versions(engine, cid) == 1
    assert [v[0] for v in await _versions(engine, sid)] == [1, 2]
    retried = await _ok(await _publish(client, cid, 2), 201)
    assert retried["companion"]["version"] == 3
    copied = await _ok(
        await client.get(f"{SCENARIOS}/{sid}", params={"projection": "full"}, headers=READ)
    )
    assert copied["world"]["description"] == WORLD["description"]


async def test_a_companion_removed_by_hand_is_made_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Every character has its companion, made with it; one removed some other way (here SQL) is
    not found by the companion read (404), and is made again by the character's next publish,
    which then publishes its first version: a character version never exists without its
    companion version."""
    created = await _character(client, "Tess Ward")
    cid = created["id"]
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM catalog.scenario_catalog WHERE id = :s"),
            {"s": created["companion_id"]},
        )
    gone = await client.get(f"{CHARACTERS}/{cid}/companion", headers=READ)
    assert _error(gone) == (404, "not_found", {"character_id": cid})
    published = await _ok(await _publish(client, cid, 1), 201)
    made = published["companion"]["id"]
    assert made != created["companion_id"]
    assert published["companion"]["version"] == 1
    assert await _versions(engine, made) == [(1, cid, 1, 1)]
    assert (await read_companion(client, cid))["id"] == made


async def test_a_publish_replays_its_companion(
    client: httpx.AsyncClient, engine: AsyncEngine, settings: Settings
) -> None:
    """A character publish retried under its ``Idempotency-Key`` answers the first result, its
    companion version included, and writes nothing more; the companion version records the
    requesting principal as its publisher. Published with the character, the companion's draft
    needs everything a scenario does (its defaults lack a world and an opening: 422
    ``publish_invalid`` naming the companion). The library publishes the companion's draft with
    the character (``CharacterPublishRequest``, ``with_companion``), even with nothing new in it,
    and refuses the pair without the companion's revision before claiming a key."""
    created = await _character(client, "Uma Brook")
    cid, sid = created["id"], created["companion_id"]
    key = f"replay-{cid}"
    first = await _publish(client, cid, 1, key=key)
    again = await _publish(client, cid, 1, key=key)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert again.headers.get(REPLAY) == "true"
    assert first.json()["companion"] == {
        "id": sid,
        "version": 1,
        "draft_revision": 2,
        "report": first.json()["companion"]["report"],
    }
    assert first.json()["companion"]["report"]["errors"] == []
    assert len(await _versions(engine, sid)) == 1

    listed = await _ok(await client.get(f"{SCENARIOS}/{sid}/versions", headers=READ))
    assert [v["published_by"] for v in listed["items"]] == ["p-catalog"]

    # Published with its character, the companion's draft needs everything a scenario does: the
    # defaults have no world or opening.
    bare = await _publish(client, cid, 2, companion=2)
    status, code, details = _error(bare)
    assert (status, code, details["entity"]) == (422, "publish_invalid", "companion")
    assert sorted(e["code"] for e in details["report"]["errors"]) == [
        "opening_required",
        "world_required",
    ]
    lib = Engine(settings, db=engine)
    try:
        ctx = RequestContext(principal="p-lib")
        with pytest.raises(InvalidRequest) as caught:
            await lib.publish_character(
                ctx, cid, CharacterPublishRequest(draft_revision=2), with_companion=True
            )
        assert caught.value.details["field"] == "body.companion_draft_revision"
        saved = await _ok(await _save(client, sid, 2, world=WORLD, starting_state=OPENING))
        pair = await lib.publish_character(
            ctx,
            cid,
            CharacterPublishRequest(
                draft_revision=2, companion_draft_revision=saved["draft_revision"]
            ),
            with_companion=True,
        )
        assert (pair.version, pair.companion.id, pair.companion.version) == (2, sid, 2)
        assert pair.companion.draft_revision == saved["draft_revision"] + 1
        # Nothing left to publish in the companion's draft: the pair still makes its version.
        unchanged = await lib.publish_character(
            ctx,
            cid,
            CharacterPublishRequest(
                draft_revision=pair.draft_revision,
                companion_draft_revision=pair.companion.draft_revision,
            ),
            with_companion=True,
        )
        assert (unchanged.version, unchanged.companion.version) == (3, 3)
    finally:
        await lib.models.aclose()
    assert await _versions(engine, sid) == [(1, cid, 1, 1), (2, cid, 2, 2), (3, cid, 3, 3)]
    listed = await _ok(await client.get(f"{SCENARIOS}/{sid}/versions", headers=READ))
    assert [v["published_by"] for v in listed["items"]] == ["p-catalog", "p-lib", "p-lib"]


async def test_a_companion_published_alone_pins_the_latest_character_version(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A companion published alone is pinned to its character's latest published version,
    whatever pin its draft holds (here SQL leaves the draft at an older one): the new version's
    columns and content name the latest, and the draft equals what was published."""
    created = await _character(client, "Vera Lind")
    cid, sid = created["id"], created["companion_id"]
    first = await _ok(await _publish(client, cid, 1), 201)
    await _ok(await _publish(client, cid, first["draft_revision"]), 201)
    draft = await read_companion(client, cid)
    saved = await _ok(
        await _save(client, sid, draft["draft_revision"], world=WORLD, starting_state=OPENING)
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE catalog.scenario_catalog "
                "SET draft = jsonb_set(draft, '{cast,roles,0,character,version}', '1') "
                "WHERE id = :s"
            ),
            {"s": sid},
        )
    assert (await read_companion(client, cid))["pinned_character_version"] == 1
    alone = await _ok(
        await client.post(
            f"{SCENARIOS}/{sid}/publish",
            json={"draft_revision": saved["draft_revision"]},
            headers=WRITE,
        ),
        201,
    )
    assert (await _versions(engine, sid))[-1] == (alone["version"], cid, 2, 2)
    assert (await read_companion(client, cid))["pinned_character_version"] == 2
