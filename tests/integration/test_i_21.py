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
- ``test_i_21__companion``: a character's companion scenario (created with it, #116) moves with
  it and cannot be transferred or related on its own; a scenario transfer leaves stories alone.
  It runs in its own scratch database (it counts every ownership event).

The detailed tests are in ``tests/integration/test_ownership.py``. The story-creation clauses
(player relations, creating from another owner's entry) are #119's ``test_i_21__story_creation``
at the end: a scenario has one player role in M3 (#108's save rule), so one player principal is
recorded per story.

- ``test_i_21__media_retire`` (#105): retire, not purge. An asset its owner deletes while
  another owner's published character references it is still served for that character, refuses
  new references, and survives the media sweep until nothing references it; then the sweep
  purges it. Until #115 gives catalog versions image fields, the version that references it is
  planted with #115's ``public_card.images`` shape.
- ``test_i_21__profiles`` (#106): every engine profile has one owner. The Lite preset is
  ``system``'s; a created or copied profile is its creator's, or the owner the request names;
  a transfer moves it with the acting principal recorded, a ``system``-owned profile's too; and
  owning one grants nothing: another principal edits, copies, validates and deletes it. The
  nothing-gates matrix covers every profile route (``ownership_fixtures.CALLS``).
- ``test_i_21__listing_filters`` (#110): the catalog lists narrow by owner, relation, label or
  listing status only when a request names the filter, whoever asks; unfiltered, they list the
  whole table (counted); and a listing-status change leaves a story made from the entry
  byte-identical. On its own scratch database, so the table is the test's alone. The
  nothing-gates matrix covers each entry's listing-status route (``CALLS``).
- ``test_i_21__offboarding`` (#117): docs/04's offboarding of A. The lookup lists A's owned and
  related entities by type with counts. The dry run counts and warns (a story to be deleted is
  shared with E; with profiles deleted instead, all three warning kinds). Without
  ``ignore_warnings`` nothing changes; with it the worker deletes A's stories (the shared one for
  E too) and personas, moves the character with its companion and the image it shows, the
  scenario and the profile to curator K, retires the other images (one still served for another
  owner's character), removes A's relation on another owner's entity, and leaves A's principal
  on its turns in a story it did not own. A retry under the key changes nothing, and the report
  lists every entity with nothing left linked to A.
- ``test_i_21__offboarding_resumes`` (#117): the worker dies after acting on an entity and
  before recording it; a fresh worker resumes from the progress row and finishes, and every
  entity is acted on exactly once (its ownership events counted).
- ``test_i_21__persona`` (#114): deleting a saved persona leaves a story created with it
  unchanged, whoever deletes it. Until #119's genesis creates stories from personas, the story's
  player character is given the persona's snapshot the way genesis will
  (``tests/persona_support.copy_into``). Personas join ``KINDS`` and ``CALLS``, so the ownership
  slice and the nothing-gates matrix cover their creation and every persona route.
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
    ScenarioCreate,
    StorySpec,
)
from epistrel.config import Settings
from epistrel.core import offboarding
from epistrel.core.db import make_engine
from epistrel.core.ids import new_ulid
from epistrel.core.media.assets import assert_referenceable
from epistrel.core.media.sweep import sweep
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.seeds.ratings import EVERYONE_ID, TEEN_ID
from epistrel.core.stories import STORY_TABLES
from epistrel.core.tables import stories
from epistrel.core.tables.ownership import EntityType
from tests import genesis_support as g
from tests.auth_support import auth_settings, bearer
from tests.catalog_support import character as catalog_character
from tests.catalog_support import ids as listed_ids
from tests.catalog_support import scratch_catalog, set_listing_status
from tests.character_support import named
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
    persona_body,
    request_path,
)
from tests.media_support import blob_path, media_client, scratch, uploaded
from tests.model_support import config_key
from tests.offboarding_support import contents as table_contents
from tests.offboarding_support import counts as table_counts
from tests.offboarding_support import (
    lookup,
    offboard,
    offboarding_db,
    ownership_changes,
    report,
    retry_now,
    run_jobs,
    worker,
    world,
)
from tests.offboarding_support import scalar as scratch_scalar
from tests.persona_support import copy_into
from tests.profile_support import EDITED, PROFILES, profile_body
from tests.respond_support import respond_client
from tests.scenario_support import scenario as catalog_scenario
from tests.story_support import story_body

READ = bearer()
Snapshot = dict[str, list[str]]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    """The stub model, and a config key so ``GET /media/{id}/url`` can sign (#107)."""
    return auth_settings(pg_url, model_adapter="stub", config_key=config_key())


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
SIGNED_TEXT = re.compile(r"([?&](?:exp|sig)=)[^&]*")
"""A signed URL's expiry and signature (#107), which differ between two fresh assets."""
TIME_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


DURATIONS = frozenset({"timings"})


def _blanked(body: Any) -> Any:
    """``body`` without what differs between two fresh entities in the same state: every id
    and time blanked, in keys too (a mapping whose keys become alike, such as true ages by
    character id, is compared as its pairs), and every list in one order. Durations
    (``DURATIONS``: a retrieval's stage timings, #141) are left out."""
    if isinstance(body, dict):
        pairs = sorted(
            ((_blanked(k), _blanked(v)) for k, v in body.items() if k not in DURATIONS),
            key=_canonical,
        )
        return dict(pairs) if len({key for key, _ in pairs}) == len(pairs) else pairs
    if isinstance(body, list):
        return sorted((_blanked(item) for item in body), key=_canonical)
    if isinstance(body, str):
        return TIME_TEXT.sub("<time>", SIGNED_TEXT.sub(r"\1<signed>", ULID_TEXT.sub("<id>", body)))
    return body


def _own_aside(body: Any, own: tuple[str, ...], requester: str) -> Any:
    """``body`` with each key in ``own`` set aside when it names ``requester``: a read of the
    requester's own record (``Call.own``)."""
    if not own:
        return body
    assert all(body[key] == requester for key in own), (own, body)
    return {key: value for key, value in body.items() if key not in own}


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
    a story and its history in the default viewpoint and the omniscient one. A message rating
    is the requester's own (FR-AUTH-4): each of the three rated the reply alike, and each reads
    the same rating, its own principal aside."""
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
            try:
                response = await client.request(
                    method,
                    request_path(kind, path, entity, extra),
                    json=call.body(entity),
                    params=query,
                    headers=headers(requester),
                )
            finally:
                await call.cleanup(client, kind, entity)
            statuses[requester] = response.status_code
            assert response.status_code == call.expected, (requester, response.text)
            if method == "GET":
                json_body = response.headers["content-type"].startswith("application/json")
                bodies[requester] = (
                    _blanked(_own_aside(response.json(), call.own, requester))
                    if json_body
                    else response.content  # media bytes
                )
    assert statuses == dict.fromkeys((OWNER, STRANGER, RELATED), call.expected)
    if method == "GET":
        assert bodies[STRANGER] == bodies[OWNER], "a stranger reads what the owner reads"
        assert bodies[RELATED] == bodies[OWNER], "a relation's holder reads what the owner reads"


# -- companions ------------------------------------------------------------------------------------


@pytest.fixture
async def companion_db(pg_url: str) -> AsyncIterator[tuple[Engine, AsyncEngine]]:
    """An Engine on its own scratch database: the test counts every ownership event there."""
    with migrated_database(pg_url, f"epistrel_i21_{secrets.token_hex(4)}") as url:
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


async def _scenario(lib: Engine, owner: str) -> str:
    """An authored scenario (nobody's companion) created as ``owner``."""
    created = await lib.create_scenario_draft(
        _as(owner), ScenarioCreate.model_validate({"public_card": {"name": "Gull Rock"}})
    )
    return created.scenario.id


@pytest.mark.integration
@pytest.mark.part("M3", "FR-AUTH-5; FR-CAT-9")
async def test_i_21__companion(companion_db: tuple[Engine, AsyncEngine]) -> None:
    """Transferring catalog character X moves X and its companion scenario Y (created with X,
    #116) to the new owner in one transaction: one ``transfer`` event each, at the same time, by
    the acting principal, with Y's ``updated_at`` moved on. A companion already at the new owner
    (Y2, W's companion, put out of step with SQL, which only SQL can do) is left as it is when
    W is transferred, with no event of its own and its ``updated_at`` unmoved. Y cannot be
    transferred, related or unrelated on its own (422 ``companion_follows_character``), and
    nothing changes. A scenario that is nobody's companion transfers and takes relations
    normally. A story whose character came from X stays byte-identical throughout.

    Complement asserted: the story snapshot does see the story's own archive."""
    lib, db = companion_db
    created = await lib.create_character_draft(
        _as(OWNER), CharacterCreate.model_validate(named("Mara Venn"))
    )
    x, y = created.character.id, created.character.companion_id
    other = await lib.create_character_draft(
        _as(OWNER), CharacterCreate.model_validate(named("Ivo Brand"))
    )
    w, y2 = other.character.id, other.character.companion_id
    assert y is not None
    assert y2 is not None
    z = await _scenario(lib, OWNER)
    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE catalog.scenario_catalog SET owner_principal = :o WHERE id = :y"),
            {"o": NEW_OWNER, "y": y2},
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
    stamp = "SELECT updated_at FROM catalog.scenario_catalog WHERE id = :y"
    y_updated, y2_updated = await scalar(db, stamp, y=y), await scalar(db, stamp, y=y2)

    moved = await lib.transfer_owner(
        _as(RELATED), EntityType.CHARACTER, x, OwnerTransferRequest(principal=NEW_OWNER)
    )
    assert (moved.owner_principal, moved.previous_owner, moved.changed) == (NEW_OWNER, OWNER, True)
    assert await _owners(db) == {x: NEW_OWNER, y: NEW_OWNER, w: OWNER, y2: NEW_OWNER, z: OWNER}
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
    assert await scalar(db, stamp, y=y) > y_updated

    # The companion already at the new owner is left as it is: no event, no new timestamp.
    await lib.transfer_owner(
        _as(RELATED), EntityType.CHARACTER, w, OwnerTransferRequest(principal=NEW_OWNER)
    )
    assert await _owners(db) == {x: NEW_OWNER, y: NEW_OWNER, w: NEW_OWNER, y2: NEW_OWNER, z: OWNER}
    later = await rows(
        db,
        "SELECT entity_type, entity_id FROM ownership_events WHERE entity_id IN (:w, :y) "
        "ORDER BY entity_type",
        w=w,
        y=y2,
    )
    assert [tuple(e) for e in later] == [("character", w)]
    assert await scalar(db, stamp, y=y2) == y2_updated

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
    assert await _owners(db) == {x: NEW_OWNER, y: NEW_OWNER, w: NEW_OWNER, y2: NEW_OWNER, z: OWNER}
    assert len(await rows(db, "SELECT 1 FROM ownership_events")) == 3

    other_scenario = await lib.transfer_owner(
        _as(STRANGER), EntityType.SCENARIO, z, OwnerTransferRequest(principal=NEW_OWNER)
    )
    assert (other_scenario.previous_owner, other_scenario.changed) == (OWNER, True)
    related = await lib.add_relation(_as(STRANGER), EntityType.SCENARIO, z, request)
    assert related.created
    listed = await lib.list_relations(_as(STRANGER), EntityType.SCENARIO, z)
    assert [(r.relation, r.principal) for r in listed.items] == [("editor", STRANGER)]
    await lib.remove_relation(_as(STRANGER), EntityType.SCENARIO, z, "editor", STRANGER)
    assert await _owners(db) == {
        x: NEW_OWNER,
        y: NEW_OWNER,
        w: NEW_OWNER,
        y2: NEW_OWNER,
        z: NEW_OWNER,
    }
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
        created = await client.post("/catalog/characters", json=named("Mara Venn"), headers=b)
        character = (await ok(created, 201))["id"]
        async with space.db.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO catalog.character_versions (catalog_id, version, content, "
                    "published_at, published_by) VALUES (:c, 1, jsonb_build_object("
                    "'public_card', jsonb_build_object('name', 'Mara Venn', 'images', "
                    "jsonb_build_object('primary', CAST(:m AS text)))), now(), 'p-media-b')"
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


# -- engine profiles (#106) ------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M3", "FR-PROF-1; FR-AUTH-5")
async def test_i_21__profiles(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Every engine profile has exactly one owner. The Lite preset, shipped with Epistrel, is
    owned by ``system`` and listed under it. A profile A creates is A's, and B's when the
    creation names B; ``system`` is refused as a named owner (422 at ``body.owner``). A copy is
    owned by whoever copies it, or by the owner the copy names, whoever owns the source. A
    transfer by a third principal moves the owner and records ``(transfer, old, new, acting
    principal)``; a ``system``-owned profile transfers like any other. Owning a profile grants
    nothing: a stranger edits A's profile into a new version, copies it, validates it and
    deletes it, and the deleted profile keeps its owner until it is purged.

    Complement asserted: the owner filter narrows the listing to A's profiles only when asked."""
    async with respond_client(stub_settings, engine) as (client, _):
        lite = await ok(await client.get(f"{PROFILES}/{LITE_PRESET_ID}", headers=READ))
        assert (lite["owner_principal"], lite["preset"]) == ("system", True)
        listed = await ok(await client.get(PROFILES, params={"owner": "system"}, headers=READ))
        assert LITE_PRESET_ID in [item["id"] for item in listed["items"]]
        shown = await ok(await client.get(f"{PROFILES}/{LITE_PRESET_ID}/relations", headers=READ))
        assert shown["owner_principal"] == "system"

        a, b, c = _unique("p-a"), _unique("p-b"), _unique("p-c")
        mine = await ok(await client.post(PROFILES, json=profile_body(), headers=headers(a)), 201)
        named = await ok(
            await client.post(PROFILES, json=profile_body(owner=b), headers=headers(a)), 201
        )
        assert (mine["owner_principal"], named["owner_principal"]) == (a, b)
        system = await client.post(PROFILES, json=profile_body(owner="system"), headers=headers(a))
        assert system.status_code == 422, system.text
        assert system.json()["error"]["details"]["field"] == "body.owner"
        copied = await ok(
            await client.post(
                f"{PROFILES}/{mine['id']}/copy", json={"name": "Copy"}, headers=headers(c)
            ),
            201,
        )
        given = await ok(
            await client.post(
                f"{PROFILES}/{LITE_PRESET_ID}/copy",
                json={"name": "Given", "owner": b},
                headers=headers(c),
            ),
            201,
        )
        assert (copied["owner_principal"], given["owner_principal"]) == (c, b)
        by_owner = await ok(await client.get(PROFILES, params={"owner": a}, headers=READ))
        assert [item["id"] for item in by_owner["items"]] == [mine["id"]]
        everyone = await ok(await client.get(PROFILES, params={"limit": 200}, headers=READ))
        assert {mine["id"], named["id"], copied["id"]} <= {i["id"] for i in everyone["items"]}

        moved = await ok(
            await client.put(
                f"{PROFILES}/{named['id']}/owner", json={"principal": a}, headers=headers(c)
            )
        )
        assert (moved["owner_principal"], moved["previous_owner"]) == (a, b)
        assert await _events(engine, named["id"]) == [("transfer", b, a, c)]
        kept = await ok(await client.post(PROFILES, json=profile_body(), headers=headers(a)), 201)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE engine_profiles SET owner_principal = 'system' WHERE id = :p"),
                {"p": kept["id"]},
            )
        handed = await ok(
            await client.put(
                f"{PROFILES}/{kept['id']}/owner", json={"principal": b}, headers=headers(c)
            )
        )
        assert handed["previous_owner"] == "system"
        assert await _events(engine, kept["id"]) == [("transfer", "system", b, c)]

        edited = await ok(
            await client.put(
                f"{PROFILES}/{mine['id']}",
                json={"name": "Edited", "settings": EDITED},
                headers=headers(c),
            )
        )
        assert (edited["result"]["version"], edited["result"]["owner_principal"]) == (2, a)
        await ok(
            await client.post(f"{PROFILES}/{mine['id']}/validate", json={}, headers=headers(c))
        )
        deleted = await ok(await client.delete(f"{PROFILES}/{mine['id']}", headers=headers(c)))
        assert deleted["deleted_at"] is not None
        assert await owner_of(engine, KINDS[EntityType.ENGINE_PROFILE], mine["id"]) == a


# -- listing filters (#110) ------------------------------------------------------------------------

CHARACTERS = "/catalog/characters"
SCENARIOS = "/catalog/scenarios"


async def _entry(
    client: Any,
    base: str,
    body: dict[str, Any],
    *,
    owner: str,
    listing: str | None = None,
    related: tuple[str, str] | None = None,
    published: bool = True,
    archived: bool = False,
) -> str:
    """A catalog entry ``owner`` creates (published unless ``published`` is false), at
    ``listing`` and holding the relation ``related`` (``(name, principal)``) when given."""
    as_owner = headers(owner)
    entry_id = (await ok(await client.post(base, json=body, headers=as_owner), 201))["id"]
    if published:
        await ok(
            await client.post(
                f"{base}/{entry_id}/publish", json={"draft_revision": 1}, headers=as_owner
            ),
            201,
        )
    if listing is not None:
        await ok(await set_listing_status(client, base, entry_id, listing, principal=owner))
    if related is not None:
        name, principal = related
        await ok(
            await client.post(
                f"{base}/{entry_id}/relations",
                json={"principal": principal, "relation": name},
                headers=as_owner,
            ),
            201,
        )
    if archived:
        await ok(await client.post(f"{base}/{entry_id}/archive", headers=as_owner))
    return str(entry_id)


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-9,11; FR-AUTH-8")
async def test_i_21__listing_filters(pg_url: str) -> None:
    """Catalog characters and scenarios of two owners, A and B, at every listing status, some
    holding relations and (scenarios) labels. Each list is narrowed by ``owner``, ``relation``,
    ``label`` (all must hold) or ``listing_status`` exactly when the request names the filter,
    to exactly the entries that match it, and the same whoever asks: A, B, a stranger or no
    principal (never narrowed to the asker). Without a filter, the published list, with the
    ``draft`` and ``archived`` ones, is every row of the table: the counts are asserted. With
    ``status=draft``, ``label`` and ``content_rating`` match a never-published entry's draft. A
    story whose character came from catalog entry X stays byte-identical while X's listing
    status changes.

    Complements asserted: each filter leaves out the entries that do not match it (a draft's
    other label or rating too); the story snapshot does see the story's own archive."""
    a, b, c, d = "p-list-a", "p-list-b", "p-list-c", "p-list-d"
    async with (
        scratch_catalog(pg_url) as (settings, db),
        respond_client(settings, db) as (
            client,
            _,
        ),
    ):
        c1 = await _entry(
            client,
            CHARACTERS,
            catalog_character("Mara"),
            owner=a,
            listing="public",
            related=("editor", c),
        )
        c2 = await _entry(client, CHARACTERS, catalog_character("Ivo"), owner=b)
        c3 = await _entry(
            client,
            CHARACTERS,
            catalog_character("Wren"),
            owner=a,
            listing="in_review",
            related=("co_author", d),
        )
        c4 = await _entry(client, CHARACTERS, catalog_character("Ada"), owner=b, published=False)
        c5 = await _entry(
            client,
            CHARACTERS,
            catalog_character("Tom"),
            owner=a,
            listing="public",
            archived=True,
        )

        def scenario(name: str, *labels: str) -> dict[str, Any]:
            return catalog_scenario(
                name, metadata={"labels": list(labels), "content_rating_id": EVERYONE_ID}
            )

        s1 = await _entry(
            client,
            SCENARIOS,
            scenario("Harbour", "family", "noir"),
            owner=a,
            listing="public",
            related=("editor", c),
        )
        s2 = await _entry(client, SCENARIOS, scenario("Orchard", "family"), owner=b)
        s3 = await _entry(
            client,
            SCENARIOS,
            scenario("Lighthouse", "noir"),
            owner=b,
            listing="in_review",
            related=("editor", d),
        )
        s4 = await _entry(client, SCENARIOS, scenario("Attic", "family"), owner=a, published=False)

        published = {CHARACTERS: {c1, c2, c3}, SCENARIOS: {s1, s2, s3}}
        cases: list[tuple[str, dict[str, Any], set[str]]] = [
            (CHARACTERS, {"owner": a}, {c1, c3}),
            (CHARACTERS, {"owner": b}, {c2}),
            (CHARACTERS, {"relation": f"editor:{c}"}, {c1}),
            (CHARACTERS, {"relation": "co_author:"}, {c3}),
            (CHARACTERS, {"listing_status": "public"}, {c1}),
            (CHARACTERS, {"listing_status": "private"}, {c2}),
            (CHARACTERS, {"listing_status": "in_review"}, {c3}),
            (SCENARIOS, {"owner": b}, {s2, s3}),
            (SCENARIOS, {"relation": "editor:"}, {s1, s3}),
            (SCENARIOS, {"relation": f"editor:{d}"}, {s3}),
            (SCENARIOS, {"label": "family"}, {s1, s2}),
            (SCENARIOS, {"label": "NOIR"}, {s1, s3}),
            (SCENARIOS, {"label": ["family", "noir"]}, {s1}),
            (SCENARIOS, {"listing_status": "private"}, {s2}),
            (SCENARIOS, {"owner": a, "listing_status": "public"}, {s1}),
        ]
        for asker in (a, b, "p-list-stranger", None):
            for base, every in published.items():
                assert set(await listed_ids(client, base, principal=asker)) == every, asker
            for base, filters, expected in cases:
                found = set(await listed_ids(client, base, principal=asker, **filters))
                assert found == expected, (asker, base, filters)
                assert found < published[base], (base, filters)

        # Unfiltered, the published list with the draft and archived ones is the whole table
        # (for scenarios, with the companions each character was created with, #116).
        whole: list[tuple[str, str, dict[str, Any]]] = [
            (CHARACTERS, "character_catalog", {}),
            (SCENARIOS, "scenario_catalog", {"include_companions": True}),
        ]
        for base, table, more in whole:
            shown = [
                await listed_ids(client, base, status=status, limit=2, **more)
                for status in ("published", "draft", "archived")
            ]
            assert shown[0] == await listed_ids(client, base, limit=2, **more)
            stored = await rows(db, f"SELECT id, status FROM catalog.{table}")  # noqa: S608
            assert sorted(i for page in shown for i in page) == sorted(str(r.id) for r in stored)
            for status, page in zip(("published", "draft", "archived"), shown, strict=True):
                assert len(page) == sum(1 for row in stored if row.status == status), status
        assert await listed_ids(client, CHARACTERS, status="draft") == [c4]
        assert await listed_ids(client, CHARACTERS, status="archived") == [c5]
        assert await listed_ids(client, SCENARIOS, status="draft") == [s4]

        # Under `status=draft`, `label` and `content_rating` read each entry's draft.
        for filters, expected_draft in (
            ({"label": "family"}, [s4]),
            ({"label": "noir"}, []),
            ({"content_rating": EVERYONE_ID}, [s4]),
            ({"content_rating": TEEN_ID}, []),
        ):
            found_draft = await listed_ids(client, SCENARIOS, status="draft", **filters)
            assert found_draft == expected_draft, filters

        # A listing-status change never touches a story made from the entry.
        story = await create_story(client, a, None)
        async with db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE characters SET source_catalog_id = :e, source_catalog_version = 1 "
                    "WHERE id = :c"
                ),
                {"e": c1, "c": story.ids["ada"]},
            )
        before = await _snapshot(db, story.id)
        for value in ("private", "in_review", "public"):
            await ok(await set_listing_status(client, CHARACTERS, c1, value, principal=b))
        assert await _snapshot(db, story.id) == before
        archived = await client.post(f"/stories/{story.id}/archive", headers=headers(a))
        assert archived.status_code == 200, archived.text
        assert await _snapshot(db, story.id) != before


# -- #114: personas -----------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-6")
async def test_i_21__persona(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A saved persona is owned by its creator. Another principal, holding no relation to it,
    deletes it, and a story whose player character was created with it stays byte-identical:
    every row of the story, and every ``stories`` and ``characters`` row. The persona is gone,
    with its relations.

    Complement asserted: the snapshot does see a change the story itself makes (archiving the
    story), so the equal snapshots are not vacuous."""
    async with respond_client(stub_settings, engine) as (client, _):
        a, b = _unique("p-persona-a"), _unique("p-persona-b")
        persona = await ok(
            await client.post("/personas", json=persona_body(), headers=headers(a)), 201
        )
        assert persona["owner_principal"] == a
        await ok(
            await client.post(
                f"/personas/{persona['id']}/relations",
                json={"principal": a, "relation": "player"},
                headers=headers(a),
            ),
            201,
        )
        story = await create_story(client, a, None)
        await copy_into(engine, story.ids["wren"], persona)
        before = await _snapshot(engine, story.id)
        deleted = await client.delete(f"/personas/{persona['id']}", headers=headers(b))
        assert deleted.status_code == 204, deleted.text
        assert await _snapshot(engine, story.id) == before
        assert (await client.get(f"/personas/{persona['id']}", headers=READ)).status_code == 404
        relations = await scalar(
            engine,
            "SELECT count(*) FROM entity_relations WHERE entity_id = :p",
            p=persona["id"],
        )
        assert relations == 0
        archived = await client.post(f"/stories/{story.id}/archive", headers=headers(a))
        assert archived.status_code == 200, archived.text
        assert await _snapshot(engine, story.id) != before


# -- #117: lookup and offboarding -----------------------------------------------------------------

DOCS_ACTIONS = {
    "stories": "delete",
    "personas": "delete",
    "characters": "transfer",
    "scenarios": "transfer",
    "media": "delete",
    "engine_profiles": "transfer",
    "relations": "remove",
}
"""docs/04 I-21's offboarding of A."""


@pytest.mark.integration
@pytest.mark.part("M3", "FR-AUTH-7")
async def test_i_21__offboarding(pg_url: str) -> None:
    """A's lookup lists what A owns and is related to, by type, with counts. Offboarding A with
    docs/04's actions: the dry run counts and warns of the story shared with E (and, with
    profiles deleted instead, of all three warning kinds), and changes nothing; without
    ``ignore_warnings`` the request answers the warnings and changes nothing; with it, the job
    carries out every action, A's turns in a story it did not own still name A, a retry under
    the key changes nothing, and the report lists every entity with nothing left linked to A.

    Complement asserted: the curator, the player and the other owner keep what is theirs, and
    the image another owner's character shows is retired yet still served."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        a, k, e = w.a, w.curator, w.player
        found = await lookup(s.client, a)
        assert {key: page["count"] for key, page in found["owned"].items()} == {
            "stories": 2,
            "characters": 1,
            "scenarios": 1,
            "personas": 1,
            "media": 3,
            "engine_profiles": 1,
        }
        assert found["owned"]["characters"]["items"][0]["companion"] == {"id": w.companion}
        assert found["related"]["characters"]["items"] == [
            {"id": w.others_character, "relations": ["editor"]}
        ]
        body = {"principal": a, "actions": DOCS_ACTIONS, "transfer_to": k}
        before = await table_counts(s.db)
        rows_before = await table_contents(s.db)
        dry, _ = await offboard(s.client, {**body, "dry_run": True}, status=200)
        assert dry["applied"] is False
        assert dry["counts"]["stories"] == {"delete": 2, "transfer": 0, "keep": 0}
        assert dry["counts"]["media"] == {"delete": 2, "transfer": 1, "keep": 0}
        assert dry["counts"]["engine_profiles"] == {"delete": 0, "transfer": 1, "keep": 0}
        assert dry["counts"]["relations"] == {"remove": 1, "keep": 0}
        kinds = [(x["kind"], x["entity_id"], x["principals"]) for x in dry["warnings"]]
        assert kinds == [
            ("shared_story", w.shared_story, [e]),
            ("media_referenced", w.shared_image, [w.other]),
        ]
        three, _ = await offboard(
            s.client,
            {**body, "actions": {**DOCS_ACTIONS, "engine_profiles": "delete"}, "dry_run": True},
            status=200,
        )
        assert [x["kind"] for x in three["warnings"]] == [
            "shared_story",
            "profile_in_use",
            "media_referenced",
        ]
        held, _ = await offboard(s.client, body, status=200)
        assert held["applied"] is False
        assert held["warnings"] == dry["warnings"]
        assert await table_counts(s.db) == before
        assert await table_contents(s.db) == rows_before
        key = new_ulid()
        queued, _ = await offboard(s.client, {**body, "ignore_warnings": True}, status=202, key=key)
        await run_jobs(s.db)
        done = await report(s.client, queued["job_id"])
        assert done["status"] == "done"
        gone = await scratch_scalar(
            s.db,
            "SELECT count(*) FROM stories WHERE id IN (:x, :y)",
            x=w.shared_story,
            y=w.own_story,
        )
        assert gone == 0
        assert (await lookup(s.client, e))["related"]["stories"]["count"] == 0
        curator = await lookup(s.client, k)
        assert [i["id"] for i in curator["owned"]["characters"]["items"]] == [w.character]
        assert curator["owned"]["characters"]["items"][0]["companion"] == {"id": w.companion}
        assert [i["id"] for i in curator["owned"]["scenarios"]["items"]] == [w.scenario]
        assert [i["id"] for i in curator["owned"]["media"]["items"]] == [w.kept_image]
        assert [i["id"] for i in curator["owned"]["engine_profiles"]["items"]] == [w.profile]
        state = await scratch_scalar(
            s.db, "SELECT state FROM media_assets WHERE id = :m", m=w.shared_image
        )
        assert state == "retired"
        served = await s.client.get(f"/media/{w.shared_image}/content", headers=READ)
        assert served.status_code == 200
        loose = await scratch_scalar(
            s.db, "SELECT state FROM media_assets WHERE id = :m", m=w.loose_image
        )
        assert loose == "retired"
        assert (await lookup(s.client, a))["related"]["characters"]["count"] == 0
        assert (await lookup(s.client, w.other))["owned"]["characters"]["count"] == 1
        turns = await scratch_scalar(
            s.db,
            "SELECT count(*) FROM events WHERE story_id = :s AND principal = :p",
            s=w.others_story,
            p=a,
        )
        assert turns > 0
        found = done["report"]
        assert sorted(found["deleted"]["stories"]) == sorted([w.shared_story, w.own_story])
        assert found["deleted"]["personas"] == [w.persona]
        assert sorted(found["deleted"]["media"]) == sorted([w.shared_image, w.loose_image])
        assert found["transferred"]["characters"] == [{"id": w.character, "to": k}]
        assert [x["id"] for x in found["transferred"]["scenarios"]] == [w.companion, w.scenario]
        assert found["transferred"]["media"] == [
            {"id": w.kept_image, "to": k, "reason": "referenced"}
        ]
        assert found["transferred"]["engine_profiles"] == [{"id": w.profile, "to": k}]
        assert found["removed"]["relations"][0]["id"] == w.others_character
        assert all(not ids for ids in found["remaining"]["owned"].values())
        assert all(not ids for ids in found["remaining"]["related"].values())
        settled = await table_counts(s.db)
        again, replay = await offboard(
            s.client, {**body, "ignore_warnings": True}, status=202, key=key
        )
        assert again["job_id"] == queued["job_id"]
        assert replay.headers["Idempotent-Replay"] == "true"
        await run_jobs(s.db)
        assert await table_counts(s.db) == settled


@pytest.mark.integration
@pytest.mark.part("M3", "FR-AUTH-7")
async def test_i_21__offboarding_resumes(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker dies after acting on the second entity (a story deletion) and before the job
    records it. A fresh worker resumes from the progress row: it records that story as deleted
    without acting again and finishes the rest. Every entity is acted on exactly once, counted
    by its ownership events, and the report lists each once.

    Complement asserted: the first worker did stop midway (the job was not done, and the second
    story was already gone)."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        real = offboarding.apply
        calls: list[str] = []

        async def dies_after_the_second(*args: Any, **kwargs: Any) -> Any:
            outcomes = await real(*args, **kwargs)
            calls.append(str(args[2]["id"]))
            if len(calls) == 2:
                raise RuntimeError("the worker died")
            return outcomes

        monkeypatch.setattr(offboarding, "apply", dies_after_the_second)
        queued, _ = await offboard(
            s.client,
            {
                "principal": w.a,
                "actions": DOCS_ACTIONS,
                "transfer_to": w.curator,
                "ignore_warnings": True,
            },
            status=202,
        )
        job_id = queued["job_id"]
        await run_jobs(s.db)
        midway = await report(s.client, job_id)
        assert midway["status"] == "running"
        assert midway["progress"]["done"] == 1
        assert midway["current"] == {"type": "stories", "id": calls[1]}
        assert (
            await scratch_scalar(s.db, "SELECT count(*) FROM stories WHERE id = :s", s=calls[1])
            == 0
        )
        monkeypatch.setattr(offboarding, "apply", real)
        await retry_now(s.db)
        await run_jobs(s.db, fresh=worker(s.db))
        done = await report(s.client, job_id)
        assert done["status"] == "done"
        assert done["progress"] == {"done": 10, "total": 10}
        assert await ownership_changes(s.db, job_id) == {
            (w.shared_story, "deleted"): 1,
            (w.own_story, "deleted"): 1,
            (w.character, "transfer"): 1,
            (w.companion, "transfer"): 1,
            (w.scenario, "transfer"): 1,
            (w.persona, "deleted"): 1,
            (w.kept_image, "transfer"): 1,
            (w.profile, "transfer"): 1,
            (w.others_character, "relation_removed"): 1,
        }
        found = done["report"]
        assert sorted(found["deleted"]["stories"]) == sorted([w.shared_story, w.own_story])
        listed = [
            record if isinstance(record, str) else record["id"]
            for bucket in ("deleted", "transferred", "removed", "kept", "skipped")
            for records in found[bucket].values()
            for record in records
        ]
        assert len(listed) == len(set(listed)) == 11


# -- #119: story creation from a scenario ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("listing", ["private", "in_review", "public"])
@pytest.mark.part("M3", "FR-AUTH-5,6,8")
async def test_i_21__story_creation(
    stub_settings: Settings, engine: AsyncEngine, listing: str
) -> None:
    """Creating a story from a scenario: the story is owned by the requester, or by the owner
    the request names; the player's principal is recorded as a ``player`` relation on the story
    (with the acting principal), which a list filter by relation finds. The chosen entries'
    owners and listing statuses are never consulted: a scenario and a catalog character owned
    by two other principals, at each listing status, create a story all the same.

    Complements asserted: the story is absent from a list filtered by a relation another
    principal holds; the owner named by one creation is not the next creation's."""
    async with respond_client(stub_settings, engine) as (client, _):
        stranger, curator = f"p-own-{new_ulid()}", f"p-cur-{new_ulid()}"
        mara = await g.catalog_character_id(client, f"Mara {new_ulid()}", owner=stranger)
        scenario_id, _ = await g.published_scenario(
            client,
            g.scenario_body(g.role("mother"), g.role("player", kind="player")),
            owner=curator,
        )
        for base, entry in (("/catalog/characters", mara), ("/catalog/scenarios", scenario_id)):
            changed = await set_listing_status(client, base, entry, listing)
            assert changed.status_code == 200, changed.text
        player_principal = f"p-play-{new_ulid()}"
        request = g.from_scenario(
            scenario_id,
            slots={"mother": g.catalog_slot(mara)},
            players={"player": g.player(player_principal)},
        )
        made = await g.create_from(client, request)
        story = await client.get(f"/stories/{made['story_id']}", headers=g.READ)
        assert story.json()["owner_principal"] == g.AUTHOR
        relations = await client.get(f"/stories/{made['story_id']}/relations", headers=g.READ)
        assert relations.status_code == 200, relations.text
        held = [(r["principal"], r["relation"], r["created_by"]) for r in relations.json()["items"]]
        assert held == [(player_principal, "player", g.AUTHOR)]
        found = await client.get(
            "/stories", params={"relation": f"player:{player_principal}"}, headers=g.READ
        )
        assert [s["id"] for s in found.json()["items"]] == [made["story_id"]]
        nobody = await client.get(
            "/stories", params={"relation": f"player:{stranger}"}, headers=g.READ
        )
        assert nobody.json()["items"] == []

        owned = await g.create_from(client, {**request, "owner": curator})
        assert (await client.get(f"/stories/{owned['story_id']}", headers=g.READ)).json()[
            "owner_principal"
        ] == curator
        again = await g.create_from(client, request)
        assert (await client.get(f"/stories/{again['story_id']}", headers=g.READ)).json()[
            "owner_principal"
        ] == g.AUTHOR
