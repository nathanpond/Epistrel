"""Catalog characters over REST (#103): drafts with optimistic concurrency, immutable published
versions, the ``card``/``full`` projections, archive, unarchive and delete, idempotency and
attribution. The milestone parts are ``tests/conformance/test_c_15.py`` (the lifecycle) and
``tests/integration/test_i_17.py`` (stories untouched)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import DraftStale, Engine, NotPublished, PrincipalRequired, RequestContext
from epistrel.config import Settings
from epistrel.core.catalog import characters
from epistrel.core.ids import new_ulid
from epistrel.testing import pauses
from tests.catalog_support import (
    BASE,
    CARD_KEYS,
    PRINCIPAL,
    READ,
    VERSION_KEYS,
    WRITE,
    character,
    count,
    create,
    entry_row,
    headers,
    publish,
    published,
    read,
    save,
    version_rows,
)
from tests.event_support import execute_in_tx, rows, scalar, sqlstate
from tests.story_support import rest

REPLAY = "Idempotent-Replay"
CONTENT_KEYS = (
    "id",
    "version",
    "published_at",
    "name",
    "public_description",
    "images",
    "labels",
    "content_rating",
    "definition",
)
"""What a version read shows of the version itself (the rest is the entry's envelope)."""


@pytest.fixture
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with rest(settings, engine) as found:
        yield found


def _content(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in CONTENT_KEYS}


def _error(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


async def _operations(engine: AsyncEngine) -> int:
    return await count(engine, "SELECT count(*) FROM operations")


async def _lock_waiters(engine: AsyncEngine, wanted: int, within: float = 10.0) -> None:
    """Return once ``wanted`` backends of this database wait on a lock."""
    deadline = time.monotonic() + within
    waiting = (
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    while await count(engine, waiting) < wanted:
        assert time.monotonic() < deadline, "no request waited on the lock in time"
        await asyncio.sleep(0.02)


# -- the tables (#103 AC1) ----------------------------------------------------------------------


async def test_catalog_tables_have_the_planned_shape(engine: AsyncEngine) -> None:
    """``catalog.character_catalog`` and ``catalog.character_versions`` hold the planned columns
    (the scenario tables beside them are ``test_scenario_catalog``'s); a version is unique per
    ``(catalog_id, version)`` and goes with its entry; the status is the enum
    ``catalog.entry_status``; stories name their catalog source by identifiers only."""
    found = await rows(
        engine,
        "SELECT table_name, column_name, udt_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'catalog' AND table_name LIKE 'character%' "
        "ORDER BY table_name, ordinal_position",
    )
    columns: dict[str, list[str]] = {}
    for row in found:
        columns.setdefault(row.table_name, []).append(row.column_name)
    assert columns == {
        "character_catalog": [
            "id",
            "owner_principal",
            "listing_status",
            "status",
            "current_version",
            "draft",
            "draft_revision",
            "created_at",
            "updated_at",
        ],
        "character_versions": [
            "catalog_id",
            "version",
            "content",
            "content_rating_version_id",
            "published_at",
            "published_by",
            "search",
        ],
    }
    types = {(r.table_name, r.column_name): (r.udt_name, r.is_nullable) for r in found}
    assert types[("character_catalog", "status")] == ("entry_status", "NO")
    assert types[("character_catalog", "draft")] == ("jsonb", "NO")
    assert types[("character_catalog", "current_version")][1] == "YES"
    assert types[("character_versions", "content")] == ("jsonb", "NO")
    assert types[("character_versions", "content_rating_version_id")][1] == "YES"
    assert types[("character_versions", "search")] == ("tsvector", "YES")  # generated (#110)
    constraints = await rows(
        engine,
        "SELECT conname, contype, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conrelid = 'catalog.character_versions'::regclass ORDER BY conname",
    )
    cascade = "FOREIGN KEY (catalog_id) REFERENCES catalog.character_catalog(id) ON DELETE CASCADE"
    assert [(c.contype, c.definition) for c in constraints] == [
        ("f", cascade),
        ("p", "PRIMARY KEY (catalog_id, version)"),
    ]
    provenance = await rows(
        engine,
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'characters' "
        "AND column_name LIKE 'source_catalog%' ORDER BY column_name",
    )
    assert [tuple(r) for r in provenance] == [
        ("source_catalog_id", "YES"),
        ("source_catalog_version", "YES"),
    ]


# -- lifecycle ----------------------------------------------------------------------------------


async def test_draft_save_publish_and_versions(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Create is a draft at revision 1; each save bumps the revision; publish makes version
    n + 1 from the draft, stores the entry as ``published`` at that version and leaves the draft
    equal to it, one revision on; while a saved edit is unpublished, the default card and the
    ``full`` read still serve the latest published version and only a draft read shows the edit;
    version 1 reads unchanged after version 2 exists; the version list gives each version's
    number, publish time and publisher, and nothing else (FR-CAT-1, FR-CAT-3)."""
    created = await create(client)
    cid = created["id"]
    assert created["draft_revision"] == 1
    assert (created["status"], created["listing_status"]) == ("draft", "private")
    assert (created["version"], created["current_version"], created["published_at"]) == (
        None,
        None,
        None,
    )
    # The companion scenario created with it (#116) is named; its own tests are
    # ``tests/integration/test_companion.py``.
    assert created["companion_id"] == await scalar(
        engine, "SELECT id FROM catalog.scenario_catalog WHERE companion_of = :c", c=cid
    )
    assert isinstance(created["config_revision"], int)
    assert created["idempotency_key"]

    saved = await save(client, cid, 1, public_description="The first description.")
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft_revision"] == 2

    first = await publish(client, cid, 2)
    assert first.status_code == 201, first.text
    assert {k: first.json()[k] for k in ("id", "version", "draft_revision", "status")} == {
        "id": cid,
        "version": 1,
        "draft_revision": 3,
        "status": "published",
    }
    assert first.json()["report"] == {"errors": [], "warnings": [], "unavailable": []}
    # The publish is stored, not just answered: the entry row and a fresh read are published.
    stored_entry = await entry_row(engine, cid)
    assert (stored_entry.status, stored_entry.current_version, stored_entry.draft_revision) == (
        "published",
        1,
        3,
    )
    card = (await read(client, cid)).json()
    assert (card["status"], card["version"]) == ("published", 1)

    version_1 = (await read(client, cid, version=1, projection="full")).json()
    assert (version_1["status"], version_1["published_at"]) == (
        "published",
        first.json()["published_at"],
    )
    draft = (await read(client, cid, draft=True, projection="full")).json()
    assert draft["draft_revision"] == 3
    (stored_pair,) = await rows(
        engine,
        "SELECT c.draft = v.content AS same FROM catalog.character_catalog c "
        "JOIN catalog.character_versions v ON v.catalog_id = c.id WHERE c.id = :c",
        c=cid,
    )
    assert stored_pair.same  # the draft is the published document
    assert {k: draft[k] for k in ("name", "public_description", "definition")} == {
        k: version_1[k] for k in ("name", "public_description", "definition")
    }
    stored = await version_rows(engine, cid)

    saved = await save(
        client,
        cid,
        3,
        public_description="The second description.",
        definition={"personality": "Wry, these days."},
    )
    assert saved.status_code == 200, saved.text
    # The draft now differs from version 1 in its description and its private definition.
    # Readers still get version 1 in both projections; only a draft read shows the edit.
    shown_card = (await read(client, cid)).json()
    assert (shown_card["version"], shown_card["public_description"]) == (
        1,
        "The first description.",
    )
    shown_full = (await read(client, cid, projection="full")).json()
    assert _content(shown_full) == _content(version_1)
    assert "draft_revision" not in shown_full
    draft_card = (await read(client, cid, draft=True)).json()
    assert (draft_card["version"], draft_card["public_description"]) == (
        1,
        "The second description.",
    )
    draft_full = (await read(client, cid, draft=True, projection="full")).json()
    assert (
        draft_full["version"],
        draft_full["draft_revision"],
        draft_full["public_description"],
        draft_full["definition"],
    ) == (
        1,
        4,
        "The second description.",
        # The patch merged into the definition: only the personality changed.
        {**created["definition"], "personality": "Wry, these days."},
    )
    second = await publish(client, cid, 4)
    assert (second.status_code, second.json()["version"]) == (201, 2)

    assert _content((await read(client, cid, version=1, projection="full")).json()) == _content(
        version_1
    )
    assert (await version_rows(engine, cid))[:1] == stored
    latest = (await read(client, cid, projection="full")).json()
    assert (latest["version"], latest["current_version"], latest["published_at"]) == (
        2,
        2,
        second.json()["published_at"],
    )
    assert latest["public_description"] == "The second description."
    assert "draft_revision" not in latest

    listed = await client.get(f"{BASE}/{cid}/versions", headers=READ)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [set(item) for item in items] == [VERSION_KEYS, VERSION_KEYS]
    assert [(v["version"], v["published_at"], v["published_by"]) for v in items] == [
        (1, first.json()["published_at"], PRINCIPAL),
        (2, second.json()["published_at"], PRINCIPAL),
    ]

    # Publishing an unchanged draft still makes a version.
    third = await publish(client, cid, 5)
    assert (third.status_code, third.json()["version"]) == (201, 3)


async def test_save_is_a_json_merge_patch(client: httpx.AsyncClient) -> None:
    """``null`` removes a field, ``definition`` merges key by key, a patch that changes nothing
    still moves the revision, and unknown keys, wrong types and a missing revision are 422 at
    their field (FR-CAT-3)."""
    created = await create(client)
    cid = created["id"]

    removed = await save(client, cid, 1, public_description=None)
    assert removed.status_code == 200, removed.text
    assert removed.json()["public_description"] is None
    assert removed.json()["definition"] == created["definition"]

    merged = await save(client, cid, 2, definition={"personality": None})
    # Only the personality went: the rest of the definition merged as it was.
    assert merged.json()["definition"] == {**created["definition"], "personality": None}
    assert merged.json()["name"] == created["name"]

    nothing = await save(client, cid, 3)
    assert (nothing.status_code, nothing.json()["draft_revision"]) == (200, 4)
    bodies: tuple[dict[str, Any] | None, ...] = ({}, None)
    for body in bodies:
        unpublished = await client.post(f"{BASE}/{cid}/publish", json=body, headers=WRITE)
        assert unpublished.status_code == 422, body
        assert _error(unpublished)["details"]["field"].startswith("body"), body

    for body, field in (
        ({"draft_revision": 4, "mood": "stormy"}, "body.mood"),
        ({"draft_revision": 4, "public_card": {"name": 7}}, "body.public_card.name"),
        # #103's top-level name moved under the public card (#115).
        ({"draft_revision": 4, "name": "Top level"}, "body.name"),
        ({"draft_revision": 4, "definition": {"secret": "x"}}, "body.definition.secret"),
        ({"public_card": {"name": "No revision"}}, "body.draft_revision"),
        ({"draft_revision": 2**31}, "body.draft_revision"),
        ({"draft_revision": "4"}, "body.draft_revision"),
    ):
        refused = await client.patch(f"{BASE}/{cid}", json=body, headers=WRITE)
        assert refused.status_code == 422, (body, refused.text)
        assert _error(refused)["details"]["field"] == field, body
    assert (await read(client, cid, draft=True, projection="full")).json()["draft_revision"] == 4


# -- optimistic concurrency (FR-CAT-3) ------------------------------------------------------------


async def test_stale_revision_is_refused_and_changes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A save or publish based on an older draft revision is 409 ``draft_stale`` naming the
    current revision, and writes nothing. Complement: the current revision is accepted."""
    cid = (await create(client))["id"]
    assert (await save(client, cid, 1, name="Mara")).status_code == 200
    entry = await entry_row(engine, cid)
    operations = await _operations(engine)

    stale = await save(client, cid, 1, name="Overwritten")
    assert stale.status_code == 409
    assert _error(stale)["code"] == "draft_stale"
    assert _error(stale)["details"] == {
        "character_id": cid,
        "draft_revision": 1,
        "current_draft_revision": 2,
    }
    stale_publish = await publish(client, cid, 1)
    assert (stale_publish.status_code, _error(stale_publish)["code"]) == (409, "draft_stale")
    assert await entry_row(engine, cid) == entry
    assert await version_rows(engine, cid) == []
    assert await _operations(engine) == operations

    fresh = await save(client, cid, 2, name="Overwritten")
    assert (fresh.status_code, fresh.json()["name"]) == (200, "Overwritten")


async def test_two_saves_from_one_revision(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """Two editors save from the same revision at the same moment: both read revision 1 and
    reach the compare-and-set (held there until both arrived), exactly one is saved and the
    other is 409 ``draft_stale``; the draft is the winner's, nobody's change is lost silently."""
    cid = (await create(client))["id"]
    with pauses.held(pauses.CATALOG_SAVE_BEFORE_CAS) as hold:
        first = asyncio.ensure_future(save(client, cid, 1, public_description="Editor one."))
        second = asyncio.ensure_future(save(client, cid, 1, public_description="Editor two."))
        await hold.arrived(2)
        hold.release()
        answers = await asyncio.gather(first, second)
    assert sorted(a.status_code for a in answers) == [200, 409]
    winner = next(a for a in answers if a.status_code == 200)
    loser = next(a for a in answers if a.status_code == 409)
    assert _error(loser)["code"] == "draft_stale"
    assert _error(loser)["details"]["current_draft_revision"] == 2
    draft = (await read(client, cid, draft=True, projection="full")).json()
    assert (draft["draft_revision"], draft["public_description"]) == (
        2,
        winner.json()["public_description"],
    )


async def test_two_publishes_of_one_draft(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """Two publishes of the same draft revision: the second waits on the entry's row lock
    while the first holds it, then finds the revision moved: one version and one 409
    ``draft_stale``. Under the same ``Idempotency-Key`` the second gets the first's result."""
    cid = (await create(client))["id"]
    with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
        first = asyncio.ensure_future(publish(client, cid, 1))
        await hold.arrived(1)
        second = asyncio.ensure_future(publish(client, cid, 1))
        await _lock_waiters(engine, 1)
        hold.release()
        won, lost = await asyncio.gather(first, second)
    assert (won.status_code, won.json()["version"]) == (201, 1)
    assert (lost.status_code, _error(lost)["code"]) == (409, "draft_stale")
    assert len(await version_rows(engine, cid)) == 1

    cid = (await create(client))["id"]
    key = f"publish-{cid}"
    with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
        first = asyncio.ensure_future(publish(client, cid, 1, key=key))
        await hold.arrived(1)
        second = asyncio.ensure_future(publish(client, cid, 1, key=key))
        await asyncio.sleep(0.2)  # the duplicate is waiting on the key's original
        hold.release()
        original, duplicate = await asyncio.gather(first, second)
    assert original.status_code == duplicate.status_code == 201
    assert duplicate.json() == original.json()
    assert duplicate.headers.get(REPLAY) == "true"
    assert len(await version_rows(engine, cid)) == 1


# -- immutability -----------------------------------------------------------------------------


async def test_published_versions_are_immutable(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The trigger refuses UPDATE (of ``content`` or any column), DELETE and TRUNCATE of a
    published version with ``EPI01``. A plain TRUNCATE is refused before the trigger since a
    scenario version's pin references the table (#108: ``0A000``), so the trigger's refusal is
    shown with ``TRUNCATE … CASCADE``. Complement: under ``epistrel.erasure = on`` (the
    exemption delete and #124's erasure use) an UPDATE goes through."""
    cid, _ = await published(client)
    stored = await version_rows(engine, cid)
    for statement in (
        "UPDATE catalog.character_versions SET content = '{}'::jsonb WHERE catalog_id = :c",
        "UPDATE catalog.character_versions SET published_by = 'p-other' WHERE catalog_id = :c",
        "DELETE FROM catalog.character_versions WHERE catalog_id = :c",
    ):
        with pytest.raises(DBAPIError) as caught:
            await execute_in_tx(engine, (statement, {"c": cid}))
        assert sqlstate(caught.value) == "EPI01", statement
    with pytest.raises(DBAPIError) as caught:
        await execute_in_tx(engine, ("TRUNCATE catalog.character_versions", {}))
    assert sqlstate(caught.value) == "0A000"  # referenced by scenario_versions' pin (#108)
    with pytest.raises(DBAPIError) as caught:
        await execute_in_tx(engine, ("TRUNCATE catalog.character_versions CASCADE", {}))
    assert sqlstate(caught.value) == "EPI01"
    assert await version_rows(engine, cid) == stored

    await execute_in_tx(
        engine,
        ("SELECT set_config('epistrel.erasure', 'on', true)", {}),
        (
            "UPDATE catalog.character_versions SET published_by = 'p-erased' WHERE catalog_id = :c",
            {"c": cid},
        ),
    )
    assert await version_rows(engine, cid) != stored


# -- projections (FR-SCN-10) -----------------------------------------------------------------------


async def test_card_never_shows_a_definition_field(client: httpx.AsyncClient) -> None:
    """Every card read — the default, ``projection=card``, a version, the draft — has exactly
    the card's keys and never the phrase planted in the private personality; ``full`` has it
    (FR-SCN-10)."""
    phrase = "hides the forged tide tables under the third stair"
    body = character(definition={"personality": f"Warm, though she {phrase}."})
    cid, _ = await published(client, body)
    for params in (
        {},
        {"projection": "card"},
        {"version": 1},
        {"draft": True},
        {"draft": True, "projection": "card"},
    ):
        card = await read(client, cid, **params)
        assert card.status_code == 200, (params, card.text)
        assert set(card.json()) == CARD_KEYS, params
        assert phrase not in card.text, params
        assert (card.json()["images"], card.json()["labels"]) == (
            {"primary": None, "gallery": []},
            [],
        )
        # A published character carries a rating (#115): the card shows its name and version.
        assert card.json()["content_rating"]["name"] == "Everyone"
    for params in ({"projection": "full"}, {"draft": True, "projection": "full"}):
        full = await read(client, cid, **params)
        assert phrase in full.json()["definition"]["personality"], params
        assert set(full.json()) >= CARD_KEYS | {"definition", "created_at", "updated_at"}


async def test_reads_refuse_what_is_not_there(client: httpx.AsyncClient) -> None:
    """Before the first publish only the draft reads (422 ``not_published`` otherwise); an
    unknown version or entry is 404; ``version`` with ``draft=true`` and a malformed projection
    or id are 422 at their field."""
    cid = (await create(client))["id"]
    for params in ({}, {"projection": "full"}):
        refused = await read(client, cid, **params)
        assert (refused.status_code, _error(refused)["code"]) == (422, "not_published")
    draft = await read(client, cid, draft=True)
    assert (draft.status_code, draft.json()["version"], draft.json()["status"]) == (
        200,
        None,
        "draft",
    )
    missing = await read(client, cid, version=1)
    assert (missing.status_code, _error(missing)["code"]) == (404, "not_found")
    assert _error(missing)["details"] == {"character_id": cid, "version": 1}
    refusals: tuple[tuple[dict[str, Any], str], ...] = (
        ({"draft": True, "version": 1}, "query.version"),
        ({"projection": "everything"}, "query.projection"),
        ({"version": 0}, "query.version"),
        ({"version": 2**31}, "query.version"),
    )
    for params, field in refusals:
        refused = await read(client, cid, **params)
        assert refused.status_code == 422, params
        assert _error(refused)["details"]["field"] == field, params
    unknown = new_ulid()
    for path in (f"{BASE}/{unknown}", f"{BASE}/{unknown}/versions"):
        found = await client.get(path, headers=READ)
        assert (found.status_code, _error(found)["code"]) == (404, "not_found")
    malformed = await client.get(f"{BASE}/not-a-ulid", headers=READ)
    assert _error(malformed)["details"]["field"] == "path.character_id"


# -- archive, unarchive, delete -------------------------------------------------------------------


async def test_archive_and_unarchive(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """An archived entry reads in both projections but cannot be saved or published (422
    ``entry_archived``); archiving again changes nothing; unarchive restores ``published``, or
    ``draft`` for an entry never published."""
    cid, revision = await published(client)
    archived = await client.post(f"{BASE}/{cid}/archive", headers=WRITE)
    assert archived.status_code == 200, archived.text
    assert (archived.json()["status"], archived.json()["current_version"]) == ("archived", 1)
    for params in ({}, {"projection": "full"}, {"draft": True, "projection": "full"}):
        found = await read(client, cid, **params)
        assert (found.status_code, found.json()["status"]) == (200, "archived"), params
    refused = await save(client, cid, revision, name="Edited while archived")
    assert (refused.status_code, _error(refused)["code"]) == (422, "entry_archived")
    refused = await publish(client, cid, revision)
    assert (refused.status_code, _error(refused)["code"]) == (422, "entry_archived")

    operations = await _operations(engine)
    again = await client.post(f"{BASE}/{cid}/archive", headers=WRITE)
    assert again.status_code == 200
    assert {**again.json(), "idempotency_key": None} == {**archived.json(), "idempotency_key": None}
    assert await _operations(engine) == operations  # nothing changed, nothing recorded

    restored = await client.post(f"{BASE}/{cid}/unarchive", headers=WRITE)
    assert (restored.status_code, restored.json()["status"]) == (200, "published")
    assert (await save(client, cid, revision, name="Edited again")).status_code == 200
    unchanged = await client.post(f"{BASE}/{cid}/unarchive", headers=WRITE)
    assert (unchanged.status_code, unchanged.json()["status"]) == (200, "published")

    draft_only = (await create(client))["id"]
    await client.post(f"{BASE}/{draft_only}/archive", headers=WRITE)
    restored = await client.post(f"{BASE}/{draft_only}/unarchive", headers=WRITE)
    assert restored.json()["status"] == "draft"


async def test_delete_removes_the_entry_and_its_versions(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Delete removes the entry and every version (204, from any status) and writes a
    ``catalog_entry_deleted`` audit row naming the entry and the principal; its key is bound in
    the deployment scope, and a retry under it replays the 204. Complement: another entry and its
    versions are untouched (count and content)."""
    cid, revision = await published(client)
    assert (await save(client, cid, revision, name="Second")).status_code == 200
    assert (await publish(client, cid, revision + 1)).status_code == 201
    await client.post(f"{BASE}/{cid}/archive", headers=WRITE)
    other, _ = await published(client, character("Wren Hale"))
    other_entry, other_versions = await entry_row(engine, other), await version_rows(engine, other)
    entries = await count(engine, "SELECT count(*) FROM catalog.character_catalog")

    key = f"delete-{cid}"
    deleted = await client.delete(f"{BASE}/{cid}", headers=headers(key))
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert deleted.headers["Idempotency-Key"] == key
    assert (await read(client, cid, draft=True)).status_code == 404
    assert await entry_row(engine, cid) is None
    assert await version_rows(engine, cid) == []
    assert await count(engine, "SELECT count(*) FROM catalog.character_catalog") == entries - 1

    (audit,) = await rows(
        engine,
        "SELECT principal, story_id, details FROM audit_log "
        "WHERE kind = 'catalog_entry_deleted' AND details->>'entity_id' = :c",
        c=cid,
    )
    assert (audit.principal, audit.story_id) == (PRINCIPAL, None)
    assert {k: audit.details[k] for k in ("entity_type", "entity_id")} == {
        "entity_type": "character",
        "entity_id": cid,
    }
    (operation,) = await rows(
        engine,
        "SELECT kind, status, principal, story_id, idempotency_key FROM operations "
        "WHERE operation_id = :o",
        o=audit.details["operation_id"],
    )
    assert tuple(operation) == ("control", "committed", PRINCIPAL, None, key)
    (bound,) = await rows(
        engine, "SELECT scope, scope_id, status FROM idempotency_keys WHERE key = :k", k=key
    )
    assert tuple(bound) == ("deployment", "deployment", "done")

    replay = await client.delete(f"{BASE}/{cid}", headers=headers(key))
    assert (replay.status_code, replay.headers.get(REPLAY)) == (204, "true")
    gone = await client.delete(f"{BASE}/{cid}", headers=WRITE)
    assert (gone.status_code, _error(gone)["code"]) == (404, "not_found")

    assert await entry_row(engine, other) == other_entry
    assert await version_rows(engine, other) == other_versions


# -- idempotency and attribution (#61, #44, FR-AUTH-5) ---------------------------------------------


async def test_every_write_is_idempotent_and_attributed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Each mutation runs under its ``Idempotency-Key`` (deployment scope): a retry replays the
    stored answer with ``Idempotent-Replay: true`` and writes nothing more; each committed write
    is one story-less ``control`` operation recording the requesting principal; the same key
    with another request is 409 ``idempotency_conflict``."""
    tag = new_ulid()
    body = character("Ilse Marr")
    created = await client.post(BASE, json=body, headers=headers(f"create-{tag}"))
    assert created.status_code == 201
    cid = created.json()["id"]
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = [
        ("create", "POST", BASE, body),
        ("save", "PATCH", f"{BASE}/{cid}", {"draft_revision": 1, "public_card": {"name": "Ilse"}}),
        ("publish", "POST", f"{BASE}/{cid}/publish", {"draft_revision": 2}),
        ("archive", "POST", f"{BASE}/{cid}/archive", None),
        ("unarchive", "POST", f"{BASE}/{cid}/unarchive", None),
    ]
    for name, method, path, json in calls:
        key = f"{name}-{tag}"
        if name == "create":
            first = created
        else:
            first = await client.request(method, path, json=json, headers=headers(key))
        assert first.is_success, (name, first.text)
        before = await _operations(engine)
        again = await client.request(method, path, json=json, headers=headers(key))
        assert (again.status_code, again.headers.get(REPLAY)) == (first.status_code, "true")
        assert again.json() == first.json(), name
        assert await _operations(engine) == before, name
        (operation,) = await rows(
            engine,
            "SELECT kind, status, principal, story_id FROM operations WHERE idempotency_key = :k",
            k=key,
        )
        assert tuple(operation) == ("control", "committed", PRINCIPAL, None), name
        (bound,) = await rows(
            engine,
            "SELECT scope, scope_id, status FROM idempotency_keys WHERE key = :k",
            k=key,
        )
        assert tuple(bound) == ("deployment", "deployment", "done"), name

    reused = await client.post(
        BASE, json=character("Someone Else"), headers=headers(f"create-{tag}")
    )
    assert (reused.status_code, _error(reused)["code"]) == (409, "idempotency_conflict")
    assert (
        await count(
            engine,
            "SELECT count(*) FROM catalog.character_catalog WHERE draft->>'name' = 'Someone Else'",
        )
        == 0
    )


async def test_owner_is_the_requester_unless_named(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Creation records the requesting principal as the owner, or the ``owner`` the body names
    (trimmed, by the principal rule; never ``system``), while the control operation still
    records who asked (FR-AUTH-5)."""
    mine = await create(client)
    assert (await entry_row(engine, mine["id"])).owner_principal == PRINCIPAL

    key = f"owned-{new_ulid()}"
    named = await client.post(
        BASE, json=character(owner="  p-someone-else\t"), headers=headers(key)
    )
    assert named.status_code == 201, named.text
    assert (await entry_row(engine, named.json()["id"])).owner_principal == "p-someone-else"
    (operation,) = await rows(
        engine, "SELECT principal FROM operations WHERE idempotency_key = :k", k=key
    )
    assert operation.principal == PRINCIPAL

    for owner in ("system", "   ", "a\x01b", "x" * 257):
        refused = await client.post(BASE, json=character(owner=owner), headers=WRITE)
        assert refused.status_code == 422, owner
        assert _error(refused)["details"]["field"] == "body.owner", owner


async def test_writes_record_the_requester_not_the_owner(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Ownership confers no permission (FR-AUTH-8), so principals other than the owner edit an
    entry as a matter of course. Each write records the principal that asked (FR-AUTH-4), never
    the owner or the creator: the control operation of every save, publish, archive, unarchive
    and delete, each version's ``published_by``, and the delete's audit row. The owner stays as
    created (FR-AUTH-5)."""
    owner, editor, reviewer = "p-owner", "p-editor", "p-reviewer"
    tag = new_ulid()
    created = await client.post(BASE, json=character(owner=owner), headers=headers(f"create-{tag}"))
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    writes: list[tuple[str, str, str, dict[str, Any] | None, str]] = [
        (
            "save",
            "PATCH",
            f"{BASE}/{cid}",
            {"draft_revision": 1, "public_card": {"name": "Edited"}},
            editor,
        ),
        ("publish", "POST", f"{BASE}/{cid}/publish", {"draft_revision": 2}, editor),
        ("republish", "POST", f"{BASE}/{cid}/publish", {"draft_revision": 3}, reviewer),
        ("archive", "POST", f"{BASE}/{cid}/archive", None, reviewer),
        ("unarchive", "POST", f"{BASE}/{cid}/unarchive", None, editor),
    ]
    for name, method, path, json, principal in writes:
        answer = await client.request(
            method, path, json=json, headers=headers(f"{name}-{tag}", principal=principal)
        )
        assert answer.is_success, (name, answer.text)

    listed = (await client.get(f"{BASE}/{cid}/versions", headers=READ)).json()["items"]
    assert [(v["version"], v["published_by"]) for v in listed] == [(1, editor), (2, reviewer)]
    assert (await entry_row(engine, cid)).owner_principal == owner

    deleted = await client.delete(
        f"{BASE}/{cid}", headers=headers(f"delete-{tag}", principal=reviewer)
    )
    assert deleted.status_code == 204
    (audit,) = await rows(
        engine,
        "SELECT principal FROM audit_log "
        "WHERE kind = 'catalog_entry_deleted' AND details->>'entity_id' = :c",
        c=cid,
    )
    assert audit.principal == reviewer

    requesters = {
        "create": PRINCIPAL,
        **{name: principal for name, *_, principal in writes},
        "delete": reviewer,
    }
    for name, principal in requesters.items():
        (operation,) = await rows(
            engine, "SELECT principal FROM operations WHERE idempotency_key = :k", k=f"{name}-{tag}"
        )
        assert operation.principal == principal, name


# -- refusals ---------------------------------------------------------------------------------


async def test_publish_refuses_an_unpublishable_draft(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A draft without a name, or one the draft schema no longer accepts, is 422
    ``publish_invalid`` with the validation report (#111's shape: each error ``rejected`` at
    its location in the draft), and nothing is written. Complement: a named draft publishes."""
    nameless = character()
    del nameless["public_card"]["name"]
    created = await client.post(BASE, json=nameless, headers=WRITE)
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    entry, operations = await entry_row(engine, cid), await _operations(engine)

    refused = await publish(client, cid, 1)
    assert (refused.status_code, _error(refused)["code"]) == (422, "publish_invalid")
    assert _error(refused)["details"]["report"] == {
        "errors": [
            {
                "code": "field_required",
                "location": {
                    "entry": cid,
                    "version": "draft",
                    "role": None,
                    "field": "public_card.name",
                    "seed_id": None,
                },
                "text": None,
                "outcome": "rejected",
                "source": None,
                "rule": "a published catalog character holds every field of its definition "
                "(FR-CAT-2)",
            }
        ],
        "warnings": [],
        "unavailable": [],
    }
    assert await entry_row(engine, cid) == entry
    assert await version_rows(engine, cid) == []
    assert await _operations(engine) == operations

    await execute_in_tx(
        engine,
        (
            "UPDATE catalog.character_catalog "
            "SET draft = jsonb_set(draft, '{public_card,name}', '\"Named\"') "
            "|| '{\"retired_field\": 1}'::jsonb "
            "WHERE id = :c",
            {"c": cid},
        ),
    )
    refused = await publish(client, cid, 1)
    assert (refused.status_code, _error(refused)["code"]) == (422, "publish_invalid")
    errors = _error(refused)["details"]["report"]["errors"]
    assert [(e["code"], e["location"]["field"], e["outcome"]) for e in errors] == [
        ("extra_forbidden", "retired_field", "rejected")
    ]
    assert await version_rows(engine, cid) == []
    # A save validates the whole merged draft, so the stored field is refused at its place.
    resaved = await save(client, cid, 1, public_description="Still no.")
    assert (resaved.status_code, _error(resaved)["details"]["field"]) == (
        422,
        "body.retired_field",
    )

    named = (await create(client))["id"]
    assert (await publish(client, named, 1)).status_code == 201


async def test_drafts_are_capped(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft over the cap (canonical JSON bytes) is 413 ``draft_too_large`` on create and on
    save, and nothing is written. Complement: a draft within the cap saves."""
    # The fixture's complete document is about 840 bytes, and 1,500 with a 700-character
    # description (#115's complete character).
    monkeypatch.setattr(characters, "DRAFT_MAX_BYTES", 1_200)
    too_large = await client.post(BASE, json=character(public_description="x" * 700), headers=WRITE)
    assert (too_large.status_code, _error(too_large)["code"]) == (413, "draft_too_large")
    assert _error(too_large)["details"]["limit"] == 1_200
    assert _error(too_large)["details"]["size"] > 1_200

    cid = (await create(client))["id"]
    entry = await entry_row(engine, cid)
    grown = await save(client, cid, 1, public_description="y" * 700)
    assert (grown.status_code, _error(grown)["code"]) == (413, "draft_too_large")
    assert await entry_row(engine, cid) == entry
    assert (await save(client, cid, 1, public_description="y" * 100)).status_code == 200


async def test_library_refuses_like_rest(settings: Settings, engine: AsyncEngine) -> None:
    """The in-process library raises the errors REST answers with, and needs a principal."""
    lib = Engine(settings, db=engine)
    ctx = RequestContext(principal=PRINCIPAL)
    created = await lib.create_character_draft(
        ctx, characters.CharacterCreate.model_validate(character("Lib"))
    )
    cid = created.character.id
    with pytest.raises(NotPublished):
        await lib.get_character(ctx, cid)
    await lib.save_character_draft(ctx, cid, characters.CharacterPatch(draft_revision=1))
    with pytest.raises(DraftStale):
        await lib.publish_character(ctx, cid, characters.PublishRequest(draft_revision=1))
    with pytest.raises(PrincipalRequired):
        await lib.archive_character(RequestContext(), cid)
    done = await lib.publish_character(ctx, cid, characters.PublishRequest(draft_revision=2))
    card = await lib.get_character(ctx, cid)
    assert isinstance(card, characters.CharacterCard)
    assert (card.version, done.version, card.name) == (1, 1, "Lib")
    assert [v.version for v in await lib.list_character_versions(ctx, cid)] == [1]
    assert (await lib.delete_character(ctx, cid)).versions == 1
