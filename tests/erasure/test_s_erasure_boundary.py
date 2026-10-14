"""S-ERASURE-BOUNDARY: erasure reaches derived prose (docs/04 §7; #124, and later #125, #127).

One file for every slice of the scenario (the traceability gate allows one file per test and
milestone): ``__catalog`` here (#124); #125's ``__restore`` and #127's resurrection slices join
it.

``test_s_erasure_boundary__catalog``: a catalog-scope erasure of a catalog character who appears
in every version of herself, a scenario's seed and role, media of her own and shared with
another character, and a story created from the scenario. Afterwards the catalog, media and
persona tables name her nowhere (the test's own scan); her versions, draft and companion are
gone; the scenario is redacted; media are purged or rewritten under ``erasure_shared_media``
(``purge`` for her, ``retire`` for a second character); and the story is untouched until erased
separately.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.media.sweep import collect_blob
from tests import character_support as characters
from tests import erasure_support as e
from tests import genesis_support as g
from tests import media_support as media
from tests import scenario_support as scenarios
from tests.auth_support import MEDIA_ROOT
from tests.event_support import rows, scalar
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

NAME, ALIAS = "Thessaly Vornhagen", "Thessa"
SECOND, SECOND_ALIAS = "Corvin Ashgrove", "Corvo"
TERMS = (NAME, ALIAS)
SECOND_TERMS = (SECOND, SECOND_ALIAS)


@pytest.fixture
def erasure_settings(pg_url: str) -> Settings:
    return e.erasure_settings(pg_url)


async def catalog_values(engine: AsyncEngine) -> dict[str, list[str]]:
    """Every text value of the catalog, media and persona tables (enumerated from
    ``information_schema``), and the deployment-scoped stored results."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT c.table_schema, c.table_name, c.column_name "
                "FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                "WHERE t.table_type = 'BASE TABLE' AND c.data_type = ANY(CAST(:types AS text[])) "
                "AND (c.table_schema = 'catalog' OR (c.table_schema = 'public' AND "
                "c.table_name IN ('personas', 'media_assets')))"
            ),
            {"types": list(e.TEXT_TYPES)},
        )
        columns: dict[tuple[str, str], list[str]] = {}
        for row in found:
            columns.setdefault((row.table_schema, row.table_name), []).append(row.column_name)
        values: dict[str, list[str]] = {}
        for (schema, table), names in columns.items():
            selected = ", ".join(f'CAST("{c}" AS text)' for c in names)
            got = await conn.execute(text(f'SELECT {selected} FROM "{schema}"."{table}"'))  # noqa: S608
            values[f"{schema}.{table}"] = [str(v) for r in got for v in r if v is not None]
        results = await conn.scalars(
            text(
                "SELECT CAST(result AS text) FROM idempotency_keys "
                "WHERE scope <> 'story' AND result IS NOT NULL"
            )
        )
        values["idempotency_keys"] = [str(v) for v in results]
    return values


async def catalog_residue(engine: AsyncEngine, terms: tuple[str, ...]) -> dict[str, int]:
    return {
        table: n
        for table, found in (await catalog_values(engine)).items()
        if (n := sum(1 for v in found if e.naming(v, terms)))
    }


async def published(client: Any, name: str, alias: str, image: str) -> str:
    body = characters.complete(
        name, metadata={"aliases": [alias]}, images={"primary": image, "gallery": []}
    )
    created = await client.post("/catalog/characters", json=body, headers=scenarios.WRITE)
    assert created.status_code == 201, created.text
    catalog_id: str = created.json()["id"]
    for revision in (1, 2):
        done = await client.post(
            f"/catalog/characters/{catalog_id}/publish",
            json={"draft_revision": revision},
            headers=scenarios.WRITE,
        )
        assert done.status_code == 201, done.text
    return catalog_id


async def story_rows(engine: AsyncEngine, story_id: str) -> dict[str, set[str]]:
    """Every row of the story's own tables (``story_id``, ``id``, ``scope_id``), as text."""
    owned: dict[str, set[str]] = {}
    async with engine.connect() as conn:
        tables = await conn.scalars(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'story_id'"
            )
        )
        for table in sorted(set(tables)):
            found = await conn.scalars(
                text(f'SELECT t::text FROM "{table}" t WHERE story_id = :s'),  # noqa: S608
                {"s": story_id},
            )
            owned[table] = set(found)
        for table, where in e.SCOPE_ID_SOURCES.items():
            if table == "idempotency_keys":
                where = "scope = 'story' AND scope_id = :s"
            found = await conn.scalars(
                text(f'SELECT t::text FROM "{table}" t WHERE {where}'),  # noqa: S608
                {"s": story_id},
            )
            owned[table] = set(found)
    return owned


async def content(client: Any, media_id: str) -> int:
    response = await client.get(f"/media/{media_id}/content", headers=e.WRITE)
    return int(response.status_code)


@pytest.mark.part("M3", "FR-ADMIN-6; FR-MEDIA-2")
async def test_s_erasure_boundary__catalog(erasure_settings: Settings, engine: AsyncEngine) -> None:
    """Erase catalog character Thessaly (``purge``), then Corvin (``retire``). For each: the dry
    run's counts are the run's and it changes nothing; every version, the draft and the
    companion are deleted; the scenario's seed and role no longer name or hold her; no catalog,
    media or persona table and no deployment-scoped stored result names her (the test's scan;
    the response's residue is 0); the ledger holds identifiers only; the story created from the
    scenario is listed with her character and its rows are unchanged (complement: they still
    name her, until it is erased separately). Media: her caption-only asset nobody else uses is
    purged with its bytes; the asset she shares with Corvin stays, without her in its subjects
    or caption; her primary image, which the story's snapshot references, is purged under
    ``purge`` (its content 404, its bytes gone, nothing left for the sweep); Corvin's, under
    ``retire``, stays retired with its bytes still served."""
    async with respond_client(erasure_settings, engine) as (client, lib):
        portrait = await media.uploaded(client, caption=f"{NAME} at the lamp room door.")
        second_portrait = await media.uploaded(client, caption=f"{SECOND} by the boats.")
        thessaly = await published(client, NAME, ALIAS, portrait["id"])
        corvin = await published(client, SECOND, SECOND_ALIAS, second_portrait["id"])
        shared = await media.uploaded(
            client,
            caption=f"{ALIAS} and {SECOND} at the quay.",
            depicted_subjects=[
                {"kind": "catalog_character", "id": thessaly},
                {"kind": "catalog_character", "id": corvin},
            ],
        )
        alone = await media.uploaded(
            client, depicted_subjects=[{"kind": "catalog_character", "id": thessaly}]
        )
        scenario = g.scenario_body(
            g.role(
                "keeper",
                fill="default",
                character={"catalog_id": thessaly},
                starting_location="kitchen",
            ),
            g.role("friend", fill="default", character={"catalog_id": corvin}),
            g.role("player", kind="player", label="The eldest"),
        )
        scenario["starting_state"]["seeded_memories"] = [
            scenarios.seed(f"{ALIAS} keeps the lamp oil in the cellar.")
        ]
        scenario_id, version = await g.published_scenario(client, scenario)
        made = await g.create_from(client, g.from_scenario(scenario_id, ignore_warnings=True))
        story = made["story_id"]
        cast = {c["role_id"]: c["id"] for c in made["characters"]}
        assert await catalog_residue(engine, TERMS)  # the catalog names her

        story_before = await story_rows(engine, story)
        before = await e.dump_all(engine)
        preview = await e.erase(client, "catalog_character", thessaly, dry_run=True)
        assert await e.dump_all(engine) == before
        done = await e.erase(client, "catalog_character", thessaly, key="erase-catalog-1")
        counts = ("records", "prose_segments", "media", "idempotency_results")
        assert {k: done[k] for k in counts} == {k: preview[k] for k in counts}
        assert done["records"]["catalog.character_catalog"] == 1
        assert done["records"]["catalog.character_versions"] == 2
        assert done["records"]["catalog.scenario_catalog"] >= 2  # her companion, the scenario
        assert done["media"] == 3  # her portrait, the asset she shares, her own
        assert done["residue"] == 0
        assert done["referencing_stories"] == [
            {"story_id": story, "character_ids": [cast["keeper"]]}
        ]

        # Gone: her draft, every version, her companion.
        for sql in (
            "SELECT count(*) FROM catalog.character_catalog WHERE id = :c",
            "SELECT count(*) FROM catalog.character_versions WHERE catalog_id = :c",
            "SELECT count(*) FROM catalog.scenario_catalog WHERE companion_of = :c",
        ):
            assert await scalar(engine, sql, c=thessaly) == 0, sql
        # The scenario: its seed redacted, its role without her, in the draft and the version.
        (draft,) = await rows(
            engine, "SELECT draft FROM catalog.scenario_catalog WHERE id = :i", i=scenario_id
        )
        (published_version,) = await rows(
            engine,
            "SELECT content, redacted_at FROM catalog.scenario_versions "
            "WHERE catalog_id = :i AND version = :v",
            i=scenario_id,
            v=version,
        )
        assert published_version.redacted_at is not None
        for document in (draft.draft, published_version.content):
            keeper = document["cast"]["roles"][0]
            assert keeper["character"] is None
            assert document["starting_state"]["seeded_memories"][0]["text"] == (
                "[erased] keeps the lamp oil in the cellar."
            )
        # No catalog, media or persona table names her; Corvin is still named (complement).
        assert await catalog_residue(engine, TERMS) == {}
        assert await catalog_residue(engine, SECOND_TERMS)
        # Media: shared stays without her; hers alone purged with its bytes; her portrait,
        # referenced by the story's snapshot, purged under ``purge``.
        (kept,) = await rows(
            engine,
            "SELECT state, caption, depicted_subjects FROM media_assets WHERE id = :i",
            i=shared["id"],
        )
        assert (kept.state, kept.caption) == ("live", f"[erased] and {SECOND} at the quay.")
        assert kept.depicted_subjects == [{"kind": "catalog_character", "id": corvin}]
        for asset in (alone, portrait):
            assert (
                await scalar(
                    engine, "SELECT count(*) FROM media_assets WHERE id = :i", i=asset["id"]
                )
                == 0
            )
            assert await content(client, asset["id"]) == 404
            assert not media.blob_path(MEDIA_ROOT, asset["checksum"]).exists()
            assert await collect_blob(engine, lib.media, asset["checksum"]) is False
        # The ledger: identifiers only.
        (entry,) = await rows(
            engine,
            "SELECT to_jsonb(l)::text AS row, scope, story_ref FROM erasure_ledger l WHERE id = :i",
            i=done["erasure_id"],
        )
        assert (entry.scope, entry.story_ref) == ("catalog", None)
        assert not e.naming(entry.row, TERMS)
        # The story is untouched (FR-STORE-14): every row of its own as it was, still naming her
        # (a deployment-scoped stored result that named her is the catalog's: purged above).
        assert await story_rows(engine, story) == story_before
        assert sum((await e.residue(engine, story, TERMS)).values()) > 0

        # Corvin under ``retire``: his portrait, which the story references, stays retired.
        changed = await client.put(
            "/admin/settings", json={"erasure_shared_media": "retire"}, headers=e.WRITE
        )
        assert changed.status_code == 200, changed.text
        try:
            second = await e.erase(client, "catalog_character", corvin)
        finally:
            restored = await client.put(
                "/admin/settings", json={"erasure_shared_media": "purge"}, headers=e.WRITE
            )
            assert restored.status_code == 200, restored.text
        assert second["residue"] == 0
        (retired,) = await rows(
            engine,
            "SELECT state, caption, depicted_subjects FROM media_assets WHERE id = :i",
            i=second_portrait["id"],
        )
        assert (retired.state, retired.caption, retired.depicted_subjects) == (
            "retired",
            "[erased] by the boats.",
            [],
        )
        assert await content(client, second_portrait["id"]) == 200  # its bytes still serve
        assert media.blob_path(MEDIA_ROOT, second_portrait["checksum"]).exists()
        assert await catalog_residue(engine, SECOND_TERMS) == {}
        assert [s["story_id"] for s in second["referencing_stories"]] == [story]
