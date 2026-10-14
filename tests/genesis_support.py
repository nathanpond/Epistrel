"""Helpers for creating stories from scenarios (#119): published catalog characters and
scenarios, the ``POST /stories`` scenario body, and row counts of everything a creation writes.

The milestone parts (I-17, I-21, I-22, I-27, U-26, C-19, C-20, S-COMPANION, S-AGE-ASSIGNMENT)
take their bodies from here, so no part names a later milestone's term (an opening's present
roles are M5's exposures; a role's starting-state values are M6's)."""

from __future__ import annotations

import copy
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.seeds.ratings import EVERYONE_ID
from tests import scenario_support as scenarios
from tests.auth_support import bearer
from tests.catalog_support import character as catalog_character

AUTHOR = "p-author"
WRITE = {**bearer(), "X-Epistrel-Principal": AUTHOR}
READ = bearer()
OPENING = scenarios.OPENING

COUNTED = ("stories", "characters", "character_snapshots", "operations", "events")
"""What a refused creation must leave as it was (AC2: no ``stories``, ``characters``,
``operations`` or ``events`` row; the snapshots too)."""


def headers(key: str | None = None, principal: str = AUTHOR) -> dict[str, str]:
    found = {**bearer(), "X-Epistrel-Principal": principal}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


async def counts(engine: AsyncEngine) -> dict[str, int]:
    """Rows of every table a creation writes."""
    found: dict[str, int] = {}
    async with engine.connect() as conn:
        for table in COUNTED:
            found[table] = int(
                await conn.scalar(text(f'SELECT count(*) FROM "{table}"'))  # noqa: S608
                or 0
            )
    return found


async def catalog_character_id(
    client: httpx.AsyncClient,
    name: str,
    *,
    age: int = 41,
    rating: str = EVERYONE_ID,
    labels: list[str] | None = None,
    versions: int = 1,
    owner: str | None = None,
    definition: dict[str, Any] | None = None,
    **fields: Any,
) -> str:
    """A catalog character (complete, #115) published ``versions`` times: its id."""
    body = catalog_character(
        name,
        definition={"true_age": age, **(definition or {})},
        metadata={"content_rating_id": rating, "labels": labels or []},
        **fields,
    )
    if owner is not None:
        body["owner"] = owner
    created = await client.post("/catalog/characters", json=body, headers=scenarios.WRITE)
    assert created.status_code == 201, created.text
    character_id: str = created.json()["id"]
    for revision in range(1, versions + 1):
        done = await client.post(
            f"/catalog/characters/{character_id}/publish",
            json={"draft_revision": revision},
            headers=scenarios.WRITE,
        )
        assert done.status_code == 201, done.text
    return character_id


def role(role_id: str, **fields: Any) -> dict[str, Any]:
    return scenarios.role(role_id, **fields)


def scenario_body(
    *roles: dict[str, Any],
    rating: str = EVERYONE_ID,
    name: str = "Sunday Dinner",
    **groups: Any,
) -> dict[str, Any]:
    """A publishable scenario: ``roles`` (default: an open ``mother`` and the ``player``), the
    rating, a world and an opening narration; ``groups`` replace whole groups."""
    body = scenarios.scenario(name)
    body["metadata"] = {"labels": ["family"], "content_rating_id": rating}
    if roles:
        body["cast"] = {"roles": list(roles)}
    body.update(copy.deepcopy(groups))
    return body


async def published_scenario(
    client: httpx.AsyncClient, body: dict[str, Any], *, owner: str | None = None
) -> tuple[str, int]:
    """A created and published scenario: its id and version."""
    sent = dict(body)
    if owner is not None:
        sent["owner"] = owner
    created = await client.post(scenarios.BASE, json=sent, headers=scenarios.WRITE)
    assert created.status_code == 201, created.text
    found = created.json()
    done = await client.post(
        f"{scenarios.BASE}/{found['id']}/publish",
        json={"draft_revision": found["draft_revision"]},
        headers=scenarios.WRITE,
    )
    assert done.status_code == 201, done.text
    return found["id"], int(done.json()["version"])


def player(
    principal: str = "p-player", *, persona_id: str | None = None, **ad_hoc: Any
) -> dict[str, Any]:
    """A player assignment: a saved persona, or an ad-hoc one (default: Sam, 23)."""
    if persona_id is not None:
        return {"principal": principal, "persona": {"id": persona_id}}
    fields = {"name": "Sam", "age": 23, "description": "The eldest, back for a weekend."}
    fields.update(ad_hoc)
    return {"principal": principal, "persona": {"ad_hoc": fields}}


def from_scenario(
    scenario_id: str,
    *,
    version: int | None = None,
    slots: dict[str, Any] | None = None,
    players: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A ``POST /stories`` body creating a story from ``scenario_id`` (default players: an
    ad-hoc persona in the ``player`` role)."""
    ref: dict[str, Any] = {"id": scenario_id}
    if version is not None:
        ref["version"] = version
    return {
        "scenario": ref,
        "slots": slots or {},
        "players": players if players is not None else {"player": player()},
        **fields,
    }


def catalog_slot(catalog_id: str, version: int | None = None) -> dict[str, Any]:
    found: dict[str, Any] = {"catalog_id": catalog_id}
    if version is not None:
        found["version"] = version
    return found


def ad_hoc_slot(name: str, age: int, definition: str | None = None) -> dict[str, Any]:
    return {
        "ad_hoc": {
            "name": name,
            "definition": definition or f"{name}, defined for this story only.",
            "true_age": age,
        }
    }


async def create_from(
    client: httpx.AsyncClient, body: dict[str, Any], *, status: int = 201, key: str | None = None
) -> dict[str, Any]:
    response = await client.post("/stories", json=body, headers=headers(key))
    assert response.status_code == status, response.text
    found: dict[str, Any] = response.json()
    return found


async def refused(client: httpx.AsyncClient, body: dict[str, Any]) -> tuple[int, str, Any]:
    """``(status, code, details)`` of a creation that must be refused."""
    response = await client.post("/stories", json=body, headers=WRITE)
    assert response.status_code != 201, response.text
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


def report_codes(details: dict[str, Any]) -> list[str]:
    report = details["report"]
    return [e["code"] for e in report["errors"]]


__all__ = [
    "AUTHOR",
    "COUNTED",
    "OPENING",
    "READ",
    "WRITE",
    "ad_hoc_slot",
    "catalog_character_id",
    "catalog_slot",
    "counts",
    "create_from",
    "from_scenario",
    "headers",
    "player",
    "published_scenario",
    "refused",
    "report_codes",
    "role",
    "scenario_body",
]
