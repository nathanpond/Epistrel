"""Helpers for the ownership tests (#104): principals, one factory per delivered owned type, and
the minimal request for every route under an owned entity. The registry-generated I-21 parts
(``tests/integration/test_i_21.py``) and the detailed tests (``test_ownership.py``) use them.

``KINDS`` holds an entry for every owned type whose routes are mounted. A story that delivers a
type adds that type's factory and ``CALLS`` entries, and ``test_every_delivered_type_is_covered``
fails until it does. ``CALLS`` maps each served ``(method, path)`` under an entity to a request
that succeeds for the entity's owner, once per query string in ``queries`` (a read's options:
a catalog entry's projections, the draft, a story's omniscient viewpoint). The nothing-gates
matrix sends that request as the owner, as a stranger, and as a principal holding relations
named ``admin`` and ``owner``, and expects the same status code each time. A read's ``prepare``
puts the owner's content where the read finds it, so the matrix can also expect the same body.
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
from tests.media_support import files, metadata, png
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


MATRIX_IMAGE = png(nonce="ownership-matrix")
"""The bytes every media entity of these tests holds: identical, so two fresh assets read the
same (one blob serves them all)."""


async def create_media(client: httpx.AsyncClient, principal: str, owner: str | None) -> Entity:
    body = metadata(
        alt_text="Mara at the lighthouse door",
        caption="Gull Rock, the night of the storm.",
        generation_metadata={"model": "sdxl", "prompt": "a lighthouse at dusk", "seed": 7},
        depicted_subjects=[{"kind": "persona", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"}],
        **({"owner": owner} if owner is not None else {}),
    )
    response = await client.post(
        "/media", files=files(MATRIX_IMAGE, body), headers=headers(principal)
    )
    created = await ok(response, 201)
    return Entity(created["id"])


@dataclass(frozen=True)
class Kind:
    """A delivered owned type: where its entities live and how to make one. ``retires``: its
    delete retires the entity instead of removing it (media, #105)."""

    entity_type: EntityType
    create: Create
    owner_sql: str
    """``SELECT owner_principal …`` of one entity (``:id``)."""
    retires: bool = False

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
    EntityType.MEDIA: Kind(
        EntityType.MEDIA,
        create_media,
        "SELECT owner_principal FROM media_assets WHERE id = :id",
        retires=True,
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


async def _send(
    client: httpx.AsyncClient,
    kind: Kind,
    entity: Entity,
    method: str,
    suffix: str,
    body: Any,
    status: int,
) -> dict[str, Any]:
    response = await client.request(
        method, kind.path(entity.id, suffix), json=body, headers=headers(OWNER)
    )
    return await ok(response, status)


async def _post(
    client: httpx.AsyncClient, kind: Kind, entity: Entity, suffix: str, body: Any, status: int
) -> dict[str, Any]:
    return await _send(client, kind, entity, "POST", suffix, body, status)


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


SETTINGS = {"turn_deadline": 30}
PINNED = {"items": [{"text": "Ada owes Wren."}]}
INSTRUCTIONS = {"text": "Keep it brief."}


PUBLIC_BASE_URL = "https://media.example.test"
"""The base of a signed media URL, set for ``GET /media/{id}/url`` and unset after it (#107)."""


async def _public_base(client: httpx.AsyncClient, base: str | None) -> None:
    response = await client.put(
        "/admin/settings", json={"public_base_url": base}, headers=headers(OWNER)
    )
    await ok(response, 200)


async def _signable(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _public_base(client, PUBLIC_BASE_URL)
    return {}


async def _unsignable(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _public_base(client, None)
    return {}


async def _configured(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _send(client, kind, entity, "PATCH", "/settings", SETTINGS, 200)
    return {}


async def _pinned(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _send(client, kind, entity, "PUT", "/pinned-memories", PINNED, 200)
    return {}


async def _instructed(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _send(client, kind, entity, "PUT", "/custom-instructions", INSTRUCTIONS, 200)
    return {}


@dataclass(frozen=True)
class Call:
    """A request that succeeds (``expected``) when the entity's owner sends it. ``json`` is the
    body, or a function of the entity; ``prepare`` runs first, as the owner, and ``cleanup``
    after the case (a deployment setting ``prepare`` changed). The request is sent once per query
    string in ``queries``, each its own matrix case."""

    expected: int
    json: Any = None
    queries: tuple[dict[str, str], ...] = ({},)
    prepare: Prepare = _nothing
    cleanup: Prepare = _nothing

    def body(self, entity: Entity) -> Any:
        return self.json(entity) if callable(self.json) else self.json


S = "/stories/{story_id}"
C = "/catalog/characters/{character_id}"
M = "/media/{media_id}"
RELATION = "/relations/{relation}/{principal:path}"
OMNISCIENT = {"viewpoint": "omniscient"}
ENTRY_READS: tuple[dict[str, str], ...] = (
    {},
    {"projection": "full"},
    {"version": "1"},
    {"draft": "true"},
    {"draft": "true", "projection": "full"},
)

CALLS: dict[tuple[str, str], Call] = {
    # -- stories: writes
    ("POST", f"{S}/respond"): Call(200, respond_body),
    ("POST", f"{S}/regenerate"): Call(200, {}, prepare=_played),
    ("POST", f"{S}/ingest"): Call(
        201,
        {"events": [{"client_event_id": "evt-1", "kind": "narration", "text": "Rain falls."}]},
    ),
    ("PATCH", f"{S}/settings"): Call(200, SETTINGS),
    ("PUT", f"{S}/pinned-memories"): Call(200, PINNED),
    ("PUT", f"{S}/custom-instructions"): Call(200, INSTRUCTIONS),
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
    # -- stories: reads, each finding content the owner put there (the story and its history
    # in the default viewpoint and the omniscient one too)
    ("GET", S): Call(200, queries=({}, OMNISCIENT)),
    ("GET", f"{S}/history"): Call(200, queries=({}, OMNISCIENT), prepare=_played),
    ("GET", f"{S}/settings"): Call(200, prepare=_configured),
    ("GET", f"{S}/config-changes"): Call(200, prepare=_configured),
    ("GET", f"{S}/pinned-memories"): Call(200, prepare=_pinned),
    ("GET", f"{S}/custom-instructions"): Call(200, prepare=_instructed),
    ("GET", f"{S}/variants"): Call(200, prepare=_regenerated),
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
    # -- catalog characters: reads of a `private` entry: the latest version as the card (the
    # default) and in full, version 1, and the draft in both projections
    ("GET", C): Call(200, queries=ENTRY_READS, prepare=_published),
    ("GET", f"{C}/versions"): Call(200, prepare=_published),
    ("GET", f"{C}/relations"): Call(200, prepare=_related),
    # -- media assets (#105): writes
    ("DELETE", M): Call(204),
    ("PUT", f"{M}/owner"): Call(200, {"principal": NEW_OWNER}),
    ("POST", f"{M}/relations"): Call(201, {"principal": NEW_OWNER, "relation": "editor"}),
    ("DELETE", f"{M}{RELATION}"): Call(204, prepare=_related),
    # -- media assets: reads of the metadata the upload stored, and of the bytes (#107: their
    # headers too, and a signed URL for them)
    ("GET", M): Call(200),
    ("GET", f"{M}/content"): Call(200),
    ("HEAD", f"{M}/content"): Call(200),
    ("GET", f"{M}/url"): Call(200, prepare=_signable, cleanup=_unsignable),
    ("GET", f"{M}/relations"): Call(200, prepare=_related),
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
    "PUBLIC_BASE_URL",
    "RELATED",
    "STRANGER",
    "Call",
    "Entity",
    "Kind",
    "create_character",
    "create_media",
    "create_story",
    "headers",
    "ok",
    "owner_of",
    "request_path",
    "respond_body",
    "served_entity_routes",
]
