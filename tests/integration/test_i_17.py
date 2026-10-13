"""I-17: genesis and snapshots (docs/04 §4), its M3 parts.

Every I-17 (M3) part lives in this file (traceability counts a test ID per milestone in one file).

- #103's part, ``test_i_17__catalog_never_touches_stories``: archiving, editing or deleting a
  catalog character never alters a story created from it, which keeps its snapshot and its
  ``source_catalog_id@version`` provenance (FR-CAT-3,9). Until #119's genesis writes the
  provenance, the fixture creates a blank story and records the provenance on its character row.
- #105's part, ``test_i_17__media_sweep``: the periodic media sweep purges an asset only when
  nothing references it, skips candidates, and deletes bytes only when no asset row has them
  (FR-MEDIA-2). It runs on a scratch database with its own media root, and until #115 gives
  catalog entries image fields the reference is planted where #115 will put it. #119 adds the
  story-snapshot half (an asset a story references survives its catalog entry's deletion).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import CharacterCreate, MediaUpload, RequestContext
from epistrel.config import Settings
from epistrel.core.media.sweep import sweep
from epistrel.core.stories import STORY_TABLES
from epistrel.core.tables import stories
from tests.catalog_support import BASE, WRITE, entry_row, publish, published, save, version_rows
from tests.catalog_support import character as catalog_character
from tests.event_support import rows
from tests.media_support import blob_path, metadata, png, scratch, sha256
from tests.story_support import create, rest, story_body

Snapshot = dict[str, list[str]]


def _rows_sql(table: Table) -> str:
    column = "id" if table.name == "stories" else "story_id"
    return f'SELECT row_to_json(t)::text FROM "{table.name}" t WHERE "{column}" = :s'  # noqa: S608


async def _snapshot(engine: AsyncEngine, story_id: str) -> Snapshot:
    """Every row of the story (``row_to_json``, per story table and its ``stories`` row), and
    every row of ``stories`` and ``characters``, whichever story it belongs to."""
    found: Snapshot = {}
    async with engine.connect() as conn:
        for table in (*STORY_TABLES, stories):
            rows = await conn.scalars(text(_rows_sql(table)), {"s": story_id})
            found[table.name] = sorted(rows)
        for name in ("stories", "characters"):
            rows = await conn.scalars(text(f'SELECT row_to_json(t)::text FROM "{name}" t'))  # noqa: S608
            found[f"every {name} row"] = sorted(rows)
    return found


async def _provenance(engine: AsyncEngine, character_id: str) -> tuple[str, int]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT source_catalog_id, source_catalog_version FROM characters WHERE id = :c"
                ),
                {"c": character_id},
            )
        ).one()
    return str(row.source_catalog_id), int(row.source_catalog_version)


@pytest.mark.part("M3", "FR-CAT-3,9")
async def test_i_17__catalog_never_touches_stories(settings: Settings, engine: AsyncEngine) -> None:
    """A story whose character came from catalog entry E at version 1 stays byte-identical —
    every row of the story, and every ``stories`` and ``characters`` row — while E is edited and
    published as version 2, archived, unarchived and deleted; the character still names E@1
    afterwards. Deleting a story made from entry F leaves F's entry and versions as they were.

    Complement asserted: the snapshot does see a change the story itself makes (archiving the
    story), so the equal snapshots are not vacuous."""
    async with rest(settings, engine) as client:
        entry, revision = await published(client, catalog_character("Mara Venn"))
        story = await create(client, story_body("Wren", "Mara Venn"))
        story_id, mara = story["story_id"], story["characters"][1]["id"]
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE characters SET source_catalog_id = :e, source_catalog_version = 1 "
                    "WHERE id = :c"
                ),
                {"e": entry, "c": mara},
            )
        before = await _snapshot(engine, story_id)
        assert before["characters"], "the story has its cast"

        edited = await save(client, entry, revision, public_description="Retired keeper.")
        assert edited.status_code == 200
        assert (await publish(client, entry, revision + 1)).json()["version"] == 2
        assert await _snapshot(engine, story_id) == before

        assert (await client.post(f"{BASE}/{entry}/archive", headers=WRITE)).status_code == 200
        assert await _snapshot(engine, story_id) == before
        assert (await client.post(f"{BASE}/{entry}/unarchive", headers=WRITE)).status_code == 200
        assert await _snapshot(engine, story_id) == before

        assert (await client.delete(f"{BASE}/{entry}", headers=WRITE)).status_code == 204
        assert await entry_row(engine, entry) is None
        assert await _snapshot(engine, story_id) == before
        assert await _provenance(engine, mara) == (entry, 1)

        other, _ = await published(client, catalog_character("Ilse Marr"))
        kept_entry = await entry_row(engine, other)
        kept_versions = await version_rows(engine, other)
        doomed = await create(client, story_body("Wren", "Ilse Marr"))
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE characters SET source_catalog_id = :e, source_catalog_version = 1 "
                    "WHERE id = :c"
                ),
                {"e": other, "c": doomed["characters"][1]["id"]},
            )
        removed = await client.delete(f"/stories/{doomed['story_id']}", headers=WRITE)
        assert removed.status_code == 204
        assert await entry_row(engine, other) == kept_entry
        assert await version_rows(engine, other) == kept_versions

        archived = await client.post(f"/stories/{story_id}/archive", headers=WRITE)
        assert archived.status_code == 200, archived.text
        assert await _snapshot(engine, story_id) != before


# -- #105: the media sweep -------------------------------------------------------------------------

MEDIA_AUTHOR = RequestContext(principal="p-media")


@pytest.mark.part("M3", "FR-MEDIA-2")
async def test_i_17__media_sweep(pg_url: str, tmp_path: Path) -> None:
    """The sweep keeps an asset a catalog draft references and an asset in state ``candidate``,
    and purges an unreferenced retired asset (its row and, since no other row has them, its
    bytes). Identical bytes uploaded twice are stored once: purging one of the two rows keeps
    the blob, purging both deletes it. References are found by querying the records.

    Complement asserted: once the draft no longer names it, the referenced asset is purged too,
    so its survival was the reference's doing."""
    async with scratch(pg_url, tmp_path / "media") as space:
        lib, db = space.lib, space.db

        async def put(data: bytes | None = None) -> str:
            request = MediaUpload.model_validate(metadata())
            return (await lib.upload_media(MEDIA_AUTHOR, request, data or png())).asset.id

        async def assets() -> set[str]:
            return {str(r.id) for r in await rows(db, "SELECT id FROM media_assets")}

        referenced, unreferenced, candidate = await put(), await put(), await put()
        twin_data = png()
        twin_a, twin_b = await put(twin_data), await put(twin_data)
        for media_id in (referenced, unreferenced, twin_a):
            await lib.delete_media(MEDIA_AUTHOR, media_id)
        entry = await lib.create_character_draft(
            MEDIA_AUTHOR, CharacterCreate(**catalog_character())
        )
        async with db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE catalog.character_catalog SET draft = draft || jsonb_build_object("
                    "'public_card', jsonb_build_object('images', jsonb_build_object('primary', "
                    "CAST(:m AS text)))) WHERE id = :c"
                ),
                {"m": referenced, "c": entry.character.id},
            )
            await conn.execute(
                text("UPDATE media_assets SET state = 'candidate' WHERE id = :m"), {"m": candidate}
            )
        unreferenced_sum = (await lib.get_media(MEDIA_AUTHOR, unreferenced)).checksum
        twin_blob = blob_path(space.root, sha256(twin_data))

        assert await sweep(db, lib.media) == {
            "assets_purged": 2,
            "blobs_deleted": 1,
            "staged_removed": 0,
        }
        assert await assets() == {referenced, candidate, twin_b}
        assert not blob_path(space.root, unreferenced_sum).exists()
        assert twin_blob.exists(), "the other row still has the bytes"

        await lib.delete_media(MEDIA_AUTHOR, twin_b)
        later = datetime.now(UTC) + timedelta(days=30)
        counts = await sweep(db, lib.media, now=later)
        assert (counts["assets_purged"], counts["blobs_deleted"]) == (1, 1)
        assert not twin_blob.exists()
        assert await assets() == {referenced, candidate}, "candidates are never swept"

        async with db.begin() as conn:
            await conn.execute(text("UPDATE catalog.character_catalog SET draft = '{}'::jsonb"))
        assert (await sweep(db, lib.media, now=later))["assets_purged"] == 1
        assert await assets() == {candidate}
