from __future__ import annotations

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import __version__
from epistrel.core.db import make_sync_engine
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
