"""I-17: genesis and snapshots (docs/04 §4), its M3 parts.

Every I-17 (M3) part lives in this file (traceability counts a test ID per milestone in one file).

- #103's part, ``test_i_17__catalog_never_touches_stories``: archiving, editing or deleting a
  catalog character never alters a story created from it, which keeps its snapshot and its
  ``source_catalog_id@version`` provenance (FR-CAT-3,9). Until #119's genesis writes the
  provenance, the fixture creates a blank story and records the provenance on its character row.
- #105's part, ``test_i_17__media_sweep``: the periodic media sweep purges an asset only when
  nothing references it, skips candidates, and deletes bytes only when no asset row of any state
  has them (FR-MEDIA-2). It runs on a scratch database with its own media root, and the
  reference is planted in #115's shape (``public_card.images``).
- #119's parts: ``test_i_17__genesis_isolated`` (FR-SCN-9: one genesis operation or nothing,
  full snapshots with provenance, catalog edits leave the story byte-identical, archived entries
  refused, truncation to 0 and a fork) and ``test_i_17__media_story_snapshot`` (FR-MEDIA-2: an
  asset a story's snapshot references survives its catalog entry's deletion until the story is
  deleted; #316).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import CharacterCreate, MediaUpload, RequestContext
from epistrel.config import Settings
from epistrel.core.genesis import create as genesis_create
from epistrel.core.media.sweep import sweep
from epistrel.core.stories import STORY_TABLES
from epistrel.core.tables import stories
from tests import genesis_support as g
from tests.catalog_support import (
    BASE,
    WRITE,
    entry_row,
    publish,
    published,
    save,
    version_rows,
)
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
    the blob, purging both deletes it. The bytes stay while an asset row of any state has them:
    after an unreferenced retired twin is purged, a retired asset a catalog version references
    still serves its content, and a candidate's bytes stay too. References are found by
    querying the records.

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
        twin_data, kept_data, held_data = png(), png(), png()
        twin_a, twin_b = await put(twin_data), await put(twin_data)
        kept, kept_twin = await put(kept_data), await put(kept_data)
        held, held_twin = await put(held_data), await put(held_data)
        for media_id in (referenced, unreferenced, twin_a, kept, kept_twin, held_twin):
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
                text(
                    "INSERT INTO catalog.character_versions (catalog_id, version, content, "
                    "published_at, published_by) VALUES (:c, 1, jsonb_build_object('name', "
                    "'Mara', 'public_card', jsonb_build_object('images', jsonb_build_object("
                    "'gallery', jsonb_build_array(CAST(:m AS text))))), now(), 'p-media')"
                ),
                {"m": kept, "c": entry.character.id},
            )
            await conn.execute(
                text("UPDATE media_assets SET state = 'candidate' WHERE id IN (:a, :b)"),
                {"a": candidate, "b": held},
            )
        unreferenced_sum = (await lib.get_media(MEDIA_AUTHOR, unreferenced)).checksum
        twin_blob = blob_path(space.root, sha256(twin_data))
        kept_blob = blob_path(space.root, sha256(kept_data))
        held_blob = blob_path(space.root, sha256(held_data))

        assert await sweep(db, lib.media) == {
            "assets_purged": 4,
            "blobs_deleted": 1,
            "staged_removed": 0,
            "sessions_expired": 0,
            "sessions_purged": 0,
            "parts_removed": 0,
            "exports_expired": 0,
        }
        assert await assets() == {referenced, candidate, twin_b, kept, held}
        assert not blob_path(space.root, unreferenced_sum).exists()
        assert twin_blob.exists(), "the other row still has the bytes"
        assert kept_blob.exists(), "a retired row still has the bytes"
        served = await lib.get_media_content(MEDIA_AUTHOR, kept)
        assert await served.read() == kept_data, "the version's asset still serves its content"
        assert held_blob.exists(), "a candidate row still has the bytes"

        await lib.delete_media(MEDIA_AUTHOR, twin_b)
        later = datetime.now(UTC) + timedelta(days=30)
        counts = await sweep(db, lib.media, now=later)
        assert (counts["assets_purged"], counts["blobs_deleted"]) == (1, 1)
        assert not twin_blob.exists()
        assert await assets() == {referenced, candidate, kept, held}, "candidates are never swept"

        async with db.begin() as conn:
            await conn.execute(text("UPDATE catalog.character_catalog SET draft = '{}'::jsonb"))
        assert (await sweep(db, lib.media, now=later))["assets_purged"] == 1
        assert await assets() == {candidate, kept, held}
        assert held_blob.exists()


# -- #119: genesis from a scenario -------------------------------------------------------------


async def _genesis_rows(engine: AsyncEngine, story_id: str) -> dict[str, list[str]]:
    """What genesis wrote that a truncation to 0 must keep: the cast, its snapshots and the
    scenario snapshot."""
    found: dict[str, list[str]] = {}
    async with engine.connect() as conn:
        for sql, name in (
            ("SELECT row_to_json(t)::text FROM characters t WHERE story_id = :s", "characters"),
            (
                "SELECT row_to_json(t)::text FROM character_snapshots t WHERE story_id = :s",
                "character_snapshots",
            ),
            ("SELECT scenario_snapshot::text FROM stories WHERE id = :s", "scenario"),
        ):
            found[name] = sorted(await conn.scalars(text(sql), {"s": story_id}))
    return found


@pytest.mark.part("M3", "FR-SCN-9")
async def test_i_17__genesis_isolated(
    settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a story from a scenario commits one genesis operation at position 0, or
    nothing: a failure injected after the ``stories`` insert leaves every count as it was. The
    story holds the scenario version's whole document and each catalog character's version in
    full, with ``catalog_id@version`` provenance. Publishing a new version, archiving and
    deleting the catalog character afterwards leave every row of the story byte-identical.
    Creating a story from an archived character or an archived scenario is refused without
    side effects. Truncating to 0 leaves the genesis state, and a fork carries the snapshots.

    Complements asserted: the counts move for the creation that commits (the same request
    before anything was archived); a request leaving the role empty is still refused for that
    once the character is deleted (``slot_required``), so the refusals name what they check."""
    async with rest(settings, engine) as client:
        mara = await g.catalog_character_id(client, "Mara Venn", age=50)
        body = g.scenario_body(g.role("mother"), g.role("player", kind="player"))
        scenario_id, version = await g.published_scenario(client, body)
        request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})

        counted = await g.counts(engine)
        real = genesis_create.insert_cast

        async def failing(*args: object, **kwargs: object) -> None:
            await real(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected after the stories insert")

        monkeypatch.setattr(genesis_create, "insert_cast", failing)
        with pytest.raises(RuntimeError, match="injected"):
            await client.post("/stories", json=request, headers=g.WRITE)
        assert await g.counts(engine) == counted
        monkeypatch.setattr(genesis_create, "insert_cast", real)

        made = await g.create_from(client, request)
        story_id = made["story_id"]
        moved = await g.counts(engine)
        assert moved["stories"] == counted["stories"] + 1
        assert moved["character_snapshots"] == counted["character_snapshots"] + 1
        mother = next(c for c in made["characters"] if c["role_id"] == "mother")
        assert await _provenance(engine, mother["id"]) == (mara, 1)
        async with engine.connect() as conn:
            stored = await conn.scalar(
                text("SELECT scenario_snapshot FROM stories WHERE id = :s"), {"s": story_id}
            )
            published = await conn.scalar(
                text(
                    "SELECT content FROM catalog.scenario_versions "
                    "WHERE catalog_id = :c AND version = :v"
                ),
                {"c": scenario_id, "v": version},
            )
            copied = await conn.scalar(
                text("SELECT content FROM character_snapshots WHERE character_id = :c"),
                {"c": mother["id"]},
            )
            original = await conn.scalar(
                text(
                    "SELECT content FROM catalog.character_versions "
                    "WHERE catalog_id = :c AND version = 1"
                ),
                {"c": mara},
            )
            genesis_ops = await conn.scalar(
                text(
                    "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'genesis' "
                    "AND narrative_turn IS NULL"
                ),
                {"s": story_id},
            )
        assert stored == published
        assert copied == original
        assert genesis_ops == 1

        before = await _snapshot(engine, story_id)
        revision = 2  # created at 1, published once
        edited = await save(client, mara, revision, public_description="Retired keeper.")
        assert edited.status_code == 200, edited.text
        assert (await publish(client, mara, revision + 1)).json()["version"] == 2
        assert await _snapshot(engine, story_id) == before
        assert (await client.post(f"{BASE}/{mara}/archive", headers=WRITE)).status_code == 200
        assert await _snapshot(engine, story_id) == before

        # An archived character, then an archived scenario: refused, nothing written.
        counted = await g.counts(engine)
        status, code, _ = await g.refused(client, request)
        assert (status, code) == (422, "entry_archived")
        assert await g.counts(engine) == counted
        assert (await client.post(f"{BASE}/{mara}/unarchive", headers=WRITE)).status_code == 200
        archived = await client.post(
            f"/catalog/scenarios/{scenario_id}/archive", headers=g.headers(principal="p-x")
        )
        assert archived.status_code == 200, archived.text
        counted = await g.counts(engine)
        status, code, _ = await g.refused(client, request)
        assert (status, code) == (422, "entry_archived")
        assert await g.counts(engine) == counted
        unarchived = await client.post(
            f"/catalog/scenarios/{scenario_id}/unarchive", headers=g.headers(principal="p-x")
        )
        assert unarchived.status_code == 200, unarchived.text

        assert (await client.delete(f"{BASE}/{mara}", headers=WRITE)).status_code == 204
        assert await _snapshot(engine, story_id) == before
        complement = await g.create_from(client, {**request, "slots": {}}, status=422)
        assert complement["error"]["code"] == "slot_required", "the entry is gone, not the rule"

        # Truncating to 0 leaves the genesis state; a fork carries the snapshots.
        genesis_state = await _genesis_rows(engine, story_id)
        cut = await client.post(
            f"/stories/{story_id}/truncate", json={"to_turn": 0}, headers=g.WRITE
        )
        assert cut.status_code == 200, cut.text
        assert await _genesis_rows(engine, story_id) == genesis_state
        read = await client.get(f"/stories/{story_id}", headers=g.READ)
        assert read.json()["next_turn_index"] == 1
        forked = await client.post(f"/stories/{story_id}/fork", json={}, headers=g.WRITE)
        assert forked.status_code == 201, forked.text
        copy = await _genesis_rows(engine, forked.json()["story_id"])
        assert copy["scenario"] == genesis_state["scenario"]
        assert len(copy["character_snapshots"]) == 1
        assert mara in copy["character_snapshots"][0]


@pytest.mark.part("M3", "FR-MEDIA-2")
async def test_i_17__media_story_snapshot(pg_url: str, tmp_path: Path) -> None:
    """A media asset a story was created with survives the deletion of its catalog entry and
    its own retirement until the story is deleted, and the periodic sweep then purges it: the
    story's ``character_snapshots`` reference it (#316).

    Complement asserted: the asset is purged by the sweep after the story's deletion, so its
    survival was the snapshot's doing."""
    async with scratch(pg_url, tmp_path / "media") as space:
        lib, db = space.lib, space.db
        image = (
            await lib.upload_media(
                MEDIA_AUTHOR, MediaUpload.model_validate(metadata(alt_text="Mara")), png()
            )
        ).asset.id
        async with rest(space.settings, db) as client:
            mara = await g.catalog_character_id(
                client, "Mara Venn", age=50, images={"primary": image}
            )
            scenario_id, _ = await g.published_scenario(
                client, g.scenario_body(g.role("mother"), g.role("player", kind="player"))
            )
            made = await g.create_from(
                client, g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})
            )
            assert (await client.delete(f"{BASE}/{mara}", headers=WRITE)).status_code == 204
        await lib.delete_media(MEDIA_AUTHOR, image)
        later = datetime.now(UTC) + timedelta(days=30)
        await sweep(db, lib.media, now=later)
        served = await lib.get_media_content(MEDIA_AUTHOR, image)
        assert await served.read(), "the story's snapshot keeps the asset served"
        await lib.delete_story(RequestContext(principal="p-author"), made["story_id"])
        assert (await sweep(db, lib.media, now=later))["assets_purged"] == 1
        assert await rows(db, "SELECT id FROM media_assets WHERE id = :m", m=image) == []
