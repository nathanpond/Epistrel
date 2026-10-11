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


async def public_tables(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        found = await conn.scalars(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version' ORDER BY table_name"
            )
        )
        return list(found)


async def dump(engine: AsyncEngine) -> dict[str, set[str]]:
    """Every row of every table as its text form (``row::text``), per table."""
    snapshot: dict[str, set[str]] = {}
    async with engine.connect() as conn:
        for table in await public_tables(engine):
            found = await conn.scalars(text(f'SELECT t::text FROM "{table}" t'))  # noqa: S608
            snapshot[table] = set(found)
    return snapshot


async def count(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql), params) or 0)
