from __future__ import annotations

import json
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import __version__
from epistrel.core.catalog.companion_slots import defaults
from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.db import make_sync_engine
from epistrel.core.ids import new_ulid
from tests.conftest import alembic_config


def _state(url: str) -> tuple[bool, str | None]:
    """(vector extension present, schema_meta.engine_version or None if the table is absent)."""
    engine = make_sync_engine(url)
    try:
        with engine.connect() as conn:
            has_vector = bool(
                conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
            )
            has_table = bool(conn.execute(text("SELECT to_regclass('schema_meta')")).scalar())
            version = (
                conn.execute(
                    text("SELECT value FROM schema_meta WHERE key = 'engine_version'")
                ).scalar()
                if has_table
                else None
            )
            return has_vector, version
    finally:
        engine.dispose()


def test_upgrade_head_then_downgrade_base(scratch_database: str) -> None:
    cfg = alembic_config(scratch_database)
    assert _state(scratch_database) == (False, None), "scratch database must start empty"

    command.upgrade(cfg, "head")
    assert _state(scratch_database) == (True, __version__)

    command.downgrade(cfg, "base")
    has_vector, version = _state(scratch_database)
    assert has_vector is False, "downgrade must drop the vector extension"
    assert version is None, "downgrade must drop schema_meta"


async def test_async_engine_reads_shared_database(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        version = await conn.scalar(
            text("SELECT value FROM schema_meta WHERE key = 'engine_version'")
        )
        has_vector = await conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    assert version == __version__
    assert has_vector == 1


def test_catalog_downgrade_keeps_other_audit_rows(scratch_database: str) -> None:
    """0020 (#103) downgrades with catalog data in place: the ``catalog`` schema and the
    characters' provenance columns go, and so do the ``catalog_entry_deleted`` audit rows the
    restored check would refuse; every other audit row stays. Upgrading again restores both."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "head")
    engine = make_sync_engine(scratch_database)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_log (id, principal, kind) VALUES "
                    "('0000000000000000000000AUD1', 'p-1', 'story_deleted'), "
                    "('0000000000000000000000AUD2', 'p-1', 'catalog_entry_deleted')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO catalog.character_catalog (id, owner_principal, listing_status, "
                    "status, draft, draft_revision, created_at, updated_at) VALUES "
                    "('0000000000000000000000CAT1', 'p-1', 'private', 'published', '{}', 2, "
                    "now(), now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO catalog.character_versions (catalog_id, version, content, "
                    "published_at, published_by) VALUES "
                    "('0000000000000000000000CAT1', 1, '{}', now(), 'p-1')"
                )
            )
        command.downgrade(cfg, "0019")
        with engine.connect() as conn:
            kinds = conn.scalars(text("SELECT kind FROM audit_log ORDER BY id")).all()
            catalog = conn.scalar(text("SELECT to_regnamespace('catalog')"))
            columns = conn.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'characters' AND column_name LIKE 'source_catalog%'"
                )
            ).all()
        assert (kinds, catalog, columns) == (["story_deleted"], None, [])
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM catalog.character_catalog")) == 0
    finally:
        engine.dispose()


BACKFILLED = {
    "0000000000000000000000STR1": "p-creator",
    "0000000000000000000000STR2": "p-forker",
    "0000000000000000000000STR3": "system",
    "0000000000000000000000STR4": "system",
}
"""What 0021 (#104) makes each story's owner: its genesis's principal; a fork's own genesis,
the newer of its two; ``system`` with no committed genesis."""


def test_ownership_backfill_and_downgrade(scratch_database: str) -> None:
    """0021 (#104) gives every existing story an owner and drops cleanly. Upgrading over stories
    made before it backfills ``owner_principal`` from the newest committed genesis operation's
    principal (a fork holds its source's copied genesis and its own, newer one), else
    ``system``. The column ends NOT NULL with its index. Downgrading with relations and
    ownership events in place drops both tables, the column and the indexes. Upgrading again
    restores them, empty."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0020")
    engine = make_sync_engine(scratch_database)
    try:
        with engine.begin() as conn:
            for story in BACKFILLED:
                conn.execute(text("INSERT INTO stories (id) VALUES (:s)"), {"s": story})
            conn.execute(
                text(
                    "INSERT INTO operations (operation_id, story_id, kind, principal, status, "
                    "committed_at) VALUES "
                    "('0000000000000000000000OPS1', '0000000000000000000000STR1', 'genesis', "
                    "'p-creator', 'committed', now() - interval '2 hours'), "
                    "('0000000000000000000000OPS2', '0000000000000000000000STR2', 'genesis', "
                    "'p-creator', 'committed', now() - interval '2 hours'), "
                    "('0000000000000000000000OPS3', '0000000000000000000000STR2', 'genesis', "
                    "'p-forker', 'committed', now() - interval '1 hour'), "
                    "('0000000000000000000000OPS4', '0000000000000000000000STR2', 'control', "
                    "'p-other', 'committed', now()), "
                    "('0000000000000000000000OPS5', '0000000000000000000000STR4', 'genesis', "
                    "'p-failed', 'failed', NULL)"
                )
            )
        command.upgrade(cfg, "0021")
        with engine.connect() as conn:
            owners = {
                str(row.id): str(row.owner_principal)
                for row in conn.execute(text("SELECT id, owner_principal FROM stories"))
            }
            nullable = conn.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'stories' AND column_name = 'owner_principal'"
                )
            )
            indexes = set(
                conn.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE indexname IN "
                        "('ix_stories_owner_principal', 'ix_character_catalog_owner_principal', "
                        "'ix_entity_relations_principal', 'ix_ownership_events_entity')"
                    )
                )
            )
        assert owners == BACKFILLED
        assert nullable == "NO"
        assert len(indexes) == 4

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO entity_relations (entity_type, entity_id, principal, relation, "
                    "created_by) VALUES ('story', '0000000000000000000000STR1', 'p-d', 'editor', "
                    "'p-creator')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO ownership_events (id, entity_type, entity_id, kind, old, new, "
                    "acting_principal) VALUES ('0000000000000000000000EVT1', 'story', "
                    "'0000000000000000000000STR1', 'transfer', '\"p-creator\"', '\"p-d\"', 'p-d')"
                )
            )
        command.downgrade(cfg, "0020")
        with engine.connect() as conn:
            tables = conn.execute(
                text(
                    "SELECT to_regclass('entity_relations'), to_regclass('ownership_events'), "
                    "to_regclass('ix_stories_owner_principal'), "
                    "to_regclass('catalog.ix_character_catalog_owner_principal')"
                )
            ).one()
            column = conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'stories' AND column_name = 'owner_principal'"
                )
            )
        assert (tuple(tables), column) == ((None, None, None, None), 0)
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM entity_relations")) == 0
            assert conn.scalar(text("SELECT count(*) FROM ownership_events")) == 0
            assert (
                conn.scalar(
                    text(
                        "SELECT owner_principal FROM stories "
                        "WHERE id = '0000000000000000000000STR2'"
                    )
                )
                == "p-forker"
            )
    finally:
        engine.dispose()


INTERIM = {
    "name": "Mara Venn",
    "public_description": "Keeps the light.",
    "public_card": {"images": {"primary": "0000000000000000000000IMG1", "gallery": []}},
    "definition": {"personality": "Wry."},
}
"""A catalog character document as #103 and #114 stored it: the card's name and public
description at the top level, its images under ``public_card``."""
MOVED = {
    "public_card": {
        "name": "Mara Venn",
        "public_description": "Keeps the light.",
        "images": {"primary": "0000000000000000000000IMG1", "gallery": []},
    },
    "definition": {"personality": "Wry."},
}
"""The same document in #115's shape."""
CAT1 = "CAT1".ljust(26, "0")
CAT2 = "CAT2".ljust(26, "0")


def _documents(url: str) -> tuple[Any, Any, Any, bool, bool]:
    """The planted draft and version, the untouched draft, whether ``mara`` finds the version,
    and whether ``characters.deflection_style`` exists."""
    engine = make_sync_engine(url)
    try:
        with engine.connect() as conn:
            draft = conn.scalar(
                text("SELECT draft FROM catalog.character_catalog WHERE id = :c"), {"c": CAT1}
            )
            other = conn.scalar(
                text("SELECT draft FROM catalog.character_catalog WHERE id = :c"), {"c": CAT2}
            )
            content, found = conn.execute(
                text(
                    "SELECT content, search @@ to_tsquery('simple', 'mara') "
                    "FROM catalog.character_versions WHERE catalog_id = :c"
                ),
                {"c": CAT1},
            ).one()
            column = conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'characters' AND column_name = 'deflection_style'"
                )
            )
        return draft, content, other, bool(found), column == 1
    finally:
        engine.dispose()


def test_character_documents_move_under_the_public_card(scratch_database: str) -> None:
    """0032 (#115) moves a catalog character's top-level ``name`` and ``public_description``
    under ``public_card``, in drafts and published versions alike (past the versions'
    immutability trigger), keeping what ``public_card`` already held; the version still searches
    by its name. It adds ``characters.deflection_style``. Downgrading puts both keys back at the
    top level and drops the column. A document without the keys is left as it is both ways."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0031")
    engine = make_sync_engine(scratch_database)
    try:
        with engine.begin() as conn:
            for entry, document in (("CAT1", INTERIM), ("CAT2", {"definition": {}})):
                conn.execute(
                    text(
                        "INSERT INTO catalog.character_catalog (id, owner_principal, "
                        "listing_status, status, draft, draft_revision, created_at, updated_at) "
                        "VALUES (rpad(:c, 26, '0'), 'p-1', 'private', 'draft', "
                        "CAST(:d AS jsonb), 1, now(), now())"
                    ),
                    {"c": entry, "d": json.dumps(document)},
                )
            conn.execute(
                text(
                    "INSERT INTO catalog.character_versions (catalog_id, version, content, "
                    "published_at, published_by) VALUES (rpad('CAT1', 26, '0'), 1, "
                    "CAST(:d AS jsonb), now(), 'p-1')"
                ),
                {"d": json.dumps(INTERIM)},
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0032")
    assert _documents(scratch_database) == (MOVED, MOVED, {"definition": {}}, True, True)
    command.downgrade(cfg, "0031")
    assert _documents(scratch_database) == (INTERIM, INTERIM, {"definition": {}}, True, False)


PUBLISHED_ID, ARCHIVED_ID, NAMELESS_ID, TWICE_ID, RATING_ID = (new_ulid() for _ in range(5))
CHARACTERS_0032 = {
    # id: (owner, listing status, status, current version, draft)
    PUBLISHED_ID: (
        "p-1",
        "public",
        "published",
        2,
        {
            "public_card": {"name": "Mara Venn"},
            "metadata": {"content_rating_id": RATING_ID},
        },
    ),
    ARCHIVED_ID: ("p-2", "private", "archived", None, {"public_card": {"name": "Ivo"}}),
    NAMELESS_ID: ("p-3", "in_review", "draft", None, {}),
    TWICE_ID: ("p-4", "private", "draft", None, {"public_card": {"name": "Wren"}}),
}
"""Catalog characters as 0032 holds them (a published, an archived, a nameless draft, and one
that two scenarios name as their companion)."""
OLDER = new_ulid()
NEWER = new_ulid()


def _companions(url: str) -> dict[str, Any]:
    """Each scenario row, by id: what a companion is."""
    engine = make_sync_engine(url)
    try:
        with engine.connect() as conn:
            found = conn.execute(
                text(
                    "SELECT id, companion_of, owner_principal, listing_status, status, "
                    "current_version, draft_revision, draft FROM catalog.scenario_catalog"
                )
            ).all()
        return {str(r.id): r for r in found}
    finally:
        engine.dispose()


def test_every_character_gets_a_companion(scratch_database: str) -> None:
    """0033 (#116) gives every catalog character without a companion scenario the one #116's
    creation makes: the defaults, named and rated as the character's draft is, pinned to its
    latest published version (none before its first publish), at the character's owner and
    listing status, ``archived`` with an archived character and a ``draft`` otherwise, at draft
    revision 1, with no version. A character two scenarios name keeps the older as its
    companion; the newer becomes an ordinary scenario. ``companion_of`` becomes unique and
    ``content_rating_explicit`` is false. Downgrading drops the flag and the unique index, and
    keeps the companions."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0032")
    engine = make_sync_engine(scratch_database)
    try:
        with engine.begin() as conn:
            for cid, (owner, listing, status, version, draft) in CHARACTERS_0032.items():
                conn.execute(
                    text(
                        "INSERT INTO catalog.character_catalog (id, owner_principal, "
                        "listing_status, status, current_version, draft, draft_revision, "
                        "created_at, updated_at) VALUES (:c, :o, :l, "
                        "CAST(:s AS catalog.entry_status), :v, CAST(:d AS jsonb), 1, now(), now())"
                    ),
                    {
                        "c": cid,
                        "o": owner,
                        "l": listing,
                        "s": status,
                        "v": version,
                        "d": json.dumps(draft),
                    },
                )
            for sid, age in ((OLDER, "2 days"), (NEWER, "1 day")):
                conn.execute(
                    text(
                        "INSERT INTO catalog.scenario_catalog (id, owner_principal, "
                        "listing_status, status, draft, draft_revision, companion_of, created_at, "
                        "updated_at) VALUES (:s, 'p-4', 'private', 'draft', '{}'::jsonb, 3, :c, "
                        "now() - CAST(:a AS interval), now())"
                    ),
                    {"s": sid, "c": TWICE_ID, "a": age},
                )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0033")
    found = _companions(scratch_database)
    assert found[NEWER].companion_of is None
    assert (found[OLDER].companion_of, found[OLDER].draft_revision) == (TWICE_ID, 3)
    made = {str(r.companion_of): r for r in found.values() if str(r.id) not in (OLDER, NEWER)}
    assert set(made) == set(CHARACTERS_0032) - {TWICE_ID}
    for cid, (owner, listing, status, version, draft) in CHARACTERS_0032.items():
        if cid not in made:
            continue
        row = made[cid]
        expected = "archived" if status == "archived" else "draft"
        assert (row.owner_principal, row.listing_status, row.status) == (owner, listing, expected)
        assert (row.current_version, row.draft_revision) == (None, 1)
        name = draft.get("public_card", {}).get("name")
        rating = draft.get("metadata", {}).get("content_rating_id")
        assert ScenarioDraft.model_validate(row.draft).document() == defaults(
            cid, name=name, rating_id=rating, pin=version
        )
    assert made[PUBLISHED_ID].draft["cast"]["roles"][0]["character"]["version"] == 2
    engine = make_sync_engine(scratch_database)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM catalog.scenario_versions")) == 0
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM catalog.scenario_catalog "
                        "WHERE content_rating_explicit"
                    )
                )
                == 0
            )
            assert conn.scalar(
                text(
                    "SELECT indexdef LIKE 'CREATE UNIQUE INDEX%' FROM pg_indexes "
                    "WHERE indexname = 'uq_scenario_catalog_companion_of'"
                )
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, "0032")
    assert set(_companions(scratch_database)) == set(found)
    engine = make_sync_engine(scratch_database)
    try:
        with engine.connect() as conn:
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE column_name = 'content_rating_explicit'"
                    )
                )
                == 0
            )
            assert conn.scalar(
                text(
                    "SELECT indexdef LIKE 'CREATE INDEX%' FROM pg_indexes "
                    "WHERE indexname = 'ix_scenario_catalog_companion_of'"
                )
            )
    finally:
        engine.dispose()


def test_json_null_idempotency_results_become_sql_null(scratch_database: str) -> None:
    """0042 (#361): a result purged to JSON ``null`` before the fix becomes SQL NULL; a stored
    result and a row that never had one are unchanged."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0041")
    engine = make_sync_engine(scratch_database)
    try:
        with engine.begin() as conn:
            for key, result in (("purged", "null"), ("stored", '{"status": 200}'), ("none", None)):
                conn.execute(
                    text(
                        "INSERT INTO idempotency_keys (scope, scope_id, key, request_hash, "
                        "status, result, owner_token, lease_until) VALUES ('deployment', "
                        "'deployment', :k, 'h', 'done', CAST(:r AS jsonb), 't', now())"
                    ),
                    {"k": key, "r": result},
                )
        command.upgrade(cfg, "0042")
        with engine.connect() as conn:
            found = {
                row.key: row.kind
                for row in conn.execute(
                    text("SELECT key, jsonb_typeof(result) AS kind FROM idempotency_keys")
                )
            }
            nulls = {
                row.key
                for row in conn.execute(
                    text("SELECT key FROM idempotency_keys WHERE result IS NULL")
                )
            }
    finally:
        engine.dispose()
    assert found == {"purged": None, "stored": "object", "none": None}
    assert nulls == {"purged", "none"}


def test_sent_hash_is_added_null_and_dropped_on_downgrade(scratch_database: str) -> None:
    """0043 (#375, #389): ``idempotency_keys.sent_hash`` is added nullable, with no backfill (a
    key stored before it holds null, which an erasure's retry answers 409), and the downgrade
    drops it and keeps the rows."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0042")
    engine = make_sync_engine(scratch_database)
    columns = (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'idempotency_keys' AND column_name = 'sent_hash'"
    )
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO idempotency_keys (scope, scope_id, key, request_hash, status, "
                    "owner_token, lease_until) VALUES ('deployment', 'deployment', 'old', 'h', "
                    "'done', 't', now())"
                )
            )
        command.upgrade(cfg, "0043")
        with engine.connect() as conn:
            assert conn.scalar(text(columns)) == "sent_hash"
            legacy = conn.execute(text("SELECT key, sent_hash FROM idempotency_keys")).all()
        command.downgrade(cfg, "0042")
        with engine.connect() as conn:
            assert conn.scalar(text(columns)) is None
            kept = conn.scalars(text("SELECT key FROM idempotency_keys")).all()
    finally:
        engine.dispose()
    assert [tuple(r) for r in legacy] == [("old", None)]
    assert kept == ["old"]


def test_every_character_gets_an_entity(scratch_database: str) -> None:
    """0054 (#139): each existing character gets a ``person`` entity with the character's id,
    its name as a ``canonical`` alias and its persona's gender; the downgrade drops the new
    columns and keeps the rows."""
    from epistrel.core.seeds.profiles import LITE_PRESET_ID

    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0053")
    engine = make_sync_engine(scratch_database)
    story, player, npc = new_ulid(), new_ulid(), new_ulid()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO stories (id, owner_principal, engine_profile_id) "
                    "VALUES (:s, 'p-test', :p)"
                ),
                {"s": story, "p": LITE_PRESET_ID},
            )
            for character, ordinal, name, persona in (
                (player, 0, "Sam", '{"name": "Sam", "gender": "Woman"}'),
                (npc, 1, "Tom", None),
            ):
                conn.execute(
                    text(
                        "INSERT INTO characters (id, story_id, ordinal, name, definition, "
                        "true_age, is_player, persona_snapshot) VALUES (:c, :s, :o, :n, 'x', 30, "
                        ":player, CAST(:persona AS jsonb))"
                    ),
                    {
                        "c": character,
                        "s": story,
                        "o": ordinal,
                        "n": name,
                        "player": ordinal == 0,
                        "persona": persona,
                    },
                )
        command.upgrade(cfg, "0054")
        with engine.connect() as conn:
            found = conn.execute(
                text(
                    "SELECT e.id, e.kind, e.canonical_name, e.character_id, e.gender, "
                    "e.descriptors, a.alias, a.source FROM entities e "
                    "JOIN entity_aliases a ON a.entity_id = e.id ORDER BY e.canonical_name"
                )
            ).all()
        command.downgrade(cfg, "0053")
        with engine.connect() as conn:
            kept = conn.scalar(text("SELECT count(*) FROM entities"))
    finally:
        engine.dispose()
    assert [tuple(r) for r in found] == [
        (player, "person", "Sam", player, "feminine", [], "Sam", "canonical"),
        (npc, "person", "Tom", npc, None, [], "Tom", "canonical"),
    ]
    assert kept == 2


def test_the_implication_depth_joins_the_profile_defaults(scratch_database: str) -> None:
    """0055 (#140): ``verification.implication_max_depth`` is added to the deployment's
    ``profile_defaults`` at the schema's default (3) and the rest of the document is kept; a
    value already there is kept; the downgrade removes it again."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0054")
    engine = make_sync_engine(scratch_database)

    def depth() -> Any:
        with engine.connect() as conn:
            return conn.scalar(
                text(
                    "SELECT profile_defaults->'verification'->'implication_max_depth' "
                    "FROM deployment_state"
                )
            )

    def defaults() -> Any:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT profile_defaults FROM deployment_state"))

    try:
        before = defaults()
        assert depth() is None
        command.upgrade(cfg, "0055")
        assert depth() == 3
        after = defaults()
        after["verification"].pop("implication_max_depth")
        assert after == before
        command.downgrade(cfg, "0054")
        assert depth() is None
        assert defaults() == before
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE deployment_state SET profile_defaults = jsonb_set(profile_defaults, "
                    "'{verification,implication_max_depth}', '5')"
                )
            )
        command.upgrade(cfg, "0055")
        assert depth() == 5
    finally:
        engine.dispose()


def test_the_retrieval_columns_and_dials(scratch_database: str) -> None:
    """0056 (#141): ``memories`` gains ``event_type``, ``participants`` (``{}`` for existing
    rows) and the generated ``search`` vector with their indexes; the guard lets ``search``
    through; the retrieval dials join the profile defaults at the schema's values and the rest
    of the document is kept; the downgrade removes all of it again."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0055")
    engine = make_sync_engine(scratch_database)

    def columns() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memories'"
                    )
                )
            )

    def defaults() -> Any:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT profile_defaults FROM deployment_state"))

    def indexes() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(text("SELECT indexname FROM pg_indexes WHERE tablename = 'memories'"))
            )

    added = {"ix_memories_participants", "ix_memories_search", "ix_memories_story_occasion"}
    try:
        before = defaults()
        assert not {"event_type", "participants", "search"} & columns()
        command.upgrade(cfg, "0056")
        assert {"event_type", "participants", "search"} <= columns()
        assert added <= indexes()
        after = defaults()
        episodic = after["memory"]["episodic"]
        assert {
            k: episodic.pop(k) for k in list(episodic) if k not in before["memory"]["episodic"]
        } == {
            "weight_similarity": 0.6,
            "weight_recency": 0.2,
            "weight_importance": 0.2,
            "recency_half_life_turns": 100,
            "retrieval_token_budget": 3_000,
        }
        assert after == before
        with engine.connect() as conn:
            guard = conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_memories_guard'")
            )
        assert "'search'" in str(guard)
        command.downgrade(cfg, "0055")
        assert not {"event_type", "participants", "search"} & columns()
        assert not added & indexes()
        assert defaults() == before
    finally:
        engine.dispose()


def test_the_escalation_dials_join_the_profile_defaults(scratch_database: str) -> None:
    """0057 (#142): ``verification.entailment_min_confidence`` (0.6) and
    ``verification.safe_disposition`` (``repair``) join the deployment's ``profile_defaults``
    and the rest of the document is kept; a stored ``entailment_gate`` of 0.5 (the unreleased
    0047's default) becomes 1.0, and any other value is kept; the downgrade removes the dials
    and leaves the gate."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0056")
    engine = make_sync_engine(scratch_database)

    def defaults() -> Any:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT profile_defaults FROM deployment_state"))

    def set_gate(value: str) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE deployment_state SET profile_defaults = jsonb_set(profile_defaults, "
                    "'{verification,entailment_gate}', CAST(:v AS jsonb))"
                ),
                {"v": value},
            )

    try:
        set_gate("0.5")
        before = defaults()
        assert "safe_disposition" not in before["verification"]
        command.upgrade(cfg, "0057")
        after = defaults()
        verification = after["verification"]
        assert verification.pop("entailment_min_confidence") == 0.6
        assert verification.pop("safe_disposition") == "repair"
        assert verification.pop("entailment_gate") == 1.0
        before["verification"].pop("entailment_gate")
        assert after == before
        command.downgrade(cfg, "0056")
        reverted = defaults()["verification"]
        assert "safe_disposition" not in reverted
        assert "entailment_min_confidence" not in reverted
        assert reverted["entailment_gate"] == 1.0
        set_gate("0.8")
        command.upgrade(cfg, "0057")
        assert defaults()["verification"]["entailment_gate"] == 0.8
    finally:
        engine.dispose()


def test_the_consolidation_columns_and_dial(scratch_database: str) -> None:
    """0058 (#143): ``memories`` gains ``covered_by`` (``{}`` for existing rows), ``level``,
    ``modality``, ``inputs_hash``, ``model``, ``model_version`` and ``prompt_hash`` with their
    checks and ``ix_memories_story_kind_turn``; the guard lets ``covered_by`` change;
    ``memory.consolidation.window_memories`` (50) joins the profile defaults and the rest of the
    document is kept; the downgrade removes all of it again."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0057")
    engine = make_sync_engine(scratch_database)
    added = {"covered_by", "level", "modality", "inputs_hash", "model", "model_version"}
    added.add("prompt_hash")

    def columns() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memories'"
                    )
                )
            )

    def defaults() -> Any:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT profile_defaults FROM deployment_state"))

    def indexes() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(text("SELECT indexname FROM pg_indexes WHERE tablename = 'memories'"))
            )

    try:
        before = defaults()
        assert not added & columns()
        command.upgrade(cfg, "0058")
        assert added <= columns()
        assert "ix_memories_story_kind_turn" in indexes()
        after = defaults()
        assert after["memory"]["consolidation"].pop("window_memories") == 50
        assert after == before
        with engine.connect() as conn:
            guard = conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_memories_guard'")
            )
        assert "'covered_by'" in str(guard)
        assert "'search'" in str(guard)
        command.downgrade(cfg, "0057")
        assert not added & columns()
        assert "ix_memories_story_kind_turn" not in indexes()
        assert defaults() == before
        with engine.connect() as conn:
            guard = conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_memories_guard'")
            )
        assert "'covered_by'" not in str(guard)
    finally:
        engine.dispose()


def test_the_quarantine_columns_guard_and_dial(scratch_database: str) -> None:
    """0059 (#145): ``memories`` gains ``quarantined_at``, ``quarantine_reason``,
    ``quarantined_by`` and ``implication_count`` (``0`` for existing rows) with their checks; the
    guard lets them change and, under ``epistrel.sanitize``, one row's text;
    ``repair.implication_threshold`` (2) joins the profile defaults and the rest of the document
    is kept; the downgrade removes all of it again."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0058")
    engine = make_sync_engine(scratch_database)
    added = {"quarantined_at", "quarantine_reason", "quarantined_by", "implication_count"}

    def columns() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memories'"
                    )
                )
            )

    def defaults() -> Any:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT profile_defaults FROM deployment_state"))

    def guard() -> str:
        with engine.connect() as conn:
            return str(
                conn.scalar(
                    text("SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_memories_guard'")
                )
            )

    try:
        before = defaults()
        assert not added & columns()
        command.upgrade(cfg, "0059")
        assert added <= columns()
        after = defaults()
        assert after["repair"].pop("implication_threshold") == 2
        assert after == before
        assert "'quarantine_reason'" in guard()
        assert "epistrel.sanitize" in guard()
        assert "'covered_by'" in guard()
        command.downgrade(cfg, "0058")
        assert not added & columns()
        assert defaults() == before
        assert "epistrel.sanitize" not in guard()
        assert "'covered_by'" in guard()
    finally:
        engine.dispose()


def test_the_commitment_tables_and_lifecycle_guard(scratch_database: str) -> None:
    """0060 (#146): ``thunks`` and ``promissory_ledger`` are created, ``fact_commit_state``
    gains ``revised``, and the facts guard lets ``commit_state`` move along the lifecycle's
    edges only; the downgrade drops the tables and types, takes ``revised`` out of the enum and
    restores 0049's guard."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0059")
    engine = make_sync_engine(scratch_database)

    def tables() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_name IN ('thunks', 'promissory_ledger')"
                    )
                )
            )

    def states() -> list[str]:
        with engine.connect() as conn:
            return list(
                conn.scalars(
                    text(
                        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                        "WHERE t.typname = 'fact_commit_state' ORDER BY e.enumsortorder"
                    )
                )
            )

    def guard() -> str:
        with engine.connect() as conn:
            return str(
                conn.scalar(
                    text("SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_facts_append_only'")
                )
            )

    try:
        assert tables() == set()
        assert states() == ["committed", "introduced", "pending"]
        command.upgrade(cfg, "0060")
        assert tables() == {"thunks", "promissory_ledger"}
        assert states() == ["committed", "introduced", "pending", "revised"]
        assert "'committed>revised'" in guard()
        assert "'introduced>revised'" not in guard()
        command.downgrade(cfg, "0059")
        assert tables() == set()
        assert states() == ["committed", "introduced", "pending"]
        assert "commit_state" not in guard()
    finally:
        engine.dispose()


def test_the_hot_set_columns_table_and_dials(scratch_database: str) -> None:
    """0061 (#147): ``memories`` gains the pin and retirement columns (a pinned row is always
    hot, CHECK) and the guard lets them change, ``stories`` gains ``hot_counts`` (``NULL``:
    computed when first needed), ``overflow_episodes`` is created, and the four new
    ``memory.hot_set`` dials join ``profile_defaults`` at the schema's defaults; the downgrade
    removes all of it and restores 0059's guard."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0060")
    engine = make_sync_engine(scratch_database)

    def columns(table: str) -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
                    ),
                    {"t": table},
                )
            )

    def guard() -> str:
        with engine.connect() as conn:
            return str(
                conn.scalar(
                    text("SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_memories_guard'")
                )
            )

    def hot_set_defaults() -> dict[str, Any]:
        with engine.connect() as conn:
            found = conn.scalar(text("SELECT profile_defaults FROM deployment_state"))
        return dict((found or {}).get("memory", {}).get("hot_set", {}))

    added = {"pinned", "pinned_by", "retired_reason", "retired_at", "retired_turn"}
    try:
        assert not added & columns("memories")
        assert "hot_counts" not in columns("stories")
        assert columns("overflow_episodes") == set()
        assert set(hot_set_defaults()) == {"memories_per_character", "vectors_per_story"}
        command.upgrade(cfg, "0061")
        assert added <= columns("memories")
        assert "hot_counts" in columns("stories")
        assert {"opened_turn", "deadline_turn", "closed_turn", "excess"} <= columns(
            "overflow_episodes"
        )
        assert "'retired_reason'" in guard()
        assert "'pinned'" in guard()
        assert hot_set_defaults() == {
            "memories_per_character": 2_000,
            "vectors_per_story": 50_000,
            "importance_floor": 0.2,
            "retirement_max_lag": 50,
            "retain_retired_vectors": False,
            "retain_dormant_vectors": False,
        }
        command.downgrade(cfg, "0060")
        assert not added & columns("memories")
        assert "hot_counts" not in columns("stories")
        assert columns("overflow_episodes") == set()
        assert "'retired_reason'" not in guard()
        assert "'implication_count'" in guard()
        assert set(hot_set_defaults()) == {"memories_per_character", "vectors_per_story"}
    finally:
        engine.dispose()


def test_the_extraction_shadow_table(scratch_database: str) -> None:
    """0062 (#148): ``extraction_shadow`` is created with its status CHECK and its indexes; the
    downgrade drops it."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0061")
    engine = make_sync_engine(scratch_database)

    def indexes() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'extraction_shadow'")
                )
            )

    try:
        assert indexes() == set()
        command.upgrade(cfg, "0062")
        assert indexes() == {
            "pk_extraction_shadow",
            "ix_extraction_shadow_story_operation",
            "ix_extraction_shadow_story_membership",
            "ix_extraction_shadow_story_created",
            "ix_extraction_shadow_created",
        }
        with engine.connect() as conn:
            checks = set(
                conn.scalars(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = "
                        "'extraction_shadow'::regclass AND contype = 'c'"
                    )
                )
            )
        assert checks == {"ck_extraction_shadow_status"}
        command.downgrade(cfg, "0061")
        assert indexes() == set()
    finally:
        engine.dispose()


def test_the_escalation_tables(scratch_database: str) -> None:
    """0065 (#155): ``escalations`` (its consequence CHECK and indexes), ``gate_thresholds``
    (mode and threshold CHECKs) and ``gate_threshold_history`` are created; the downgrade drops
    all three."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0064")
    engine = make_sync_engine(scratch_database)

    def tables() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text(
                        "SELECT tablename FROM pg_tables WHERE tablename IN "
                        "('escalations', 'gate_thresholds', 'gate_threshold_history')"
                    )
                )
            )

    def named(sql: str) -> set[str]:
        with engine.connect() as conn:
            return set(conn.scalars(text(sql)))

    try:
        assert tables() == set()
        command.upgrade(cfg, "0065")
        assert tables() == {"escalations", "gate_thresholds", "gate_threshold_history"}
        assert named("SELECT indexname FROM pg_indexes WHERE tablename = 'escalations'") == {
            "pk_escalations",
            "ix_escalations_story_operation",
            "ix_escalations_story_membership",
            "ix_escalations_at",
            "ix_escalations_shape_at",
        }
        checks = named(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conrelid IN "
            "('escalations'::regclass, 'gate_thresholds'::regclass, "
            "'gate_threshold_history'::regclass)"
        )
        assert checks == {
            "ck_escalations_consequence",
            "ck_gate_thresholds_mode",
            "ck_gate_thresholds_threshold",
            "ck_gate_threshold_history_action",
        }
        command.downgrade(cfg, "0064")
        assert tables() == set()
    finally:
        engine.dispose()


MEMORY_STORIES = {
    "01HZZZZZZZZZZZZZZZZZZZZZS1": 4,  # vectors on every other memory
    "01HZZZZZZZZZZZZZZZZZZZZZS2": 3,
    "01HZZZZZZZZZZZZZZZZZZZZZS3": 5,
    "01HZZZZZZZZZZZZZZZZZZZZZS4": 0,  # a story with no memory still gets its partition
}


def _memory_digests(conn: Any) -> dict[str, tuple[int, str]]:
    """Per story: its memories' count and the MD5 of every column of every row, in id order."""
    return {
        str(r.story_id).rstrip(): (int(r.n), str(r.digest))
        for r in conn.execute(
            text(
                "SELECT story_id, count(*) AS n, "
                "md5(string_agg(m::text, '|' ORDER BY m.id)) AS digest "
                "FROM memories m GROUP BY story_id"
            )
        )
    }


def test_memories_are_partitioned_by_story(scratch_database: str) -> None:
    """0151 (#784, ADR-187): over a pinned deployment's memories (three stories with memories,
    vectors on some, and one story with none), every story gets its own LIST partition of
    ``memories`` and each story's memories land in it with every column unchanged (per-story
    count and checksum); the parent's primary key is ``(id, story_id)``, it has no default
    partition, its indexes are the old names (the HNSW index included) and each partition
    carries ``fillfactor = 70``. The downgrade rebuilds the unpartitioned table, with the same
    rows, its ``(id)`` primary key, its indexes, triggers and ``fillfactor``."""
    from epistrel.core.memory.story_partitions import partition_name

    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0150")
    engine = make_sync_engine(scratch_database)

    def scalar(sql: str, **params: Any) -> Any:
        with engine.connect() as conn:
            return conn.scalar(text(sql), params)

    def indexes() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(text("SELECT indexname FROM pg_indexes WHERE tablename = 'memories'"))
            )

    def triggers() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.scalars(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'memories'::regclass "
                        "AND NOT tgisinternal"
                    )
                )
            )

    def primary_key() -> str:
        return str(
            scalar(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'memories'::regclass AND contype = 'p'"
            )
        )

    try:
        with engine.begin() as conn:
            # The pin's retype (``core.embeddings.retype``) at 4 dimensions.
            conn.execute(text("ALTER TABLE memories ALTER COLUMN embedding TYPE vector(4)"))
            conn.execute(
                text(
                    "CREATE INDEX ix_memories_embedding_hnsw ON memories USING hnsw "
                    "(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
                )
            )
            profile = conn.scalar(text("SELECT id FROM engine_profiles ORDER BY id LIMIT 1"))
            for ordinal, (story, count) in enumerate(MEMORY_STORIES.items()):
                operation = f"{story[:-2]}O{ordinal}"
                conn.execute(
                    text(
                        "INSERT INTO stories (id, owner_principal, engine_profile_id) "
                        "VALUES (:s, 'p-1', :p)"
                    ),
                    {"s": story, "p": profile},
                )
                conn.execute(
                    text(
                        "INSERT INTO operations (operation_id, story_id, kind, principal, status, "
                        "membership_tx) VALUES (:o, :s, 'genesis', 'p-1', 'committed', now())"
                    ),
                    {"o": operation, "s": story},
                )
                for n in range(count):
                    conn.execute(
                        text(
                            "INSERT INTO memories (id, story_id, kind, text, provenance, "
                            "trust_tier, operation_id, membership_tx, importance, embedding, "
                            "participants) VALUES (:i, :s, 'episodic', :t, 'render', 'in_world', "
                            ":o, now(), :imp, CAST(:v AS vector), ARRAY[:s]::char(26)[])"
                        ),
                        {
                            "i": new_ulid(),
                            "s": story,
                            "t": f"Memory {n} of story {ordinal}.",
                            "o": operation,
                            "imp": n / 10,
                            "v": None if n % 2 else f"[{n + 1}, {ordinal}, 0.5, 0.25]",
                        },
                    )
        with engine.connect() as conn:
            before = _memory_digests(conn)
        assert {s: n for s, (n, _) in before.items()} == {
            s: n for s, n in MEMORY_STORIES.items() if n
        }
        flat_indexes = indexes()
        assert "ix_memories_embedding_hnsw" in flat_indexes
        assert primary_key() == "PRIMARY KEY (id)"

        command.upgrade(cfg, "0151")
        with engine.connect() as conn:
            assert _memory_digests(conn) == before
            landed = {
                (str(r.story_id).rstrip(), str(r.partition))
                for r in conn.execute(
                    text("SELECT DISTINCT story_id, tableoid::regclass AS partition FROM memories")
                )
            }
            partitions = set(
                conn.scalars(
                    text(
                        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = "
                        "i.inhrelid WHERE i.inhparent = 'memories'::regclass"
                    )
                )
            )
            fillfactors = set(
                conn.scalars(
                    text(
                        "SELECT array_to_string(c.reloptions, ',') FROM pg_inherits i "
                        "JOIN pg_class c ON c.oid = i.inhrelid "
                        "WHERE i.inhparent = 'memories'::regclass"
                    )
                )
            )
        assert landed == {(s, partition_name(s)) for s, n in MEMORY_STORIES.items() if n}
        assert partitions == {partition_name(s) for s in MEMORY_STORIES}
        assert fillfactors == {"fillfactor=70"}
        assert scalar("SELECT relkind FROM pg_class WHERE relname = 'memories'") == "p"
        assert scalar("SELECT pg_get_partkeydef(CAST('memories' AS regclass))") == "LIST (story_id)"
        assert scalar("SELECT partdefid FROM pg_partitioned_table") == 0  # no default
        assert scalar("SELECT to_regclass('memories_old')") is None
        assert primary_key() == "PRIMARY KEY (id, story_id)"
        assert indexes() == flat_indexes
        assert triggers() == {"memories_guard", "memories_no_truncate"}
        assert (
            scalar(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'memories_no_truncate' "
                "AND tgrelid = CAST(:p AS regclass)",
                p=partition_name(next(iter(MEMORY_STORIES))),
            )
            == 1
        )

        command.downgrade(cfg, "0150")
        with engine.connect() as conn:
            assert _memory_digests(conn) == before
        assert scalar("SELECT relkind FROM pg_class WHERE relname = 'memories'") == "r"
        assert scalar("SELECT reloptions FROM pg_class WHERE relname = 'memories'") == [
            "fillfactor=70"
        ]
        assert scalar("SELECT count(*) FROM pg_class WHERE relname LIKE 'memories_p_%'") == 0
        assert primary_key() == "PRIMARY KEY (id)"
        assert indexes() == flat_indexes
        assert triggers() == {"memories_guard", "memories_no_truncate"}
        assert "hnsw" in str(
            scalar("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_memories_embedding_hnsw'")
        )
    finally:
        engine.dispose()


def _closers(url: str) -> dict[str, tuple[set[str], set[str]]]:
    """Per fact: its stored ``closed_by``, and the rows the pre-#471 ``_closed_by`` anti-join
    finds (``supersedes`` containing its id, same story)."""
    engine = make_sync_engine(url)
    try:
        with engine.connect() as conn:
            return {
                str(r.id): ({str(c) for c in r.closed_by}, {str(c) for c in r.oracle})
                for r in conn.execute(
                    text(
                        "SELECT f.id, f.closed_by, ARRAY(SELECT s.id FROM facts s "
                        "WHERE s.story_id = f.story_id AND s.supersedes @> ARRAY[f.id]) AS oracle "
                        "FROM facts f"
                    )
                )
            }
    finally:
        engine.dispose()


async def _closing_history(url: str) -> tuple[str, str, set[str]]:
    """A story with supersedes (one write closing two versions, with remainders), an erased
    closer, and a fork of it; answers the story, the fork and the erased ids."""
    from epistrel.core.db import make_engine
    from tests.auth_support import auth_settings
    from tests.canon_support import at, commit, supersede, world
    from tests.fork_support import fork

    engine = make_engine(auth_settings(url))
    try:
        w = await world(engine)
        story = w.story_id
        bobby, ann = w.people["Bobby"], w.people["Ann"]
        kitchen, garden, school = (w.places[p] for p in ("kitchen", "garden", "school"))
        first = await commit(engine, story, at(bobby, kitchen, 0, 50))
        await commit(engine, story, at(bobby, garden, 20, 30))  # closes first, keeps remainders
        moved = await commit(engine, story, at(ann, kitchen, 0, None))
        await supersede(engine, story, moved.id, at(ann, garden, 0, None))
        closer = await commit(engine, story, at(ann, school, 10, 20))
        forked = await fork(engine, story)
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL epistrel.erasure = 'on'"))
            await conn.execute(text("DELETE FROM facts WHERE id = :i"), {"i": closer.id})
        assert first.id
        return story, forked.story_id, {closer.id}
    finally:
        await engine.dispose()


def test_facts_closing_marker_backfill(scratch_database: str) -> None:
    """0152 (#471, ADR-189): on a database with supersedes (a write closing a version with
    remainders, a supersede), an erased closer and a fork, the backfill sets every fact's
    ``closed_by`` to exactly the rows the old ``_closed_by`` anti-join finds, and equal to what
    the write path stored, but for the erased closer. The guard lets ``closed_by`` only grow.
    The downgrade drops the column and restores ``ix_facts_supersedes`` and 0060's guard."""
    import asyncio

    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "head")
    story, forked, erased = asyncio.run(_closing_history(scratch_database))
    engine = make_sync_engine(scratch_database)

    def scalar(sql: str, **params: Any) -> Any:
        with engine.connect() as conn:
            return conn.scalar(text(sql), params)

    try:
        live = _closers(scratch_database)
        stories = {
            str(s).rstrip()
            for s in (
                scalar("SELECT array_agg(DISTINCT story_id) FROM facts WHERE closed_by <> '{}'")
                or ()
            )
        }
        assert {story, forked} <= stories  # both the source and the fork have closed versions
        assert any(stored - oracle == erased for stored, oracle in live.values())

        command.downgrade(cfg, "0151")
        assert scalar("SELECT to_regclass('ix_facts_supersedes')") == "ix_facts_supersedes"
        assert (
            scalar(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'facts' AND column_name = 'closed_by'"
            )
            == 0
        )
        guard = str(
            scalar("SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_facts_append_only'")
        )
        assert "closed_by" not in guard
        assert "'committed>revised'" in guard

        command.upgrade(cfg, "0152")
        backfilled = _closers(scratch_database)
        assert set(backfilled) == set(live)
        assert sum(1 for stored, _ in backfilled.values() if stored) >= 3
        for fact_id, (stored, oracle) in backfilled.items():
            assert stored == oracle, fact_id
            assert stored == live[fact_id][0] - erased, fact_id
        assert scalar("SELECT to_regclass('ix_facts_supersedes')") is None

        closed = next(k for k, (stored, _) in backfilled.items() if stored)
        with pytest.raises(DBAPIError, match="keeps every closer"), engine.begin() as conn:
            conn.execute(text("UPDATE facts SET closed_by = '{}' WHERE id = :i"), {"i": closed})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE facts SET closed_by = closed_by || ARRAY[:n]::char(26)[] WHERE id = :i"
                ),
                {"i": closed, "n": new_ulid()},
            )
    finally:
        engine.dispose()


_STORY_DELETE_GUARDS = """
SELECT DISTINCT c.relname::text, p.proname::text, pg_get_functiondef(p.oid) LIKE '%(0126)%'
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE NOT t.tgisinternal
  AND p.prokind = 'f'
  AND (p.proname LIKE '%append_only' OR p.proname LIKE '%_guard')
  AND c.relname::text = ANY(:tables)
  AND pg_get_functiondef(p.oid) LIKE '%allow_story_delete%'
"""


def test_every_prunable_guard_keeps_the_prune_exemption(scratch_database: str) -> None:
    """0126 splices pruning's exemption into the guard of every table a pruning deletes from. A
    later migration that replaces such a guard whole (0152's facts guard, #471) must splice it
    in again, at head and after stepping back down past it, or a pruning can no longer delete
    those rows.

    Complement asserted: the facts guard is among the guards checked, at both revisions."""
    from epistrel.core.prune import PRUNABLE_TABLES

    cfg = alembic_config(scratch_database)
    engine = make_sync_engine(scratch_database)
    tables = [t.name for t in PRUNABLE_TABLES]

    def guards() -> dict[tuple[str, str], bool]:
        with engine.connect() as conn:
            rows = conn.execute(text(_STORY_DELETE_GUARDS), {"tables": tables}).all()
        return {(r[0], r[1]): r[2] for r in rows}

    try:
        for target in ("head", "0151"):
            command.upgrade(cfg, "head")
            if target != "head":
                command.downgrade(cfg, target)
            found = guards()
            assert ("facts", "epistrel_facts_append_only") in found, target
            missing = sorted(k for k, has in found.items() if not has)
            assert not missing, f"{target}: guards without 0126's prune exemption: {missing}"
    finally:
        engine.dispose()


def test_the_profile_scope_joins_the_profile_defaults(scratch_database: str) -> None:
    """0155 (#905): ``memory.episodic.profile_scope`` joins a deployment's ``profile_defaults``
    at the schema's default, ``message``, where the document does not name it, and the rest of
    the document is kept; a deployment that names it keeps its value; the downgrade removes it.

    Complement asserted: the deployment at 0154 is made not to name the setting first, so the
    upgrade is what adds it."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0154")
    engine = make_sync_engine(scratch_database)

    def defaults() -> Any:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT profile_defaults FROM deployment_state"))

    def store(document: Any) -> None:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE deployment_state SET profile_defaults = CAST(:d AS jsonb)"),
                {"d": json.dumps(document)},
            )

    try:
        before = defaults()
        before["memory"]["episodic"].pop("profile_scope", None)
        store(before)
        command.upgrade(cfg, "0155")
        after = defaults()
        assert after["memory"]["episodic"].pop("profile_scope") == "message"
        assert after == before
        command.downgrade(cfg, "0154")
        assert defaults() == before
        named = json.loads(json.dumps(before))
        named["memory"]["episodic"]["profile_scope"] = "full"
        store(named)
        command.upgrade(cfg, "0155")
        assert defaults() == named
    finally:
        engine.dispose()
