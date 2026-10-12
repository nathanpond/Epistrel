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
