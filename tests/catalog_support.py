"""Helpers for the catalog tests (#103): request bodies, REST calls on catalog characters, and
row snapshots."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.auth_support import bearer

BASE = "/catalog/characters"
PRINCIPAL = "p-catalog"
WRITE = {**bearer(), "X-Epistrel-Principal": PRINCIPAL}
READ = bearer()

CARD_KEYS = frozenset(
    {
        "id",
        "version",
        "name",
        "public_description",
        "images",
        "labels",
        "content_rating",
        "listing_status",
        "status",
    }
)
"""The public card's keys (#103 AC3): nothing else may appear in a ``card`` read."""


def character(name: str = "Mara Venn", **fields: Any) -> dict[str, Any]:
    """A ``POST /catalog/characters`` body."""
    return {
        "name": name,
        "public_description": f"{name} keeps the light on Gull Rock.",
        "definition": {"personality": f"{name} is patient, private and slow to trust."},
        **fields,
    }


def headers(key: str | None = None, principal: str = PRINCIPAL) -> dict[str, str]:
    """Write headers, with an ``Idempotency-Key`` when ``key`` is given."""
    found = {**bearer(), "X-Epistrel-Principal": principal}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


async def create(client: httpx.AsyncClient, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await client.post(
        BASE, json=body if body is not None else character(), headers=WRITE
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def save(
    client: httpx.AsyncClient, character_id: str, revision: int, **fields: Any
) -> httpx.Response:
    return await client.patch(
        f"{BASE}/{character_id}", json={"draft_revision": revision, **fields}, headers=WRITE
    )


async def publish(
    client: httpx.AsyncClient, character_id: str, revision: int, *, key: str | None = None
) -> httpx.Response:
    return await client.post(
        f"{BASE}/{character_id}/publish",
        json={"draft_revision": revision},
        headers=headers(key),
    )


async def read(client: httpx.AsyncClient, character_id: str, **params: Any) -> httpx.Response:
    query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
    return await client.get(f"{BASE}/{character_id}", params=query, headers=READ)


async def published(
    client: httpx.AsyncClient, body: dict[str, Any] | None = None
) -> tuple[str, int]:
    """A created and published entry: its id and its draft revision after the publish."""
    created = await create(client, body)
    done = await publish(client, created["id"], created["draft_revision"])
    assert done.status_code == 201, done.text
    return created["id"], int(done.json()["draft_revision"])


async def version_rows(engine: AsyncEngine, character_id: str) -> list[str]:
    """The entry's ``character_versions`` rows as JSON text, by version."""
    async with engine.connect() as conn:
        found = await conn.scalars(
            text(
                "SELECT row_to_json(v)::text FROM catalog.character_versions v "
                "WHERE catalog_id = :c ORDER BY version"
            ),
            {"c": character_id},
        )
        return list(found)


async def entry_row(engine: AsyncEngine, character_id: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM catalog.character_catalog WHERE id = :c"), {"c": character_id}
            )
        ).one_or_none()


async def count(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql), params) or 0)


__all__ = [
    "BASE",
    "CARD_KEYS",
    "PRINCIPAL",
    "READ",
    "WRITE",
    "character",
    "count",
    "create",
    "entry_row",
    "headers",
    "publish",
    "published",
    "read",
    "save",
    "version_rows",
]
