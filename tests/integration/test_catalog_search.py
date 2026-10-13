"""Listing, searching and the listing status in detail (#110). The milestone parts are
``test_c_15__listing_status`` (``tests/conformance/test_c_15.py``) and
``test_i_21__listing_filters`` (``tests/integration/test_i_21.py``); the query builder's
repository check is ``tests/test_catalog_listing.py``.

Each test runs on its own scratch database, so a list holds exactly what the test put there.
A catalog character's labels, aliases and content rating have no request field until #115, so
the tests that need them plant a published version in #115's planned shape
(``metadata.labels``, ``metadata.aliases``, ``metadata.content_rating_id``)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.api import create_app
from epistrel.core.catalog import listing
from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest
from epistrel.core.seeds.ratings import EVERYONE_ID, TEEN_ID
from epistrel.core.tables.ownership import EntityType
from epistrel.engine import Engine
from epistrel.testing import pauses
from tests import scenario_support as scenarios
from tests.catalog_support import (
    BASE,
    CARD_KEYS,
    LIST_EXTRA_KEYS,
    READ,
    WRITE,
    character,
    count,
    every,
    headers,
    ids,
    scratch_catalog,
    set_listing_status,
)
from tests.catalog_support import (
    listing as list_page,
)
from tests.event_support import rows

CHARACTERS = BASE
SCENARIOS = scenarios.BASE
PLANTED = "counts the silver twice before the harbour wakes"
"""A phrase only a private definition holds."""
REPLAY = "Idempotent-Replay"


@dataclass
class Space:
    """A scratch deployment: the REST client, the database and the app's Engine."""

    client: httpx.AsyncClient
    db: AsyncEngine
    lib: Engine


@pytest.fixture
async def space(pg_url: str) -> AsyncIterator[Space]:
    async with scratch_catalog(pg_url) as (settings, db):
        app = create_app(settings)
        lib: Engine = app.state.epistrel
        lib.bind(db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Space(client, db, lib)
        await lib.models.aclose()


async def _ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


async def _character(
    client: httpx.AsyncClient, name: str, *, owner: str = "p-a", publish: bool = True, **fields: Any
) -> str:
    created = await _ok(
        await client.post(CHARACTERS, json=character(name, owner=owner, **fields), headers=WRITE),
        201,
    )
    if publish:
        await _ok(
            await client.post(
                f"{CHARACTERS}/{created['id']}/publish",
                json={"draft_revision": 1},
                headers=WRITE,
            ),
            201,
        )
    return str(created["id"])


async def _scenario(
    client: httpx.AsyncClient, body: dict[str, Any], *, publish: bool = True
) -> str:
    created = await _ok(await client.post(SCENARIOS, json=body, headers=WRITE), 201)
    if publish:
        await _ok(
            await client.post(
                f"{SCENARIOS}/{created['id']}/publish",
                json={"draft_revision": 1},
                headers=WRITE,
            ),
            201,
        )
    return str(created["id"])


async def _plant(db: AsyncEngine, entry_id: str, content: dict[str, Any]) -> None:
    """Publish ``content`` as the next version of catalog character ``entry_id``, as #115's
    shape will hold it (no request field takes these keys yet)."""
    async with db.begin() as conn:
        version = await conn.scalar(
            text(
                "SELECT coalesce(max(version), 0) + 1 FROM catalog.character_versions "
                "WHERE catalog_id = :c"
            ),
            {"c": entry_id},
        )
        await conn.execute(
            text(
                "INSERT INTO catalog.character_versions (catalog_id, version, content, "
                "published_at, published_by) VALUES (:c, :v, CAST(:doc AS jsonb), now(), 'p-a')"
            ),
            {"c": entry_id, "v": version, "doc": json.dumps(content)},
        )
        await conn.execute(
            text(
                "UPDATE catalog.character_catalog SET status = 'published', "
                "current_version = :v, updated_at = now() WHERE id = :c"
            ),
            {"c": entry_id, "v": version},
        )


async def _updated(client: httpx.AsyncClient, entry_id: str) -> datetime:
    """A catalog character's ``updated_at``, as its ``full`` read shows it."""
    full = await _ok(
        await client.get(f"{CHARACTERS}/{entry_id}", params={"projection": "full"}, headers=READ)
    )
    return datetime.fromisoformat(full["updated_at"])


async def _companion_of(db: AsyncEngine, scenario_id: str, character_id: str) -> None:
    """Make ``scenario_id`` the companion of ``character_id`` (#116 does that at creation)."""
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE catalog.scenario_catalog SET companion_of = :c, listing_status = "
                "(SELECT listing_status FROM catalog.character_catalog WHERE id = :c) "
                "WHERE id = :s"
            ),
            {"c": character_id, "s": scenario_id},
        )


# -- search -------------------------------------------------------------------------------------


async def test_catalog_search(space: Space) -> None:
    """``q=garden`` finds the character whose public description says it, not the one whose
    private definition does (which ``full`` returns); labels filter with AND semantics; the
    owner and relation filters each keep exactly their entries; and paging with a small
    ``limit`` returns every entry exactly once, newest first, for a list and for a search.

    Complements asserted: the private phrase is in the definition (so its absence from the
    results is the index's doing); each single label lists more than the two together."""
    client = space.client
    shown = await _character(
        client, "Mara Venn", public_description="Keeps the garden on the rock."
    )
    hidden = await _character(
        client,
        "Ivo Brand",
        definition={"personality": f"Tends a secret garden; he {PLANTED}."},
    )
    found = await _ok(await list_page(client, CHARACTERS, q="garden"))
    assert [item["id"] for item in found["items"]] == [shown]
    assert found["items"][0]["rank"] > 0
    assert await ids(client, CHARACTERS, q=PLANTED) == []
    full = await _ok(
        await client.get(f"{CHARACTERS}/{hidden}", params={"projection": "full"}, headers=READ)
    )
    assert PLANTED in full["definition"]["personality"]

    def labelled(name: str, *labels: str) -> dict[str, Any]:
        return scenarios.scenario(name, metadata={"labels": list(labels)})

    both = await _scenario(client, labelled("Harbour", "family", "noir"))
    family = await _scenario(client, labelled("Orchard", "Family"))
    noir = await _scenario(client, labelled("Lighthouse", "noir"))
    assert set(await ids(client, SCENARIOS, label="family")) == {both, family}
    assert set(await ids(client, SCENARIOS, label="noir")) == {both, noir}
    assert await ids(client, SCENARIOS, label=["family", "noir"]) == [both]
    assert await ids(client, SCENARIOS, label=["family", "noir", "comedy"]) == []

    owned = await _character(client, "Wren Hale", owner="p-b")
    await _ok(
        await client.post(
            f"{CHARACTERS}/{owned}/relations",
            json={"principal": "p-c", "relation": "editor"},
            headers=WRITE,
        ),
        201,
    )
    assert await ids(client, CHARACTERS, owner="p-b") == [owned]
    assert await ids(client, CHARACTERS, relation="editor:p-c") == [owned]
    assert await ids(client, CHARACTERS, relation="editor:p-d") == []
    assert set(await ids(client, CHARACTERS, owner="p-a")) == {shown, hidden}

    many = [
        await _character(client, f"Sailor {n}", public_description="A sailor.") for n in range(7)
    ]
    paged = await ids(client, CHARACTERS, limit=2)
    assert len(paged) == len(set(paged)) == 10
    assert set(paged) == {shown, hidden, owned, *many}
    assert paged[:7] == list(reversed(many))  # newest first
    searched = await every(client, CHARACTERS, q="sailor", limit=3)
    assert sorted(item["id"] for item in searched) == sorted(many)
    ranks = [item["rank"] for item in searched]
    assert ranks == sorted(ranks, reverse=True)


async def test_search_reads_only_the_latest_published_public_card(space: Space) -> None:
    """``q`` reads the latest published version of each entry's public card. A draft's edit is
    found only once published, and the version it replaced is no longer found by its old words.
    A scenario's name, labels, role public labels and public description are searched; its
    world (description and entity aliases), seeds and director guidance never are. A name
    match ranks above a description match. Quoted phrases and ``-word`` follow
    ``websearch_to_tsquery``; a ``q`` with no word lists nothing; archived entries are
    searched when asked for.

    Complement asserted: each private phrase is in the scenario's ``full`` read."""
    client = space.client
    mara = await _character(client, "Mara Venn", public_description="Keeps the lamp.")
    edited = await client.patch(
        f"{CHARACTERS}/{mara}",
        json={"draft_revision": 2, "public_description": "Keeps the orchard."},
        headers=WRITE,
    )
    assert edited.status_code == 200, edited.text
    assert await ids(client, CHARACTERS, q="orchard") == []
    assert await ids(client, CHARACTERS, q="lamp") == [mara]
    await _ok(
        await client.post(
            f"{CHARACTERS}/{mara}/publish", json={"draft_revision": 3}, headers=WRITE
        ),
        201,
    )
    assert await ids(client, CHARACTERS, q="orchard") == [mara]
    assert await ids(client, CHARACTERS, q="lamp") == []

    complete = await _scenario(client, scenarios.complete())
    full = (await scenarios.read(client, complete, projection="full")).text
    for phrase in (*scenarios.PRIVATE_PHRASES, "the deed"):
        assert phrase in full, phrase
        assert await ids(client, SCENARIOS, q=f'"{phrase}"') == [], phrase
    for word in ("Harrow", "aunt", "eldest", "will", "dark"):
        assert await ids(client, SCENARIOS, q=word) == [complete], word

    named = await _character(client, "Harbour Jones", public_description="A pilot.")
    described = await _character(client, "Tess Lyle", public_description="Rows the harbour.")
    ranked = await _ok(await list_page(client, CHARACTERS, q="harbour"))
    assert [item["id"] for item in ranked["items"]] == [named, described]
    assert ranked["items"][0]["rank"] > ranked["items"][1]["rank"]
    assert await ids(client, CHARACTERS, q='"rows the harbour"') == [described]
    assert await ids(client, CHARACTERS, q='"the rows harbour"') == []
    assert await ids(client, CHARACTERS, q="harbour -pilot") == [described]
    assert await ids(client, CHARACTERS, q="pilot or orchard") == [named, mara]
    nothing = await _ok(await list_page(client, CHARACTERS, q="!!! ..."))
    assert nothing == {"items": [], "next_cursor": None}

    await _ok(await client.post(f"{CHARACTERS}/{described}/archive", headers=WRITE))
    assert await ids(client, CHARACTERS, q="harbour") == [named]
    assert await ids(client, CHARACTERS, q="harbour", status="archived") == [described]


async def test_character_search_reads_labels_and_aliases(space: Space) -> None:
    """In #115's planned shape (planted here), a character's aliases and labels are searched
    with its name and public description, its labels filter, and so does its content rating:
    exactly that rating, no other. A rating that does not exist is 422 at the filter."""
    client = space.client
    meg = await _character(client, "Margaret Lowe", publish=False)
    await _plant(
        space.db,
        meg,
        {
            "public_card": {"name": "Margaret Lowe", "public_description": "Runs the inn."},
            "metadata": {
                "aliases": ["Old Meg"],
                "labels": ["quiet", "stern"],
                "content_rating_id": TEEN_ID,
            },
        },
    )
    other = await _character(client, "Ivo Brand")
    assert await ids(client, CHARACTERS, q="meg") == [meg]
    assert await ids(client, CHARACTERS, q="stern") == [meg]
    assert await ids(client, CHARACTERS, q="inn") == [meg]
    assert await ids(client, CHARACTERS, label="quiet") == [meg]
    assert await ids(client, CHARACTERS, label=["quiet", "chatty"]) == []
    assert await ids(client, CHARACTERS, content_rating=TEEN_ID) == [meg]
    assert await ids(client, CHARACTERS, content_rating=EVERYONE_ID) == []
    assert set(await ids(client, CHARACTERS)) == {meg, other}
    unknown = await list_page(client, CHARACTERS, content_rating="0" * 26)
    assert _error(unknown)[:2] == (422, "validation_error")
    assert _error(unknown)[2]["field"] == "query.content_rating"

    rated = await _scenario(
        client,
        scenarios.scenario("Rated", metadata={"labels": ["noir"], "content_rating_id": TEEN_ID}),
    )
    await _scenario(client, scenarios.scenario("Unrated"))
    assert await ids(client, SCENARIOS, content_rating=TEEN_ID) == [rated]


# -- the cards ----------------------------------------------------------------------------------


async def test_list_items_are_the_single_read_cards(space: Space) -> None:
    """Every listed item is exactly the card a single read answers (the latest published
    version; a never-published entry's draft, ``version`` null; an archived entry's latest
    version), plus the entry's ``owner_principal`` and ``updated_at`` as ``full`` reads them,
    and ``rank`` only on a search."""
    client = space.client
    mara = await _character(client, "Mara Venn")
    draft = await _character(client, "Ada Lowe", publish=False)
    archived = await _character(client, "Tom Reed")
    await _ok(await client.post(f"{CHARACTERS}/{archived}/archive", headers=WRITE))
    complete = await _scenario(client, scenarios.complete())
    unpublished = await _scenario(client, scenarios.scenario("Attic"), publish=False)
    expected = [
        (CHARACTERS, mara, "published", {}),
        (CHARACTERS, draft, "draft", {"draft": "true"}),
        (CHARACTERS, archived, "archived", {}),
        (SCENARIOS, complete, "published", {}),
        (SCENARIOS, unpublished, "draft", {"draft": "true"}),
    ]
    for base, entry_id, status, read_as in expected:
        (item,) = [i for i in await every(client, base, status=status) if i["id"] == entry_id]
        card = await _ok(await client.get(f"{base}/{entry_id}", params=read_as, headers=READ))
        assert {k: v for k, v in item.items() if k not in LIST_EXTRA_KEYS} == card, entry_id
        full = await _ok(
            await client.get(
                f"{base}/{entry_id}", params={**read_as, "projection": "full"}, headers=READ
            )
        )
        assert (item["owner_principal"], item["updated_at"]) == (
            full["owner_principal"],
            full["updated_at"],
        )
    (character_item,) = (await _ok(await list_page(client, CHARACTERS)))["items"]
    assert set(character_item) == CARD_KEYS | LIST_EXTRA_KEYS
    (found,) = (await _ok(await list_page(client, CHARACTERS, q="mara")))["items"]
    assert set(found) == CARD_KEYS | LIST_EXTRA_KEYS | {"rank"}
    projected = await list_page(client, CHARACTERS, projection="full")
    assert _error(projected)[:2] == (422, "validation_error")
    assert (await list_page(client, CHARACTERS, projection="card")).status_code == 200


async def test_statuses_and_companions(space: Space) -> None:
    """A list shows published entries; ``status`` shows the drafts or the archived ones.
    Companion scenarios are left out of every scenario list, a search or a filtered one
    included, unless ``include_companions=true``; a companion still reads directly."""
    client = space.client
    mara = await _character(client, "Mara Venn")
    authored = await _scenario(client, scenarios.scenario("Sunday Dinner"))
    companion = await _scenario(client, scenarios.scenario("Sunday Supper"))
    await _companion_of(space.db, companion, mara)
    drafted = await _scenario(client, scenarios.scenario("Sunday Lunch"), publish=False)
    assert await ids(client, SCENARIOS) == [authored]
    assert await ids(client, SCENARIOS, include_companions=True) == [companion, authored]
    assert await ids(client, SCENARIOS, q="sunday") == [authored]
    assert set(await ids(client, SCENARIOS, q="sunday", include_companions=True)) == {
        authored,
        companion,
    }
    assert await ids(client, SCENARIOS, label="family") == [authored]
    assert await ids(client, SCENARIOS, label="family", include_companions=True) == [
        companion,
        authored,
    ]
    assert await ids(client, SCENARIOS, status="draft") == [drafted]
    card = await _ok(await client.get(f"{SCENARIOS}/{companion}", headers=READ))
    assert card["companion_of"] == mara
    listed = await every(client, SCENARIOS, include_companions=True)
    assert {item["id"]: item["companion_of"] for item in listed} == {
        companion: mara,
        authored: None,
    }


# -- requests -----------------------------------------------------------------------------------


async def test_list_requests_are_checked(space: Space) -> None:
    """Each malformed listing request is 422 at its field, nothing listed: a blank or overlong
    ``q``, ``q`` with ``status=draft``, ``limit`` out of 1 to 200, an unknown status, a
    malformed label or rating id, a listing status the deployment does not have
    (``invalid_listing_status``), and a cursor no listing gave or one given for other filters or
    the other kind (``cursor_invalid``). Unknown parameters are ignored. The library refuses a
    type that is not a catalog kind."""
    client = space.client
    for n in range(3):
        await _character(client, f"Sailor {n}")
        await _scenario(client, scenarios.scenario(f"Voyage {n}"))
    refusals: list[tuple[str, dict[str, Any], str, str]] = [
        (CHARACTERS, {"q": ""}, "validation_error", "query.q"),
        (CHARACTERS, {"q": "x" * 201}, "validation_error", "query.q"),
        (CHARACTERS, {"q": "sailor", "status": "draft"}, "validation_error", "query.q"),
        (CHARACTERS, {"limit": 0}, "validation_error", "query.limit"),
        (CHARACTERS, {"limit": 201}, "validation_error", "query.limit"),
        (SCENARIOS, {"status": "deleted"}, "validation_error", "query.status"),
        (SCENARIOS, {"label": "no:colons"}, "validation_error", "query.label[0]"),
        (SCENARIOS, {"label": "   "}, "validation_error", "query.label[0]"),
        (CHARACTERS, {"content_rating": "rating-1"}, "validation_error", "query.content_rating"),
        (
            CHARACTERS,
            {"listing_status": "featured"},
            "invalid_listing_status",
            "query.listing_status",
        ),
        (SCENARIOS, {"relation": "editor"}, "validation_error", "query.relation[0]"),
        (CHARACTERS, {"cursor": "not a cursor"}, "cursor_invalid", "query.cursor"),
        (CHARACTERS, {"cursor": "x" * 257}, "validation_error", "query.cursor"),
    ]
    for base, params, code, field in refusals:
        status, found, details = _error(await list_page(client, base, **params))
        assert (status, found, details["field"]) == (422, code, field), params
    featured = _error(await list_page(client, CHARACTERS, listing_status="featured"))[2]
    assert featured["allowed_values"] == ["private", "in_review", "public"]

    first = await _ok(await list_page(client, CHARACTERS, limit=2, label="sailor"))
    assert first["items"] == []
    page = await _ok(await list_page(client, CHARACTERS, limit=2))
    cursor = page["next_cursor"]
    assert cursor is not None
    same = await _ok(await list_page(client, CHARACTERS, limit=1, cursor=cursor))
    assert len(same["items"]) == 1, "limit is not bound to the cursor"
    for base, params in (
        (CHARACTERS, {"owner": "p-a"}),
        (CHARACTERS, {"q": "sailor"}),
        (CHARACTERS, {"status": "archived"}),
        (SCENARIOS, {}),
    ):
        status, code, details = _error(await list_page(client, base, cursor=cursor, **params))
        assert (status, code) == (422, "cursor_invalid"), (base, params)
        assert details["reason"] == ("malformed" if "q" in params else "filters_changed")
    ignored = await list_page(client, CHARACTERS, colour="blue", sort="name")
    assert len((await _ok(ignored))["items"]) == 3
    with pytest.raises(InvalidRequest) as refused:
        await space.lib.search_catalog(RequestContext(), EntityType.STORY)
    assert refused.value.details["field"] == "entity_type"


# -- listing status writes ----------------------------------------------------------------------


async def test_listing_status_writes(space: Space) -> None:
    """A listing-status change is idempotent under its key (a retry is replayed, one event);
    the value is checked before the key is claimed (a refused value under a used key is 422,
    not 409); the current value changes and records nothing; an archived entry changes too;
    the change moves ``updated_at`` (so the entry lists first) and records one ``control``
    operation by the requester. The body's value is trimmed, and a blank, overlong or
    malformed body is 422 at its field; an unknown entry is 404 (after the value's check).

    Complement asserted: the no-op left the ``ownership_events`` count and ``updated_at``
    alone."""
    client, db = space.client, space.db
    mara = await _character(client, "Mara Venn")
    ivo = await _character(client, "Ivo Brand")
    assert await ids(client, CHARACTERS) == [ivo, mara]
    before = await _updated(client, mara)

    key = "status-key-1"
    first = await set_listing_status(
        client, CHARACTERS, mara, "  public ", principal="p-ed", key=key
    )
    assert (await _ok(first))["listing_status"] == "public"
    again = await set_listing_status(client, CHARACTERS, mara, "public", principal="p-ed", key=key)
    assert again.json() == first.json()
    assert again.headers.get(REPLAY) == "true"
    events = "SELECT count(*) FROM ownership_events WHERE kind = 'listing_status'"
    assert await count(db, events) == 1
    (operation,) = await rows(
        db, "SELECT kind, principal FROM operations WHERE idempotency_key = :k", k=key
    )
    assert tuple(operation) == ("control", "p-ed")
    bad = await set_listing_status(client, CHARACTERS, mara, "bogus", principal="p-ed", key=key)
    assert _error(bad)[:2] == (422, "invalid_listing_status")
    after = await _updated(client, mara)
    assert after > before
    assert await ids(client, CHARACTERS) == [mara, ivo]

    noop_key = "status-key-2"
    noop = await set_listing_status(client, CHARACTERS, mara, "public", key=noop_key)
    assert (await _ok(noop))["listing_status"] == "public"
    assert await count(db, events) == 1
    assert (
        await count(db, "SELECT count(*) FROM operations WHERE idempotency_key = :k", k=noop_key)
        == 0
    )
    assert await _updated(client, mara) == after

    await _ok(await client.post(f"{CHARACTERS}/{ivo}/archive", headers=WRITE))
    archived = await set_listing_status(client, CHARACTERS, ivo, "in_review")
    assert (await _ok(archived))["listing_status"] == "in_review"

    ghost = "0" * 26
    assert _error(await set_listing_status(client, CHARACTERS, ghost, "public"))[:2] == (
        404,
        "not_found",
    )
    assert _error(await set_listing_status(client, SCENARIOS, ghost, "bogus"))[:2] == (
        422,
        "invalid_listing_status",
    )
    for body, field in (
        ({"value": ""}, "body.value"),
        ({"value": "   "}, "body.value"),
        ({"value": "x" * 41}, "body.value"),
        ({"value": "a\nb"}, "body.value"),
        ({"value": 7}, "body.value"),
        ({}, "body.value"),
        ({"value": "public", "why": "x"}, "body.why"),
    ):
        refused = await client.put(f"{CHARACTERS}/{mara}/listing-status", json=body, headers=WRITE)
        assert _error(refused)[0:2] == (422, "validation_error"), body
        assert _error(refused)[2]["field"] == field, body
    assert await count(db, events) == 2


async def test_companions_follow_and_hooks_run(space: Space) -> None:
    """A character's listing-status change moves its companion scenarios in the same
    transaction (one event each, by the acting principal, a companion already at the value
    left alone) and runs every registered hook once per entry it changes, inside that
    transaction; a companion's own change is 422 ``companion_follows_character`` and runs no
    hook. A scenario that is nobody's companion changes alone."""
    client, db = space.client, space.db
    mara = await _character(client, "Mara Venn")
    first = await _scenario(client, scenarios.scenario("Gull Rock"))
    second = await _scenario(client, scenarios.scenario("Tide Pool"), publish=False)
    ahead = await _scenario(client, scenarios.scenario("Low Water"))
    for companion in (first, second, ahead):
        await _companion_of(db, companion, mara)
    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE catalog.scenario_catalog SET listing_status = 'public' WHERE id = :s"),
            {"s": ahead},
        )
    calls: list[tuple[Any, ...]] = []

    async def hook(
        conn: AsyncConnection, kind: EntityType, entry_id: str, old: str, new: str, who: str
    ) -> None:
        calls.append((conn.in_transaction(), kind, entry_id, old, new, who))

    listing.on_listing_status_change(hook)
    try:
        moved = await set_listing_status(client, CHARACTERS, mara, "public", principal="p-ed")
        assert moved.status_code == 200, moved.text
        refused = await set_listing_status(client, SCENARIOS, first, "private")
        assert _error(refused)[:2] == (422, "companion_follows_character")
        assert _error(refused)[2] == {
            "entity_type": "scenario",
            "entity_id": first,
            "character_id": mara,
        }
    finally:
        listing.remove_listing_status_hook(hook)
    companions = sorted([first, second])
    assert calls == [
        (True, EntityType.CHARACTER, mara, "private", "public", "p-ed"),
        *((True, EntityType.SCENARIO, s, "private", "public", "p-ed") for s in companions),
    ]
    found = await rows(
        db,
        "SELECT entity_type, entity_id, old, new, acting_principal FROM ownership_events "
        "WHERE kind = 'listing_status' ORDER BY entity_type, entity_id",
    )
    assert [tuple(row) for row in found] == [
        ("character", mara, "private", "public", "p-ed"),
        *(("scenario", s, "private", "public", "p-ed") for s in companions),
    ]
    statuses = await rows(
        db,
        "SELECT id, listing_status FROM catalog.scenario_catalog WHERE companion_of = :c",
        c=mara,
    )
    assert dict(tuple(row) for row in statuses) == dict.fromkeys((first, second, ahead), "public")
    alone = await _scenario(client, scenarios.scenario("Inland"))
    assert (await _ok(await set_listing_status(client, SCENARIOS, alone, "public")))[
        "listing_status"
    ] == "public"


# -- the deployment's values ------------------------------------------------------------------


async def test_listing_status_values_setting(space: Space) -> None:
    """``GET /admin/settings`` shows ``listing_status_values`` (default ``private``,
    ``in_review``, ``public``). A write replaces the list: its values are trimmed, and an empty
    list, a repeated, blank, overlong or control-character value is 422 at the setting. A
    reorder is a change, recorded as one prospective configuration change, after which new
    entries start at the new first value; the same values again write nothing. Removing a
    value is 409 ``listing_status_in_use`` while any entry holds it, counting every holder:
    published, never published, archived, companion, either kind."""
    client, db = space.client, space.db
    shown = await _ok(await client.get("/admin/settings", headers=READ))
    assert shown["listing_status_values"] == ["private", "in_review", "public"]

    async def write(values: Any) -> httpx.Response:
        return await client.put(
            "/admin/settings", json={"listing_status_values": values}, headers=WRITE
        )

    for values in ([], ["a", "a"], ["a", "   "], ["x" * 41], ["a\tb\x01"], [7], "public"):
        refused = await write(values)
        assert refused.status_code == 422, (values, refused.text)
        assert _error(refused)[2]["field"].startswith("body.listing_status_values"), values
    changes = "SELECT count(*) FROM config_changes WHERE setting = 'listing_status_values'"
    assert await count(db, changes) == 0
    unchanged = await _ok(await write(["  private", "in_review ", "public"]))
    assert unchanged["listing_status_values"] == ["private", "in_review", "public"]
    assert await count(db, changes) == 0

    reordered = await _ok(await write(["public", "private", "in_review"]))
    assert reordered["config_revision"] == shown["config_revision"] + 1
    (change,) = await rows(
        db,
        "SELECT old, new, class, actor FROM config_changes WHERE setting = 'listing_status_values'",
    )
    assert tuple(change) == (
        ["private", "in_review", "public"],
        ["public", "private", "in_review"],
        "prospective",
        "p-catalog",
    )
    published = await _character(client, "Mara Venn")
    created = await _ok(await client.get(f"{CHARACTERS}/{published}", headers=READ))
    assert created["listing_status"] == "public"

    drafted = await _character(client, "Ada Lowe", publish=False)
    archived = await _character(client, "Tom Reed")
    await _ok(await client.post(f"{CHARACTERS}/{archived}/archive", headers=WRITE))
    companion = await _scenario(client, scenarios.scenario("Gull Rock"), publish=False)
    await _companion_of(db, companion, published)
    held = await write(["private", "in_review"])
    status, code, details = _error(held)
    assert (status, code, details["value"], details["count"]) == (
        409,
        "listing_status_in_use",
        "public",
        4,
    )
    for entry_id in (drafted, archived, published):
        await _ok(await set_listing_status(client, CHARACTERS, entry_id, "private"))
    assert (await _ok(await write(["private", "in_review"])))["listing_status_values"] == [
        "private",
        "in_review",
    ]


async def _lock_waiters(db: AsyncEngine, wanted: int, within: float = 10.0) -> None:
    """Return once ``wanted`` backends of this database wait on a lock."""
    deadline = time.monotonic() + within
    waiting = (
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    while await count(db, waiting) < wanted:
        assert time.monotonic() < deadline, "no request waited on the lock in time"
        await asyncio.sleep(0.02)


async def test_a_value_is_never_removed_while_a_change_sets_it(space: Space) -> None:
    """A change that found its value holds the deployment row ``FOR SHARE`` until it commits:
    a settings write removing that value meanwhile waits for it, then finds the value held
    (409 ``listing_status_in_use``). So no entry ever holds a value the deployment lacks."""
    client = space.client
    mara = await _character(client, "Mara Venn")
    with pauses.held(pauses.CATALOG_LISTING_STATUS_LOCKED) as hold:
        change = asyncio.ensure_future(set_listing_status(client, CHARACTERS, mara, "in_review"))
        await hold.arrived(1)
        removal = asyncio.ensure_future(
            client.put(
                "/admin/settings",
                json={"listing_status_values": ["private", "public"]},
                headers=headers(principal="p-admin"),
            )
        )
        await _lock_waiters(space.db, 1)
        hold.release()
        changed, removed = await asyncio.gather(change, removal)
    assert (await _ok(changed))["listing_status"] == "in_review"
    assert _error(removed)[:2] == (409, "listing_status_in_use")
    shown = await _ok(await client.get("/admin/settings", headers=READ))
    assert shown["listing_status_values"] == ["private", "in_review", "public"]
