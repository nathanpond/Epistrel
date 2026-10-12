"""C-15: the catalog and story-creation API (docs/04 §5), its M3 parts.

Every C-15 (M3) part lives in this file (traceability counts a test ID per milestone in one file).
#103's part, ``test_c_15__catalog_lifecycle``: a catalog character's drafts and immutable
published versions (FR-CAT-1,3) and the ``card``/``full`` projections (FR-SCN-10). The details
are in ``tests/integration/test_catalog_characters.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.catalog_support import (
    BASE,
    CARD_KEYS,
    READ,
    VERSION_KEYS,
    WRITE,
    character,
    create,
    publish,
    read,
    save,
    version_rows,
)
from tests.event_support import execute_in_tx, sqlstate
from tests.story_support import rest

PLANTED = "counts the silver twice before the harbour wakes"
"""A phrase only the private definition holds."""


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-1,3; FR-SCN-10")
async def test_c_15__catalog_lifecycle(settings: Settings, engine: AsyncEngine) -> None:
    """A catalog character starts as an editable draft, is published as version 1, edited and
    published as version 2; version 1 reads unchanged afterwards and its row cannot be updated;
    the version list names both, each with its publish time and publisher. A read with no
    projection is the public card of the ``published`` entry: exactly its nine keys and never a
    definition field (the phrase planted in the private personality appears in no card), while
    ``full`` returns it; an archived entry still reads in both projections.

    Complement asserted: the phrase is present in ``full``, so its absence from every card is
    the projection's doing."""
    async with rest(settings, engine) as client:
        created = await create(
            client, character(definition={"personality": f"Generous, yet she {PLANTED}."})
        )
        cid = created["id"]
        assert (created["status"], created["draft_revision"], created["version"]) == (
            "draft",
            1,
            None,
        )
        edited = await save(client, cid, 1, public_description="Keeper of the Gull Rock light.")
        assert (edited.status_code, edited.json()["draft_revision"]) == (200, 2)
        first = await publish(client, cid, 2)
        assert (first.status_code, first.json()["version"]) == (201, 1)
        version_1 = (await read(client, cid, version=1, projection="full")).json()

        edited = await save(client, cid, 3, public_description="Keeper, retired.")
        assert edited.status_code == 200
        second = await publish(client, cid, 4)
        assert (second.status_code, second.json()["version"]) == (201, 2)

        again = (await read(client, cid, version=1, projection="full")).json()
        for key in ("version", "published_at", "name", "public_description", "definition"):
            assert again[key] == version_1[key], key
        stored = await version_rows(engine, cid)
        with pytest.raises(DBAPIError) as refused:
            await execute_in_tx(
                engine,
                (
                    "UPDATE catalog.character_versions SET content = '{}'::jsonb "
                    "WHERE catalog_id = :c AND version = 1",
                    {"c": cid},
                ),
            )
        assert sqlstate(refused.value) == "EPI01"
        assert await version_rows(engine, cid) == stored
        listed = await client.get(f"{BASE}/{cid}/versions", headers=READ)
        assert [set(v) for v in listed.json()["items"]] == [VERSION_KEYS, VERSION_KEYS]
        assert [(v["version"], v["published_at"]) for v in listed.json()["items"]] == [
            (1, version_1["published_at"]),
            (2, second.json()["published_at"]),
        ]

        card = await read(client, cid)
        assert card.status_code == 200
        assert set(card.json()) == CARD_KEYS
        assert (
            card.json()["version"],
            card.json()["status"],
            card.json()["public_description"],
        ) == (2, "published", "Keeper, retired.")
        assert PLANTED not in card.text
        full = await read(client, cid, projection="full")
        assert PLANTED in full.json()["definition"]["personality"]

        archived = await client.post(f"{BASE}/{cid}/archive", headers=WRITE)
        assert archived.json()["status"] == "archived"
        for projection in ("card", "full"):
            found = await read(client, cid, projection=projection)
            assert (found.status_code, found.json()["status"]) == (200, "archived")
            assert (PLANTED in found.text) == (projection == "full")
