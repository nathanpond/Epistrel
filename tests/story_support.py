"""Helpers for the story-lifecycle tests: request bodies, an async REST client bound to a test
database, and whole-database row snapshots."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core.memory.story_partitions import not_a_partition
from tests.auth_support import bearer

WRITE = {**bearer(), "X-Epistrel-Principal": "p-author"}
READ = bearer()


def character(name: str, age: int = 30, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "definition": f"{name}, as the cast sheet describes them.",
        "true_age": age,
        **fields,
    }


def story_body(*names: str, **fields: Any) -> dict[str, Any]:
    """A ``POST /stories`` body: the first name is the player character."""
    cast = [character(n, 20 + i, is_player=(i == 0)) for i, n in enumerate(names or ("Ada",))]
    return {"name": "A story", "characters": cast, **fields}


@asynccontextmanager
async def rest(settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The REST app over ``engine`` (no lifespan: the Engine is bound directly)."""
    app = create_app(settings)
    app.state.epistrel.bind(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def create(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/stories", json=body, headers=WRITE)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def override(
    client: httpx.AsyncClient, story_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    """Set the story's overrides of its engine profile's dials (#113: the engine dials M2 kept in
    a story's settings), confirmed; returns the answer."""
    response = await client.patch(
        f"/stories/{story_id}/profile-overrides",
        json={"settings": settings, "ignore_warnings": True},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    assert found["applied"] is True, found
    return found


async def public_tables(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        found = await conn.scalars(
            text(
                "SELECT table_name FROM information_schema.tables "  # noqa: S608 - a fixed catalog predicate
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                f"AND table_name <> 'alembic_version' AND {not_a_partition()} "
                "ORDER BY table_name"
            )
        )
        return list(found)


PARTITIONS_PER_READ = 50
"""How many partitions ``table_rows`` reads in one transaction."""


async def table_rows(engine: AsyncEngine, table: str, schema: str = "public") -> list[str]:
    """Every row of ``schema.table`` as its text form (``row::text``). A partitioned table
    (``memories``, #784) is read partition by partition, ``PARTITIONS_PER_READ`` to a
    transaction: one statement over the parent would plan over, and lock, every story's
    partition and its indexes at once, which exhausts the shared lock table on a database that
    holds many stories (a long CI shard's)."""
    name = f'"{schema}"."{table}"'
    async with engine.connect() as conn:
        partitions = list(
            await conn.scalars(
                text(
                    "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE i.inhparent = CAST(:t AS regclass) ORDER BY c.relname"
                ),
                {"t": name},
            )
        )
        kind = await conn.scalar(
            text("SELECT relkind FROM pg_class WHERE oid = CAST(:t AS regclass)"), {"t": name}
        )
        if kind != "p":
            return list(await conn.scalars(text(f"SELECT t::text FROM {name} t")))  # noqa: S608
    rows: list[str] = []
    for start in range(0, len(partitions), PARTITIONS_PER_READ):
        chunk = partitions[start : start + PARTITIONS_PER_READ]
        async with engine.connect() as conn:
            rows.extend(
                await conn.scalars(
                    text(" UNION ALL ".join(f"SELECT t::text FROM {p} t" for p in chunk))  # noqa: S608
                )
            )
    return rows


async def dump(engine: AsyncEngine) -> dict[str, set[str]]:
    """Every row of every table as its text form (``row::text``), per table (``table_rows``)."""
    return {table: set(await table_rows(engine, table)) for table in await public_tables(engine)}


async def count(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql), params) or 0)
