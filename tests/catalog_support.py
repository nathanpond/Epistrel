"""Helpers for the catalog tests (#103): request bodies, REST calls on catalog characters, and
row snapshots."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine
from tests.auth_support import auth_settings, bearer
from tests.character_support import complete
from tests.conformance.conftest import migrated_database

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

VERSION_KEYS = frozenset({"version", "published_at", "published_by"})
"""The keys of each item a version listing returns (#103 AC3)."""


def character(
    name: str = "Mara Venn",
    *,
    public_description: str | None = None,
    definition: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A ``POST /catalog/characters`` body that publishes as it is (#115's complete document,
    ``tests.character_support.complete``): ``definition`` changes fields of the complete
    definition, and ``fields`` are further top-level keys (``owner``, ``metadata``)."""
    return complete(
        name, public_description=public_description, definition_changes=definition, **fields
    )


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


CARD_FIELDS = ("name", "public_description")
"""The public card's text fields, which a draft keeps under ``public_card`` (#115)."""


async def save(
    client: httpx.AsyncClient, character_id: str, revision: int, **fields: Any
) -> httpx.Response:
    """``PATCH`` the draft at ``revision``: ``name`` and ``public_description`` go under
    ``public_card``; every other field is a top-level key of the merge patch."""
    card = {key: fields.pop(key) for key in CARD_FIELDS if key in fields}
    body: dict[str, Any] = {"draft_revision": revision, **fields}
    if card:
        body["public_card"] = {**card, **body.get("public_card", {})}
    return await client.patch(f"{BASE}/{character_id}", json=body, headers=WRITE)


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


# -- listing status and listings (#110) ------------------------------------------------------------

LIST_EXTRA_KEYS = frozenset({"owner_principal", "updated_at"})
"""What a listing's card adds to the single read's card (and ``rank`` on a search)."""


async def set_listing_status(
    client: httpx.AsyncClient,
    base: str,
    entry_id: str,
    value: str,
    *,
    principal: str = PRINCIPAL,
    key: str | None = None,
) -> httpx.Response:
    """``PUT {base}/{id}/listing-status`` (``base``: ``/catalog/characters`` or
    ``/catalog/scenarios``)."""
    return await client.put(
        f"{base}/{entry_id}/listing-status",
        json={"value": value},
        headers=headers(key, principal),
    )


async def listing(
    client: httpx.AsyncClient, base: str, *, principal: str | None = None, **params: Any
) -> httpx.Response:
    """One page of ``GET {base}`` with ``params``, as ``principal`` (``None``: no principal)."""
    sent = READ if principal is None else {**READ, "X-Epistrel-Principal": principal}
    query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
    return await client.get(base, params=query, headers=sent)


async def every(
    client: httpx.AsyncClient, base: str, *, principal: str | None = None, **params: Any
) -> list[dict[str, Any]]:
    """Every item of ``GET {base}`` with ``params``, page after page (``next_cursor``)."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await listing(
            client, base, principal=principal, **params, **({"cursor": cursor} if cursor else {})
        )
        assert page.status_code == 200, page.text
        body = page.json()
        found.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return found


async def ids(
    client: httpx.AsyncClient, base: str, *, principal: str | None = None, **params: Any
) -> list[str]:
    """The ids ``GET {base}`` lists with ``params``, every page, in order."""
    return [item["id"] for item in await every(client, base, principal=principal, **params)]


@asynccontextmanager
async def scratch_catalog(pg_url: str) -> AsyncIterator[tuple[Settings, AsyncEngine]]:
    """A fresh database migrated to head (dropped afterwards), for a test that counts the whole
    catalog or changes the deployment's listing status values: the app's settings and an
    engine on it."""
    with migrated_database(pg_url, f"epistrel_catalog_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            yield settings, db
        finally:
            await db.dispose()


__all__ = [
    "BASE",
    "CARD_KEYS",
    "LIST_EXTRA_KEYS",
    "PRINCIPAL",
    "READ",
    "VERSION_KEYS",
    "WRITE",
    "character",
    "count",
    "create",
    "entry_row",
    "every",
    "headers",
    "ids",
    "listing",
    "publish",
    "published",
    "read",
    "save",
    "scratch_catalog",
    "set_listing_status",
    "version_rows",
]
