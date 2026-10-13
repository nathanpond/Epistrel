"""C-15: the catalog and story-creation API (docs/04 §5), its M3 parts.

Every C-15 (M3) part lives in this file (traceability counts a test ID per milestone in one file).

- #103's part, ``test_c_15__catalog_lifecycle``: a catalog character's drafts and immutable
  published versions (FR-CAT-1,3) and the ``card``/``full`` projections (FR-SCN-10). The details
  are in ``tests/integration/test_catalog_characters.py``.
- #105's part, ``test_c_15__media_upload``: the media upload endpoint (FR-MEDIA-1, FR-API-6).
  The details are in ``tests/integration/test_media_assets.py``.
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
from tests.media_support import (
    READ as MEDIA_READ,
)
from tests.media_support import (
    blob_path,
    media_client,
    metadata,
    png,
    set_media,
    sha256,
    upload,
)
from tests.story_support import rest

PLANTED = "counts the silver twice before the harbour wakes"
"""A phrase only the private definition holds."""


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-1,3; FR-SCN-10")
async def test_c_15__catalog_lifecycle(settings: Settings, engine: AsyncEngine) -> None:
    """A catalog character starts as an editable draft, is published as version 1, edited (the
    edit shows only in a draft read until it is published) and published as version 2; version 1
    reads unchanged afterwards and its row cannot be updated; the version list names both, each
    with its publish time and publisher. A read with no projection is the public card of the
    ``published`` entry: exactly its nine keys and never a definition field (the phrase planted
    in the private personality appears in no card), while ``full`` returns it; an archived entry
    still reads in both projections.

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
        # Until it is published, the edit is the draft's alone: a read still serves version 1.
        shown = (await read(client, cid)).json()
        assert (shown["version"], shown["public_description"]) == (
            1,
            "Keeper of the Gull Rock light.",
        )
        pending = (await read(client, cid, draft=True)).json()
        assert (pending["version"], pending["public_description"]) == (1, "Keeper, retired.")
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


GENERATION = {
    "adapter": "draw-things",
    "model": "sdxl-turbo",
    "prompt": "A lighthouse keeper at dusk, oil on canvas",
    "negative_prompt": "text, watermark",
    "seed": 4_294_967_295,
    "parameters": {"steps": 30, "cfg_scale": 7.5, "sampler": "euler_a", "hires": {"scale": 1.5}},
    "reference_ids": ["01J9Z8Q4N5V6W7X8Y9Z0A1B2C8"],
}
"""FR-CAT-4's generation metadata: seed, model, prompts, parameters and reference ids."""


@pytest.mark.integration
@pytest.mark.part("M3", "FR-MEDIA-1; FR-API-6")
async def test_c_15__media_upload(settings: Settings, engine: AsyncEngine) -> None:
    """``POST /media`` stores an image with its metadata (201): the declared subjects, the
    sniffed type, dimensions, checksum and rating, and generation metadata that reads back
    exactly as sent; ``GET /media/{id}`` and ``…/content`` serve it. An upload that omits
    ``depicted_subjects`` is 422 ``subjects_required``, while an explicitly empty list is
    accepted (201). A type outside ``media_allowed_types`` is 415 ``media_type_not_allowed``
    and a file over ``media_max_bytes`` is 413 ``media_too_large``; neither stores a row or a
    blob.

    Complement asserted: the same bytes refused for their size are accepted once the limit
    allows them, so the 413 was the limit's doing."""
    subjects = [{"kind": "catalog_character", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C5"}]
    data = png(8, 6)
    async with media_client(settings, engine) as client:
        created = await upload(
            client,
            data,
            metadata(
                alt_text="Mara at the door",
                caption="Gull Rock",
                generation_metadata=GENERATION,
                depicted_subjects=subjects,
            ),
        )
        assert created.status_code == 201, created.text
        asset = created.json()
        assert (asset["kind"], asset["mime_type"], asset["width"], asset["height"]) == (
            "image",
            "image/png",
            8,
            6,
        )
        assert (asset["checksum"], asset["size"]) == (sha256(data), len(data))
        assert asset["depicted_subjects"] == subjects
        assert asset["generation_metadata"] == GENERATION
        read = await client.get(f"/media/{asset['id']}", headers=MEDIA_READ)
        assert read.json()["generation_metadata"] == GENERATION
        assert read.json()["state"] == "live"
        content = await client.get(f"/media/{asset['id']}/content", headers=MEDIA_READ)
        assert (content.status_code, content.content) == (200, data)

        omitted_bytes = png()
        omitted = await upload(client, omitted_bytes, metadata(depicted_subjects=None))
        assert (omitted.status_code, omitted.json()["error"]["code"]) == (422, "subjects_required")
        assert not blob_path(settings.media_root, sha256(omitted_bytes)).exists()
        empty = await upload(client, omitted_bytes, metadata(depicted_subjects=[]))
        assert (empty.status_code, empty.json()["depicted_subjects"]) == (201, [])

        gif = b"GIF89a" + png()[8:]
        refused_type = await upload(client, gif, content_type="image/gif")
        assert (refused_type.status_code, refused_type.json()["error"]["code"]) == (
            415,
            "media_type_not_allowed",
        )
        assert not blob_path(settings.media_root, sha256(gif)).exists()

        big = png(40, 40)
        try:
            await set_media(client, media_max_bytes=len(big) - 1)
            refused_size = await upload(client, big)
            assert (refused_size.status_code, refused_size.json()["error"]["code"]) == (
                413,
                "media_too_large",
            )
            assert not blob_path(settings.media_root, sha256(big)).exists()
            await set_media(client, media_max_bytes=len(big))
            assert (await upload(client, big)).status_code == 201
        finally:
            await set_media(client, media_max_bytes=50 * 1024 * 1024)
