"""Helpers for the ownership tests (#104): principals, one factory per delivered owned type, and
the minimal request for every route under an owned entity. The registry-generated I-21 parts
(``tests/integration/test_i_21.py``) and the detailed tests (``test_ownership.py``) use them.

``KINDS`` holds an entry for every owned type whose routes are mounted. A story that delivers a
type adds that type's factory and ``CALLS`` entries, and ``test_every_delivered_type_is_covered``
fails until it does. ``CALLS`` maps each served ``(method, path)`` under an entity to a request
that succeeds for the entity's owner. The nothing-gates matrix sends that request as the owner,
as a stranger, and as a principal holding relations named ``admin`` and ``owner``, and expects
the same status code each time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx2 as httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ownership import REGISTRY
from epistrel.core.tables.ownership import EntityType
from tests.auth_support import bearer
from tests.catalog_support import character as catalog_character
from tests.story_support import story_body

OWNER = "p-owner-a"
STRANGER = "p-stranger-b"
RELATED = "p-related-c"
NEW_OWNER = "p-new-owner"
PRIVILEGED_NAMES = ("admin", "owner")
"""Relation names that sound like grants. ``RELATED`` holds both, and they must grant nothing."""


def headers(principal: str, key: str | None = None) -> dict[str, str]:
    found = {**bearer(), "X-Epistrel-Principal": principal}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    body: dict[str, Any] = response.json() if response.content else {}
    return body


# -- one factory per delivered type ----------------------------------------------------------------


@dataclass
class Entity:
    """A created entity: its id, plus ids a request may need (the story's cast)."""

    id: str
    ids: dict[str, str] = field(default_factory=dict)


Create = Callable[[httpx.AsyncClient, str, str | None], Awaitable[Entity]]


async def create_story(client: httpx.AsyncClient, principal: str, owner: str | None) -> Entity:
    body = story_body("Wren", "Ada", **({"owner": owner} if owner is not None else {}))
    created = await ok(await client.post("/stories", json=body, headers=headers(principal)), 201)
    cast = {c["name"]: c["id"] for c in created["characters"]}
    return Entity(created["story_id"], {"wren": cast["Wren"], "ada": cast["Ada"]})


async def create_character(client: httpx.AsyncClient, principal: str, owner: str | None) -> Entity:
    body = catalog_character("Mara Venn", **({"owner": owner} if owner is not None else {}))
    response = await client.post("/catalog/characters", json=body, headers=headers(principal))
    created = await ok(response, 201)
    return Entity(created["id"])


@dataclass(frozen=True)
class Kind:
    """A delivered owned type: where its entities live and how to make one."""

    entity_type: EntityType
    create: Create
    owner_sql: str
    """``SELECT owner_principal …`` of one entity (``:id``)."""

    @property
    def base(self) -> str:
        return f"/{REGISTRY[self.entity_type].segment}"

    @property
    def item(self) -> str:
        """The served path of one entity: ``/stories/{story_id}``."""
        return f"{self.base}/{{{REGISTRY[self.entity_type].id_param}}}"

    def path(self, entity_id: str, suffix: str = "") -> str:
        return f"{self.base}/{entity_id}{suffix}"


KINDS: dict[EntityType, Kind] = {
    EntityType.STORY: Kind(
        EntityType.STORY, create_story, "SELECT owner_principal FROM stories WHERE id = :id"
    ),
    EntityType.CHARACTER: Kind(
        EntityType.CHARACTER,
        create_character,
        "SELECT owner_principal FROM catalog.character_catalog WHERE id = :id",
    ),
}


async def owner_of(engine: AsyncEngine, kind: Kind, entity_id: str) -> str | None:
    async with engine.connect() as conn:
        found = await conn.scalar(text(kind.owner_sql), {"id": entity_id})
    return None if found is None else str(found)


# -- one minimal request per route -----------------------------------------------------------------

Prepare = Callable[[httpx.AsyncClient, Kind, Entity], Awaitable[dict[str, Any]]]
"""Setup run as the owner before the request. Returns extra values for the request's path."""


async def _nothing(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    return {}


def respond_body(entity: Entity) -> dict[str, Any]:
    return {
        "actor_character_id": entity.ids["wren"],
        "player_input": {"say": "Is there a room for the night?"},
        "prompt_params": {"length": "short"},
    }


async def _post(
    client: httpx.AsyncClient, kind: Kind, entity: Entity, suffix: str, body: Any, status: int
) -> dict[str, Any]:
    response = await client.post(kind.path(entity.id, suffix), json=body, headers=headers(OWNER))
    return await ok(response, status)


async def _played(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _post(client, kind, entity, "/respond", respond_body(entity), 200)
    return {}


async def _truncated(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _played(client, kind, entity)
    await _post(client, kind, entity, "/truncate", {"to_turn": 0}, 200)
    return {}


async def _regenerated(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _played(client, kind, entity)
    await _post(client, kind, entity, "/regenerate", {}, 200)
    return {}


async def _archived(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _post(client, kind, entity, "/archive", None, 200)
    return {}


async def _published(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _post(client, kind, entity, "/publish", {"draft_revision": 1}, 201)
    return {}


async def _related(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    body = {"principal": NEW_OWNER, "relation": "editor"}
    await _post(client, kind, entity, "/relations", body, 201)
    return {"relation": "editor", "principal": quote(NEW_OWNER, safe="")}


@dataclass(frozen=True)
class Call:
    """A request that succeeds (``expected``) when the entity's owner sends it. ``json`` is the
    body, or a function of the entity; ``prepare`` runs first, as the owner."""

    expected: int
    json: Any = None
    params: dict[str, str] = field(default_factory=dict)
    prepare: Prepare = _nothing

    def body(self, entity: Entity) -> Any:
        return self.json(entity) if callable(self.json) else self.json


S = "/stories/{story_id}"
C = "/catalog/characters/{character_id}"
RELATION = "/relations/{relation}/{principal:path}"

CALLS: dict[tuple[str, str], Call] = {
    # -- stories: writes
    ("POST", f"{S}/respond"): Call(200, respond_body),
    ("POST", f"{S}/regenerate"): Call(200, {}, prepare=_played),
    ("POST", f"{S}/ingest"): Call(
        201,
        {"events": [{"client_event_id": "evt-1", "kind": "narration", "text": "Rain falls."}]},
    ),
    ("PATCH", f"{S}/settings"): Call(200, {"turn_deadline": 30}),
    ("PUT", f"{S}/pinned-memories"): Call(200, {"items": [{"text": "Ada owes Wren."}]}),
    ("PUT", f"{S}/custom-instructions"): Call(200, {"text": "Keep it brief."}),
    ("POST", f"{S}/truncate"): Call(200, {"to_turn": 0}, prepare=_played),
    ("POST", f"{S}/untruncate"): Call(200, {}, prepare=_truncated),
    ("POST", f"{S}/swap"): Call(
        200, {"to_variant_chain": {"turn": 1, "variant": 1}}, prepare=_regenerated
    ),
    ("POST", f"{S}/fork"): Call(201, {"at_turn": 0}),
    ("POST", f"{S}/archive"): Call(200),
    ("POST", f"{S}/unarchive"): Call(200, prepare=_archived),
    ("DELETE", S): Call(204),
    ("PUT", f"{S}/owner"): Call(200, {"principal": NEW_OWNER}),
    ("POST", f"{S}/relations"): Call(201, {"principal": NEW_OWNER, "relation": "editor"}),
    ("DELETE", f"{S}{RELATION}"): Call(204, prepare=_related),
    # -- stories: reads
    ("GET", S): Call(200),
    ("GET", f"{S}/history"): Call(200, prepare=_played),
    ("GET", f"{S}/settings"): Call(200),
    ("GET", f"{S}/config-changes"): Call(200),
    ("GET", f"{S}/pinned-memories"): Call(200),
    ("GET", f"{S}/custom-instructions"): Call(200),
    ("GET", f"{S}/variants"): Call(200),
    ("GET", f"{S}/relations"): Call(200, prepare=_related),
    # -- catalog characters: writes
    ("PATCH", C): Call(200, {"draft_revision": 1, "public_description": "Keeps the logbook."}),
    ("POST", f"{C}/publish"): Call(201, {"draft_revision": 1}),
    ("POST", f"{C}/archive"): Call(200),
    ("POST", f"{C}/unarchive"): Call(200, prepare=_archived),
    ("DELETE", C): Call(204),
    ("PUT", f"{C}/owner"): Call(200, {"principal": NEW_OWNER}),
    ("POST", f"{C}/relations"): Call(201, {"principal": NEW_OWNER, "relation": "editor"}),
    ("DELETE", f"{C}{RELATION}"): Call(204, prepare=_related),
    # -- catalog characters: reads (a `private` entry, owned by someone else, in both projections)
    ("GET", C): Call(200, params={"projection": "full"}, prepare=_published),
    ("GET", f"{C}/versions"): Call(200, prepare=_published),
    ("GET", f"{C}/relations"): Call(200, prepare=_related),
}
"""Every route under an owned entity's path, ``/admin`` excluded."""


def served_entity_routes(app: FastAPI, kind: Kind) -> set[tuple[str, str]]:
    """Every ``(method, path)`` the app serves under one ``kind`` entity, ``/admin`` excluded."""
    found: set[tuple[str, str]] = set()
    for context in iter_route_contexts(app.routes):
        path = context.path or ""
        if not isinstance(context.original_route, APIRoute):
            continue
        if path == kind.item or path.startswith(f"{kind.item}/"):
            found.update((method, path) for method in context.methods or ())
    return found


def request_path(kind: Kind, route: str, entity: Entity, extra: dict[str, Any]) -> str:
    """``route`` (as served) for ``entity``, with ``extra`` path values."""
    path = route.replace(kind.item, kind.path(entity.id), 1).replace(
        "{principal:path}", "{principal}"
    )
    return path.format(**extra) if extra else path


__all__ = [
    "CALLS",
    "KINDS",
    "NEW_OWNER",
    "OWNER",
    "PRIVILEGED_NAMES",
    "RELATED",
    "STRANGER",
    "Call",
    "Entity",
    "Kind",
    "create_character",
    "create_story",
    "headers",
    "ok",
    "owner_of",
    "request_path",
    "respond_body",
    "served_entity_routes",
]
