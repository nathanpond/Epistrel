from __future__ import annotations

import json
from typing import Any

from alembic import command
from sqlalchemy import text
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
