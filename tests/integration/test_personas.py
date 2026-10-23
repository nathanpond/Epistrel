"""Personas, ad-hoc story characters and their promotion to the catalog (#114).

Four milestone parts live here, each the only file for its test ID's part:

- ``test_c_19``, C-19's M3 part (docs/04 §5): saved personas created, read, listed by owner,
  edited and deleted; a player character's snapshot unchanged by the persona's edit and
  deletion; and a creation that names a catalog character as a player's persona, or puts the
  player in another role, refused without side effects. Until #119's genesis exists, the
  snapshot is written onto a player character the way genesis writes it (``personas.snapshot``
  and ``personas.player_character``, a direct update: #103's FR-CAT-9 pattern), and the
  creation's player checks (``core.genesis.models``) run directly against the database. #119
  wires them into ``POST /stories``: the test then asserts the zero-row refusals end to end,
  and a story created with a saved persona keeps its copy through the persona's edit and
  deletion.
- ``test_c_19__audience``, C-19's M5 part (#173): a persona seeded by audience, its
  coworkers' beliefs and a stranger's age band, revised by observation without a canon
  contradiction.
- ``test_s_promote``, S-PROMOTE's M3 part (docs/04 §7): a delivery driver added mid-story stays
  out of the catalog until the author promotes him, and the draft promotion makes carries his
  definition and none of the story's facts.
- ``test_s_promote__seeded_memories``, S-PROMOTE's M4 part (FR-CAT-8; #498): promoting him
  again with two selected memories seeds exactly those into the new character's companion
  scenario draft, never into the character.

The persona details follow here; a story character's addition and promotion, and the catalog
draft fields promotion fills, are in ``tests/integration/test_story_characters.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.routes.admin import ADMIN_PREFIX
from epistrel.config import Settings
from epistrel.core import membership
from epistrel.core.catalog.report import Outcome, ValidationReport
from epistrel.core.context import RequestContext
from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import transitions
from epistrel.core.errors import InvalidRequest
from epistrel.core.genesis.models import (
    PERSONA_REQUIRED,
    PLAYER_ROLE_FIXED,
    PlayerAssignment,
    player_role_entries,
    resolve_personas,
)
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from epistrel.core.tables.memories import memories
from tests import genesis_support as g
from tests import memory_support as memory
from tests import nested_support as ns
from tests import partition_support as parts
from tests import scenario_support as scenarios
from tests import seeded_partitions_support as sp
from tests.auth_support import auth_settings, bearer
from tests.catalog_support import every, ids
from tests.event_support import execute_in_tx, rows, scalar
from tests.media_support import png, uploaded
from tests.persona_support import copy_into
from tests.respond_support import body as respond_body
from tests.respond_support import respond, respond_client
from tests.story_support import create, story_body
from tests.structured_turn_support import beat, render, schema_calls, text_of

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

        # -- end to end (#119): POST /stories refuses both with nothing written
        mother = {"mother": g.catalog_slot(catalog_id)}
        as_persona = g.from_scenario(
            scenario_id,
            slots=mother,
            players={"player": g.player(persona_id=catalog_id)},
        )
        in_mother = g.from_scenario(
            scenario_id, slots=mother, players={"mother": g.player(), "player": g.player()}
        )
        for request, code in ((as_persona, PERSONA_REQUIRED), (in_mother, PLAYER_ROLE_FIXED)):
            counted = await g.counts(engine)
            status, found_code, details = await g.refused(client, request)
            assert (status, found_code) == (422, code)
            assert code in g.report_codes(details)
            assert await g.counts(engine) == counted

        # -- a saved persona a story was created with: its edit leaves the story's copy
        jo = await saved(client, other, name=f"Jo {new_ulid()}", age=31)
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id,
                slots=mother,
                players={"player": g.player(persona_id=jo["id"])},
            ),
        )
        player_id = next(c["id"] for c in made["characters"] if c["role_id"] == "player")
        copied_row = await player_row(engine, player_id)
        assert json.loads(copied_row)["persona_id"] == jo["id"]
        renamed = await client.patch(f"/personas/{jo['id']}", json={"age": 32}, headers=as_(AUTHOR))
        assert renamed.status_code == 200, renamed.text
        assert await player_row(engine, player_id) == copied_row
        gone = await client.delete(f"/personas/{jo['id']}", headers=as_(AUTHOR))
        assert gone.status_code == 204, gone.text
        assert await player_row(engine, player_id) == copied_row


@pytest.mark.integration
@pytest.mark.part("M5", "FR-SCN-8")
async def test_c_19__audience(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-19's M5 part: a persona is seeded by audience (#173). Sam (23, "short-tempered, flies
    off the handle") is a coworker of Alice and Bob and a stranger to Carol: the coworkers
    hold Sam's name, exact age and both traits as beliefs (citing the persona's
    ``seed_materialized`` act); Carol holds none of them and, rendered, sees Sam only as "early
    twenties"; no NPC but an acquaintance holds the age 23. No canon record comes from the
    persona's description. When Sam stays calm in front of Bob, Bob's belief about the temper
    is revised through the ordinary belief operations (an ``observation`` transition closes it),
    and no canon contradiction is raised: the facts are as they were."""
    ns.deliver_nested(monkeypatch)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        profile = await sp.partitioned_profile(client, "Seeded office")
        scenario_id, _ = await g.published_scenario(client, sp.office())
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id,
                slots={
                    name: g.ad_hoc_slot(name.title(), 40, f"{name.title()} works here.")
                    for name in ("alice", "bob", "carol")
                },
                players={"player": g.player(**sp.TEMPER)},
                engine_profile_id=profile,
                ignore_warnings=True,
            ),
        )
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        sam = by_role["player"]
        rows = await sp.beliefs(engine, story)
        about_sam = {
            role: sorted(
                (r.predicate, str((r.literal or {}).get("value")))
                for r in rows
                if str(r.holder) == by_role[role] and str(r.subject_id) == sam
            )
            for role in ("alice", "bob", "carol")
        }
        expected = sorted(
            [("age", "23"), ("name", "Sam"), *(("trait", t) for t in sp.TEMPER_TRAITS)]
        )
        assert about_sam == {"alice": expected, "bob": expected, "carol": []}
        assert {r.act for r in rows if str(r.subject_id) == sam} == {"seed_materialized"}
        ages = {str(r.holder) for r in rows if r.predicate == "age" and str(r.subject_id) == sam}
        assert ages == {by_role["alice"], by_role["bob"]}
        facts_before = await sp.rows_of(
            engine,
            "SELECT f.id, pr.name, p.literal FROM facts f JOIN propositions p "
            "ON p.id = f.proposition_id JOIN predicates pr ON pr.id = p.predicate_id "
            "WHERE f.story_id = :s ORDER BY f.id",
            s=story,
        )
        assert not [f for f in facts_before if f.name == "trait"]
        assert not [f for f in facts_before if "temper" in json.dumps(f.literal or {})]

        # -- Carol, a stranger, sees an age band
        stub = epistrel.models.stub
        stub.calls.clear()
        stub.script("generation", [render(beat("do", "Carol looks up."))])
        answer = await respond(
            client,
            story,
            respond_body(sam, "Morning.", render_character_ids=[by_role["carol"]]),
        )
        assert answer.status_code == 200, answer.text
        (call,) = schema_calls(stub, "CharacterRender")
        carol = text_of(call)
        assert "Sam (early twenties)" in carol
        assert "23" not in carol
        assert "short-tempered" not in carol

        # -- Sam stays calm: Bob revises the temper belief by observation
        (temper,) = [
            r
            for r in rows
            if str(r.holder) == by_role["bob"]
            and r.predicate == "trait"
            and (r.literal or {}).get("value") == "short-tempered"
        ]
        trait_id = await scalar(
            engine,
            "SELECT id FROM predicates WHERE story_id = :s AND name = 'trait'",
            s=story,
        )
        await parts.transit(
            engine,
            story,
            by_role["bob"],
            Proposition(
                subject_id=sam,
                predicate_id=str(trait_id),
                literal=TypedLiteral(kind=LiteralKind.STR, value="short-tempered"),
                polarity="negated",
                valid_time=Interval(start=None, end=None),
            ),
            cause=transitions.Kind.OBSERVATION,
            confidence=transitions.Confidence(level=BeliefConfidenceLevel.BELIEVED),
        )
        after = await sp.rows_of(
            engine,
            "SELECT b.id, b.polarity, b.held_to_turn FROM beliefs b JOIN propositions p "
            "ON p.id = b.proposition_id WHERE b.story_id = :s AND b.holder_character_id = :h "
            "AND p.predicate_id = :t AND p.literal->>'value' = 'short-tempered'",
            s=story,
            h=by_role["bob"],
            t=str(trait_id),
        )
        closed = {str(r.id): r for r in after}
        assert closed[str(temper.id)].held_to_turn is not None
        assert [r.polarity for r in after if r.held_to_turn is None] == ["negated"]
        facts_after = await sp.rows_of(
            engine, "SELECT f.id FROM facts f WHERE f.story_id = :s ORDER BY f.id", s=story
        )
        assert [str(f.id) for f in facts_after][: len(facts_before)] == [
            str(f.id) for f in facts_before
        ]
        assert not await sp.rows_of(
            engine,
            "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.predicate_id = :t",
            s=story,
            t=str(trait_id),
        )


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-7,8")
async def test_s_promote(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A delivery driver the author adds to a running story is story-scoped: a ``characters``
    row with his true age and no catalog source, which no catalog listing shows, whatever the
    status it lists. The author promotes him: a new **draft** catalog entry, owned by the author
    (the request names no owner), never published, holds his name, his true age, his definition
    (appearance, personality and dispositions, likes and dislikes, interaction style), his safe
    fallback line and his image, by reference, rated as the story, and none of the story's
    facts: a phrase planted in the story's narration, pinned memories and custom instructions
    appears nowhere in the entry. The answer carries the new ``catalog_id``. Like any catalog
    character, the draft is created with its companion scenario (#116).

    Complements asserted: promoting him again makes a second draft (no deduplication); his row
    is byte-identical through both promotions except ``promoted_to``, which names both entries,
    oldest first, and an omniscient ``GET /stories/{id}`` shows the same link (a plain read shows
    no ``promoted_to``); the drafts a promotion makes are listed among the catalog's drafts."""
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

        entries = await scalar(engine, "SELECT count(*) FROM catalog.character_catalog")
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
        # Adding him writes no catalog entry: no new row, none named for him (the name lives
        # under the draft's public_card since 0032), and no listing shows him by id or by name.
        assert await scalar(engine, "SELECT count(*) FROM catalog.character_catalog") == entries
        for status in ("published", "draft", "archived"):
            assert dale not in await ids(client, "/catalog/characters", status=status), status
            listed = await every(client, "/catalog/characters", status=status)
            assert "Dale Pruitt" not in {item["name"] for item in listed}, status
        named = await scalar(
            engine,
            "SELECT count(*) FROM catalog.character_catalog "
            "WHERE draft->'public_card'->>'name' = :n",
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
        assert entry["owner_principal"] == AUTHOR  # the requesting principal, by default
        assert entry["definition"] == {
            "true_age": 52,
            "appearance": None,
            "personality": None,
            "likes_dislikes": None,
            "dispositions": None,
            "values_motivations": None,
            "interaction_style": None,
            "background_flavor": DRIVER,
            "deflection_style": None,
            "safe_fallback_text": DRIVER_FALLBACK,
        }
        shown = entry["images"]["primary"]
        assert (shown["media_id"], shown["alt_text"], entry["images"]["gallery"]) == (
            portrait["id"],
            "Dale at the door",
            [],
        )
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
        # Like any creation, the promotion creates the draft's companion scenario with it (#116):
        # the author's, named and rated as the draft.
        companion = await client.get(
            f"/catalog/characters/{first}/companion", params={"projection": "full"}, headers=READ
        )
        assert companion.status_code == 200, companion.text
        made = companion.json()
        assert (made["id"], made["companion_of"], made["owner_principal"]) == (
            entry["companion_id"],
            first,
            AUTHOR,
        )
        assert (made["public_card"]["name"], made["metadata"]["content_rating_id"]) == (
            "Dale Pruitt",
            story["content_rating"]["id"],
        )

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
        # The link is read back where a consumer sees it: an omniscient GET /stories/{id} shows
        # both entries, oldest first; a plain read shows no promoted_to at all.
        omniscient = await client.get(
            f"/stories/{story_id}", params={"viewpoint": "omniscient"}, headers=READ
        )
        assert omniscient.status_code == 200, omniscient.text
        (shown_dale,) = [c for c in omniscient.json()["characters"] if c["id"] == dale]
        assert shown_dale["promoted_to"] == [first, second]
        plain = await client.get(f"/stories/{story_id}", headers=READ)
        assert plain.status_code == 200, plain.text
        (plain_dale,) = [c for c in plain.json()["characters"] if c["id"] == dale]
        assert "promoted_to" not in plain_dale


HELD = "Dale remembers that the ferryman still owes him for the March crossing"
"""A memory Dale holds (S-PROMOTE M4): one of the two seeded."""
STALE = "Dale remembers the ferry leaving at six"
"""A memory Dale held until a newer derivation superseded it: ``inactive``."""
PLANTED = "Dale remembers the harbourmaster asking him to keep the vault code"
"""A memory Dale holds that the author quarantined: ``quarantined``."""
FORGED = "Ada remembers forging the harbourmaster's signature"
"""A memory Ada holds: Dale can neither hold nor know it."""


async def companion_draft(client: httpx.AsyncClient, catalog_id: str) -> dict[str, Any]:
    """The draft of the catalog character's companion scenario, in ``full``."""
    found = await client.get(
        f"/catalog/characters/{catalog_id}/companion",
        params={"draft": "true", "projection": "full"},
        headers=READ,
    )
    assert found.status_code == 200, found.text
    document: dict[str, Any] = found.json()
    return document


async def catalog_counts(engine: AsyncEngine) -> tuple[int, int]:
    """``(catalog characters, scenarios)``."""
    found = await rows(
        engine,
        "SELECT (SELECT count(*) FROM catalog.character_catalog), "
        "(SELECT count(*) FROM catalog.scenario_catalog)",
    )
    return int(found[0][0]), int(found[0][1])


@pytest.mark.integration
@pytest.mark.part("M4", "FR-CAT-8")
async def test_s_promote__seeded_memories(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Promoting Dale again with two selected memories (one he holds, one the story's shared
    episodic memory of his reply) seeds exactly those two into the new character's companion
    scenario draft: two ``starting_state.seeded_memories`` items, in the order named, each
    with the memory's text, held by the locked ``npc`` role, audience ``self`` and disclosure
    ``open`` (FR-SCN-8), and nothing of either memory, nor of the memory left unselected, goes
    into the character itself. The companion's validation (the one a publish runs) reaches
    the seeds and finds their attributes declared, and a retry under the same key
    replays the promotion without seeding again.

    Complements asserted: a memory of another story (``not_in_story``), a memory Ada holds
    (``not_known``, named after one Dale may seed), a memory Dale held until it was superseded
    (``inactive``) and one he holds that is quarantined (``quarantined``, the poisoning defence:
    #145) are each 422 ``seed_memory_invalid`` at their index, with no catalog character,
    scenario or link written (#507); an empty
    ``seed_memories`` promotes as M3 does, with an unseeded companion."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create(client, story_body("Wren", "Ada"))
        story_id = story["story_id"]
        wren, ada = (c["id"] for c in story["characters"])
        await memory.use_structured(engine, story_id)
        added = await client.post(
            f"/stories/{story_id}/characters",
            # Not test_s_promote's "Dale Pruitt": its no-listing-shows-him check reads the shared
            # catalog, which this test's promotions write to (#508).
            json={"name": "Dale Mercer", "definition": DRIVER, "true_age": 52},
            headers=as_(AUTHOR),
        )
        assert added.status_code == 201, added.text
        dale = added.json()["id"]
        op, shared = await memory.turn(
            engine,
            story_id,
            actor=wren,
            say="Where is the ferry?",
            reply="Late again, love.",
            voiced=[dale],
        )
        asked, replied = shared  # public episodic memories: anyone in the story can know them
        sources = list((await memory.event_ids(engine, op.operation_id)).values())
        held = await memory.derived_memory(engine, story_id, HELD, sources, holder=dale)
        forged = await memory.derived_memory(engine, story_id, FORGED, sources, holder=ada)
        stale = await memory.derived_memory(engine, story_id, STALE, sources, holder=dale)
        async with engine.begin() as conn:  # a newer derivation replaced it (#143's rerun)
            assert await membership.supersede(conn, memories, [stale], story_id=story_id) == 1
        planted = await memory.derived_memory(engine, story_id, PLANTED, sources, holder=dale)
        quarantined = await client.post(
            f"{ADMIN_PREFIX}/stories/{story_id}/memories/{planted}/quarantine",
            json={"reason": "A planted line."},
            headers=as_(AUTHOR),
        )
        assert quarantined.status_code == 200, quarantined.text
        assert quarantined.json()["memory"]["quarantined"] is True
        other = await create(client, story_body("Kit", "Lee"))
        await memory.use_structured(engine, other["story_id"])
        _, (elsewhere, *_) = await memory.turn(engine, other["story_id"])

        # -- refused: nothing written
        before = await catalog_counts(engine)
        for seeds, index, reason in (
            ([elsewhere.id], 0, "not_in_story"),
            ([held, forged], 1, "not_known"),
            ([held, stale], 1, "inactive"),
            ([planted, held], 0, "quarantined"),
        ):
            refused = await client.post(
                f"/stories/{story_id}/characters/{dale}/promote",
                json={"seed_memories": seeds},
                headers=as_(AUTHOR),
            )
            assert error_of(refused)[:2] == (422, "seed_memory_invalid"), refused.text
            details = error_of(refused)[2]
            assert (details["field"], details["memory_id"], details["reason"]) == (
                f"body.seed_memories[{index}]",
                seeds[index],
                reason,
            )
        assert await catalog_counts(engine) == before
        promoted = await scalar(engine, "SELECT promoted_to FROM characters WHERE id = :c", c=dale)
        assert promoted == []

        # -- an empty list: M3's promotion, an unseeded companion
        plain = await client.post(
            f"/stories/{story_id}/characters/{dale}/promote",
            json={"seed_memories": []},
            headers=as_(AUTHOR),
        )
        assert plain.status_code == 201, plain.text
        unseeded = await companion_draft(client, plain.json()["catalog_id"])
        assert unseeded["starting_state"]["seeded_memories"] == []

        # -- two selected memories: exactly those, in the companion only
        key = new_ulid()
        body = {"seed_memories": [held, replied.id]}
        seeded = await client.post(
            f"/stories/{story_id}/characters/{dale}/promote", json=body, headers=as_(AUTHOR, key)
        )
        assert seeded.status_code == 201, seeded.text
        catalog_id = seeded.json()["catalog_id"]
        assert seeded.json()["draft_revision"] == 1
        companion = await companion_draft(client, catalog_id)
        assert companion["draft_revision"] == 1
        items = companion["starting_state"]["seeded_memories"]
        assert [
            (i["text"], i["holder"], i["audience"], i["disclosure"], i["valid_time"]) for i in items
        ] == [
            (HELD, "npc", ["self"], "open", None),
            (replied.text, "npc", ["self"], "open", None),
        ]
        assert all(i["id"] for i in items)
        assert companion["starting_state"] == {
            **unseeded["starting_state"],
            "seeded_memories": items,
        }
        (npc,) = [r for r in companion["cast"]["roles"] if r["id"] == "npc"]
        assert (npc["fill"], npc["character"]["catalog_id"]) == ("locked", catalog_id)
        stored = json.dumps(companion)
        for left_out in (asked.text, FORGED, elsewhere.text, STALE, PLANTED):
            assert left_out not in stored
        character = await client.get(
            f"/catalog/characters/{catalog_id}",
            params={"draft": "true", "projection": "full"},
            headers=READ,
        )
        assert character.status_code == 200, character.text
        row = await scalar(
            engine,
            "SELECT row_to_json(c)::text FROM catalog.character_catalog c WHERE id = :c",
            c=catalog_id,
        )
        for text_ in (HELD, replied.text, asked.text, FORGED):
            assert text_ not in json.dumps(character.json())
            assert text_ not in str(row)
        assert character.json()["definition"] == {
            **{k: None for k in character.json()["definition"]},
            "true_age": 52,
            "background_flavor": DRIVER,
        }
        # The seeds go through the companion's own validation, the one a publish runs (FR-SCN-5):
        # their holder, audience and disclosure are declared, so no ``seed_missing_*`` error;
        # on this Structured story's deployment the seed normalization reaches them too, and the
        # stub model normalizes no prose (``seed_unnormalizable``), which an author resolves
        # before publishing.
        checked = await client.post(
            f"/catalog/scenarios/{companion['id']}/validate", json={}, headers=as_(AUTHOR)
        )
        assert checked.status_code == 200, checked.text
        report = checked.json()["report"]
        on_seeds = {
            e["code"]
            for e in report["errors"] + report["warnings"]
            if "seeded_memories" in str(e["location"].get("field"))
        }
        assert on_seeds <= {"seed_unnormalizable"}, report

        # -- replayed under its key: the same answer, no second seeding
        counts = await catalog_counts(engine)
        replay = await client.post(
            f"/stories/{story_id}/characters/{dale}/promote", json=body, headers=as_(AUTHOR, key)
        )
        assert (replay.json(), replay.headers.get("Idempotent-Replay")) == (seeded.json(), "true")
        assert await catalog_counts(engine) == counts
        promoted = await scalar(engine, "SELECT promoted_to FROM characters WHERE id = :c", c=dale)
        assert promoted == [plain.json()["catalog_id"], catalog_id]


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


async def test_only_a_player_character_holds_a_persona(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A persona's id and snapshot are kept on player characters only
    (``ck_characters_persona_player``): the database refuses either one on another character
    (the snapshot alone too, as an ad-hoc persona's has no id), and refuses a player character
    that holds a persona ceasing to be a player. Each refusal leaves both rows as they were.

    Complement asserted: the story's player character takes the persona's id and snapshot."""
    async with respond_client(stub_settings, engine) as (client, _):
        persona = await saved(client, OWNER)
        story = await create(client, story_body("Wren", "Ada"))
        wren, ada = (c["id"] for c in story["characters"])
        await copy_into(engine, wren, {key: persona[key] for key in PERSONA_KEYS})
        before = (await player_row(engine, wren), await player_row(engine, ada))
        wren_row = json.loads(before[0])
        assert (wren_row["is_player"], wren_row["persona_id"]) == (True, persona["id"])
        assert wren_row["persona_snapshot"]["persona_id"] == persona["id"]
        assert json.loads(before[1])["is_player"] is False
        ad_hoc = json.dumps({**wren_row["persona_snapshot"], "persona_id": None})
        for sql, params in (
            ("UPDATE characters SET persona_id = :p WHERE id = :c", {"p": persona["id"], "c": ada}),
            (
                "UPDATE characters SET persona_snapshot = CAST(:s AS jsonb) WHERE id = :c",
                {"s": ad_hoc, "c": ada},
            ),
            ("UPDATE characters SET is_player = false WHERE id = :c", {"c": wren}),
        ):
            with pytest.raises(IntegrityError, match="ck_characters_persona_player"):
                await execute_in_tx(engine, (sql, params))
        assert (await player_row(engine, wren), await player_row(engine, ada)) == before
