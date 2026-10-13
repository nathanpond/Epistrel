"""I-21: ownership, relations, and principal offboarding (docs/04 §4), its M3 parts.

Every I-21 (M3) part lives in this file, because traceability counts a test ID per milestone in
one file. Later stories add theirs here (#106 profiles, #110 listing status, #117 offboarding).
#105's part, ``test_i_21__media_retire``, is at the end. #104's parts:

- ``test_i_21__ownership``: creation sets the owner of every delivered type, a fork included. A
  transfer moves it and records the acting principal. A ``system``-owned entity transfers like
  any other. Relations are added, listed, filtered (over names and principals that cross between
  stories, and with the owner filter's complement) and removed. A list narrows only by the
  filters it names, whichever principal asks. A story made from a catalog entry stays
  byte-identical through the entry's transfer and relation changes.
- ``test_i_21__nothing_gates``: the matrix. Every route under an owned entity answers its owner,
  a stranger, and a principal holding relations named ``admin`` and ``owner`` with the same
  status, and every read with the same body.
- ``test_i_21__companion``: a character's companion scenario moves with it and cannot be
  transferred or related on its own; a scenario transfer leaves stories alone. Until #108 and
  #116 create ``catalog.scenario_catalog`` with ``companion_of``, the test stands one in on its
  own scratch database. When the real table arrives, the test is rebuilt on it.

The detailed tests are in ``tests/integration/test_ownership.py``. The story-creation clauses
(player relations, creating from another owner's entry) are #119's.

- ``test_i_21__media_retire`` (#105): retire, not purge. An asset its owner deletes while
  another owner's published character references it is still served for that character, refuses
  new references, and survives the media sweep until nothing references it; then the sweep
  purges it. Until #115 gives catalog versions image fields, the version that references it is
  planted with #115's ``public_card.images`` shape.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import (
    CharacterCreate,
    CompanionFollowsCharacter,
    Engine,
    MediaRetired,
    OwnerTransferRequest,
    RelationRequest,
    RequestContext,
    StorySpec,
)
from epistrel.config import Settings
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.media.assets import assert_referenceable
from epistrel.core.media.sweep import sweep
from epistrel.core.stories import STORY_TABLES
from epistrel.core.tables import stories
from epistrel.core.tables.ownership import EntityType
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.event_support import rows, scalar
from tests.integration.ownership_fixtures import (
    CALLS,
    KINDS,
    NEW_OWNER,
    OWNER,
    PRIVILEGED_NAMES,
    RELATED,
    STRANGER,
    Kind,
    create_character,
    create_story,
    headers,
    ok,
    owner_of,
    request_path,
)
from tests.media_support import blob_path, media_client, scratch, uploaded
from tests.respond_support import respond_client
from tests.story_support import story_body

READ = bearer()
Snapshot = dict[str, list[str]]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _unique(prefix: str) -> str:
    """A principal or relation name no other test uses."""
    return f"{prefix}-{secrets.token_hex(4)}"


def _rows_sql(table: Table) -> str:
    column = "id" if table.name == "stories" else "story_id"
    return f'SELECT row_to_json(t)::text FROM "{table.name}" t WHERE "{column}" = :s'  # noqa: S608


async def _snapshot(engine: AsyncEngine, story_id: str) -> Snapshot:
    """Every row of the story (per story table, and its ``stories`` row), and every row of
    ``stories`` and ``characters``, whichever story it belongs to."""
    found: Snapshot = {}
    async with engine.connect() as conn:
        for table in (*STORY_TABLES, stories):
            found[table.name] = sorted(await conn.scalars(text(_rows_sql(table)), {"s": story_id}))
        for name in ("stories", "characters"):
            every = await conn.scalars(text(f'SELECT row_to_json(t)::text FROM "{name}" t'))  # noqa: S608
            found[f"every {name} row"] = sorted(every)
    return found


async def _events(engine: AsyncEngine, entity_id: str) -> list[tuple[Any, ...]]:
    """The entity's ownership events: ``(kind, old, new, acting_principal)``, oldest first."""
    found = await rows(
        engine,
        "SELECT kind, old, new, acting_principal FROM ownership_events "
        "WHERE entity_id = :e ORDER BY at, id",
        e=entity_id,
    )
    return [tuple(r) for r in found]


async def _listed(client: Any, principal: str | None = None, **params: Any) -> list[str]:
    """The story ids ``GET /stories`` lists when ``principal`` asks (``None``: no principal)."""
    sent = READ if principal is None else headers(principal)
    response = await client.get("/stories", params=params, headers=sent)
    return [item["id"] for item in (await ok(response))["items"]]


@pytest.mark.integration
@pytest.mark.part("M3", "FR-AUTH-5,6,8; FR-CAT-9")
async def test_i_21__ownership(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Every delivered owned type (stories, catalog characters) is owned by its creator, or by
    the owner the creation names, and a fork by whoever forked it. A transfer by a third
    principal moves the owner and records ``(transfer, old, new, acting principal)`` with a
    ``control`` operation by that principal; the same transfer again is answered with nothing
    recorded; an entity ``system`` owns transfers like any other. Relations on stories and on a
    character are added, listed, filtered by (``name:principal``, ``name:`` for any principal,
    repeated filters all holding) and removed, each change recorded. The stories' relations
    cross: ``editor`` and ``co_author`` each name C on one story and D on another, so a filter
    lists a story only when one of its relations has both the name and the principal asked for.
    A story whose character came from catalog entry E stays byte-identical while E is
    transferred and related.

    Complements asserted: ``owner=A`` leaves out B's story while no filter lists both, whoever
    asks (A, B, a stranger or no principal: a list is never narrowed to the asker); a relation
    filter leaves out each story that holds only its name, only its principal, or the two on
    different relations, and two filters that each hold on a different story list neither; a
    removed relation no longer filters; the story snapshot does see the story's own archive."""
    async with respond_client(stub_settings, engine) as (client, _):
        # -- creation sets the owner, for every delivered type --------------------------------
        for kind in KINDS.values():
            mine = await kind.create(client, OWNER, None)
            named = await kind.create(client, OWNER, NEW_OWNER)
            assert await owner_of(engine, kind, mine.id) == OWNER, kind.entity_type
            assert await owner_of(engine, kind, named.id) == NEW_OWNER, kind.entity_type
            listed = await ok(await client.get(kind.path(mine.id, "/relations"), headers=READ))
            assert (listed["owner_principal"], listed["items"]) == (OWNER, [])

        source = await create_story(client, OWNER, None)
        read = await ok(await client.get(f"/stories/{source.id}", headers=READ))
        assert read["owner_principal"] == OWNER
        character = await create_character(client, OWNER, None)
        full = await client.get(
            f"/catalog/characters/{character.id}",
            params={"draft": "true", "projection": "full"},
            headers=READ,
        )
        assert (await ok(full))["owner_principal"] == OWNER
        forked = await client.post(
            f"/stories/{source.id}/fork", json={"at_turn": 0}, headers=headers(RELATED)
        )
        named_fork = await client.post(
            f"/stories/{source.id}/fork",
            json={"at_turn": 0, "owner": NEW_OWNER},
            headers=headers(RELATED),
        )
        story_kind = KINDS[EntityType.STORY]
        assert await owner_of(engine, story_kind, (await ok(forked, 201))["story_id"]) == RELATED
        fork_id = (await ok(named_fork, 201))["story_id"]
        assert await owner_of(engine, story_kind, fork_id) == NEW_OWNER

        # -- a transfer moves the owner and records who did it --------------------------------
        for kind in KINDS.values():
            entity = await kind.create(client, OWNER, None)
            key = _unique("transfer")
            moved = await client.put(
                kind.path(entity.id, "/owner"),
                json={"principal": NEW_OWNER},
                headers=headers(RELATED, key),
            )
            body = await ok(moved)
            assert (body["entity_type"], body["entity_id"]) == (kind.entity_type.value, entity.id)
            assert (body["owner_principal"], body["previous_owner"]) == (NEW_OWNER, OWNER)
            assert await owner_of(engine, kind, entity.id) == NEW_OWNER
            assert await _events(engine, entity.id) == [("transfer", OWNER, NEW_OWNER, RELATED)]
            (operation,) = await rows(
                engine, "SELECT kind, principal FROM operations WHERE idempotency_key = :k", k=key
            )
            assert tuple(operation) == ("control", RELATED)
            noop = _unique("noop")
            again = await client.put(
                kind.path(entity.id, "/owner"),
                json={"principal": NEW_OWNER},
                headers=headers(OWNER, noop),
            )
            assert (await ok(again))["previous_owner"] == NEW_OWNER
            assert len(await _events(engine, entity.id)) == 1, "a no-op records nothing"
            recorded = await scalar(
                engine, "SELECT count(*) FROM operations WHERE idempotency_key = :k", k=noop
            )
            assert recorded == 0, "a no-op records no operation"

        # A preset (FR-PROF-3) is owned by `system` and transfers like any other entity.
        kept = await create_character(client, OWNER, None)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE catalog.character_catalog SET owner_principal = 'system' WHERE id = :c"
                ),
                {"c": kept.id},
            )
        moved = await client.put(
            f"/catalog/characters/{kept.id}/owner",
            json={"principal": NEW_OWNER},
            headers=headers(STRANGER),
        )
        assert (await ok(moved))["previous_owner"] == "system"
        assert await _events(engine, kept.id) == [("transfer", "system", NEW_OWNER, STRANGER)]

        # -- relations: added, listed, filtered by, removed ----------------------------------
        a, b, e = _unique("p-a"), _unique("p-b"), _unique("p-e")
        d, c = _unique("p-d"), _unique("p-c")
        co_author, editor = f"co_author_{secrets.token_hex(3)}", f"editor_{secrets.token_hex(3)}"
        mine = await create_story(client, a, None)
        theirs = await create_story(client, b, None)
        entry = await create_character(client, a, None)
        story_relations = f"/stories/{mine.id}/relations"
        added = await client.post(
            story_relations, json={"principal": d, "relation": co_author}, headers=headers(RELATED)
        )
        row = await ok(added, 201)
        assert (row["principal"], row["relation"], row["created_by"]) == (d, co_author, RELATED)
        repeat = await client.post(
            story_relations, json={"principal": d, "relation": co_author}, headers=headers(a)
        )
        assert (await ok(repeat))["created_at"] == row["created_at"]
        await ok(
            await client.post(
                story_relations, json={"principal": c, "relation": editor}, headers=headers(a)
            ),
            201,
        )
        await ok(
            await client.post(
                f"/stories/{theirs.id}/relations",
                json={"principal": d, "relation": co_author},
                headers=headers(b),
            ),
            201,
        )
        await ok(
            await client.post(
                f"/catalog/characters/{entry.id}/relations",
                json={"principal": c, "relation": editor},
                headers=headers(a),
            ),
            201,
        )
        # The names and principals cross. `mine` holds editor:c and co_author:d, so these two
        # stories each share one half with one of its relations: editor:d (the name, another
        # principal) and co_author:c (the principal, another name).
        edited_by_d = await create_story(client, e, None)
        coauthored_by_c = await create_story(client, e, None)
        for story, principal, name in ((edited_by_d, d, editor), (coauthored_by_c, c, co_author)):
            await ok(
                await client.post(
                    f"/stories/{story.id}/relations",
                    json={"principal": principal, "relation": name},
                    headers=headers(e),
                ),
                201,
            )
        listing = await ok(await client.get(story_relations, headers=READ))
        assert [(i["relation"], i["principal"]) for i in listing["items"]] == sorted(
            [(co_author, d), (editor, c)]
        )
        assert await _events(engine, mine.id) == [
            ("relation_added", None, {"principal": d, "relation": co_author}, RELATED),
            ("relation_added", None, {"principal": c, "relation": editor}, a),
        ]

        assert await _listed(client, owner=a) == [mine.id]
        assert set(await _listed(client)) >= {mine.id, theirs.id}
        for asker in (a, b, STRANGER):  # whoever asks, only the filters asked for narrow
            assert set(await _listed(client, asker)) >= {mine.id, theirs.id}, asker
            assert await _listed(client, asker, owner=a) == [mine.id], asker
            assert await _listed(client, asker, owner=b) == [theirs.id], asker
            assert await _listed(client, asker, relation=f"{editor}:{c}") == [mine.id], asker
        # Every cell of {editor, co_author} x {c, d}, and each name for any principal, newest
        # first. A filter that dropped its principal, dropped its name, or matched the two on
        # different relations would list an extra story in at least one cell.
        cells = {
            f"{editor}:{c}": [mine],
            f"{editor}:{d}": [edited_by_d],
            f"{co_author}:{c}": [coauthored_by_c],
            f"{co_author}:{d}": [theirs, mine],
            f"{editor}:": [edited_by_d, mine],
            f"{co_author}:": [coauthored_by_c, theirs, mine],
        }
        for wanted, expected in cells.items():
            assert await _listed(client, relation=wanted) == [s.id for s in expected], wanted
        both = await _listed(client, relation=[f"{co_author}:{d}", f"{editor}:{c}"])
        assert both == [mine.id]
        crossed = await _listed(client, relation=[f"{editor}:{d}", f"{co_author}:{c}"])
        assert crossed == [], "each filter holds on a story of its own, both on none"
        assert await _listed(client, owner=b, relation=f"{editor}:{c}") == []

        removed = await client.delete(f"{story_relations}/{co_author}/{d}", headers=headers(b))
        assert removed.status_code == 204, removed.text
        assert await _listed(client, relation=f"{co_author}:{d}") == [theirs.id]
        assert (await _events(engine, mine.id))[-1] == (
            "relation_removed",
            {"principal": d, "relation": co_author},
            None,
            b,
        )
        gone = await client.delete(f"{story_relations}/{co_author}/{d}", headers=headers(b))
        assert (gone.status_code, gone.json()["error"]["code"]) == (404, "not_found")

        # -- a catalog entry's owner and relations never touch a story made from it -----------
        await ok(
            await client.post(
                f"/catalog/characters/{entry.id}/publish",
                json={"draft_revision": 1},
                headers=headers(a),
            ),
            201,
        )
        made = await create_story(client, a, None)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE characters SET source_catalog_id = :e, source_catalog_version = 1 "
                    "WHERE id = :c"
                ),
                {"e": entry.id, "c": made.ids["ada"]},
            )
        before = await _snapshot(engine, made.id)
        entry_path = f"/catalog/characters/{entry.id}"
        await ok(await client.put(f"{entry_path}/owner", json={"principal": b}, headers=headers(d)))
        await ok(
            await client.post(
                f"{entry_path}/relations",
                json={"principal": d, "relation": co_author},
                headers=headers(d),
            ),
            201,
        )
        dropped = await client.delete(f"{entry_path}/relations/{editor}/{c}", headers=headers(d))
        assert dropped.status_code == 204, dropped.text
        assert await _snapshot(engine, made.id) == before
        assert await owner_of(engine, KINDS[EntityType.CHARACTER], entry.id) == b

        archived = await client.post(f"/stories/{made.id}/archive", headers=headers(a))
        assert archived.status_code == 200, archived.text
        assert await _snapshot(engine, made.id) != before


# -- the nothing-gates matrix ----------------------------------------------------------------------


def _kind_of(path: str) -> Kind:
    for kind in KINDS.values():
        if path == kind.item or path.startswith(f"{kind.item}/"):
            return kind
    raise AssertionError(f"{path} is under no delivered owned type")


CASES = [(route, query) for route in sorted(CALLS) for query in CALLS[route].queries]
"""Every route under an owned entity, once per query string its ``Call`` names."""


def _label(case: tuple[tuple[str, str], dict[str, str]]) -> str:
    (method, path), query = case
    return f"{method} {path}" + (f"?{urlencode(query)}" if query else "")


ULID_TEXT = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
TIME_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _blanked(body: Any) -> Any:
    """``body`` without what differs between two fresh entities in the same state: every id
    and time blanked, in keys too (a mapping whose keys become alike, such as true ages by
    character id, is compared as its pairs), and every list in one order."""
    if isinstance(body, dict):
        pairs = sorted(((_blanked(k), _blanked(v)) for k, v in body.items()), key=_canonical)
        return dict(pairs) if len({key for key, _ in pairs}) == len(pairs) else pairs
    if isinstance(body, list):
        return sorted((_blanked(item) for item in body), key=_canonical)
    if isinstance(body, str):
        return TIME_TEXT.sub("<time>", ULID_TEXT.sub("<id>", body))
    return body


@pytest.mark.integration
@pytest.mark.part("M3", "FR-AUTH-8")
@pytest.mark.parametrize(("route", "query"), CASES, ids=[_label(case) for case in CASES])
async def test_i_21__nothing_gates(
    route: tuple[str, str], query: dict[str, str], stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Each route under an owned entity answers a request about an entity owned by A with the
    same status code whether A sends it, a stranger B sends it, or C sends it while holding
    relations named ``admin`` and ``owner`` on the entity. Each requester gets a fresh entity in
    the same state. The code compared is the route's success code, so every request really ran:
    the owner owns no extra permission, and neither does a relation, whatever its name.

    A read answers all three with the same body too, once ids and times are blanked: the
    content A put there (a played turn, settings, pinned memories, relations, a published
    version, an uploaded image's metadata and its bytes) is there for B and C, nothing
    narrowed. A ``private`` catalog character is read as
    its card (the default) and in ``full``, as version 1, and as its draft in both projections;
    a story and its history in the default viewpoint and the omniscient one."""
    method, path = route
    kind, call = _kind_of(path), CALLS[route]
    statuses: dict[str, int] = {}
    bodies: dict[str, Any] = {}
    async with respond_client(stub_settings, engine) as (client, _):
        for requester in (OWNER, STRANGER, RELATED):
            entity = await kind.create(client, OWNER, None)
            for name in PRIVILEGED_NAMES:
                granted = await client.post(
                    kind.path(entity.id, "/relations"),
                    json={"principal": RELATED, "relation": name},
                    headers=headers(OWNER),
                )
                await ok(granted, 201)
            extra = await call.prepare(client, kind, entity)
            response = await client.request(
                method,
                request_path(kind, path, entity, extra),
                json=call.body(entity),
                params=query,
                headers=headers(requester),
            )
            statuses[requester] = response.status_code
            assert response.status_code == call.expected, (requester, response.text)
            if method == "GET":
                json_body = response.headers["content-type"].startswith("application/json")
                bodies[requester] = (
                    _blanked(response.json()) if json_body else response.content  # media bytes
                )
    assert statuses == dict.fromkeys((OWNER, STRANGER, RELATED), call.expected)
    if method == "GET":
        assert bodies[STRANGER] == bodies[OWNER], "a stranger reads what the owner reads"
        assert bodies[RELATED] == bodies[OWNER], "a relation's holder reads what the owner reads"


# -- companions ------------------------------------------------------------------------------------

STAND_IN = """
CREATE TABLE catalog.scenario_catalog (
    id char(26) PRIMARY KEY,
    owner_principal text NOT NULL,
    companion_of char(26) NULL,
    updated_at timestamptz NOT NULL
)
"""
"""The columns ownership reads of #108's scenario catalog with #116's ``companion_of``."""


@pytest.fixture
async def companion_db(pg_url: str) -> AsyncIterator[tuple[Engine, AsyncEngine]]:
    """An Engine on its own scratch database, where a stand-in ``catalog.scenario_catalog``
    exists (see the module docstring)."""
    with migrated_database(pg_url, f"epistrel_i21_{secrets.token_hex(4)}") as url:
        sync = make_sync_engine(url)
        try:
            with sync.begin() as conn:
                exists = conn.execute(text("SELECT to_regclass('catalog.scenario_catalog')"))
                assert exists.scalar() is None, (
                    "catalog.scenario_catalog exists now (#108): rebuild this test on real "
                    "scenarios and companions"
                )
                conn.execute(text(STAND_IN))
        finally:
            sync.dispose()
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        lib = Engine(settings, db=db)
        try:
            yield lib, db
        finally:
            await lib.models.aclose()
            await db.dispose()


def _as(principal: str) -> RequestContext:
    return RequestContext(principal=principal)


async def _owners(db: AsyncEngine) -> dict[str, str]:
    found = await rows(
        db,
        "SELECT id, owner_principal FROM catalog.scenario_catalog UNION ALL "
        "SELECT id, owner_principal FROM catalog.character_catalog",
    )
    return {str(r.id): str(r.owner_principal) for r in found}


@pytest.mark.integration
@pytest.mark.part("M3", "FR-AUTH-5; FR-CAT-9")
async def test_i_21__companion(companion_db: tuple[Engine, AsyncEngine]) -> None:
    """Transferring catalog character X moves X and its companion scenario Y to the new owner in
    one transaction: one ``transfer`` event each, at the same time, by the acting principal,
    with Y's ``updated_at`` bumped. A companion row already at the new owner (Y2, planted out of
    step; #116 keeps one per character) is left as it is, with no event of its own. Y cannot be
    transferred, related or unrelated on its own
    (422 ``companion_follows_character``), and nothing changes. A scenario that is nobody's
    companion transfers and takes relations normally. A story whose character came from X stays
    byte-identical throughout.

    Complement asserted: the story snapshot does see the story's own archive."""
    lib, db = companion_db
    created = await lib.create_character_draft(_as(OWNER), CharacterCreate(name="Mara Venn"))
    x = created.character.id
    y, z = "0000000000000000000000CMP1", "0000000000000000000000SCN1"
    y2 = "0000000000000000000000CMP2"  # out of step: already the new owner's
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO catalog.scenario_catalog VALUES "
                "(:y, :o, :x, '2026-01-01T00:00:00Z'), (:z, :o, NULL, '2026-01-01T00:00:00Z'), "
                "(:y2, :n, :x, '2026-01-01T00:00:00Z')"
            ),
            {"y": y, "z": z, "y2": y2, "o": OWNER, "n": NEW_OWNER, "x": x},
        )
    story = await lib.create_story(_as(OWNER), StorySpec.model_validate(story_body("Wren", "Ada")))
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE characters SET source_catalog_id = :x, source_catalog_version = 1 "
                "WHERE id = :c"
            ),
            {"x": x, "c": story.characters[1].id},
        )
    before = await _snapshot(db, story.story_id)

    moved = await lib.transfer_owner(
        _as(RELATED), EntityType.CHARACTER, x, OwnerTransferRequest(principal=NEW_OWNER)
    )
    assert (moved.owner_principal, moved.previous_owner, moved.changed) == (NEW_OWNER, OWNER, True)
    assert await _owners(db) == {x: NEW_OWNER, y: NEW_OWNER, y2: NEW_OWNER, z: OWNER}
    events = await rows(
        db,
        "SELECT entity_type, entity_id, kind, old, new, acting_principal, at "
        "FROM ownership_events ORDER BY entity_type",
    )
    assert [tuple(e)[:6] for e in events] == [
        ("character", x, "transfer", OWNER, NEW_OWNER, RELATED),
        ("scenario", y, "transfer", OWNER, NEW_OWNER, RELATED),
    ]
    assert events[0].at == events[1].at, "one transaction"
    # The companion already at the new owner is left as it is: no event of its own.
    bumped = await scalar(
        db, "SELECT updated_at > '2026-01-01' FROM catalog.scenario_catalog WHERE id = :y", y=y
    )
    assert bumped is True

    request = RelationRequest(principal=STRANGER, relation="editor")
    refusals = [
        lib.transfer_owner(
            _as(OWNER), EntityType.SCENARIO, y, OwnerTransferRequest(principal=OWNER)
        ),
        lib.add_relation(_as(OWNER), EntityType.SCENARIO, y, request),
        lib.remove_relation(_as(OWNER), EntityType.SCENARIO, y, "editor", STRANGER),
    ]
    for refused in refusals:
        with pytest.raises(CompanionFollowsCharacter) as caught:
            await refused
        assert caught.value.details == {
            "entity_type": "scenario",
            "entity_id": y,
            "character_id": x,
        }
    assert await _owners(db) == {x: NEW_OWNER, y: NEW_OWNER, y2: NEW_OWNER, z: OWNER}
    assert len(await rows(db, "SELECT 1 FROM ownership_events")) == 2

    other = await lib.transfer_owner(
        _as(STRANGER), EntityType.SCENARIO, z, OwnerTransferRequest(principal=NEW_OWNER)
    )
    assert (other.previous_owner, other.changed) == (OWNER, True)
    related = await lib.add_relation(_as(STRANGER), EntityType.SCENARIO, z, request)
    assert related.created
    listed = await lib.list_relations(_as(STRANGER), EntityType.SCENARIO, z)
    assert [(r.relation, r.principal) for r in listed.items] == [("editor", STRANGER)]
    await lib.remove_relation(_as(STRANGER), EntityType.SCENARIO, z, "editor", STRANGER)
    assert await _owners(db) == {x: NEW_OWNER, y: NEW_OWNER, y2: NEW_OWNER, z: NEW_OWNER}
    assert await _snapshot(db, story.story_id) == before

    await lib.archive_story(_as(OWNER), story.story_id)
    assert await _snapshot(db, story.story_id) != before


# -- retire, not purge (#105) ----------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M3", "FR-MEDIA-2")
async def test_i_21__media_retire(pg_url: str, tmp_path: Path) -> None:
    """Principal A uploads an image; B's catalog character publishes a version that shows it.
    A deletes the image: it is retired, not purged. It leaves A's listing and refuses a new
    reference (422 ``media_retired``), yet its bytes are still served for B's character, and
    the media sweep keeps it while B's version references it. Once B deletes the character
    (versions and all), the next sweep purges the asset and its bytes.

    Complement asserted: an unreferenced asset A retires at the same time is purged by the very
    first sweep, so the referenced one's survival is the reference's doing."""
    a, b = headers("p-media-a"), headers("p-media-b")
    async with (
        scratch(pg_url, tmp_path / "media") as space,
        media_client(space.settings, space.db) as client,
    ):
        shown = await uploaded(client, headers=a)
        alone = await uploaded(client, headers=a)
        created = await client.post("/catalog/characters", json={"name": "Mara Venn"}, headers=b)
        character = (await ok(created, 201))["id"]
        async with space.db.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO catalog.character_versions (catalog_id, version, content, "
                    "published_at, published_by) VALUES (:c, 1, jsonb_build_object('name', "
                    "'Mara Venn', 'public_card', jsonb_build_object('images', jsonb_build_object("
                    "'primary', CAST(:m AS text)))), now(), 'p-media-b')"
                ),
                {"c": character, "m": shown["id"]},
            )
        for media_id in (shown["id"], alone["id"]):
            deleted = await client.delete(f"/media/{media_id}", headers=a)
            assert deleted.status_code == 204, deleted.text

        listed = await ok(await client.get("/media", params={"owner": "p-media-a"}, headers=READ))
        assert listed["items"] == []
        async with space.db.begin() as conn:
            with pytest.raises(MediaRetired) as refused:
                await assert_referenceable(conn, [shown["id"]])
        assert (refused.value.http_status, refused.value.details["reason"]) == (422, "retired")

        counts = await sweep(space.db, space.lib.media)
        assert counts["assets_purged"] == 1
        served = await client.get(f"/media/{shown['id']}/content", headers=READ)
        assert served.status_code == 200
        assert (await ok(await client.get(f"/media/{shown['id']}", headers=READ)))["state"] == (
            "retired"
        )
        gone = await client.get(f"/media/{alone['id']}", headers=READ)
        assert gone.status_code == 404
        assert not blob_path(space.root, alone["checksum"]).exists()

        removed = await client.delete(f"/catalog/characters/{character}", headers=b)
        assert removed.status_code == 204, removed.text
        assert (await sweep(space.db, space.lib.media))["assets_purged"] == 1
        assert (await client.get(f"/media/{shown['id']}/content", headers=READ)).status_code == 404
        assert not blob_path(space.root, shown["checksum"]).exists()
