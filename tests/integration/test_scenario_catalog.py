"""The scenario catalog over REST (#108): drafts with optimistic concurrency, immutable published
versions, the save rules for roles and references, server-assigned seed ids, the
starting-cast ceiling, the ``card``/``full`` projections, media and rating references, the
subject-reference index, archive, unarchive and delete, idempotency and attribution. The
milestone parts are ``tests/conformance/test_c_15.py`` (the lifecycle, the field classes and
the projections) and ``tests/test_u_26.py`` (roles and the starting-cast limit)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import (
    DraftStale,
    Engine,
    NotPublished,
    PrincipalRequired,
    RequestContext,
    ScenarioCard,
    ScenarioCreate,
    ScenarioNotFound,
    ScenarioPatch,
)
from epistrel.config import Settings
from epistrel.core.catalog import scenarios
from epistrel.core.catalog.service import PublishRequest
from epistrel.core.ids import new_ulid
from epistrel.core.media.sweep import sweep
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.testing import pauses
from tests.catalog_support import character
from tests.event_support import execute_in_tx, rows, scalar, sqlstate
from tests.media_support import media_client, scratch, uploaded, video
from tests.scenario_support import (
    BASE,
    CARD_KEYS,
    CARD_ROLE_KEYS,
    CARD_ROLES,
    FULL_KEYS,
    GROUPS,
    PRINCIPAL,
    PRIVATE_PHRASES,
    READ,
    VERSION_KEYS,
    WRITE,
    cast_characters,
    complete_with,
    create,
    document_of,
    entry_row,
    error_of,
    fields_of,
    headers,
    publish,
    published,
    read,
    role,
    save,
    scenario,
    seed,
    set_cast_limits,
    stored_form,
    undeliverable_checks,
    version_rows,
)
from tests.story_support import rest

REPLAY = "Idempotent-Replay"


@pytest.fixture
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with rest(settings, engine) as found:
        yield found


async def _operations(engine: AsyncEngine) -> int:
    return int(await scalar(engine, "SELECT count(*) FROM operations"))


async def _lock_waiters(engine: AsyncEngine, wanted: int, within: float = 10.0) -> None:
    """Return once ``wanted`` backends of this database wait on a lock."""
    deadline = time.monotonic() + within
    waiting = (
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    while int(await scalar(engine, waiting)) < wanted:
        assert time.monotonic() < deadline, "no request waited on the lock in time"
        await asyncio.sleep(0.02)


def _cast(*roles: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"roles": list(roles), **fields}


def _content(body: dict[str, Any]) -> dict[str, Any]:
    """What a version read shows of the version itself (the rest is the scenario's envelope)."""
    return {key: body[key] for key in ("id", "version", "published_at", "schema_version", *GROUPS)}


# -- the tables -------------------------------------------------------------------------------


async def test_scenario_tables_have_the_planned_shape(engine: AsyncEngine) -> None:
    """``catalog.scenario_catalog`` mirrors the character catalog plus ``companion_of`` (a
    foreign key to the character catalog that cascades, unique: one companion per character,
    #116) and ``content_rating_explicit`` (#116: false until a companion's rating is set on
    it); ``catalog.scenario_versions`` is keyed ``(catalog_id, version)``, cascades with its
    scenario, and pins a character version through a foreign key that is set to NULL (both pin
    columns or neither), and keeps the validation report its publish produced (#111). The
    deployment gains the starting-cast default (16) and ceiling (none)."""
    found = await rows(
        engine,
        "SELECT table_name, column_name, udt_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'catalog' AND table_name LIKE 'scenario%' "
        "ORDER BY table_name, ordinal_position",
    )
    columns: dict[str, list[str]] = {}
    for row in found:
        columns.setdefault(row.table_name, []).append(row.column_name)
    assert columns == {
        "scenario_catalog": [
            "id",
            "owner_principal",
            "listing_status",
            "status",
            "current_version",
            "draft",
            "draft_revision",
            "companion_of",
            "created_at",
            "updated_at",
            "content_rating_explicit",
        ],
        "scenario_versions": [
            "catalog_id",
            "version",
            "content",
            "content_rating_version_id",
            "pinned_character_id",
            "pinned_character_version",
            "published_at",
            "published_by",
            "search",
            "report",
            # When an erasure rewrote the version in place (#124).
            "redacted_at",
        ],
    }
    types = {(r.table_name, r.column_name): (r.udt_name, r.is_nullable) for r in found}
    assert types[("scenario_catalog", "status")] == ("entry_status", "NO")
    assert types[("scenario_catalog", "draft")] == ("jsonb", "NO")
    assert types[("scenario_catalog", "companion_of")] == ("bpchar", "YES")
    assert types[("scenario_catalog", "content_rating_explicit")] == ("bool", "NO")
    unique = await rows(
        engine,
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'catalog' "
        "AND tablename = 'scenario_catalog' AND indexdef LIKE '%(companion_of)%'",
    )
    assert [tuple(r) for r in unique] == [
        (
            "uq_scenario_catalog_companion_of",
            "CREATE UNIQUE INDEX uq_scenario_catalog_companion_of ON catalog.scenario_catalog "
            "USING btree (companion_of)",
        )
    ]
    assert types[("scenario_versions", "content")] == ("jsonb", "NO")
    assert types[("scenario_versions", "search")] == ("tsvector", "YES")  # generated (#110)
    # Generated by Postgres from the version's content (#110), never written by a trigger or
    # the application.
    (generated,) = await rows(
        engine,
        "SELECT is_generated, generation_expression FROM information_schema.columns "
        "WHERE table_schema = 'catalog' AND table_name = 'scenario_versions' "
        "AND column_name = 'search'",
    )
    assert generated.is_generated == "ALWAYS"
    assert generated.generation_expression == "catalog.epistrel_scenario_search(content)"
    assert types[("scenario_versions", "report")] == ("jsonb", "YES")  # #111
    constraints = await rows(
        engine,
        "SELECT conrelid::regclass::text AS tbl, pg_get_constraintdef(oid) AS definition "
        "FROM pg_constraint WHERE conrelid IN "
        "('catalog.scenario_catalog'::regclass, 'catalog.scenario_versions'::regclass) "
        "AND contype IN ('f', 'c') ORDER BY 1, 2",
    )
    assert [(c.tbl, c.definition) for c in constraints] == [
        (
            "catalog.scenario_catalog",
            "FOREIGN KEY (companion_of) REFERENCES catalog.character_catalog(id) ON DELETE CASCADE",
        ),
        (
            "catalog.scenario_versions",
            "CHECK (((pinned_character_id IS NULL) = (pinned_character_version IS NULL)))",
        ),
        (
            "catalog.scenario_versions",
            "FOREIGN KEY (catalog_id) REFERENCES catalog.scenario_catalog(id) ON DELETE CASCADE",
        ),
        (
            "catalog.scenario_versions",
            "FOREIGN KEY (pinned_character_id, pinned_character_version) REFERENCES "
            "catalog.character_versions(catalog_id, version) ON DELETE SET NULL",
        ),
    ]
    deployment = await rows(
        engine,
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'deployment_state' AND column_name LIKE 'max_characters%' "
        "ORDER BY column_name",
    )
    assert [tuple(r) for r in deployment] == [
        ("max_characters_at_start_ceiling", "YES", None),
        ("max_characters_at_start_default", "NO", "16"),
    ]


# -- lifecycle --------------------------------------------------------------------------------


async def test_draft_save_publish_and_versions(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Create is a draft at revision 1 in ``full``; each save bumps the revision; publish makes
    version n + 1 from the draft and leaves the draft equal to it, one revision on; an
    unpublished edit is seen only by a draft read; version 1 reads unchanged after version 2
    exists; the version list gives each version's number, publish time and publisher."""
    created = await create(client)
    sid = created["id"]
    assert set(created) == FULL_KEYS | {"draft_revision", "config_revision", "idempotency_key"}
    assert (created["status"], created["listing_status"], created["draft_revision"]) == (
        "draft",
        "private",
        1,
    )
    assert (created["version"], created["current_version"], created["published_at"]) == (
        None,
        None,
        None,
    )
    assert (created["companion_of"], created["owner_principal"]) == (None, PRINCIPAL)
    assert created["schema_version"] == 1

    saved = await save(client, sid, 1, public_card={"public_description": "First words."})
    assert (saved.status_code, saved.json()["draft_revision"]) == (200, 2)
    first = await publish(client, sid, 2)
    assert first.status_code == 201, first.text
    assert {k: first.json()[k] for k in ("id", "version", "draft_revision", "status")} == {
        "id": sid,
        "version": 1,
        "draft_revision": 3,
        "status": "published",
    }
    report = first.json()["report"]
    assert (report["errors"], report["warnings"]) == ([], [])
    assert set(report["unavailable"]) >= set(undeliverable_checks())
    stored = await entry_row(engine, sid)
    assert (stored.status, stored.current_version, stored.draft_revision) == ("published", 1, 3)
    (pair,) = await rows(
        engine,
        "SELECT c.draft = v.content AS same FROM catalog.scenario_catalog c "
        "JOIN catalog.scenario_versions v ON v.catalog_id = c.id WHERE c.id = :c",
        c=sid,
    )
    assert pair.same
    version_1 = (await read(client, sid, version=1, projection="full")).json()
    before = await version_rows(engine, sid)

    edited = await save(client, sid, 3, public_card={"public_description": "Second words."})
    assert edited.status_code == 200, edited.text
    shown = (await read(client, sid)).json()
    assert (shown["version"], shown["public_description"]) == (1, "First words.")
    pending = (await read(client, sid, draft=True, projection="full")).json()
    assert (pending["version"], pending["draft_revision"]) == (1, 4)
    assert pending["public_card"]["public_description"] == "Second words."
    second = await publish(client, sid, 4)
    assert (second.status_code, second.json()["version"]) == (201, 2)

    again = (await read(client, sid, version=1, projection="full")).json()
    assert _content(again) == _content(version_1)
    assert _content(again)["public_card"]["public_description"] == "First words."
    assert (await version_rows(engine, sid))[:1] == before
    latest = (await read(client, sid, projection="full")).json()
    assert (latest["version"], latest["current_version"], latest["published_at"]) == (
        2,
        2,
        second.json()["published_at"],
    )
    assert "draft_revision" not in latest
    listed = (await client.get(f"{BASE}/{sid}/versions", headers=READ)).json()["items"]
    assert [set(item) for item in listed] == [VERSION_KEYS, VERSION_KEYS]
    assert [(v["version"], v["published_at"], v["published_by"]) for v in listed] == [
        (1, first.json()["published_at"], PRINCIPAL),
        (2, second.json()["published_at"], PRINCIPAL),
    ]
    third = await publish(client, sid, 5)
    assert (third.status_code, third.json()["version"]) == (201, 3)


async def test_save_is_a_json_merge_patch(client: httpx.AsyncClient) -> None:
    """A group merges key by key, ``null`` removes a field (or resets a whole group), an array
    replaces the stored one, a patch that changes nothing still moves the revision; unknown
    keys, wrong types and a missing revision are 422 at their field, wherever the field came
    from."""
    created = await create(client)
    sid = created["id"]
    removed = await save(client, sid, 1, public_card={"public_description": None})
    assert removed.status_code == 200, removed.text
    card = removed.json()["public_card"]
    assert (card["name"], card["public_description"]) == ("Sunday Dinner", None)

    replaced = await save(
        client, sid, 2, cast={"roles": [role("aunt"), role("player", kind="player")]}
    )
    assert [r["id"] for r in replaced.json()["cast"]["roles"]] == ["aunt", "player"]
    reset = await save(client, sid, 3, world=None, metadata={"labels": ["Dark Theme", "RPG"]})
    assert reset.json()["world"] == {
        "description": None,
        "locations": [],
        "entities": [],
        "event_types": None,
    }
    assert reset.json()["metadata"]["labels"] == ["dark theme", "rpg"]
    nothing = await save(client, sid, 4)
    assert (nothing.status_code, nothing.json()["draft_revision"]) == (200, 5)

    for body, field in (
        ({"draft_revision": 5, "mood": "stormy"}, "body.mood"),
        ({"draft_revision": 5, "world": {"secret": "x"}}, "body.world.secret"),
        ({"draft_revision": 5, "public_card": {"name": 7}}, "body.public_card.name"),
        ({"draft_revision": 5, "public_card": {"name": "   "}}, "body.public_card.name"),
        ({"draft_revision": 5, "metadata": {"labels": ["a", "A"]}}, "body.metadata.labels"),
        ({"draft_revision": 5, "metadata": {"labels": ["no/slash"]}}, "body.metadata.labels[0]"),
        (
            {"draft_revision": 5, "cast": {"max_characters_at_start": 0}},
            "body.cast.max_characters_at_start",
        ),
        ({"draft_revision": 5, "schema_version": 2}, "body.schema_version"),
        ({"public_card": {"name": "No revision"}}, "body.draft_revision"),
        ({"draft_revision": 2**31}, "body.draft_revision"),
    ):
        refused = await client.patch(f"{BASE}/{sid}", json=body, headers=WRITE)
        assert refused.status_code == 422, (body, refused.text)
        assert error_of(refused)["details"]["field"] == field, body
    assert (await read(client, sid, draft=True, projection="full")).json()["draft_revision"] == 5


async def test_numbers_in_stored_structures_must_be_finite(client: httpx.AsyncClient) -> None:
    """A number too large for a float (``1e400``) parses as Infinity, which a stored document
    cannot hold: it is refused at its field, on create and on save, rather than silently
    stored as ``null``. Complement: a finite number is stored as sent."""
    sid = (await create(client))["id"]
    huge = b'{"starting_state": {"clock": {"speed": 1e400}}, "public_card": {"name": "Big"}}'
    created = await client.post(
        BASE, content=huge, headers={**WRITE, "Content-Type": "application/json"}
    )
    assert (created.status_code, error_of(created)["details"]["field"]) == (
        422,
        "body.starting_state.clock",
    )
    patched = await client.patch(
        f"{BASE}/{sid}",
        content=b'{"draft_revision": 1, "starting_state": {"clock": {"speed": -1e400}}}',
        headers={**WRITE, "Content-Type": "application/json"},
    )
    assert (patched.status_code, error_of(patched)["details"]["field"]) == (
        422,
        "body.starting_state.clock",
    )
    kept = await save(client, sid, 1, starting_state={"clock": {"speed": 2.5, "skip": -3}})
    assert kept.json()["starting_state"]["clock"] == {"speed": 2.5, "skip": -3}


# -- optimistic concurrency (FR-CAT-3, shared with #103's service) -----------------------------


async def test_stale_revision_is_refused_and_changes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A save or publish based on an older draft revision is 409 ``draft_stale`` naming the
    scenario and the current revision, and writes nothing. Complement: the current revision is
    accepted."""
    sid = (await create(client))["id"]
    assert (await save(client, sid, 1, drivers={"plot_hooks": ["A knock."]})).status_code == 200
    entry, operations = await entry_row(engine, sid), await _operations(engine)
    stale = await save(client, sid, 1, drivers={"plot_hooks": ["Overwritten."]})
    assert (stale.status_code, error_of(stale)["code"]) == (409, "draft_stale")
    assert error_of(stale)["details"] == {
        "scenario_id": sid,
        "draft_revision": 1,
        "current_draft_revision": 2,
    }
    stale_publish = await publish(client, sid, 1)
    assert (stale_publish.status_code, error_of(stale_publish)["code"]) == (409, "draft_stale")
    assert await entry_row(engine, sid) == entry
    assert await version_rows(engine, sid) == []
    assert await _operations(engine) == operations
    fresh = await save(client, sid, 2, drivers={"plot_hooks": ["Overwritten."]})
    assert (fresh.status_code, fresh.json()["drivers"]["plot_hooks"]) == (200, ["Overwritten."])


async def test_two_saves_from_one_revision(client: httpx.AsyncClient) -> None:
    """Two editors save from the same revision at once (both held at the compare-and-set until
    both arrived): exactly one is saved, the other is 409 ``draft_stale``, and the draft is the
    winner's."""
    sid = (await create(client))["id"]
    with pauses.held(pauses.CATALOG_SAVE_BEFORE_CAS) as hold:
        first = asyncio.ensure_future(save(client, sid, 1, drivers={"plot_hooks": ["One."]}))
        second = asyncio.ensure_future(save(client, sid, 1, drivers={"plot_hooks": ["Two."]}))
        await hold.arrived(2)
        hold.release()
        answers = await asyncio.gather(first, second)
    assert sorted(a.status_code for a in answers) == [200, 409]
    winner = next(a for a in answers if a.status_code == 200)
    loser = next(a for a in answers if a.status_code == 409)
    assert error_of(loser)["details"]["current_draft_revision"] == 2
    draft = (await read(client, sid, draft=True, projection="full")).json()
    assert (draft["draft_revision"], draft["drivers"]["plot_hooks"]) == (
        2,
        winner.json()["drivers"]["plot_hooks"],
    )


async def test_two_publishes_of_one_draft(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """Two publishes of one draft revision: the second waits on the row lock the first holds,
    then finds the revision moved (one version, one 409). Under one ``Idempotency-Key`` the
    second gets the first's result."""
    sid = (await create(client))["id"]
    with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
        first = asyncio.ensure_future(publish(client, sid, 1))
        await hold.arrived(1)
        second = asyncio.ensure_future(publish(client, sid, 1))
        await _lock_waiters(engine, 1)
        hold.release()
        won, lost = await asyncio.gather(first, second)
    assert (won.status_code, won.json()["version"]) == (201, 1)
    assert (lost.status_code, error_of(lost)["code"]) == (409, "draft_stale")
    assert len(await version_rows(engine, sid)) == 1

    sid = (await create(client))["id"]
    key = f"publish-{sid}"
    with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
        first = asyncio.ensure_future(publish(client, sid, 1, key=key))
        await hold.arrived(1)
        second = asyncio.ensure_future(publish(client, sid, 1, key=key))
        await asyncio.sleep(0.2)  # the duplicate waits on the key's original
        hold.release()
        original, duplicate = await asyncio.gather(first, second)
    assert original.status_code == duplicate.status_code == 201
    assert duplicate.json() == original.json()
    assert duplicate.headers.get(REPLAY) == "true"
    assert len(await version_rows(engine, sid)) == 1


async def test_published_versions_are_immutable(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The trigger refuses UPDATE, DELETE and TRUNCATE of a published scenario version with
    ``EPI01``. Complement: under ``epistrel.erasure = on`` an UPDATE goes through."""
    sid, _ = await published(client)
    stored = await version_rows(engine, sid)
    for statement in (
        "UPDATE catalog.scenario_versions SET content = '{}'::jsonb WHERE catalog_id = :c",
        "UPDATE catalog.scenario_versions SET published_by = 'p-other' WHERE catalog_id = :c",
        "DELETE FROM catalog.scenario_versions WHERE catalog_id = :c",
    ):
        with pytest.raises(DBAPIError) as caught:
            await execute_in_tx(engine, (statement, {"c": sid}))
        assert sqlstate(caught.value) == "EPI01", statement
    with pytest.raises(DBAPIError) as caught:
        await execute_in_tx(engine, ("TRUNCATE catalog.scenario_versions", {}))
    assert sqlstate(caught.value) == "EPI01"
    assert await version_rows(engine, sid) == stored
    await execute_in_tx(
        engine,
        ("SELECT set_config('epistrel.erasure', 'on', true)", {}),
        (
            "UPDATE catalog.scenario_versions SET published_by = 'p-erased' WHERE catalog_id = :c",
            {"c": sid},
        ),
    )
    assert await version_rows(engine, sid) != stored


# -- the save rules ---------------------------------------------------------------------------


async def test_role_rules_are_enforced_at_save(client: httpx.AsyncClient) -> None:
    """Each role rule refuses a create (and a save) with 422 ``validation_error`` at the field:
    ``locked``/``default`` without a character, ``open`` with one, two player roles, a player
    role that is not ``open``, a duplicate or reserved role id, a ``null`` character version.
    Every finding is listed. Complement: a role of each fill mode, set up as its mode needs,
    saves."""
    character_ref = {"catalog_id": new_ulid(), "version": 2}
    cases: list[tuple[list[dict[str, Any]], str]] = [
        ([role("mother", fill="locked")], "body.cast.roles[0].character"),
        ([role("mother", fill="default")], "body.cast.roles[0].character"),
        ([role("mother", character=character_ref)], "body.cast.roles[0].character"),
        (
            [role("a", kind="player"), role("b", kind="player")],
            "body.cast.roles[1].kind",
        ),
        (
            [role("me", kind="player", fill="locked", character=character_ref)],
            "body.cast.roles[0].fill",
        ),
        ([role("mother"), role("mother")], "body.cast.roles[1].id"),
        ([role("world")], "body.cast.roles[0].id"),
        ([role("public")], "body.cast.roles[0].id"),
        (
            [role("mother", fill="locked", character={"catalog_id": new_ulid(), "version": None})],
            "body.cast.roles[0].character.version",
        ),
        ([role("Mother")], "body.cast.roles[0].id"),
    ]
    for roles, field in cases:
        refused = await client.post(BASE, json=scenario(cast=_cast(*roles)), headers=WRITE)
        assert refused.status_code == 422, (roles, refused.text)
        assert (error_of(refused)["code"], error_of(refused)["details"]["field"]) == (
            "validation_error",
            field,
        ), roles

    several = await client.post(
        BASE,
        json=scenario(
            cast=_cast(role("a", fill="locked"), role("b", kind="player", fill="default"))
        ),
        headers=WRITE,
    )
    assert fields_of(several) == [
        "body.cast.roles[0].character",
        "body.cast.roles[1].fill",
        "body.cast.roles[1].character",
    ]

    created = await create(
        client,
        scenario(
            cast=_cast(
                role("mother", fill="locked", character={"catalog_id": new_ulid()}),
                role("father", fill="default", character=character_ref),
                role("guest"),
                role("npc"),
                role("player", kind="player"),
            )
        ),
    )
    fills = [(r["id"], r["fill"], r.get("character")) for r in created["cast"]["roles"]]
    assert [(i, f) for i, f, _ in fills] == [
        ("mother", "locked"),
        ("father", "default"),
        ("guest", "open"),
        ("npc", "open"),
        ("player", "open"),
    ]
    assert fills[1][2] == character_ref
    assert created["cast"]["roles"][0]["character"]["version"] is None  # floating
    refused = await save(
        client,
        created["id"],
        1,
        cast=_cast(role("guest", kind="player"), role("player", kind="player")),
    )
    assert error_of(refused)["details"]["field"] == "body.cast.roles[1].kind"


async def test_references_are_checked_at_save(client: httpx.AsyncClient) -> None:
    """Relationships, seed holders, starting locations, the opening, the reveals and the
    default viewpoint must name what the scenario declares (a holder may also be ``world``); a
    relationship never names its own role; location, entity and seed ids are unique."""
    good = scenario()
    cases: list[tuple[dict[str, Any], str]] = [
        (
            {"cast": _cast(role("mother", relationships=[{"to_role": "aunt", "name": "sister"}]))},
            "body.cast.roles[0].relationships[0].to_role",
        ),
        (
            {"cast": _cast(role("mother", relationships=[{"to_role": "mother", "name": "self"}]))},
            "body.cast.roles[0].relationships[0].to_role",
        ),
        (
            {"cast": _cast(role("mother", seeded_memories=[seed("A secret.", holder="aunt")]))},
            "body.cast.roles[0].seeded_memories[0].holder",
        ),
        (
            {"cast": _cast(role("mother", starting_location="attic"))},
            "body.cast.roles[0].starting_location",
        ),
        (
            {"starting_state": {"opening": {"starting_location": "attic"}}},
            "body.starting_state.opening.starting_location",
        ),
        (
            {"starting_state": {"opening": {"roles_present": ["mother", "aunt"]}}},
            "body.starting_state.opening.roles_present[1]",
        ),
        (
            {
                "drivers": {
                    "planned_reveals": [{"truth": seed("Hidden."), "allowed_roles": ["aunt"]}]
                }
            },
            "body.drivers.planned_reveals[0].allowed_roles[0]",
        ),
        (
            {"story_settings_defaults": {"default_output_viewpoint": "aunt"}},
            "body.story_settings_defaults.default_output_viewpoint",
        ),
        (
            {"world": {"locations": [{"id": "hall", "name": "Hall"}, {"id": "hall", "name": "H"}]}},
            "body.world.locations[1].id",
        ),
        (
            {"world": {"entities": [{"id": "deed", "name": "Deed"}, {"id": "deed", "name": "D"}]}},
            "body.world.entities[1].id",
        ),
        (
            {"story_settings_defaults": {"unknown_setting": True}},
            "body.story_settings_defaults.unknown_setting",
        ),
    ]
    for groups, field in cases:
        refused = await client.post(BASE, json={**good, **groups}, headers=WRITE)
        assert refused.status_code == 422, (groups, refused.text)
        assert error_of(refused)["details"]["field"] == field, groups
    held = await create(
        client,
        {
            **good,
            "starting_state": {
                "seeded_memories": [seed("Known to all.", holder="world", audience=["public"])],
                "opening": {"starting_location": "kitchen", "roles_present": ["mother"]},
            },
        },
    )
    assert held["starting_state"]["seeded_memories"][0]["holder"] == "world"


async def test_seeds_get_server_ids(client: httpx.AsyncClient) -> None:
    """Every seed is stored with an ``id`` (a ULID): a new one when it has none, its own when
    it is sent back; two seeds with one id are refused. A draft seed may leave out its holder,
    audience and disclosure."""
    created = await create(
        client,
        scenario(
            starting_state={"seeded_memories": [seed("The well is dry."), {"text": "Bare."}]},
            drivers={"planned_reveals": [{"truth": seed("Hidden.")}]},
        ),
    )
    memories = created["starting_state"]["seeded_memories"]
    ids = [m["id"] for m in memories] + [created["drivers"]["planned_reveals"][0]["truth"]["id"]]
    assert all(isinstance(i, str) and len(i) == 26 for i in ids)
    assert len(set(ids)) == 3
    assert (memories[1]["holder"], memories[1]["audience"], memories[1]["disclosure"]) == (
        None,
        None,
        None,
    )

    kept = await save(
        client,
        created["id"],
        1,
        starting_state={"seeded_memories": [*memories, seed("New one.")]},
    )
    stored = kept.json()["starting_state"]["seeded_memories"]
    assert [m["id"] for m in stored[:2]] == ids[:2]
    assert stored[2]["id"] not in ids
    duplicate = await save(
        client,
        created["id"],
        2,
        starting_state={"seeded_memories": [memories[0], memories[0]]},
    )
    assert error_of(duplicate)["details"]["field"] == "body.starting_state.seeded_memories[1].id"
    for bad in ({**seed("x"), "audience": []}, {**seed("x"), "disclosure": "loud"}, {"text": " "}):
        refused = await save(client, created["id"], 2, starting_state={"seeded_memories": [bad]})
        assert refused.status_code == 422, bad


async def test_seed_limit(client: httpx.AsyncClient) -> None:
    """A scenario holds at most 200 seeds, all lists together; the 201st is refused at its
    place. Complement: 200 save."""
    world = [seed(f"Fact {n}.") for n in range(150)]
    roles = _cast(
        role("mother", seeded_memories=[seed(f"Memory {n}.", holder="mother") for n in range(50)]),
        role("player", kind="player"),
    )
    created = await create(client, scenario(cast=roles, starting_state={"seeded_memories": world}))
    refused = await client.post(
        BASE,
        json=scenario(
            cast=roles,
            starting_state={"seeded_memories": world},
            drivers={"planned_reveals": [{"truth": seed("One more.")}]},
        ),
        headers=WRITE,
    )
    assert (refused.status_code, error_of(refused)["details"]["field"]) == (
        422,
        "body.drivers.planned_reveals[0].truth",
    )
    assert len(created["starting_state"]["seeded_memories"]) == 150


async def test_publish_needs_a_name_and_complete_seeds(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Publishing needs ``public_card.name``, a content rating, a role, a world description, an
    opening narration and, on every seed, a holder, an audience and a disclosure: otherwise 422
    ``publish_invalid`` with one report entry per gap (in the report's order: by field), each
    ``rejected``, the seed's entries naming its id, and nothing is written. Complement: once
    complete, it publishes."""
    created = await create(
        client,
        {
            "public_card": {"public_description": "Nameless."},
            "starting_state": {"seeded_memories": [{"text": "Half a fact."}]},
        },
    )
    sid = created["id"]
    seed_id = created["starting_state"]["seeded_memories"][0]["id"]
    entry, operations = await entry_row(engine, sid), await _operations(engine)
    refused = await publish(client, sid, 1)
    assert (refused.status_code, error_of(refused)["code"]) == (422, "publish_invalid")
    report = error_of(refused)["details"]["report"]
    assert [(e["code"], e["location"]["field"]) for e in report["errors"]] == [
        ("role_required", "cast.roles"),
        ("content_rating_missing", "metadata.content_rating_id"),
        ("field_required", "public_card.name"),
        ("opening_required", "starting_state.opening.narration"),
        ("seed_missing_audience", "starting_state.seeded_memories[0].audience"),
        ("seed_missing_disclosure", "starting_state.seeded_memories[0].disclosure"),
        ("seed_missing_holder", "starting_state.seeded_memories[0].holder"),
        ("world_required", "world.description"),
    ]
    assert {e["outcome"] for e in report["errors"]} == {"rejected"}
    assert {e["location"]["entry"] for e in report["errors"]} == {sid}
    assert {e["location"]["version"] for e in report["errors"]} == {"draft"}
    assert [e["location"]["seed_id"] for e in report["errors"][4:7]] == [seed_id] * 3
    assert error_of(refused)["details"]["scenario_id"] == sid
    assert await entry_row(engine, sid) == entry
    assert await version_rows(engine, sid) == []
    assert await _operations(engine) == operations
    whole = scenario("Named")
    fixed = await save(
        client,
        sid,
        1,
        public_card={"name": "Named"},
        metadata=whole["metadata"],
        world=whole["world"],
        cast=whole["cast"],
        starting_state={
            **whole["starting_state"],
            "seeded_memories": [{**seed("Half a fact."), "id": seed_id}],
        },
    )
    assert fixed.status_code == 200, fixed.text
    assert (await publish(client, sid, 2)).status_code == 201


async def test_drafts_are_capped(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft over the cap is 413 ``draft_too_large`` on create and on save, and nothing is
    written. Complement: a draft within the cap saves."""
    monkeypatch.setattr(scenarios, "DRAFT_MAX_BYTES", 3_000)
    big = {"description": "x" * 4_000, "locations": [{"id": "kitchen", "name": "The kitchen"}]}
    too_large = await client.post(BASE, json=scenario(world=big), headers=WRITE)
    assert (too_large.status_code, error_of(too_large)["code"]) == (413, "draft_too_large")
    assert error_of(too_large)["details"]["limit"] == 3_000
    sid = (await create(client))["id"]
    entry = await entry_row(engine, sid)
    grown = await save(client, sid, 1, world=big)
    assert (grown.status_code, error_of(grown)["code"]) == (413, "draft_too_large")
    assert await entry_row(engine, sid) == entry
    assert (await save(client, sid, 1, world={"description": "small"})).status_code == 200


# -- the starting-cast ceiling (FR-SCN-4) -----------------------------------------------------


async def test_the_ceiling_refuses_a_larger_scenario(client: httpx.AsyncClient) -> None:
    """With a deployment ceiling, a scenario saved above it is 422 ``cast_limit_exceeded``
    (``details``: the field, the value and the ceiling) and one at it saves; a scenario that
    sets nothing saves whatever the ceiling. Lowering the ceiling below a stored scenario is
    allowed, and that scenario's next save is refused until its value is lowered. Clearing the
    ceiling (``null``) lifts the limit. The default and the ceiling are deployment settings
    with the planned defaults."""
    current = (await client.get("/admin/settings", headers=READ)).json()
    assert (
        current["max_characters_at_start_default"],
        current["max_characters_at_start_ceiling"],
    ) == (16, None)
    try:
        await set_cast_limits(client, ceiling=20)
        over = await client.post(
            BASE, json=scenario(cast=_cast(max_characters_at_start=21)), headers=WRITE
        )
        assert (over.status_code, error_of(over)["code"]) == (422, "cast_limit_exceeded")
        assert error_of(over)["details"] == {
            "field": "body.cast.max_characters_at_start",
            "max_characters_at_start": 21,
            "ceiling": 20,
        }
        at = await create(client, scenario(cast=_cast(max_characters_at_start=20)))
        unset = await create(client)
        assert unset["cast"]["max_characters_at_start"] is None

        await set_cast_limits(client, ceiling=10)
        refused = await save(client, at["id"], 1, drivers={"plot_hooks": ["Later."]})
        assert (refused.status_code, error_of(refused)["code"]) == (422, "cast_limit_exceeded")
        lowered = await save(client, at["id"], 1, cast={"max_characters_at_start": 10})
        assert lowered.status_code == 200, lowered.text

        await set_cast_limits(client, ceiling=None)
        lifted = await client.post(
            BASE, json=scenario(cast=_cast(max_characters_at_start=1000)), headers=WRITE
        )
        assert lifted.status_code == 201, lifted.text
        bounds = await client.put(
            "/admin/settings", json={"max_characters_at_start_default": 0}, headers=WRITE
        )
        assert error_of(bounds)["details"]["field"] == "body.max_characters_at_start_default"
    finally:
        await set_cast_limits(client, ceiling=None, default=16)


# -- projections (FR-SCN-10) ------------------------------------------------------------------


async def test_card_and_full_projections(client: httpx.AsyncClient) -> None:
    """Every card read (the default, ``projection=card``, a version, the draft) has exactly the
    card's keys, each role exactly its five with the values written (an open, a locked and a
    default role and the player's, each with its kind and fill mode), and none of the phrases
    planted in the world description, a role's seed and the director guidance; ``full`` has
    every group and every phrase, and the whole document as sent (the published version and the
    draft alike). Labels are stored lower-cased, and the card shows the scenario's rating."""
    sent = complete_with(*await cast_characters(client))
    sid, _ = await published(client, sent)
    card_reads: tuple[dict[str, Any], ...] = (
        {},
        {"projection": "card"},
        {"version": 1},
        {"draft": True},
    )
    for params in card_reads:
        card = await read(client, sid, **params)
        assert card.status_code == 200, (params, card.text)
        assert set(card.json()) == CARD_KEYS, params
        assert [set(r) for r in card.json()["roles"]] == [CARD_ROLE_KEYS] * 4, params
        assert card.json()["roles"] == CARD_ROLES, params
        for phrase in PRIVATE_PHRASES:
            assert phrase not in card.text, (params, phrase)
        assert card.json()["labels"] == ["family", "dark theme"]
        assert card.json()["media"] == {"cover": None, "gallery": [], "videos": []}
        rating = card.json()["content_rating"]
        assert (rating["id"], rating["name"]) == (EVERYONE_ID, "Everyone")
    full_reads: tuple[dict[str, Any], ...] = (
        {"projection": "full"},
        {"draft": True, "projection": "full"},
    )
    for params in full_reads:
        full = await read(client, sid, **params)
        assert set(full.json()) >= FULL_KEYS, params
        for phrase in PRIVATE_PHRASES:
            assert phrase in full.text, (params, phrase)
        assert document_of(full.json()) == stored_form(sent), params
        mother = full.json()["cast"]["roles"][0]
        assert mother["eligibility"]["age_range"] == [45, 75]
        assert mother["seeded_memories"][0]["id"]


async def test_card_shows_media_and_rating(pg_url: str, tmp_path: Any) -> None:
    """A scenario's card resolves its media (alt text from the asset, a video's caption from
    the scenario, else the asset's) and its content rating (id, version and name: for a draft
    the rating's current version, for a published version the one it recorded)."""
    async with (
        scratch(pg_url, tmp_path / "media") as space,
        media_client(space.settings, space.db) as client,
    ):
        cover = await uploaded(
            client, headers=WRITE, alt_text="A farmhouse at dusk", caption="Harrow Farm"
        )
        clip = await uploaded(client, headers=WRITE, caption="The asset's own caption")
        film = await uploaded(
            client, video(), content_type="video/mp4", headers=WRITE, kind="video"
        )
        body = scenario(
            public_card={
                "name": "Shown",
                "media": {
                    "cover": cover["id"],
                    "gallery": [clip["id"]],
                    "videos": [{"media_id": film["id"], "caption": "Supper, 1987"}],
                },
            },
            metadata={"labels": [], "content_rating_id": EVERYONE_ID},
        )
        sid, _ = await published(client, body)
        card = (await read(client, sid)).json()
        assert card["media"] == {
            "cover": {
                "media_id": cover["id"],
                "alt_text": "A farmhouse at dusk",
                "caption": "Harrow Farm",
            },
            "gallery": [
                {"media_id": clip["id"], "alt_text": None, "caption": "The asset's own caption"}
            ],
            "videos": [{"media_id": film["id"], "alt_text": None, "caption": "Supper, 1987"}],
        }
        rating = card["content_rating"]
        assert (rating["id"], rating["name"]) == (EVERYONE_ID, "Everyone")
        (recorded,) = await rows(
            space.db,
            "SELECT content_rating_version_id FROM catalog.scenario_versions WHERE catalog_id = :c",
            c=sid,
        )
        assert rating["version_id"] == recorded.content_rating_version_id
        assert (await read(client, sid, draft=True)).json()["content_rating"] == rating


async def test_media_and_rating_references_are_checked(pg_url: str, tmp_path: Any) -> None:
    """A save that adds a retired or unknown media asset is 422 ``media_retired`` at the field;
    an unknown content rating is 422 ``rating_retired``. A draft that shows an asset retired
    since saves on: only the assets a save adds are checked. The media sweep keeps an asset only
    a scenario's draft shows, and one only a published version shows. Complement: the retired
    assets nothing shows are purged by the same sweep."""
    async with (
        scratch(pg_url, tmp_path / "media") as space,
        media_client(space.settings, space.db) as client,
    ):
        versioned = await uploaded(client, headers=WRITE)
        drafted = await uploaded(client, headers=WRITE)
        retired = await uploaded(client, headers=WRITE)
        alone = await uploaded(client, headers=WRITE)
        assert (await client.delete(f"/media/{retired['id']}", headers=WRITE)).status_code == 204
        refused = await client.post(
            BASE,
            json=scenario(public_card={"name": "X", "media": {"gallery": [retired["id"]]}}),
            headers=WRITE,
        )
        assert (refused.status_code, error_of(refused)["code"]) == (422, "media_retired")
        assert error_of(refused)["details"]["field"] == "body.public_card.media.gallery[0]"
        ghost = new_ulid()
        unknown = await client.post(
            BASE,
            json=scenario(public_card={"name": "X", "media": {"videos": [{"media_id": ghost}]}}),
            headers=WRITE,
        )
        assert error_of(unknown)["details"] == {
            "media_id": ghost,
            "field": "body.public_card.media.videos[0].media_id",
            "reason": "unknown",
        }
        rating = await client.post(
            BASE, json=scenario(metadata={"content_rating_id": new_ulid()}), headers=WRITE
        )
        assert (rating.status_code, error_of(rating)["code"]) == (422, "rating_retired")
        assert error_of(rating)["details"]["field"] == "body.metadata.content_rating_id"

        sid, revision = await published(
            client, scenario(public_card={"name": "Y", "media": {"cover": versioned["id"]}})
        )
        moved = await save(
            client,
            sid,
            revision,
            public_card={"media": {"cover": None, "gallery": [drafted["id"]]}},
        )
        assert moved.status_code == 200, moved.text
        for media_id in (versioned["id"], drafted["id"], alone["id"]):
            assert (await client.delete(f"/media/{media_id}", headers=WRITE)).status_code == 204
        later = await save(client, sid, revision + 1, drivers={"plot_hooks": ["Still fine."]})
        assert later.status_code == 200, later.text
        counts = await sweep(space.db, space.lib.media)
        assert counts["assets_purged"] == 2
        for kept in (versioned, drafted):
            assert (await client.get(f"/media/{kept['id']}", headers=READ)).status_code == 200
        for purged in (retired, alone):
            assert (await client.get(f"/media/{purged['id']}", headers=READ)).status_code == 404


async def test_subject_references_are_indexed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Every draft write and publish indexes the catalog characters a scenario names: a role's
    character by id (``declared``) and a character's name in its prose (``mentioned``), on the
    draft (``scenario_catalog``) and each version (``scenario_versions``, ``<id>@<n>``). A save
    rewrites the draft's rows; a delete removes every row of the scenario."""
    name = f"Odile Vance {new_ulid()[-6:]}"
    held = (await client.post("/catalog/characters", json=character(name), headers=WRITE)).json()
    published_held = await client.post(
        f"/catalog/characters/{held['id']}/publish", json={"draft_revision": 1}, headers=WRITE
    )
    assert published_held.status_code == 201, published_held.text  # a locked role's character
    body = scenario(
        cast=_cast(
            role("mother", fill="locked", character={"catalog_id": held["id"]}),
            role("player", kind="player", seeded_memories=[seed(f"{name} lied.", holder="player")]),
        )
    )
    sid, revision = await published(client, body)
    # The character's companion (#116) indexes it too; this scenario's rows are the ones here.
    found = await rows(
        engine,
        "SELECT record_table, record_id, field, ref_kind FROM subject_references "
        "WHERE subject_id = :s AND (record_id = :sid OR record_id LIKE :version) "
        "ORDER BY record_table, field",
        s=held["id"],
        sid=sid,
        version=f"{sid}@%",
    )
    assert [tuple(r) for r in found] == [
        ("scenario_catalog", sid, "cast.roles[0].character.catalog_id", "declared"),
        ("scenario_catalog", sid, "cast.roles[1].seeded_memories[0].text", "mentioned"),
        ("scenario_versions", f"{sid}@1", "cast.roles[0].character.catalog_id", "declared"),
        ("scenario_versions", f"{sid}@1", "cast.roles[1].seeded_memories[0].text", "mentioned"),
    ]
    edited = await save(client, sid, revision, cast=_cast(role("player", kind="player")))
    assert edited.status_code == 200, edited.text
    drafts = await scalar(
        engine,
        "SELECT count(*) FROM subject_references WHERE record_table = 'scenario_catalog' "
        "AND record_id = :s",
        s=sid,
    )
    assert drafts == 0
    assert (await client.delete(f"{BASE}/{sid}", headers=WRITE)).status_code == 204
    left = await scalar(
        engine,
        "SELECT count(*) FROM subject_references WHERE record_id = :s OR record_id LIKE :v",
        s=sid,
        v=f"{sid}@%",
    )
    assert left == 0


# -- reads ------------------------------------------------------------------------------------


async def test_reads_refuse_what_is_not_there(client: httpx.AsyncClient) -> None:
    """Before the first publish only the draft reads (422 ``not_published``); an unknown version
    or scenario is 404 naming ``scenario_id``; ``version`` with ``draft=true``, a malformed
    projection or id are 422 at their field."""
    sid = (await create(client))["id"]
    refused = await read(client, sid)
    assert (refused.status_code, error_of(refused)["code"]) == (422, "not_published")
    assert error_of(refused)["details"] == {"scenario_id": sid}
    missing = await read(client, sid, version=1)
    assert (missing.status_code, error_of(missing)["details"]) == (
        404,
        {"scenario_id": sid, "version": 1},
    )
    for params, field in (
        ({"draft": True, "version": 1}, "query.version"),
        ({"projection": "everything"}, "query.projection"),
        ({"version": 0}, "query.version"),
    ):
        bad = await read(client, sid, **params)
        assert (bad.status_code, error_of(bad)["details"]["field"]) == (422, field), params
    unknown = new_ulid()
    for path in (f"{BASE}/{unknown}", f"{BASE}/{unknown}/versions"):
        found = await client.get(path, headers=READ)
        assert (found.status_code, error_of(found)["code"]) == (404, "not_found")
    malformed = await client.get(f"{BASE}/not-a-ulid", headers=READ)
    assert error_of(malformed)["details"]["field"] == "path.scenario_id"


# -- archive, unarchive, delete ----------------------------------------------------------------


async def test_archive_unarchive_and_delete(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """An archived scenario reads in both projections but cannot be saved or published (422
    ``entry_archived``); archiving again records nothing; unarchive restores ``published``, or
    ``draft``. Delete removes the scenario and its versions (204), drops its relations with a
    ``deleted`` event and writes a ``catalog_entry_deleted`` audit row; a retry under its key
    replays the 204. Complement: another scenario is untouched."""
    sid, revision = await published(client)
    archived = await client.post(f"{BASE}/{sid}/archive", headers=WRITE)
    assert (archived.status_code, archived.json()["status"]) == (200, "archived")
    assert archived.json()["companion_of"] is None
    for params in ({}, {"projection": "full"}, {"draft": True}):
        found = await read(client, sid, **params)
        assert (found.status_code, found.json()["status"]) == (200, "archived"), params
    refused = await save(client, sid, revision, drivers={"plot_hooks": ["No."]})
    assert (refused.status_code, error_of(refused)["code"]) == (422, "entry_archived")
    assert error_of(refused)["details"] == {"scenario_id": sid}
    operations = await _operations(engine)
    again = await client.post(f"{BASE}/{sid}/archive", headers=WRITE)
    assert again.status_code == 200
    assert await _operations(engine) == operations
    restored = await client.post(f"{BASE}/{sid}/unarchive", headers=WRITE)
    assert restored.json()["status"] == "published"
    draft_only = (await create(client))["id"]
    await client.post(f"{BASE}/{draft_only}/archive", headers=WRITE)
    assert (await client.post(f"{BASE}/{draft_only}/unarchive", headers=WRITE)).json()[
        "status"
    ] == "draft"

    other, _ = await published(client, scenario("Other"))
    other_entry, other_versions = await entry_row(engine, other), await version_rows(engine, other)
    related = await client.post(
        f"{BASE}/{sid}/relations", json={"principal": "p-ed", "relation": "editor"}, headers=WRITE
    )
    assert related.status_code == 201, related.text
    key = f"delete-{sid}"
    deleted = await client.delete(f"{BASE}/{sid}", headers=headers(key))
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert await entry_row(engine, sid) is None
    assert await version_rows(engine, sid) == []
    (audit,) = await rows(
        engine,
        "SELECT principal, details FROM audit_log "
        "WHERE kind = 'catalog_entry_deleted' AND details->>'entity_id' = :s",
        s=sid,
    )
    assert (audit.principal, audit.details["entity_type"]) == (PRINCIPAL, "scenario")
    events = await rows(
        engine,
        "SELECT kind, old FROM ownership_events WHERE entity_id = :s ORDER BY at, id",
        s=sid,
    )
    assert [e.kind for e in events] == ["relation_added", "deleted"]
    assert events[-1].old["relations"] == [{"principal": "p-ed", "relation": "editor"}]
    replay = await client.delete(f"{BASE}/{sid}", headers=headers(key))
    assert (replay.status_code, replay.headers.get(REPLAY)) == (204, "true")
    gone = await client.delete(f"{BASE}/{sid}", headers=WRITE)
    assert (gone.status_code, error_of(gone)["code"]) == (404, "not_found")
    assert await entry_row(engine, other) == other_entry
    assert await version_rows(engine, other) == other_versions


async def test_every_write_is_idempotent_and_attributed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Each mutation runs under its ``Idempotency-Key`` (deployment scope): a retry replays the
    stored answer and writes nothing more; each committed write is one story-less ``control``
    operation by the requester, who need not be the owner; the same key with another request is
    409 ``idempotency_conflict``."""
    tag = new_ulid()
    body = {**scenario("Attributed"), "owner": "p-owner"}
    created = await client.post(BASE, json=body, headers=headers(f"create-{tag}"))
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert (await entry_row(engine, sid)).owner_principal == "p-owner"
    calls: list[tuple[str, str, str, dict[str, Any] | None, str]] = [
        ("create", "POST", BASE, body, PRINCIPAL),
        ("save", "PATCH", f"{BASE}/{sid}", {"draft_revision": 1}, "p-editor"),
        ("publish", "POST", f"{BASE}/{sid}/publish", {"draft_revision": 2}, "p-editor"),
        ("archive", "POST", f"{BASE}/{sid}/archive", None, "p-reviewer"),
        ("unarchive", "POST", f"{BASE}/{sid}/unarchive", None, "p-reviewer"),
    ]
    for name, method, path, json, principal in calls:
        key = f"{name}-{tag}"
        sent = headers(key, principal=principal)
        first = (
            created
            if name == "create"
            else await client.request(method, path, json=json, headers=sent)
        )
        assert first.is_success, (name, first.text)
        before = await _operations(engine)
        again = await client.request(method, path, json=json, headers=sent)
        assert (again.status_code, again.headers.get(REPLAY)) == (first.status_code, "true")
        assert again.json() == first.json(), name
        assert await _operations(engine) == before, name
        (operation,) = await rows(
            engine,
            "SELECT kind, status, principal, story_id FROM operations WHERE idempotency_key = :k",
            k=key,
        )
        assert tuple(operation) == ("control", "committed", principal, None), name
    listed = (await client.get(f"{BASE}/{sid}/versions", headers=READ)).json()["items"]
    assert [v["published_by"] for v in listed] == ["p-editor"]
    reused = await client.post(BASE, json=scenario("Else"), headers=headers(f"create-{tag}"))
    assert (reused.status_code, error_of(reused)["code"]) == (409, "idempotency_conflict")
    for owner in ("system", "   "):
        refused = await client.post(BASE, json={**scenario(), "owner": owner}, headers=WRITE)
        assert error_of(refused)["details"]["field"] == "body.owner", owner


async def test_library_matches_rest(settings: Settings, engine: AsyncEngine) -> None:
    """The in-process library raises the errors REST answers with, and needs a principal."""
    lib = Engine(settings, db=engine)
    ctx = RequestContext(principal=PRINCIPAL)
    created = await lib.create_scenario_draft(ctx, ScenarioCreate.model_validate(scenario("Lib")))
    sid = created.scenario.id
    assert all(s.id is not None for r in created.scenario.cast.roles for s in r.seeded_memories)
    with pytest.raises(NotPublished):
        await lib.get_scenario(ctx, sid)
    await lib.save_scenario_draft(ctx, sid, ScenarioPatch(draft_revision=1))
    with pytest.raises(DraftStale):
        await lib.publish_scenario(ctx, sid, PublishRequest(draft_revision=1))
    with pytest.raises(PrincipalRequired):
        await lib.archive_scenario(RequestContext(), sid)
    done = await lib.publish_scenario(ctx, sid, PublishRequest(draft_revision=2))
    card = await lib.get_scenario(ctx, sid)
    assert isinstance(card, ScenarioCard)
    assert (card.version, done.version, card.name) == (1, 1, "Lib")
    assert [v.version for v in await lib.list_scenario_versions(ctx, sid)] == [1]
    assert (await lib.delete_scenario(ctx, sid)).versions == 1
    with pytest.raises(ScenarioNotFound) as missing:
        await lib.get_scenario(ctx, sid)
    assert missing.value.details == {"scenario_id": sid}
