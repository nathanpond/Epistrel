"""S-ERASURE-BOUNDARY: erasure reaches derived prose (docs/04 §7; #124, and later #125, #127).

One file for every slice of the scenario (the traceability gate allows one file per test and
milestone): ``__catalog`` here (#124), #125's ``__restore`` and ``__docs``; #127's resurrection
slices join it.

``test_s_erasure_boundary__catalog``: a catalog-scope erasure of a catalog character who appears
in every version of herself, a scenario's seed and role, media of her own and shared with
another character, and a story created from the scenario. Afterwards the catalog, media and
persona tables name her nowhere (the test's own scan); her versions, draft and companion are
gone; the scenario is redacted; media are purged or rewritten under ``erasure_shared_media``
(bytes another live asset shares are kept)
(``purge`` for her, ``retire`` for a second character); and the story is untouched until erased
separately.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core.erasure import boundary
from epistrel.core.erasure.ledger import LedgerFile
from epistrel.core.media.sweep import collect_blob
from epistrel.testing import pauses
from epistrel.tools import backup
from tests import character_support as characters
from tests import erasure_support as e
from tests import genesis_support as g
from tests import ledger_support as ledger
from tests import media_support as media
from tests import scenario_support as scenarios
from tests.auth_support import MEDIA_ROOT, auth_settings
from tests.conftest import DUMMY_DSN, REPO_ROOT
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
    purged with its bytes; her asset whose bytes a bystander's asset shares is purged while the
    bytes stay and serve the bystander's; the asset she shares with Corvin stays, without her
    in its subjects or caption; her primary image, which the story's snapshot references, is
    purged under ``purge`` (its content 404, its bytes gone, nothing left for the sweep);
    Corvin's, under ``retire``, stays retired with its bytes still served."""
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
        same_bytes = media.png()
        twin = await media.uploaded(
            client, same_bytes, depicted_subjects=[{"kind": "catalog_character", "id": thessaly}]
        )
        bystander = await media.uploaded(client, same_bytes, caption="The quay at low tide.")
        assert twin["checksum"] == bystander["checksum"]
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
        assert done["media"] == 4  # her portrait, the asset she shares, her own two
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
        # Her twin is purged, but another live asset shares its bytes: they stay and serve.
        assert (
            await scalar(engine, "SELECT count(*) FROM media_assets WHERE id = :i", i=twin["id"])
            == 0
        )
        assert await content(client, twin["id"]) == 404
        assert await content(client, bystander["id"]) == 200
        assert media.blob_path(MEDIA_ROOT, bystander["checksum"]).exists()
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


@pytest.fixture(scope="module")
def ledger_server() -> Iterator[ledger.Server]:
    with ledger.dedicated_server() as server:
        yield server


async def _until_applied(client: Any, timeout_s: float = 60.0) -> dict[str, Any]:
    """Poll ``GET /admin/erasure/status`` until nothing is pending."""
    async with asyncio.timeout(timeout_s):
        while True:
            found = await client.get("/admin/erasure/status", headers=e.WRITE)
            assert found.status_code == 200, found.text
            body: dict[str, Any] = found.json()
            if body["pending_count"] == 0 and not body["in_progress"]:
                return body
            await asyncio.sleep(0.1)


@pytest.mark.part("M3", "FR-ADMIN-7")
async def test_s_erasure_boundary__restore(ledger_server: ledger.Server, tmp_path: Path) -> None:
    """Dump the database, erase Marisol from her story, restore the dump (the container's own
    ``pg_restore``), start the app: the restored story names her again and is refused with 503
    ``erasure_pending`` (``Retry-After``, the erasure's id) while the reapply is held, and listed
    as ``blocked_by_erasure``; the story that never named her is served meanwhile (complement).
    Once the reapply ends: the ledger table holds the file's entry again (same id and hashes,
    settled ``reapplied``), her story is served with her name ``[erased]`` and an erasure
    marker in the audit view, and its status is back. The ledger file, untouched by the
    restore, drove it. ``backup verify`` lists the post-erasure dump's markers as expected gaps
    and exits 0; on the pre-erasure dump it lists the erasure as newer than the dump."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    config = ledger.settings(url, ledger_dir)
    before = tmp_path / "before.dump"

    async with ledger.started(config) as (client, _):
        stories = await ledger.two_stories(client)
        ledger_server.dump(url, before)
        done = await e.erase(client, "character", stories.subject, stories.named)
    erasure_id = done["erasure_id"]
    (line,) = LedgerFile(ledger_dir).read()
    assert (line.erasure_id, line.story_id, line.subject_id) == (
        erasure_id,
        stories.named,
        stories.subject,
    )
    file_bytes = (ledger_dir / "ledger.jsonl").read_bytes()

    ledger_server.restore(url, before)
    assert await ledger.ledger_rows(url) == []  # the dump predates the erasure
    assert ledger.SUBJECT in " ".join(await ledger.pins(url, stories.named))

    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, _):
            await hold.arrived()
            refused = await client.get(f"/stories/{stories.named}", headers=e.WRITE)
            assert refused.status_code == 503, refused.text
            assert refused.headers["Retry-After"] == "30"
            error = refused.json()["error"]
            assert error["code"] == "erasure_pending"
            assert error["details"] == {
                "story_id": stories.named,
                "erasure_id": erasure_id,
                "pending_count": 1,
            }
            for path in ("history", "pinned-memories"):
                blocked = await client.get(f"/stories/{stories.named}/{path}", headers=e.WRITE)
                assert blocked.status_code == 503, (path, blocked.text)
                assert ledger.SUBJECT not in blocked.text
            write = await client.put(
                f"/stories/{stories.named}/pinned-memories",
                json={"items": [{"text": "Nothing."}]},
                headers=e.WRITE,
            )
            assert write.status_code == 503, write.text
            served = await client.get(f"/stories/{stories.other}", headers=e.WRITE)
            assert served.status_code == 200, served.text
            listed = await client.get("/stories", headers=e.WRITE)
            statuses = {s["id"]: s["status"] for s in listed.json()["items"]}
            assert statuses[stories.named] == "blocked_by_erasure"
            assert statuses[stories.other] == "active"
            pending = await client.get("/admin/erasure/status", headers=e.WRITE)
            assert pending.json()["pending"] == [
                {"erasure_id": erasure_id, "story_id": stories.named}
            ]
            health = (await client.get("/health")).json()
            assert (health["status"], health["checks"]["erasure_pending"]) == ("degraded", 1)

            hold.release()
            status = await _until_applied(client)
            assert status["applied_count"] == 1

            shown = await client.get(
                f"/stories/{stories.named}/history",
                params={"viewpoint": "omniscient", "include_audit": 1, "limit": 1000},
                headers=e.WRITE,
            )
            assert shown.status_code == 200, shown.text
            assert ledger.SUBJECT not in shown.text
            markers = [i for i in shown.json()["audit"] if i["kind"] == "erasure_marker"]
            assert markers, "the reapplied erasure left no marker"
            assert {m["text"] for m in markers} == {f"[erasure marker: {stories.subject}, story]"}
            story = await client.get(f"/stories/{stories.named}", headers=e.WRITE)
            assert story.status_code == 200, story.text
            assert story.json()["status"] == "active"
            assert stories.subject not in {c["id"] for c in story.json()["characters"]}

    assert await ledger.pins(url, stories.named) == [
        f"[erased] keeps the lighthouse key; {ledger.BYSTANDER} keeps the boat."
    ]
    (row,) = await ledger.ledger_rows(url)
    assert (row.id, row.ledger_hash_prev, row.ledger_hash) == (
        line.erasure_id,
        line.ledger_hash_prev,
        line.ledger_hash,
    )
    assert (row.applied_note, row.story_ref) == ("reapplied", stories.named)
    assert row.applied_at is not None
    assert (ledger_dir / "ledger.jsonl").read_bytes() == file_bytes  # one line, as written

    after = ledger_server.dump(url, tmp_path / "after.dump")
    wrapper = ledger_server.pg_restore_wrapper(tmp_path)
    report = backup.verify(after, ledger_dir=ledger_dir, pg_restore=str(wrapper))
    assert (report.exit_code, report.problems) == (backup.EXIT_OK, [])
    assert report.markers
    assert {m["erasure_id"] for m in report.markers} == {erasure_id}
    assert {m["story_id"] for m in report.markers} == {stories.named}
    assert report.redacted_events >= 0
    assert (report.ledger_rows, report.newer_than_dump) == (1, [])
    assert ledger.SUBJECT not in json.dumps(report.as_json())
    old = backup.verify(before, ledger_dir=ledger_dir, pg_restore=str(wrapper))
    assert (old.exit_code, old.ledger_rows, old.newer_than_dump) == (
        backup.EXIT_OK,
        0,
        [erasure_id],
    )


@pytest.mark.part("M3", "FR-ADMIN-8")
def test_s_erasure_boundary__docs() -> None:
    """The erasure boundary reads the same from the API (``GET /admin/erasure/boundary``), the
    API description (the route's and ``info.description``, served and in ``docs/openapi.json``)
    and ``docs/OPERATIONS.md`` (its marked block), whitespace aside; every out-of-scope item is
    there: delivered exports, provider retention, subjects shown only visually, older backups
    and the logs."""
    rendered = boundary.render_boundary()
    client = TestClient(create_app(auth_settings(DUMMY_DSN)))  # reads no database
    served = client.get("/admin/erasure/boundary", headers=e.WRITE)
    schema = client.get("/openapi.json").json()
    assert served.status_code == 200, served.text
    assert served.json() == {
        "in_scope": list(boundary.IN_SCOPE),
        "out_of_scope": list(boundary.OUT_OF_SCOPE),
        "logs": boundary.LOGS,
    }
    for item in (
        boundary.DELIVERED_EXPORTS,
        boundary.PROVIDER_RETENTION,
        boundary.VISUAL_ONLY,
        boundary.OLD_BACKUPS,
        boundary.LOGS_ITEM,
    ):
        assert item in served.json()["out_of_scope"]
    assert boundary.LOGS == "operational logs contain no story content by default (NFR-SEC-4)"

    operations = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text()
    block = operations.split("<!-- erasure-boundary:start -->", 1)[1].split(
        "<!-- erasure-boundary:end -->", 1
    )[0]
    assert boundary.normalised(block) == boundary.normalised(rendered)
    committed = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text())
    for document in (schema, committed):
        description = document["info"]["description"]
        assert boundary.normalised(description).endswith(boundary.normalised(rendered))
        route = document["paths"]["/admin/erasure/boundary"]["get"]["description"]
        assert boundary.normalised(rendered) in boundary.normalised(route)
