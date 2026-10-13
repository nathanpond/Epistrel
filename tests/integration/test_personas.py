"""Personas, ad-hoc story characters and their promotion to the catalog (#114).

Two milestone parts live here, each the only file for its test ID's M3 part:

- ``test_c_19``, C-19's M3 part (docs/04 §5): saved personas created, read, listed by owner,
  edited and deleted; a player character's snapshot unchanged by the persona's edit and
  deletion; and a creation that names a catalog character as a player's persona, or puts the
  player in another role, refused without side effects. Until #119's genesis exists, the
  snapshot is written onto a player character the way #119 will write it (``personas.snapshot``
  and ``personas.player_character``, a direct update: #103's FR-CAT-9 pattern), and the
  creation's player checks (``core.genesis.models``) run directly against the database. #119
  wires them into ``POST /stories`` and asserts the zero-row refusal end to end, in this file.
- ``test_s_promote``, S-PROMOTE's M3 part (docs/04 §7): a delivery driver added mid-story stays
  out of the catalog until the author promotes him, and the draft promotion makes carries his
  definition and none of the story's facts.

The persona details follow here; a story character's addition and promotion, and the catalog
draft fields promotion fills, are in ``tests/integration/test_story_characters.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.catalog.report import Outcome, ValidationReport
from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest
from epistrel.core.genesis.models import (
    PERSONA_REQUIRED,
    PLAYER_ROLE_FIXED,
    PlayerAssignment,
    player_role_entries,
    resolve_personas,
)
from epistrel.core.ids import new_ulid
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings, bearer
from tests.catalog_support import ids
from tests.event_support import rows, scalar
from tests.media_support import png, uploaded
from tests.persona_support import copy_into
from tests.respond_support import respond_client
from tests.story_support import create, story_body

AUTHOR = "p-author"
OWNER = "p-persona-owner"
OTHER = "p-persona-other"
READ = bearer()
PERSONA_KEYS = frozenset(
    {"id", "owner_principal", "name", "gender", "age", "description", "created_at", "updated_at"}
)
"""A persona's keys, as a read answers it."""
PHRASE = "Quillon Marrowgate owes the ferryman forty crowns"
"""A story fact planted in the story S-PROMOTE promotes from: no draft may hold it."""
DRIVER = (
    "Dale is a broad man in a grease-stained courier jacket. Cheerful and nosy, he warms to "
    "anyone who offers coffee, dislikes dogs, and talks fast, in short bursts, always in a hurry."
)
DRIVER_FALLBACK = "Dale shrugs: he has three more drops before noon."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def as_(principal: str, key: str | None = None) -> dict[str, str]:
    found = {**bearer(), "X-Epistrel-Principal": principal}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


def body_of(**fields: Any) -> dict[str, Any]:
    """A ``POST /personas`` body: a fresh name unless ``fields`` name one."""
    return {
        "name": f"Sam {new_ulid()}",
        "gender": "man",
        "age": 23,
        "description": "Short-tempered and flies off the handle, but loyal to a fault.",
        **fields,
    }


async def saved(client: httpx.AsyncClient, principal: str = OWNER, **fields: Any) -> dict[str, Any]:
    """A persona created by ``principal`` (201): its body."""
    response = await client.post("/personas", json=body_of(**fields), headers=as_(principal))
    assert response.status_code == 201, response.text
    found: dict[str, Any] = response.json()
    return found


async def listed(client: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    """Every persona ``GET /personas`` lists with ``params``, page after page."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        page = await client.get("/personas", params=query, headers=READ)
        assert page.status_code == 200, page.text
        found.extend(page.json()["items"])
        cursor = page.json()["next_cursor"]
        if cursor is None:
            return found


def error_of(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


async def player_row(engine: AsyncEngine, character_id: str) -> str:
    """The character's whole row, as text."""
    found = await scalar(
        engine, "SELECT row_to_json(c)::text FROM characters c WHERE id = :c", c=character_id
    )
    return str(found)


# -- the parts -------------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-6; FR-API-6; FR-AUTH-5")
async def test_c_19(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Saved personas (a name, a gender, an age and a short description) are created, read,
    listed by owner, edited and deleted. A persona is owned by its creator, or by the owner its
    creation names, and a list by owner holds only that owner's personas; without a filter it
    holds every owner's. A name is unique per owner, ignoring case. A player character that
    holds a persona's snapshot keeps its row byte-identical when the persona is edited and when
    it is deleted. A creation that names a catalog character as a player's persona is refused
    with ``persona_required``, and one that puts the player in a role the scenario did not make
    the player's, or fills the player's role as a character slot, with ``player_role_fixed``;
    the checks write nothing (the ``stories`` count does not move).

    Complements asserted: another owner's personas are absent from a list by owner; the same
    name is accepted for another owner; the snapshot holds what the persona said when it was
    copied, not what it says after the edit; a well-formed assignment (the player in the
    player's role, with a saved persona of any owner or an ad-hoc one) gives no entry."""
    owner, other = f"p-owner-{new_ulid()}", f"p-other-{new_ulid()}"
    async with respond_client(stub_settings, engine) as (client, _):
        # -- create and read: owned by the creator, or by the owner the creation names
        mine = await saved(client, owner, name="  Sam Ortiz ", gender="man", age=23)
        assert set(mine) == PERSONA_KEYS | {"config_revision", "idempotency_key"}
        assert (mine["owner_principal"], mine["name"], mine["gender"], mine["age"]) == (
            owner,
            "Sam Ortiz",
            "man",
            23,
        )
        theirs = await saved(client, owner, owner=other, name="Sam Ortiz")
        assert theirs["owner_principal"] == other  # the same name, for another owner
        read = await client.get(f"/personas/{mine['id']}", headers=READ)
        assert read.status_code == 200, read.text
        assert read.json() == {key: mine[key] for key in PERSONA_KEYS}
        taken = await client.post("/personas", json=body_of(name="SAM ORTIZ"), headers=as_(owner))
        assert error_of(taken)[:2] == (409, "persona_name_taken")

        # -- list by owner: only that owner's; no filter: every owner's
        assert [p["id"] for p in await listed(client, owner=owner)] == [mine["id"]]
        assert [p["id"] for p in await listed(client, owner=other)] == [theirs["id"]]
        everyone = {p["id"] for p in await listed(client)}
        assert {mine["id"], theirs["id"]} <= everyone

        # -- a player character holding the persona's snapshot
        story = await create(client, story_body("Wren", "Ada"))
        wren = story["characters"][0]["id"]
        await copy_into(engine, wren, read.json())
        before = await player_row(engine, wren)
        copied = json.loads(before)
        assert copied["persona_id"] == mine["id"]
        assert (copied["name"], copied["true_age"], copied["definition"]) == (
            "Sam Ortiz",
            23,
            mine["description"],
        )
        assert copied["persona_snapshot"]["description"] == mine["description"]

        # -- edit: the persona changes, the snapshot does not
        edited = await client.patch(
            f"/personas/{mine['id']}",
            json={"name": "Samuel Ortiz", "age": 24, "gender": None, "description": "Calmer."},
            headers=as_(AUTHOR),
        )
        assert edited.status_code == 200, edited.text
        assert (edited.json()["name"], edited.json()["age"], edited.json()["gender"]) == (
            "Samuel Ortiz",
            24,
            None,
        )
        assert edited.json()["updated_at"] > mine["updated_at"]
        assert await player_row(engine, wren) == before
        kept = json.loads(await player_row(engine, wren))["persona_snapshot"]
        assert (kept["name"], kept["age"], kept["gender"]) == ("Sam Ortiz", 23, "man")

        # -- delete: the persona goes, the snapshot stays
        deleted = await client.delete(f"/personas/{mine['id']}", headers=as_(AUTHOR))
        assert deleted.status_code == 204, deleted.text
        assert (await client.get(f"/personas/{mine['id']}", headers=READ)).status_code == 404
        assert await listed(client, owner=owner) == []
        assert await player_row(engine, wren) == before

        # -- the creation's player checks, on a published scenario (roles: mother, player)
        catalog_id = await scenarios.published_character(client, f"Mara {new_ulid()}")
        scenario_id, _ = await scenarios.published(client, scenarios.scenario("Sunday Dinner"))
        full = await scenarios.read(client, scenario_id, projection="full")
        roles = {role["id"]: role["kind"] for role in full.json()["cast"]["roles"]}
        assert roles == {"mother": "npc", "player": "player"}
        stories_before = await scalar(engine, "SELECT count(*) FROM stories")

        wrong_role = player_role_entries(scenario_id, 1, roles, ["mother"], [])
        slotted = player_role_entries(scenario_id, 1, roles, ["player"], ["player", "mother"])
        assert [(e.code, e.location.role, e.location.field, e.outcome) for e in wrong_role] == [
            (PLAYER_ROLE_FIXED, "mother", "players.mother", Outcome.rejected)
        ]
        assert [(e.code, e.location.field) for e in slotted] == [
            (PLAYER_ROLE_FIXED, "slots.player")
        ]
        assert player_role_entries(scenario_id, 1, roles, ["player"], ["mother"]) == []

        def player(persona: dict[str, Any]) -> dict[str, PlayerAssignment]:
            assignment = {"principal": "p-player", "persona": persona}
            return {"player": PlayerAssignment.model_validate(assignment)}

        async with engine.connect() as conn:
            refused = await resolve_personas(conn, scenario_id, 1, player({"id": catalog_id}))
            found, fine = await resolve_personas(conn, scenario_id, 1, player({"id": theirs["id"]}))
            ad_hoc = await resolve_personas(
                conn,
                scenario_id,
                1,
                player({"ad_hoc": {"name": "Jo", "age": 30, "description": "Wry."}}),
            )
            with pytest.raises(InvalidRequest) as unknown:
                await resolve_personas(conn, scenario_id, 1, player({"id": new_ulid()}))
        assert refused[0] == {}
        assert [(e.code, e.location.field, e.source, e.outcome) for e in refused[1]] == [
            (PERSONA_REQUIRED, "players.player.persona.id", catalog_id, Outcome.rejected)
        ]
        assert (found["player"].id, found["player"].owner_principal, fine) == (
            theirs["id"],
            other,
            [],
        )
        assert ad_hoc == ({}, [])
        assert unknown.value.details["field"] == "body.players.player.persona.id"
        report = ValidationReport.of([*wrong_role, *slotted, *refused[1]])
        assert not report.ok
        assert {e.code for e in report.errors} == {PERSONA_REQUIRED, PLAYER_ROLE_FIXED}
        assert await scalar(engine, "SELECT count(*) FROM stories") == stories_before


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-7,8")
async def test_s_promote(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A delivery driver the author adds to a running story is story-scoped: a ``characters``
    row with his true age and no catalog source, which no catalog listing shows, whatever the
    status it lists. The author promotes him: a new **draft** catalog entry, never published,
    holds his name, his true age, his definition (appearance, personality and dispositions,
    likes and dislikes, interaction style), his safe fallback line and his image, by reference,
    rated as the story, and none of the story's facts: a phrase planted in the story's
    narration, pinned memories and custom instructions appears nowhere in the entry. The answer
    carries the new ``catalog_id``.

    Complements asserted: promoting him again makes a second draft (no deduplication); his row
    is byte-identical through both promotions except ``promoted_to``, which names both entries,
    oldest first; the drafts a promotion makes are listed among the catalog's drafts."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create(client, story_body("Wren", "Ada"))
        story_id = story["story_id"]
        portrait = await uploaded(client, png(nonce=new_ulid()), alt_text="Dale at the door")
        for path, payload in (
            (
                "ingest",
                {"events": [{"client_event_id": "e-1", "kind": "narration", "text": PHRASE}]},
            ),
            ("pinned-memories", {"items": [{"text": PHRASE}]}),
            ("custom-instructions", {"text": PHRASE}),
        ):
            method = "POST" if path == "ingest" else "PUT"
            planted = await client.request(
                method, f"/stories/{story_id}/{path}", json=payload, headers=as_(AUTHOR)
            )
            assert planted.status_code in (200, 201), planted.text

        added = await client.post(
            f"/stories/{story_id}/characters",
            json={
                "name": "Dale Pruitt",
                "definition": DRIVER,
                "true_age": 52,
                "safe_fallback_text": DRIVER_FALLBACK,
                "images": [portrait["id"]],
            },
            headers=as_(AUTHOR),
        )
        assert added.status_code == 201, added.text
        dale = added.json()["id"]
        assert (added.json()["name"], added.json()["ordinal"]) == ("Dale Pruitt", 2)
        (row,) = await rows(
            engine,
            "SELECT source_catalog_id, source_catalog_version, true_age, is_player, images "
            "FROM characters WHERE id = :c AND story_id = :s",
            c=dale,
            s=story_id,
        )
        assert tuple(row) == (None, None, 52, False, [portrait["id"]])
        for status in ("published", "draft", "archived"):
            assert dale not in await ids(client, "/catalog/characters", status=status), status
        named = await scalar(
            engine,
            "SELECT count(*) FROM catalog.character_catalog WHERE draft->>'name' = :n",
            n="Dale Pruitt",
        )
        assert named == 0
        before = json.loads(await player_row(engine, dale))

        promoted = await client.post(
            f"/stories/{story_id}/characters/{dale}/promote", json={}, headers=as_(AUTHOR)
        )
        assert promoted.status_code == 201, promoted.text
        first = promoted.json()["catalog_id"]
        assert promoted.headers["Location"] == f"/catalog/characters/{first}"
        assert (promoted.json()["draft_revision"], promoted.json()["warnings"]) == (1, [])
        draft = await client.get(
            f"/catalog/characters/{first}",
            params={"draft": "true", "projection": "full"},
            headers=READ,
        )
        assert draft.status_code == 200, draft.text
        entry = draft.json()
        assert (entry["status"], entry["version"], entry["name"]) == ("draft", None, "Dale Pruitt")
        assert entry["definition"] == {
            "personality": None,
            "true_age": 52,
            "background_flavor": DRIVER,
            "safe_fallback_text": DRIVER_FALLBACK,
        }
        assert entry["images"] == [{"media_id": portrait["id"], "alt_text": "Dale at the door"}]
        assert entry["content_rating"]["id"] == story["content_rating"]["id"]
        assert (entry["public_description"], entry["labels"]) == (None, [])
        stored = await scalar(
            engine,
            "SELECT row_to_json(c)::text FROM catalog.character_catalog c WHERE id = :c",
            c=first,
        )
        assert PHRASE not in json.dumps(entry)
        assert PHRASE not in str(stored)
        unpublished = await client.get(f"/catalog/characters/{first}", headers=READ)
        assert error_of(unpublished)[:2] == (422, "not_published")

        again = await client.post(
            f"/stories/{story_id}/characters/{dale}/promote", json={}, headers=as_(AUTHOR)
        )
        assert again.status_code == 201, again.text
        second = again.json()["catalog_id"]
        assert second != first
        after = json.loads(await player_row(engine, dale))
        assert after["promoted_to"] == [first, second]
        assert {k: v for k, v in after.items() if k != "promoted_to"} == {
            k: v for k, v in before.items() if k != "promoted_to"
        }
        assert before["promoted_to"] == []
        drafts = await ids(client, "/catalog/characters", status="draft")
        assert {first, second} <= set(drafts)


# -- persona details -------------------------------------------------------------------------------


async def test_persona_fields_are_checked(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A persona's fields are checked at the boundary: a name of 1 to 120 characters (trimmed),
    a gender of 1 to 40, an age from 0 to 150 (an integer, never a string or a fraction), a
    description of 1 to 4,000, and an owner that follows the principal rule (never ``system``).
    Each refusal is 422 at its field, with nothing written."""
    async with respond_client(stub_settings, engine) as (client, _):
        before = await scalar(engine, "SELECT count(*) FROM personas")
        operations = await scalar(engine, "SELECT count(*) FROM operations")
        for fields, field in (
            ({"name": "   "}, "body.name"),
            ({"name": "x" * 121}, "body.name"),
            ({"gender": ""}, "body.gender"),
            ({"gender": "g" * 41}, "body.gender"),
            ({"age": -1}, "body.age"),
            ({"age": 151}, "body.age"),
            ({"age": "23"}, "body.age"),
            ({"age": 23.5}, "body.age"),
            ({"description": ""}, "body.description"),
            ({"description": "d" * 4001}, "body.description"),
            ({"owner": "system"}, "body.owner"),
            ({"mood": "x"}, "body.mood"),
        ):
            response = await client.post("/personas", json=body_of(**fields), headers=as_(OWNER))
            status, code, details = error_of(response)
            assert (status, code, details["field"]) == (422, "validation_error", field), fields
        missing = await client.post(
            "/personas", json={"name": "No age", "description": "x"}, headers=as_(OWNER)
        )
        assert error_of(missing)[2]["field"] == "body.age"
        edges = await saved(client, OWNER, name="y" * 120, gender="g" * 40, age=0)
        assert (len(edges["name"]), len(edges["gender"]), edges["age"]) == (120, 40, 0)
        assert await scalar(engine, "SELECT count(*) FROM personas") == before + 1
        assert await scalar(engine, "SELECT count(*) FROM operations") == operations + 1


async def test_persona_edits_merge_and_record(stub_settings: Settings, engine: AsyncEngine) -> None:
    """An edit is a merge patch, last write wins: a key sets its field, ``null`` removes the
    gender and is 422 for a required field, an unknown key is 422, and an edit that changes
    nothing records nothing (no operation, ``updated_at`` kept). A rename to a name its owner
    already uses, ignoring case, is 409 ``persona_name_taken``. Each edit that changes something
    records one ``control`` operation by its requester."""
    async with respond_client(stub_settings, engine) as (client, _):
        owner = f"p-edits-{new_ulid()}"
        first = await saved(client, owner, name="Ana Reyes")
        await saved(client, owner, name="Bea Reyes")
        url = f"/personas/{first['id']}"
        for patch, field in (
            ({"name": None}, "body.name"),
            ({"age": None}, "body.age"),
            ({"description": None}, "body.description"),
            ({"owner": "p-x"}, "body.owner"),
            ({"age": 151}, "body.age"),
        ):
            refused = await client.patch(url, json=patch, headers=as_(AUTHOR))
            assert error_of(refused)[:2] == (422, "validation_error"), patch
            assert error_of(refused)[2]["field"] == field, patch
        clash = await client.patch(url, json={"name": " bea REYES "}, headers=as_(AUTHOR))
        assert error_of(clash) == (
            409,
            "persona_name_taken",
            {"field": "body.name", "owner_principal": owner, "persona_id": first["id"]},
        )
        operations = await scalar(engine, "SELECT count(*) FROM operations")
        same = await client.patch(url, json={"name": "Ana Reyes", "age": 23}, headers=as_(AUTHOR))
        assert same.status_code == 200, same.text
        assert same.json()["updated_at"] == first["updated_at"]
        assert await scalar(engine, "SELECT count(*) FROM operations") == operations
        key = new_ulid()
        removed = await client.patch(url, json={"gender": None}, headers=as_("p-editor", key))
        assert removed.status_code == 200, removed.text
        assert (removed.json()["gender"], removed.json()["name"]) == (None, "Ana Reyes")
        (operation,) = await rows(
            engine,
            "SELECT kind, principal, story_id, status FROM operations WHERE idempotency_key = :k",
            k=key,
        )
        assert tuple(operation) == ("control", "p-editor", None, "committed")
        renamed = await client.patch(url, json={"name": "ana reyes"}, headers=as_(AUTHOR))
        assert renamed.json()["name"] == "ana reyes"  # its own name, another case: no clash


async def test_persona_names_follow_their_owner(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A transfer that would give the new owner two personas of one name, ignoring case, is 409
    ``persona_name_taken`` and changes nothing; a transfer to an owner without that name moves
    the persona, records the transfer and bumps ``updated_at``. Nothing gates on the owner: a
    stranger transfers, edits and deletes."""
    async with respond_client(stub_settings, engine) as (client, _):
        stem = new_ulid()
        a, b, c = (f"p-{side}-{stem}" for side in ("a", "b", "c"))
        moving = await saved(client, a, name="Kit Lane")
        await saved(client, b, name="KIT LANE")
        url = f"/personas/{moving['id']}"
        refused = await client.put(f"{url}/owner", json={"principal": b}, headers=as_(OTHER))
        assert error_of(refused) == (
            409,
            "persona_name_taken",
            {"field": "body.principal", "owner_principal": b, "persona_id": moving["id"]},
        )
        assert (await client.get(url, headers=READ)).json()["owner_principal"] == a
        moved = await client.put(f"{url}/owner", json={"principal": c}, headers=as_(OTHER))
        assert moved.status_code == 200, moved.text
        assert (moved.json()["owner_principal"], moved.json()["previous_owner"]) == (c, a)
        now = (await client.get(url, headers=READ)).json()
        assert now["owner_principal"] == c
        assert now["updated_at"] > moving["updated_at"]
        (event,) = await rows(
            engine,
            "SELECT kind, old, new, acting_principal FROM ownership_events "
            "WHERE entity_type = 'persona' AND entity_id = :e",
            e=moving["id"],
        )
        assert tuple(event) == ("transfer", a, c, OTHER)
        edited = await client.patch(url, json={"age": 40}, headers=as_(OTHER))
        assert edited.json()["age"] == 40


async def test_persona_lists_page_and_filter(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A list is newest first and pages by an opaque cursor that binds its filters: every
    persona once, across pages; a cursor sent with other filters is 422 ``cursor_invalid``
    (``filters_changed``), and a malformed one (``malformed``). ``relation`` narrows the list
    to the personas holding it (``name:principal``, or ``name:`` for any principal); a plain
    string is one relation for the library."""
    async with respond_client(stub_settings, engine) as (client, lib):
        owner = f"p-pager-{new_ulid()}"
        made = [await saved(client, owner) for _ in range(5)]
        newest_first = [p["id"] for p in reversed(made)]
        first = await client.get("/personas", params={"owner": owner, "limit": 2}, headers=READ)
        assert [p["id"] for p in first.json()["items"]] == newest_first[:2]
        assert [p["id"] for p in await listed(client, owner=owner, limit=2)] == newest_first
        cursor = first.json()["next_cursor"]
        changed = await client.get(
            "/personas", params={"owner": OWNER, "cursor": cursor}, headers=READ
        )
        assert error_of(changed) == (
            422,
            "cursor_invalid",
            {"field": "query.cursor", "reason": "filters_changed"},
        )
        junk = await client.get("/personas", params={"cursor": "not-a-cursor"}, headers=READ)
        assert error_of(junk)[2]["reason"] == "malformed"
        related = await client.post(
            f"/personas/{made[1]['id']}/relations",
            json={"principal": "p-coach", "relation": "coach"},
            headers=as_(owner),
        )
        assert related.status_code == 201, related.text
        for value in ("coach:p-coach", "coach:"):
            found = await listed(client, owner=owner, relation=value)
            assert [p["id"] for p in found] == [made[1]["id"]], value
        page = await lib.list_personas(
            RequestContext(principal=owner), owner=owner, relation="coach:p-coach"
        )
        assert [p.id for p in page.items] == [made[1]["id"]]
        too_big = await client.get("/personas", params={"limit": 201}, headers=READ)
        assert error_of(too_big)[2]["field"] == "query.limit"


async def test_persona_deletion_records(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A delete removes the persona and its relations, writes a ``persona_deleted`` audit row
    naming its requester and the operation, and records a ``deleted`` ownership event with the
    owner and the relations; a second delete is 404; the same request again under its key is
    replayed (204, ``Idempotent-Replay``), deleting nothing more."""
    async with respond_client(stub_settings, engine) as (client, _):
        persona = await saved(client, OWNER)
        await client.post(
            f"/personas/{persona['id']}/relations",
            json={"principal": "p-fan", "relation": "player"},
            headers=as_(OWNER),
        )
        key = new_ulid()
        gone = await client.delete(f"/personas/{persona['id']}", headers=as_("p-cleaner", key))
        assert gone.status_code == 204, gone.text
        assert (
            await scalar(engine, "SELECT count(*) FROM personas WHERE id = :p", p=persona["id"])
            == 0
        )
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM entity_relations WHERE entity_id = :p",
                p=persona["id"],
            )
            == 0
        )
        (audit,) = await rows(
            engine,
            "SELECT principal, details FROM audit_log WHERE kind = 'persona_deleted' "
            "AND details->>'entity_id' = :p",
            p=persona["id"],
        )
        (operation,) = await rows(
            engine, "SELECT operation_id FROM operations WHERE idempotency_key = :k", k=key
        )
        assert tuple(audit) == (
            "p-cleaner",
            {
                "entity_type": "persona",
                "entity_id": persona["id"],
                "operation_id": operation.operation_id,
            },
        )
        (event,) = await rows(
            engine,
            "SELECT old, acting_principal FROM ownership_events WHERE entity_id = :p "
            "AND kind = 'deleted'",
            p=persona["id"],
        )
        assert tuple(event) == (
            {"owner_principal": OWNER, "relations": [{"principal": "p-fan", "relation": "player"}]},
            "p-cleaner",
        )
        again = await client.delete(f"/personas/{persona['id']}", headers=as_("p-cleaner"))
        assert error_of(again)[:2] == (404, "not_found")
        replay = await client.delete(f"/personas/{persona['id']}", headers=as_("p-cleaner", key))
        assert (replay.status_code, replay.headers.get("Idempotent-Replay")) == (204, "true")
        audits = await scalar(
            engine,
            "SELECT count(*) FROM audit_log WHERE kind = 'persona_deleted' "
            "AND details->>'entity_id' = :p",
            p=persona["id"],
        )
        assert audits == 1


async def test_persona_writes_are_idempotent(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A creation retried under its ``Idempotency-Key`` answers the stored result (the same
    persona, ``Idempotent-Replay: true``) and creates nothing more; the same key with another
    body is 409 ``idempotency_conflict``."""
    async with respond_client(stub_settings, engine) as (client, _):
        key = new_ulid()
        body = body_of()
        first = await client.post("/personas", json=body, headers=as_(OWNER, key))
        again = await client.post("/personas", json=body, headers=as_(OWNER, key))
        assert (first.status_code, again.status_code) == (201, 201)
        assert again.json() == first.json()
        assert again.headers.get("Idempotent-Replay") == "true"
        assert (
            await scalar(engine, "SELECT count(*) FROM personas WHERE name = :n", n=body["name"])
            == 1
        )
        other = await client.post("/personas", json=body_of(), headers=as_(OWNER, key))
        assert error_of(other)[:2] == (409, "idempotency_conflict")
