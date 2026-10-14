"""The subject-reference index (#123): every place a person could be named is indexed the moment
it is written, and the rebuild tool recomputes the same rows from the records.

Unmarked, as the drift notes direct: the planned ``test_subject_index`` maps to no test ID of
docs/04's traceability table (S-ERASURE-BOUNDARY is #124's), and M2 left such checks unmarked.

- ``test_the_coverage_check_reports_a_writer_that_never_indexes`` runs first: the AST check finds
  a registered fixture writer that never calls the indexer.
- ``test_every_content_table_has_a_writer`` enumerates the story and catalog tables with text or
  JSON columns from ``information_schema``: each is registered (or exempt with a reason), and
  every registered writer module calls the indexer.
- ``test_subject_index``: a played Lite story with catalog-sourced Linda (alias "Lin"); every
  record that names her has its rows, a message that does not has none, and a rebuild equals the
  incremental index (the tool reports a removed row and puts it back).
- ``test_indexing_logs_counts_only``: the indexer's DEBUG line carries counts and ids, never
  the text it scanned or the name it found.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.erasure import index
from epistrel.core.erasure.index import (
    EXEMPT_TABLES,
    SUBJECT_WRITERS,
    SubjectWriter,
    compute,
    current,
    index_references,
    rebuild_scope,
    recording,
    records_of,
)
from epistrel.core.ids import new_ulid
from epistrel.observability import logging as telemetry_logging
from epistrel.tools import reindex_subjects
from tests import character_support as characters
from tests import genesis_support as g
from tests import media_support as media
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, rows
from tests.feedback_support import RATER, rate
from tests.respond_support import body, played, respond_client
from tests.story_support import WRITE, override, story_body
from tests.summary_support import run_due_jobs, segment_rows

pytestmark = pytest.mark.integration

INDEXERS = frozenset({"index_references", "index_records", "index_rows", "reindex_story"})
"""The calls that write a record's rows (``core.erasure.index`` and ``records.index_rows``)."""
HAND_LISTED = frozenset({"config_changes", "idempotency_keys", "personas", "media_assets"})
"""Indexed tables the enumeration cannot find: keyed by ``scope_id`` rather than ``story_id``,
or catalog records outside the ``catalog`` schema."""
NAME = "Linda"
ALIAS = "Lin"
SUMMARY_OVERRIDES = {
    "memory": {"rolling_summary": {"window_messages": 4, "compress_threshold_tokens": 10_000}}
}
OVERRIDE = "Mum"
"""The alias the scenario's ``mother`` role gives whoever fills it (its ``name_overrides``)."""
REPLIES = [
    "Linda passes the salt.",
    "Lin laughs at the old joke.",
    "The rain keeps falling.",
    "Mum's patience is wearing thin.",
]
"""The stub's replies: the first two name her (by name, by her catalog alias), the third does
not, the fourth names her by her role's alias (possessive)."""
NAMING = (0, 1, 3)
NARRATION = "Thunder rolls over the river and the lamps flicker."
"""An ingested narration that names nobody."""
IDEMPOTENCY_KEY = "subject-index-turn-1"


# -- the coverage check ------------------------------------------------------------------------


def calls_indexer(source: str) -> bool:
    """Whether ``source`` calls one of ``INDEXERS`` (by name or as an attribute)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in INDEXERS:
                return True
    return False


def uncovered(entries: Iterable[SubjectWriter]) -> list[tuple[str, str]]:
    """``(table, module)`` for each registered writer module that never calls the indexer."""
    found: list[tuple[str, str]] = []
    for entry in entries:
        for module in entry.writers:
            spec = importlib.util.find_spec(module)
            assert spec is not None, module
            assert spec.origin is not None, module
            if not calls_indexer(Path(spec.origin).read_text(encoding="utf-8")):
                found.append((entry.table, module))
    return found


def test_the_coverage_check_reports_a_writer_that_never_indexes() -> None:
    """A registered writer whose module never calls the indexer is reported (the fixture writer
    inserts a row and indexes nothing). Complements: a call by name or as an attribute counts;
    an assignment of the name does not."""
    fixture = SubjectWriter("story_notes", ("tests.subject_writer_fixture",), "tests.none:none")
    assert uncovered([fixture]) == [("story_notes", "tests.subject_writer_fixture")]
    assert calls_indexer("async def f(conn):\n    await index_records(conn, [])\n")
    assert calls_indexer("async def f(conn):\n    await index.index_references(conn, 't', 'r')\n")
    assert not calls_indexer("index_references = None\n")


async def content_tables(engine: AsyncEngine) -> set[str]:
    """Every public table with a ``story_id`` column and every ``catalog`` table that has a text
    or JSON column."""
    found = await rows(
        engine,
        "SELECT DISTINCT c.table_name FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE t.table_type = 'BASE TABLE' "
        "AND c.data_type IN ('text', 'jsonb', 'json', 'ARRAY', 'character varying') "
        "AND (c.table_schema = 'catalog' OR (c.table_schema = 'public' AND EXISTS ("
        "SELECT 1 FROM information_schema.columns s WHERE s.table_schema = 'public' "
        "AND s.table_name = c.table_name AND s.column_name = 'story_id')))",
    )
    return {str(r.table_name) for r in found} | {"stories"}


async def test_every_content_table_has_a_writer(engine: AsyncEngine) -> None:
    """Every story-scoped and catalog table with a text or JSON column is registered with a
    writer or exempt with a reason, never both; the hand-listed tables are registered; every
    registered table exists; each writer module calls the indexer; each reader resolves."""
    registered = {entry.table for entry in SUBJECT_WRITERS}
    assert len(registered) == len(SUBJECT_WRITERS)
    enumerated = await content_tables(engine)
    assert enumerated - set(EXEMPT_TABLES) - registered == set()
    assert registered & set(EXEMPT_TABLES) == set()
    assert registered >= HAND_LISTED
    existing = await rows(
        engine,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema IN ('public', 'catalog')",
    )
    assert registered | set(EXEMPT_TABLES) <= {str(r.table_name) for r in existing}
    assert uncovered(SUBJECT_WRITERS) == []
    for entry in SUBJECT_WRITERS:
        assert callable(entry.read()), entry


async def test_the_index_table_has_its_lookups(engine: AsyncEngine) -> None:
    """``subject_references`` has erasure's lookup index ``(subject_id, scope, story_id)`` and a
    record's ``(record_table, record_id)``."""
    found = await rows(
        engine,
        "SELECT i.relname AS name, array_agg(a.attname ORDER BY k.n) AS columns "
        "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
        "JOIN pg_class t ON t.oid = x.indrelid "
        "CROSS JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, n) "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
        "WHERE t.relname = 'subject_references' GROUP BY i.relname",
    )
    shapes = {r.name: list(r.columns) for r in found}
    assert shapes["ix_subject_references_subject"] == ["subject_id", "scope", "story_id"]
    assert shapes["ix_subject_references_record"] == ["record_table", "record_id"]


# -- the played story --------------------------------------------------------------------------


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


Ref = tuple[str, str, str | None, str]
"""``(record_table, record_id, field, ref_kind)``."""


async def refs(engine: AsyncEngine, subject_kind: str, subject_id: str) -> set[Ref]:
    found = await rows(
        engine,
        "SELECT record_table, record_id, field, ref_kind FROM subject_references "
        "WHERE subject_kind = :k AND subject_id = :s",
        k=subject_kind,
        s=subject_id,
    )
    return {(r.record_table, r.record_id, r.field, r.ref_kind) for r in found}


def records(found: set[Ref], table: str) -> set[str]:
    return {record_id for t, record_id, _, _ in found if t == table}


async def linda(client: Any) -> str:
    """Catalog character Linda (alias Lin), published twice: her id."""
    document = characters.complete(NAME, metadata={"aliases": [ALIAS], "labels": ["warm"]})
    created = await client.post("/catalog/characters", json=document, headers=scenarios.WRITE)
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


async def test_subject_index(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A played Lite story created from a scenario whose ``mother`` is catalog character Linda
    ("Lin" from her metadata aliases, "Mum" from the role's name overrides): references to her
    exist for her story character row, her snapshot, the story's scenario snapshot and deferred
    seed, each message naming "Linda", "Lin" or "Mum's" (each with its catalog character row
    too), the summary segment, a pin, the stored respond result, her catalog versions and draft,
    the scenario version's seed, her media caption and a rating's text. Complements: the reply
    that names her nowhere is hers only by the characters it renders, never by a mention, and an
    ingested narration that names nobody has no row for her. Rebuild: the story's rebuilt index
    equals the incremental one, and so do the catalog rows of this test's records; the tool
    reports a row removed behind its back (exit 1), puts it back with ``--write``, and then
    reports nothing (exit 0)."""
    with recording() as seen:
        async with respond_client(stub_settings, engine) as (client, lib):
            await played_with_linda(client, lib, engine, seen, monkeypatch, stub_settings)


async def played_with_linda(
    client: Any,
    lib: Any,
    engine: AsyncEngine,
    seen: list[str],
    monkeypatch: pytest.MonkeyPatch,
    stub_settings: Settings,
) -> None:
    """``test_subject_index``'s body (``seen``: the tables the indexer was called for)."""
    stub = lib.models.stub
    catalog_id = await linda(client)
    asset = await media.uploaded(client, caption=f"{NAME} at the orchard gate.")
    scenario = g.scenario_body(
        g.role("mother", starting_location="kitchen", name_overrides={"aliases": [OVERRIDE]}),
        g.role("player", kind="player", label="The eldest"),
    )
    scenario["starting_state"]["seeded_memories"] = [
        scenarios.seed(f"{ALIAS} hid the orchard deed under the stairs.")
    ]
    scenario_id, version = await g.published_scenario(client, scenario)
    made = await g.create_from(
        client,
        g.from_scenario(
            scenario_id, slots={"mother": g.catalog_slot(catalog_id)}, ignore_warnings=True
        ),
    )
    story = made["story_id"]
    cast = {c["role_id"]: c["id"] for c in made["characters"]}
    mother, player = cast["mother"], cast["player"]

    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = settings || CAST(:p AS jsonb) WHERE id = :s",
            {"s": story, "p": json.dumps({"lite_summary_chunk": 2})},
        ),
    )
    await override(client, story, SUMMARY_OVERRIDES)
    pinned = await client.put(
        f"/stories/{story}/pinned-memories",
        json={"items": [{"text": f"{ALIAS} cannot abide walnuts."}]},
        headers=WRITE,
    )
    assert pinned.status_code == 200, pinned.text

    stub.script("generation", REPLIES)
    turns = [
        await played(
            client, story, body(player, "Pass the bread."), headers={"Idempotency-Key": key}
        )
        for key in (IDEMPOTENCY_KEY, "subject-index-turn-2", "subject-index-turn-3", "t-4")
    ]
    messages = [t["message"]["message_id"] for t in turns]
    ingested = await client.post(
        f"/stories/{story}/ingest",
        json={"events": [{"client_event_id": "evt-quiet", "kind": "narration", "text": NARRATION}]},
        headers=WRITE,
    )
    assert ingested.status_code in (200, 201), ingested.text
    stub.script("lite_summary", [f"{NAME} and the eldest argued about the deed."] * 6)
    await run_due_jobs(engine, lib.models, story)
    assert await segment_rows(engine, story), "the summary job wrote no segment"
    rated = await rate(client, story, messages[0], {"rating": 4, "text": f"{ALIAS} shines."})
    assert rated.status_code == 201, rated.text

    character = await refs(engine, "character", mother)
    catalog = await refs(engine, "catalog_character", catalog_id)
    # Her own rows: the story character defines its subject, linked to the catalog's.
    assert ("characters", mother, None, "declared") in character
    assert ("characters", mother, None, "declared") in catalog
    assert f"{story}:{mother}" in records(character, "character_snapshots")
    assert story in records(character, "stories")  # the scenario snapshot's seed
    assert records(character, "genesis_deferred")  # the deferred secret seed
    # Every message naming her, by name or alias, and the catalog row beside it.
    events = await rows(
        engine,
        "SELECT id, type, payload FROM events WHERE story_id = :s ORDER BY seq",
        s=story,
    )
    replies = {e.payload["text"]: str(e.id) for e in events if e.type == "message_rendered"}
    for reply in (REPLIES[i] for i in NAMING):
        assert (
            "events",
            replies[reply],
            "payload.text",
            "mentioned",
        ) in character, reply
        assert ("events", replies[reply], "payload.text", "mentioned") in catalog, reply
    # Complement: the reply that names neither is hers only by its structured voice (the
    # characters it renders), never by a mention; an ingested narration that names neither
    # has no row for her at all.
    quiet = replies[REPLIES[2]]
    assert {(f, k) for t, r, f, k in character | catalog if t == "events" and r == quiet} == {
        ("payload.render_character_ids[0]", "declared")
    }
    narration = await rows(
        engine,
        "SELECT id FROM events WHERE story_id = :s AND payload->>'text' = :t",
        s=story,
        t=NARRATION,
    )
    assert len(narration) == 1
    assert str(narration[0].id) not in records(character, "events") | records(catalog, "events")
    summaries = {str(e.id) for e in events if e.type == "lite_summary_segment"}
    assert summaries
    assert summaries <= records(character, "events")
    pins = {str(e.id) for e in events if e.type == "pinned_memories_set"}
    assert pins
    assert pins <= records(character, "events")
    stored = f"story:{story}:{IDEMPOTENCY_KEY}"
    assert stored in records(character, "idempotency_keys")
    assert stored in records(catalog, "idempotency_keys")
    assert f"{messages[0]}:{RATER}" in records(catalog, "message_feedback")
    # The catalog side: her draft and versions, the scenario's seed, her caption.
    assert ("character_catalog", catalog_id, None, "declared") in catalog
    assert {f"{catalog_id}@1", f"{catalog_id}@2"} <= records(catalog, "character_versions")
    assert f"{scenario_id}@{version}" in records(catalog, "scenario_versions")
    assert scenario_id in records(catalog, "scenario_catalog")
    assert (
        "media_assets",
        asset["id"],
        "caption",
        "mentioned",
    ) in catalog

    # The rebuild equals the incremental index: the whole story, and this test's catalog
    # records (the shared database holds other tests' records, written before Linda).
    assert (await rebuild_scope(engine, story)).clean
    ours = {catalog_id, f"{catalog_id}@1", f"{catalog_id}@2", scenario_id, asset["id"]}
    ours.add(f"{scenario_id}@{version}")
    async with engine.connect() as conn:
        mine = [
            r for r in await records_of(conn, None) if r.record_id in ours and r.table != "events"
        ]
        expected = set(await compute(conn, mine))
        held = {r for r in await current(conn, None) if r[5] in ours and r[4] != "events"}
    assert expected == held
    # The runtime half of the coverage check: every table holding one of these records
    # was indexed by its writer during the run.
    async with engine.connect() as conn:
        written = {r.table for r in await records_of(conn, story)} | {r.table for r in mine}
    assert written <= set(seen)
    assert {"events", "idempotency_keys", "message_feedback", "media_assets"} <= written

    # The tool: a removed row is reported, put back by --write, then nothing differs.
    await execute_in_tx(
        engine,
        (
            "DELETE FROM subject_references WHERE record_table = 'events' "
            "AND record_id = :r AND subject_id = :s",
            {"r": replies[REPLIES[0]], "s": mother},
        ),
    )
    monkeypatch.setattr(reindex_subjects, "load_settings", lambda: stub_settings)
    assert await tool("--story", story) == (1, {replies[REPLIES[0]]})
    assert (await tool("--story", story, "--write"))[0] == 1
    assert await tool("--story", story) == (0, set())
    assert ("events", replies[REPLIES[0]], "payload.text", "mentioned") in await refs(
        engine, "character", mother
    )


async def tool(*argv: str) -> tuple[int, set[str]]:
    """``reindex_subjects`` with ``argv``: its exit code and the record ids of its missing
    rows."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = await reindex_subjects.execute(list(argv))
    report = json.loads(out.getvalue())
    missing = {row["record_id"] for scope in report["scopes"] for row in scope["missing"]}
    return code, missing


# -- the story row and the persona link (verification round 1) --------------------------------


async def test_a_story_named_after_its_cast_mentions_them(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A story created from a cast (no scenario) whose name mentions one of its characters has
    the ``stories`` row's ``name`` mention for that character, and nothing for the one it does
    not name; the rebuild equals the incremental index. Regression: the story row was indexed
    before its cast existed."""
    async with respond_client(stub_settings, engine) as (client, _lib):
        made = await client.post(
            "/stories",
            json=story_body("Ada", "Bram", name="Bram and the lighthouse"),
            headers=WRITE,
        )
        assert made.status_code == 201, made.text
    story = made.json()["story_id"]
    cast = {c["name"]: c["id"] for c in made.json()["characters"]}
    assert ("stories", story, "name", "mentioned") in await refs(engine, "character", cast["Bram"])
    assert story not in records(await refs(engine, "character", cast["Ada"]), "stories")
    assert (await rebuild_scope(engine, story)).clean


PERSONA = "Wren"
PERSONA_REPLY = "Wren shrugs off the rain and steps inside."


async def test_a_persona_player_is_named_as_the_persona(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A player character made from a saved persona: a message that names the persona has a
    ``character`` row for the player character and a ``persona`` row for the persona (the
    subject link), and a reply that names neither has no ``persona`` mention. The persona
    snapshot's name stays one of the character's terms when its own name differs (set
    directly: no route renames a character)."""
    owner = f"p-persona-{new_ulid()}"
    async with respond_client(stub_settings, engine) as (client, lib):
        saved = await client.post(
            "/personas",
            json={"name": PERSONA, "age": 29, "description": "Wry, and soaked through."},
            headers=g.headers(principal=owner),
        )
        assert saved.status_code == 201, saved.text
        persona = saved.json()["id"]
        scenario = g.scenario_body(
            g.role("mother", starting_location="kitchen"),
            g.role("player", kind="player", label="The eldest"),
        )
        scenario_id, _version = await g.published_scenario(client, scenario)
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id,
                slots={"mother": g.ad_hoc_slot("Maud", 58)},
                players={"player": g.player(persona_id=persona)},
                ignore_warnings=True,
            ),
        )
        story = made["story_id"]
        player = next(c["id"] for c in made["characters"] if c["role_id"] == "player")
        lib.models.stub.script("generation", [PERSONA_REPLY, "The kettle sings."])
        for key in ("persona-turn-1", "persona-turn-2"):
            await played(client, story, body(player, "Hello."), headers={"Idempotency-Key": key})

    events = await rows(
        engine,
        "SELECT id, payload FROM events WHERE story_id = :s AND type = 'message_rendered'",
        s=story,
    )
    replies = {e.payload["text"]: str(e.id) for e in events}
    named = ("events", replies[PERSONA_REPLY], "payload.text", "mentioned")
    assert named in await refs(engine, "character", player)
    by_persona = await refs(engine, "persona", persona)
    assert named in by_persona
    assert ("characters", player, None, "declared") in by_persona
    quiet = replies["The kettle sings."]
    assert not {k for t, r, _, k in by_persona if t == "events" and r == quiet} - {"declared"}
    assert (await rebuild_scope(engine, story)).clean

    await execute_in_tx(
        engine,
        ("UPDATE characters SET name = 'The eldest' WHERE id = :c", {"c": player}),
    )
    async with engine.connect() as conn:
        subjects = {s.id: s for s in await index.story_subjects(conn, story)}
    assert PERSONA in subjects[player].terms


# -- invariant 3 -------------------------------------------------------------------------------


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Epistrel's JSON handler at DEBUG on a buffer; the root logger restored after."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    stream = io.StringIO()
    handler = telemetry_logging.configure_logging(level="DEBUG", stream=stream)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.handlers[:] = handlers
        root.setLevel(level)


async def test_indexing_logs_counts_only(engine: AsyncEngine, captured: io.StringIO) -> None:
    """Indexing a caption that names a persona logs one DEBUG line on the module's logger with
    the table, the record, the counts and no text: neither the caption nor the name appears in
    anything logged. (Run in a transaction that is rolled back.)"""
    name = "Quillon Marchbank"
    caption = f"{name} waves from the quay at dusk"
    record = new_ulid()
    async with engine.connect() as conn, conn.begin() as tx:
        await conn.execute(
            text(
                "INSERT INTO personas (id, owner_principal, name, age, description, created_at, "
                "updated_at) VALUES (:i, 'p-log', :n, 40, 'A harbour pilot.', now(), now())"
            ),
            {"i": new_ulid(), "n": name},
        )
        written = await index_references(
            conn, "media_assets", record, story_id=None, fields={"caption": caption}
        )
        await tx.rollback()
    assert written == 1
    logged = captured.getvalue()
    for planted in (name, "Quillon", "Marchbank", "quay"):
        assert planted not in logged
    (line,) = [
        json.loads(raw)
        for raw in logged.splitlines()
        if json.loads(raw).get("logger") == index.__name__
    ]
    assert line["level"] == "debug"
    assert line["event"] == "subject references indexed"
    assert (line["record_table"], line["record_id"], line["records"], line["rows"]) == (
        "media_assets",
        record,
        1,
        1,
    )
    assert set(line) <= {
        "event",
        "level",
        "logger",
        "timestamp",
        "record_table",
        "record_id",
        "story_id",
        "records",
        "rows",
        "trace_id",
        "span_id",
    }
