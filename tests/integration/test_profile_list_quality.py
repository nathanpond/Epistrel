"""#789 (FR-PROF-16, Console nathanpond/Epistrel-Console#339): ``GET /engine-profiles`` fills
``quality`` on every item, the same shape and value ``GET /engine-profiles/{id}`` reads for that
profile, from a number of queries that does not grow with the page.

It runs on a scratch database of its own: a certified profile would change what other tests'
profiles report.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.db import make_engine
from epistrel.core.ids import new_ulid
from epistrel.tools.generated import REPO_ROOT
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.profile_support import PROFILES, SEEDED_PROFILES, SETTINGS, profile_body
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-list-quality"}
READ = bearer()
FLOOR = "smoke.turn_completed"


@pytest.fixture
async def space(pg_url: str) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncEngine]]:
    """The REST app on a fresh database migrated to head."""
    with migrated_database(pg_url, f"epistrel_list_quality_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            async with respond_client(settings, db) as (client, _):
                yield client, db
        finally:
            await db.dispose()


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json()
    return found


def _record(fingerprint: str) -> dict[str, Any]:
    """A certification record for ``fingerprint`` with one floor passed (an import, as an
    operator would bring one from another deployment)."""
    report = "# A hand run\n\nsmoke passed.\n"
    return {
        "schema_version": 1,
        "certification_id": new_ulid(),
        "fingerprint": fingerprint,
        "floors_passed": [
            {
                "metric_id": FLOOR,
                "rule": "rate",
                "n": 10,
                "seeds": 2,
                "verdict": "pass",
                "floor": 0.95,
                "mean": 1.0,
                "sd": 0.0,
            }
        ],
        "seed_count": 2,
        "report_sha256": hashlib.sha256(report.encode()).hexdigest(),
        "run_at": "2026-11-01T09:00:00+00:00",
        "report_md": report,
    }


async def _listed(client: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    page = await ok(await client.get(PROFILES, params={"limit": 200, **params}, headers=READ))
    assert page["next_cursor"] is None
    items: list[dict[str, Any]] = page["items"]
    return items


@contextmanager
def _statements(db: AsyncEngine) -> Iterator[list[str]]:
    """Every statement the app's database engine runs inside."""
    seen: list[str] = []

    def statement(_conn: Any, _cursor: Any, sql: str, *_: Any) -> None:
        seen.append(sql)

    event.listen(db.sync_engine, "before_cursor_execute", statement)
    try:
        yield seen
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", statement)


async def test_list_quality_matches_the_single_read(
    space: tuple[httpx.AsyncClient, AsyncEngine],
) -> None:
    """Every listed profile carries ``quality``, equal to what ``GET /engine-profiles/{id}``
    reads for it (the whole item equal, in fact), with and without ``preset=true``: the four
    presets, a certified custom profile and an uncertified one. The certified profile lists as
    ``certified`` with its floor. Complements: no item, deleted ones included, omits
    ``quality``; every uncertified profile lists as ``uncertified`` with no floor met, never
    ``certified``."""
    client, _ = space
    certified = await ok(
        await client.post(PROFILES, json=profile_body("Certified", owner="p-a"), headers=WRITE),
        201,
    )
    plain_settings = {**SETTINGS, "pins": {"max_pinned": 9}}
    plain = await ok(
        await client.post(
            PROFILES,
            json=profile_body("Plain", owner="p-a", settings=plain_settings),
            headers=WRITE,
        ),
        201,
    )
    gone = await ok(
        await client.post(PROFILES, json=profile_body("Gone", owner="p-a"), headers=WRITE), 201
    )
    await ok(await client.delete(f"{PROFILES}/{gone['id']}", headers=WRITE))

    single = await ok(await client.get(f"{PROFILES}/{certified['id']}", headers=READ))
    assert single["quality"]["status"] == "uncertified"
    await ok(
        await client.post(
            "/admin/certifications/import",
            json=_record(single["quality"]["fingerprint"]),
            headers=WRITE,
        )
    )

    every = await _listed(client, include_deleted="true")
    presets = await _listed(client, preset="true")
    assert {item["id"] for item in presets} == set(SEEDED_PROFILES)
    assert {item["id"] for item in every} == {
        *SEEDED_PROFILES,
        certified["id"],
        plain["id"],
        gone["id"],
    }
    for item in [*every, *presets, *await _listed(client)]:
        assert "quality" in item, item["id"]
        assert set(item["quality"]) == {"fingerprint", "status", "floors_met"}
        read = await ok(await client.get(f"{PROFILES}/{item['id']}", headers=READ))
        assert item == read, item["id"]

    by_id = {item["id"]: item["quality"] for item in every}
    assert by_id[certified["id"]] == {
        "fingerprint": single["quality"]["fingerprint"],
        "status": "certified",
        "floors_met": [FLOOR],
    }
    uncertified = set(by_id) - {certified["id"]}
    assert uncertified >= {*SEEDED_PROFILES, plain["id"]}
    for profile_id in uncertified:
        assert by_id[profile_id]["status"] == "uncertified", profile_id
        assert by_id[profile_id]["floors_met"] == [], profile_id
        assert by_id[profile_id]["fingerprint"] != single["quality"]["fingerprint"]


async def test_list_quality_queries_do_not_grow_with_the_page(
    space: tuple[httpx.AsyncClient, AsyncEngine],
) -> None:
    """The list runs as many statements for a page of 2 as for a page of 9 (four presets and
    five custom profiles): quality comes from the deployment read once, not one read per
    profile. Complement: the larger page does hold more items, each with ``quality``."""
    client, db = space
    for n in range(5):
        await ok(
            await client.post(PROFILES, json=profile_body(f"P{n}", owner="p-a"), headers=WRITE),
            201,
        )
    counts: dict[int, int] = {}
    for limit in (2, 9):
        with _statements(db) as seen:
            page = await ok(await client.get(PROFILES, params={"limit": limit}, headers=READ))
        assert len(page["items"]) == limit
        assert all("quality" in item for item in page["items"])
        counts[limit] = len(seen)
    assert counts[2] == counts[9], counts


def test_list_item_schema_requires_quality() -> None:
    """``docs/openapi.json``: the list's items are ``ListedProfile``, which requires
    ``quality`` (``{fingerprint, status, floors_met}``). Complement: the single read's
    ``EngineProfile`` keeps it optional, as before (a write's answer carries none)."""
    schemas = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text())["components"]["schemas"]
    items = schemas["ProfileListBody"]["properties"]["items"]["items"]
    assert items == {"$ref": "#/components/schemas/ListedProfile"}
    listed = schemas["ListedProfile"]
    assert "quality" in listed["required"]
    assert listed["properties"]["quality"] == {
        "$ref": "#/components/schemas/FloorsMet",
        "description": listed["properties"]["quality"]["description"],
    }
    assert set(schemas["FloorsMet"]["required"]) == {"fingerprint", "status", "floors_met"}
    assert "quality" not in schemas["EngineProfile"]["required"]
