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
from typing import Any, cast
from urllib.parse import quote

import httpx2 as httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.ownership import REGISTRY
from epistrel.core.profiles.certification import install_harness
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.tables.ownership import EntityType
from tests.auth_support import bearer
from tests.catalog_support import character as catalog_character
from tests.media_support import files, metadata, png
from tests.profile_support import EDITED, PROFILES, profile_body
from tests.scenario_support import scenario as catalog_scenario
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
    body = catalog_character("Mara Venn")
    if owner is not None:
        body["owner"] = owner
    response = await client.post("/catalog/characters", json=body, headers=headers(principal))
    created = await ok(response, 201)
    return Entity(created["id"])


async def create_scenario(client: httpx.AsyncClient, principal: str, owner: str | None) -> Entity:
    body = catalog_scenario("Sunday Dinner", **({"owner": owner} if owner is not None else {}))
    response = await client.post("/catalog/scenarios", json=body, headers=headers(principal))
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


async def create_profile(client: httpx.AsyncClient, principal: str, owner: str | None) -> Entity:
    body = profile_body(**({"owner": owner} if owner is not None else {}))
    response = await client.post(PROFILES, json=body, headers=headers(principal))
    created = await ok(response, 201)
    return Entity(created["id"])


def persona_body(**fields: Any) -> dict[str, Any]:
    """A ``POST /personas`` body (#114). Its name is unique (a persona's name is unique per
    owner), and only by an id, which the matrix blanks, so two fresh personas read alike."""
    return {
        "name": f"Sam {new_ulid()}",
        "gender": "man",
        "age": 23,
        "description": "Short-tempered, but loyal to a fault.",
        **fields,
    }


async def create_persona(client: httpx.AsyncClient, principal: str, owner: str | None) -> Entity:
    body = persona_body(**({"owner": owner} if owner is not None else {}))
    created = await ok(await client.post("/personas", json=body, headers=headers(principal)), 201)
    return Entity(created["id"])


@dataclass(frozen=True)
class Kind:
    """A delivered owned type: where its entities live and how to make one. ``retires``: its
    delete retires the entity instead of removing it (media, #105; engine profiles, #106, until
    the sweep purges them). ``deleted_status``: what its delete answers. ``listed``: the query
    its list needs to show a fresh entity (a catalog list shows published entries unless asked
    for drafts, #110)."""

    entity_type: EntityType
    create: Create
    owner_sql: str
    """``SELECT owner_principal …`` of one entity (``:id``)."""
    retires: bool = False
    deleted_status: int = 204
    listed: tuple[tuple[str, str], ...] = ()

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
        listed=(("status", "draft"),),
    ),
    EntityType.SCENARIO: Kind(
        EntityType.SCENARIO,
        create_scenario,
        "SELECT owner_principal FROM catalog.scenario_catalog WHERE id = :id",
        listed=(("status", "draft"),),
    ),
    EntityType.PERSONA: Kind(
        EntityType.PERSONA,
        create_persona,
        "SELECT owner_principal FROM personas WHERE id = :id",
    ),
    EntityType.MEDIA: Kind(
        EntityType.MEDIA,
        create_media,
        "SELECT owner_principal FROM media_assets WHERE id = :id",
        retires=True,
    ),
    EntityType.ENGINE_PROFILE: Kind(
        EntityType.ENGINE_PROFILE,
        create_profile,
        "SELECT owner_principal FROM engine_profiles WHERE id = :id",
        retires=True,
        deleted_status=200,
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


UNKNOWN_CANDIDATE = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
"""A candidate no entry has (#260): its reads are 404 and its discard 204 for anyone."""


async def _no_candidate(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    return {"candidate_id": UNKNOWN_CANDIDATE}


CERTIFICATION_RUN = {"suites": ["smoke"], "seeds": 2, "sample": 10}


async def _harness(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The eval harness a certification run's estimate needs (#233; it plays nothing)."""
    from epistrel.evals.certify import EvalHarness

    install_harness(EvalHarness(cast(Any, None)))
    return {}


async def _no_harness(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    install_harness(None)
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


async def _reply(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A played turn: its reply's ``message_id`` for the message-rating routes (#112)."""
    played = await _post(client, kind, entity, "/respond", respond_body(entity), 200)
    return {"message_id": played["message"]["message_id"]}


RATING = {"rating": 2, "tags": ["too_long"], "text": "It rambles about the weather."}


async def _rated(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A played turn whose reply the owner rated."""
    extra = await _reply(client, kind, entity)
    path = f"/messages/{extra['message_id']}/feedback"
    await _send(client, kind, entity, "PUT", path, RATING, 201)
    return extra


async def _rated_by_all(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A played turn whose reply each of the matrix's three requesters rated the same way: a
    principal's rating is keyed by that principal (FR-AUTH-4), so each reads its own."""
    extra = await _reply(client, kind, entity)
    path = kind.path(entity.id, f"/messages/{extra['message_id']}/feedback")
    for principal in (OWNER, STRANGER, RELATED):
        await ok(await client.put(path, json=RATING, headers=headers(principal)), 201)
    return extra


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


SETTINGS = {"regenerate_retry_budget": 3}
OVERRIDES = {"settings": {"pins": {"max_pinned": 10}}}
"""A story's override of a dial no capability hangs on (#113): applied at once."""
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


PROVIDER = "ownership-ledger"
"""An in-process provider the binding routes name (#204): registered once, deployment-wide."""
BINDING = {"external_ids": {"player": "p-1"}}


async def _provider(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The provider registered (an earlier case may have registered it: 409 then)."""
    body = {
        "name": PROVIDER,
        "trust_tier": "authoritative",
        "transport": "in_process",
        "manifest": {"values": {"gold": {"type": "number", "writable": False}}},
    }
    response = await client.post("/admin/external-providers", json=body, headers=headers(OWNER))
    assert response.status_code in (201, 409), response.text
    return {"name": PROVIDER}


async def _bound(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The story bound to the provider by its owner (#204)."""
    found = await _provider(client, kind, entity)
    await _send(client, kind, entity, "PUT", f"/providers/{PROVIDER}/binding", BINDING, 200)
    return found


async def _story_character(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The story's non-player character, Ada, for a promotion (#114)."""
    return {"character_id": entity.ids["ada"]}


PROMPT_ONLY = {"clock": {"level": "prompt_only"}, "states": {"level": "prompt_only"}}
"""#203: the Lite story's overrides for the clock, scheduled-event and State routes: the world
clock and States in their prompt-only Lite forms (FR-LITE-5)."""
GOLD = {"name": "gold", "type": "number", "initial_value": 10}


async def _prompt_only(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The story runs the clock and States at ``prompt_only`` (#203; a Lite story's forms)."""
    await _send(
        client,
        kind,
        entity,
        "PATCH",
        "/profile-overrides",
        {"settings": PROMPT_ONLY, "ignore_warnings": True},
        200,
    )
    return {}


async def _state_name(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A declared State's name for the State routes (#186), on the story running States at
    ``prompt_only`` (#203)."""
    await _prompt_only(client, kind, entity)
    await _post(client, kind, entity, "/states", {"definition": GOLD}, 201)
    return {"name": "gold"}


async def _state_proposal(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A ``proposed`` State proposal for the proposal routes (#192), on the story running States
    at ``prompt_only`` (#203). No route files one (the analysis is a job), so it is written as
    the analysis writes it."""
    await _prompt_only(client, kind, entity)
    proposal = new_ulid()
    db = client._transport.app.state.epistrel.db  # type: ignore[attr-defined]
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO state_proposals (id, story_id, name, suggested_type, "
                "suggested_scope, suggested_unit, created_by_operation_id) VALUES "
                "(:i, :s, 'arrows', 'number', 'story', 'arrows', :o)"
            ),
            {"i": proposal, "s": entity.id, "o": new_ulid()},
        )
    return {"proposal_id": proposal}


async def _outbox_effect(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A listed effect for the reconciliation actions (#218): the story, on ``Structured (M4)``
    with States on (#203), whose gold a write-capable in-process provider owns, with Wren's
    purchase of Ada's sword confirmed after the player rewound past it (listed
    ``confirmed_discarded_origin``)."""
    from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID
    from tests import late_settlement_support as ls

    epistrel = client._transport.app.state.epistrel  # type: ignore[attr-defined]
    db = epistrel.db
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stories SET engine_profile_id = :p, engine_profile_version_pin = 1 "
                "WHERE id = :s"
            ),
            {"s": entity.id, "p": STRUCTURED_M4_PROFILE_ID},
        )
    bank = await ls.bank(db, epistrel, entity.id)
    bank.script_writes("gold", "confirmed")
    await ls.swords(db, entity.id, entity.ids["ada"])
    listed = await ls.discarded(
        db, epistrel, entity.id, bank.name, entity.ids["wren"], entity.ids["ada"]
    )
    return {"outbox_id": listed}


async def _consumer_listing(
    client: httpx.AsyncClient, kind: Kind, entity: Entity
) -> dict[str, Any]:
    """#661: a listed consumer-applied effect for the resolve action: the story, on
    ``Structured (M4)`` with States on, with a turn's ``requested_effect`` listed
    ``settlement_conflict`` (no public call lists one)."""
    from epistrel.core import outbox as core_outbox
    from epistrel.core.enums import OperationKind, Provenance
    from epistrel.core.events.operation import Operation
    from epistrel.core.events.types import REQUESTED_EFFECT, RequestedEffect
    from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID
    from tests import state_support as ss

    db = client._transport.app.state.epistrel.db  # type: ignore[attr-defined]
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stories SET engine_profile_id = :p, engine_profile_version_pin = 1 "
                "WHERE id = :s"
            ),
            {"s": entity.id, "p": STRUCTURED_M4_PROFILE_ID},
        )
    await ss.set_level(db, entity.id, "on")
    request_id = new_ulid()
    async with Operation(
        db, story_id=entity.id, kind=OperationKind.TURN, principal="p-owner"
    ) as op:
        op.append("player_input", {"text": "pays"}, provenance=Provenance.PLAYER)
        op.append(
            REQUESTED_EFFECT,
            RequestedEffect(
                effect_id=request_id,
                state="gold",
                scope_key=entity.ids["wren"],
                delta=-5,
                reason="a sword",
            ),
            provenance=Provenance.ENGINE,
        )
        op.append("message_rendered", {"text": "paid"})
        await op.commit()
    async with db.begin() as conn:
        await core_outbox.mark(conn, entity.id, None, "settlement_conflict", request_id=request_id)
    return {"listing": request_id}


def _refund(entity: Entity) -> dict[str, Any]:
    """#218: a refund of the listed purchase's payment, to Wren's external gold."""
    refund = {"state": "gold", "delta": 5, "reason": "refund", "scope_key": entity.ids["wren"]}
    return {"effect": refund}


async def _scheduled_event(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A scheduled event for the event writes (#196), on the story running the clock at
    ``prompt_only`` (#203)."""
    await _prompt_only(client, kind, entity)
    created = await _post(
        client,
        kind,
        entity,
        "/scheduled-events",
        {"proposition": "the delivery", "planned": 60},
        201,
    )
    return {"event_id": created["event"]["event_id"]}


STORY_IMAGE = {
    "position": {"slot_after_turn": 0},
    "audience_viewpoint": "omniscient",
    "depicted_subjects": [],
}
"""#261: an omniscient image in the slot after genesis (the story has no completed turn)."""


async def _story_image(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A story image the owner uploaded (#261), read for its audience, ``omniscient``."""
    response = await client.post(
        kind.path(entity.id, "/images"),
        files=files(MATRIX_IMAGE, STORY_IMAGE),
        headers=headers(OWNER),
    )
    created = await ok(response, 201)
    return {"image_id": created["image_id"], "query.viewpoint": "omniscient"}


async def _omniscient(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The omniscient viewpoint, as a query parameter (#196's list)."""
    return {"query.viewpoint": "omniscient"}


async def _believing(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The query naming the story's non-player character, Ada, whose belief projection the
    read answers (#159): a ``query.``-prefixed value is a query parameter, not a path value."""
    return {"query.character": entity.ids["ada"]}


async def _edited_persona(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _send(client, kind, entity, "PATCH", "", {"age": 24, "gender": None}, 200)
    return {}


async def _edited(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    await _send(client, kind, entity, "PUT", "", {"name": "Brisk Lite", "settings": EDITED}, 200)
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


RATINGS = "/admin/content-ratings"
REPLACED = (
    "name",
    "description",
    "display_order",
    "prompt_guidance",
    "image_guidance",
    "policy_ids",
    "inherits_from",
)


async def _fresh_rating(client: httpx.AsyncClient, *, guarded: bool) -> str:
    """A new uniquely named rating inheriting from nothing (#118): with no policy, so moving any
    story to it relaxes whatever the shared database's ratings list; or ``guarded``, listing one
    new rating policy no other rating lists, so moving a story to it tightens."""
    made = await client.post(RATINGS, json={"name": f"I-21 {new_ulid()}"}, headers=headers(OWNER))
    rating = await ok(made, 201)
    if not guarded:
        return str(rating["id"])
    policy = {
        "scope": "rating",
        "scope_id": rating["id"],
        "name": f"i21-guard-{new_ulid().lower()}",
        "rule": {"type": "wordlist", "words": [f"zq{new_ulid().lower()}"]},
        "message": "Not at this rating.",
    }
    guard = await ok(await client.post("/admin/policies", json=policy, headers=headers(OWNER)), 201)
    listed = {**{k: rating[k] for k in REPLACED}, "policy_ids": [guard["id"]]}
    path = f"{RATINGS}/{rating['id']}"
    await ok(await client.put(path, json=listed, headers=headers(OWNER)), 200)
    return str(rating["id"])


async def _relaxable(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A rating with no policies for the story to relax to (#118): applied at once (200)."""
    entity.ids["rating"] = await _fresh_rating(client, guarded=False)
    return {}


def rating_body(entity: Entity) -> dict[str, Any]:
    return {"content_rating_id": entity.ids["rating"]}


async def _checking(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The owner tightened the story's rating (#118): its check started (202) and, with no job
    worker in the matrix's app, stays ``checking``."""
    target = {"content_rating_id": await _fresh_rating(client, guarded=True)}
    started = await _send(client, kind, entity, "PUT", "/content-rating", target, 202)
    return {"job_id": started["job_id"]}


async def _propose(client: httpx.AsyncClient, story_id: str, relation: str) -> str:
    """An auto predicate of the story with a pending ``functional`` proposal (#131), written
    as the structured pipeline does (no public call files one) on the app's own database;
    idempotent for one relation (the same predicate and pending proposal)."""
    from epistrel.core.propositions import registry
    from epistrel.core.propositions.model import Cardinality

    db = client._transport.app.state.epistrel.db  # type: ignore[attr-defined]
    async with db.begin() as conn:
        found = await registry.auto_register(conn, story_id, relation)
        await registry.file_proposal(
            conn, story_id, found.id, registry.ProposedChanges(cardinality=Cardinality.FUNCTIONAL)
        )
    return str(found.id)


async def _proposed(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The story's registry with one pending proposal, the same for every requester."""
    await _propose(client, entity.id, "sails with")
    return {}


async def _fresh_proposal(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A new auto predicate with a pending proposal, for one approval or rejection."""
    return {"predicate_id": await _propose(client, entity.id, f"rides with {new_ulid()}")}


async def _committing(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """The story on the seeded ``Structured (M4)`` profile, which runs commitment (#146's
    ledger inspection answers 409 ``module_off`` otherwise), confirmed past the impact report's
    warnings (capabilities gained)."""
    from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID

    await _send(
        client,
        kind,
        entity,
        "PUT",
        "/engine-profile",
        {"profile_id": STRUCTURED_M4_PROFILE_ID, "ignore_warnings": True},
        200,
    )
    return {}


async def _profiled(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """An entity of the story (#137's profile read), the same one for every requester: created
    once as #139's resolver would, on the app's own database."""
    from sqlalchemy import select

    from epistrel.core.propositions import store
    from epistrel.core.tables import entities

    db = client._transport.app.state.epistrel.db  # type: ignore[attr-defined]
    async with db.begin() as conn:
        found = await conn.scalar(
            select(entities.c.id).where(
                entities.c.story_id == entity.id, entities.c.canonical_name == "Profiled"
            )
        )
        if found is None:
            found = await store.add_entity(conn, entity.id, "person", "Profiled")
    return {"entity_id": str(found)}


async def _unmodeled(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A ``person`` entity of the story that no character is yet (#163's modeling directive),
    created as #139's resolver would, on the app's own database."""
    from epistrel.core.propositions import store

    db = client._transport.app.state.epistrel.db  # type: ignore[attr-defined]
    async with db.begin() as conn:
        found = await store.add_entity(conn, entity.id, "person", "The neighbor")
    return {"entity_id": str(found)}


async def _a_memory(entity: Entity, client: httpx.AsyncClient, *, pinned: bool) -> str:
    """A fresh memory of the story (#147's pin calls), written straight into ``memories`` on the
    app's own database against the story's first operation."""
    from epistrel.core.enums import MemoryKind, MemoryTrustTier, RecordProvenance
    from epistrel.core.membership import insert_derived
    from epistrel.core.tables.memories import memories

    db = client._transport.app.state.epistrel.db  # type: ignore[attr-defined]
    memory_id = new_ulid()
    async with db.begin() as conn:
        operation_id = await conn.scalar(
            text(
                "SELECT operation_id FROM operations WHERE story_id = :s "
                "ORDER BY started_at LIMIT 1"
            ),
            {"s": entity.id},
        )
        await insert_derived(
            conn,
            memories,
            [
                {
                    "id": memory_id,
                    "story_id": entity.id,
                    "kind": MemoryKind.SEMANTIC.value,
                    "text": "Wren keeps the inn's ledger.",
                    "provenance": RecordProvenance.AUTHOR.value,
                    "trust_tier": MemoryTrustTier.AUTHOR.value,
                    "operation_id": str(operation_id),
                    "pinned": pinned,
                    "pinned_by": OWNER if pinned else None,
                }
            ],
        )
    return memory_id


async def _memory(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A fresh memory for one pin (#147)."""
    return {"memory_id": await _a_memory(entity, client, pinned=False)}


async def _pinned_memory(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    """A fresh pinned memory for one unpin (#147)."""
    return {"memory_id": await _a_memory(entity, client, pinned=True)}


@dataclass(frozen=True)
class Call:
    """A request that succeeds (``expected``) when the entity's owner sends it. ``json`` is the
    body, or a function of the entity; ``prepare`` runs first, as the owner (or, for a rating
    each requester reads as its own, as each of them), and ``cleanup`` after the case (a
    deployment setting ``prepare`` changed). The request is sent once per query string in
    ``queries``, each its own matrix case."""

    expected: int
    json: Any = None
    queries: tuple[dict[str, str], ...] = ({},)
    prepare: Prepare = _nothing
    cleanup: Prepare = _nothing
    own: tuple[str, ...] = ()
    """Keys of a read's body that name the requesting principal, because the read answers that
    principal's own record (a principal's rating of a message, FR-AUTH-4, #112): the matrix
    compares the bodies with them set aside."""

    def body(self, entity: Entity) -> Any:
        return self.json(entity) if callable(self.json) else self.json


S = "/stories/{story_id}"
C = "/catalog/characters/{character_id}"
PE = "/personas/{persona_id}"
SC = "/catalog/scenarios/{scenario_id}"
M = "/media/{media_id}"
P = "/engine-profiles/{profile_id}"
RELATION = "/relations/{relation}/{principal:path}"
FEEDBACK = "/messages/{message_id}/feedback"
OMNISCIENT = {"viewpoint": "omniscient"}
ENTRY_READS: tuple[dict[str, str], ...] = (
    {},
    {"projection": "full"},
    {"version": "1"},
    {"draft": "true"},
    {"draft": "true", "projection": "full"},
)

NO_SUCH = "01J00000000000000000000000"
"""An id no story holds: the rewrite and amendment routes below answer 404 for it (#223-#238)."""


async def _no_session(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    return {"session_id": NO_SUCH}


async def _no_conflict(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    return {"cid": NO_SUCH}


NO_PURPOSE = "no_such_purpose_826"
"""A consumer purpose no deployment registers (#826): a story-bound call of it is 404 for
anyone, after the story's own prechecks."""


async def _no_purpose(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    return {"name": NO_PURPOSE}


async def _no_amendment(client: httpx.AsyncClient, kind: Kind, entity: Entity) -> dict[str, Any]:
    return {"amendment_id": NO_SUCH}


CALLS: dict[tuple[str, str], Call] = {
    # -- stories: writes
    ("POST", f"{S}/respond"): Call(200, respond_body),
    # #223-#226, #238: the history side-channel on a Lite story (no planner: 409 module_off),
    # its reads, and the routes of a session or an amendment the story does not hold (404).
    # #245: a Lite story's session runs (the stub's reach finds no line: a no-op).
    ("POST", f"{S}/rewrites"): Call(
        422, {"instruction": "Ada was always a sailor."}, queries=(OMNISCIENT,)
    ),
    ("GET", f"{S}/rewrites"): Call(200, queries=(OMNISCIENT,)),
    ("GET", f"{S}/rewrites/{{session_id}}"): Call(404, queries=(OMNISCIENT,), prepare=_no_session),
    # #826: a consumer purpose called inside the story (an unknown one: 404 for anyone).
    ("POST", f"{S}/purposes/{{name}}/calls"): Call(
        404, {"inputs": {"voter": "Ash"}}, prepare=_no_purpose
    ),
    ("POST", f"{S}/rewrites/{{session_id}}/answers"): Call(
        404,
        {"answers": [{"question_id": NO_SUCH, "accept_recommended": True}]},
        queries=(OMNISCIENT,),
        prepare=_no_session,
    ),
    ("POST", f"{S}/rewrites/{{session_id}}/apply"): Call(
        404, {"base_revision": 0}, queries=(OMNISCIENT,), prepare=_no_session
    ),
    ("POST", f"{S}/rewrites/{{session_id}}/cancel"): Call(
        404, queries=(OMNISCIENT,), prepare=_no_session
    ),
    ("POST", f"{S}/entities/merge"): Call(
        422, {"entity_ids": [NO_SUCH, NO_SUCH]}, queries=(OMNISCIENT,)
    ),
    # #244: a Lite story's nudge (steer_queued) for every requester.
    ("POST", f"{S}/directives"): Call(
        200, {"text": "Make this scene tense.", "kind": "ephemeral"}, queries=(OMNISCIENT,)
    ),
    ("GET", f"{S}/amendments"): Call(200, queries=(OMNISCIENT,)),
    ("GET", f"{S}/amendments/{{amendment_id}}"): Call(
        404, queries=(OMNISCIENT,), prepare=_no_amendment
    ),
    ("POST", f"{S}/amendments/{{amendment_id}}/undo"): Call(
        404, {}, queries=(OMNISCIENT,), prepare=_no_amendment
    ),
    ("POST", f"{S}/amendments/{{amendment_id}}/redo"): Call(
        404, {}, queries=(OMNISCIENT,), prepare=_no_amendment
    ),
    # #228: a story with no backfill reads and cancels none (404 for every requester).
    ("GET", f"{S}/backfill"): Call(404),
    ("DELETE", f"{S}/backfill"): Call(404),
    ("POST", f"{S}/backfill"): Call(409, {}),  # a story with no turn has nothing to backfill
    # #247: nothing to prune: the report, applied false.
    ("POST", f"{S}/prune"): Call(200, {"ignore_warnings": True}),
    ("GET", f"{S}/prune-runs"): Call(200),  # #248
    # #230: no backfill, so no conflict to resolve.
    ("POST", f"{S}/backfill/conflicts/{{cid}}"): Call(
        404, {"accept_recommendation": True}, prepare=_no_conflict
    ),
    # #233: a certification run's estimate (the harness installed; nothing written).
    ("POST", f"{S}/certification-runs"): Call(
        200, CERTIFICATION_RUN, prepare=_harness, cleanup=_no_harness
    ),
    ("POST", f"{P}/certification-runs"): Call(
        200, CERTIFICATION_RUN, prepare=_harness, cleanup=_no_harness
    ),
    ("POST", f"{S}/regenerate"): Call(200, {}, prepare=_played),
    # #598 (FR-RESP-14): the played turn replaced with new input.
    ("POST", f"{S}/correct"): Call(
        200, {"turn": 1, "player_input": {"say": "Hello again."}}, prepare=_played
    ),
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
    # -- stories: a character added to the running story, and one promoted to a catalog draft
    # (#114)
    ("POST", f"{S}/characters"): Call(
        201, {"name": "Dale", "definition": "A delivery driver in a hurry.", "true_age": 52}
    ),
    ("POST", f"{S}/characters/{{character_id}}/promote"): Call(201, {}, prepare=_story_character),
    # -- stories: a character's narrative weight, and an entity modeled as a minor (#163)
    ("POST", f"{S}/characters/{{character_id}}/weight"): Call(
        200, {"weight": "minor"}, prepare=_story_character
    ),
    ("POST", f"{S}/entities/{{entity_id}}/model"): Call(201, {"true_age": 40}, prepare=_unmodeled),
    # -- stories: the world clock (#193): read, settings changed, and an advance, on the story
    # running the clock at prompt_only (#203: a Lite story's form)
    ("GET", f"{S}/clock"): Call(200),
    ("PATCH", f"{S}/clock"): Call(200, {"mode": "manual"}),
    ("POST", f"{S}/clock/advance"): Call(
        200, {"minutes": 30, "reason": "they wait"}, prepare=_prompt_only
    ),
    # -- stories: its scheduled events (#196): the list, one created, rescheduled, cancelled
    ("GET", f"{S}/scheduled-events"): Call(200, prepare=_omniscient),
    ("POST", f"{S}/scheduled-events"): Call(
        201, {"proposition": "the delivery", "planned": 60}, prepare=_prompt_only
    ),
    ("PATCH", f"{S}/scheduled-events/{{event_id}}"): Call(
        200, {"planned": 90}, prepare=_scheduled_event
    ),
    ("DELETE", f"{S}/scheduled-events/{{event_id}}"): Call(200, prepare=_scheduled_event),
    # -- stories: its images (#261, #263). The matrix sends JSON: the same route's generation
    # request (#263), 409 ``not_configured`` for anyone (the matrix configures no image adapter);
    # the image the owner uploaded reads and deletes alike.
    ("POST", f"{S}/images"): Call(409, STORY_IMAGE),
    ("GET", f"{S}/images/{{image_id}}"): Call(200, prepare=_story_image),
    ("DELETE", f"{S}/images/{{image_id}}"): Call(204, prepare=_story_image),
    # #269: the story's image usage and cost (a read anyone with the credential may make)
    ("GET", f"{S}/image-usage"): Call(200),
    # -- stories: its States (#186), on the story running States at prompt_only (#203)
    ("GET", f"{S}/states"): Call(200, prepare=_state_name),
    ("GET", f"{S}/states/{{name}}"): Call(200, prepare=_state_name),
    ("PUT", f"{S}/states/{{name}}"): Call(
        200, {"delta": 5, "reason": "found a purse"}, prepare=_state_name
    ),
    ("POST", f"{S}/states"): Call(201, {"definition": GOLD}, prepare=_prompt_only),
    ("GET", f"{S}/states/{{name}}/definition"): Call(200, prepare=_state_name),  # #680
    ("PATCH", f"{S}/states/{{name}}/definition"): Call(200, {"unit": "coins"}, prepare=_state_name),
    # -- stories: engine-proposed States (#192), on the story running States at prompt_only
    ("POST", f"{S}/state-proposals/analyze"): Call(202, prepare=_prompt_only),
    ("GET", f"{S}/state-proposals"): Call(200, prepare=_state_proposal),
    ("POST", f"{S}/state-proposals/{{proposal_id}}/approve"): Call(
        201, {"definition": {}}, prepare=_state_proposal
    ),
    ("POST", f"{S}/state-proposals/{{proposal_id}}/dismiss"): Call(200, prepare=_state_proposal),
    # -- stories: the reconciliation status of its external effects (#210)
    ("GET", f"{S}/reconciliation"): Call(200),
    # #679: the story's outbox rows (an omniscient read)
    ("GET", f"{S}/outbox"): Call(200, queries=({"viewpoint": "omniscient"},)),
    # #681: the story's operations, listed
    ("GET", f"{S}/operations"): Call(200),
    # #218: the explicit actions on a listed effect (#203: on a story running States)
    ("POST", f"{S}/reconciliation/{{outbox_id}}/settle-now"): Call(200, prepare=_outbox_effect),
    ("POST", f"{S}/reconciliation/{{outbox_id}}/compensate"): Call(
        201, _refund, prepare=_outbox_effect
    ),
    # #661: the resolve-only action on a listed consumer-applied effect
    ("POST", f"{S}/reconciliation/{{listing}}/resolve"): Call(
        200, {"resolution": "applied_elsewhere"}, prepare=_consumer_listing
    ),
    # -- stories: a character's participation role (#176)
    ("PATCH", f"{S}/characters/{{character_id}}/participation"): Call(
        200, {"role": "primary", "fixed": True}, prepare=_story_character
    ),
    # -- stories: the configuration preview (#109), which writes nothing, and the story's
    # profile and overrides (#113): the story's own profile again (a no-op), and an override no
    # capability hangs on (applied at once)
    ("POST", f"{S}/impact-preview"): Call(200, {"overrides": {"pins": {"max_pinned": 10}}}),
    ("PUT", f"{S}/engine-profile"): Call(200, {"profile_id": LITE_PRESET_ID}),
    ("PATCH", f"{S}/profile-overrides"): Call(200, OVERRIDES),
    # -- stories: reads, each finding content the owner put there (the story and its history
    # in the default viewpoint and the omniscient one too; its effective configuration, #109)
    ("GET", S): Call(200, queries=({}, OMNISCIENT)),
    ("GET", f"{S}/history"): Call(200, queries=({}, OMNISCIENT), prepare=_played),
    ("GET", f"{S}/settings"): Call(200, prepare=_configured),
    ("GET", f"{S}/config-changes"): Call(200, prepare=_configured),
    ("GET", f"{S}/pinned-memories"): Call(200, prepare=_pinned),
    ("GET", f"{S}/custom-instructions"): Call(200, prepare=_instructed),
    ("GET", f"{S}/variants"): Call(200, prepare=_regenerated),
    ("GET", f"{S}/relations"): Call(200, prepare=_related),
    ("GET", f"{S}/effective-config"): Call(200, prepare=_played),
    ("GET", f"{S}/guarantees"): Call(200, prepare=_played),
    # The predicate registry (#131): listed, and a fresh proposal approved or rejected.
    ("GET", f"{S}/predicates"): Call(200, prepare=_proposed),
    ("POST", f"{S}/predicates/{{predicate_id}}/approve"): Call(200, {}, prepare=_fresh_proposal),
    ("POST", f"{S}/predicates/{{predicate_id}}/reject"): Call(200, prepare=_fresh_proposal),
    # The structured projections (#137): the scene and an entity's standing profile.
    ("GET", f"{S}/scene"): Call(200, queries=(OMNISCIENT,)),
    ("GET", f"{S}/entities/{{entity_id}}/profile"): Call(
        200, queries=(OMNISCIENT,), prepare=_profiled
    ),
    # Commitments (#146): the thunks and the promissory ledger, omniscient.
    ("GET", f"{S}/inspect/ledger"): Call(200, queries=(OMNISCIENT,), prepare=_committing),
    # Belief partitions (#159) and #178's knowledge inspections, omniscient: the story keeps no
    # partitions (#181 delivers the module), so every requester gets the same 409 module_off.
    ("GET", f"{S}/inspect/beliefs"): Call(409, queries=(OMNISCIENT,), prepare=_believing),
    ("GET", f"{S}/inspect/divergences"): Call(409, queries=(OMNISCIENT,)),
    ("GET", f"{S}/inspect/exposures"): Call(409, queries=(OMNISCIENT,)),
    # #207's snapshotted external facts: no partitions gate, so an empty page for everyone.
    ("GET", f"{S}/inspect/external-facts"): Call(200, queries=(OMNISCIENT,)),
    # Author pins (#147): a fresh memory pinned, a fresh pinned memory unpinned.
    ("POST", f"{S}/memories/{{memory_id}}/pin"): Call(200, prepare=_memory),
    ("DELETE", f"{S}/memories/{{memory_id}}/pin"): Call(200, prepare=_pinned_memory),
    # Retrieval (#141): the debugging read, omniscient.
    ("GET", f"{S}/retrieve"): Call(200, queries=({"q": "the game", **OMNISCIENT},)),
    # -- stories: the story's bindings to its providers (#204): bound by each requester, and
    # read after the owner bound it.
    ("PUT", f"{S}/providers/{{name}}/binding"): Call(200, BINDING, prepare=_provider),
    ("GET", f"{S}/providers"): Call(200, prepare=_bound),
    ("GET", f"{S}/providers/{{name}}/binding"): Call(200, prepare=_bound),
    # -- stories: message ratings (#112). Each requester rates a fresh story's reply, retracts
    # the owner's rating (none of its own: still 204), and reads its own rating of a reply all
    # three rated alike.
    ("PUT", f"{S}{FEEDBACK}"): Call(201, RATING, prepare=_reply),
    ("DELETE", f"{S}{FEEDBACK}"): Call(204, prepare=_rated),
    ("GET", f"{S}{FEEDBACK}"): Call(200, prepare=_rated_by_all, own=("principal",)),
    # -- stories: the content rating (#118). Each requester relaxes a fresh story's rating to a
    # new rating with no policies (applied at once), and reads the check the owner's tightening
    # started (still checking: no worker runs here).
    ("PUT", f"{S}/content-rating"): Call(200, rating_body, prepare=_relaxable),
    ("GET", f"{S}/content-rating/checks/{{job_id}}"): Call(200, prepare=_checking),
    # -- catalog characters: writes
    ("PATCH", C): Call(
        200, {"draft_revision": 1, "public_card": {"public_description": "Keeps the logbook."}}
    ),
    ("POST", f"{C}/publish"): Call(201, {"draft_revision": 1}),
    ("POST", f"{C}/archive"): Call(200),
    ("POST", f"{C}/unarchive"): Call(200, prepare=_archived),
    ("DELETE", C): Call(204),
    ("PUT", f"{C}/owner"): Call(200, {"principal": NEW_OWNER}),
    ("POST", f"{C}/relations"): Call(201, {"principal": NEW_OWNER, "relation": "editor"}),
    ("DELETE", f"{C}{RELATION}"): Call(204, prepare=_related),
    ("PUT", f"{C}/listing-status"): Call(200, {"value": "public"}),
    # the advisory rating table (#121), of the draft: a read that writes nothing
    ("POST", f"{C}/rating-suggestion"): Call(200),
    # catalog image candidates (#260): with no image adapter configured a request is 409
    # `not_configured` for anyone; the list reads; a candidate the entry never had is 404 to
    # read and 204 to discard (idempotent). Ownership gates none of them (FR-AUTH-8).
    ("POST", f"{C}/images/generate"): Call(409, {"slot": "primary"}),
    ("GET", f"{C}/images/candidates"): Call(200),
    ("GET", f"{C}/images/candidates/{{candidate_id}}/content"): Call(404, prepare=_no_candidate),
    ("GET", f"{C}/images/candidates/{{candidate_id}}/url"): Call(404, prepare=_no_candidate),
    ("DELETE", f"{C}/images/candidates/{{candidate_id}}"): Call(204, prepare=_no_candidate),
    ("GET", f"{C}/image-usage"): Call(200),  # #269
    # a base appearance prompt drafted from the draft, not saved (#259)
    ("POST", f"{C}/draft/visual-identity/draft-base-prompt"): Call(200),
    # -- catalog characters: reads of a `private` entry: the latest version as the card (the
    # default) and in full, version 1, and the draft in both projections
    ("GET", C): Call(200, queries=ENTRY_READS, prepare=_published),
    ("GET", f"{C}/versions"): Call(200, prepare=_published),
    ("GET", f"{C}/relations"): Call(200, prepare=_related),
    # -- catalog characters: the companion scenario made with the character (#116), read as a
    # scenario is, after the character's publish made its first version
    ("GET", f"{C}/companion"): Call(200, queries=ENTRY_READS, prepare=_published),
    # -- scenarios (#108): writes
    ("PATCH", SC): Call(200, {"draft_revision": 1, "drivers": {"plot_hooks": ["A knock."]}}),
    ("POST", f"{SC}/publish"): Call(201, {"draft_revision": 1}),
    ("POST", f"{SC}/archive"): Call(200),
    ("POST", f"{SC}/unarchive"): Call(200, prepare=_archived),
    ("DELETE", SC): Call(204),
    ("PUT", f"{SC}/owner"): Call(200, {"principal": NEW_OWNER}),
    ("POST", f"{SC}/relations"): Call(201, {"principal": NEW_OWNER, "relation": "editor"}),
    ("DELETE", f"{SC}{RELATION}"): Call(204, prepare=_related),
    ("PUT", f"{SC}/listing-status"): Call(200, {"value": "public"}),
    ("POST", f"{SC}/validate"): Call(200),  # a dry run that writes nothing (#111)
    ("POST", f"{SC}/rating-suggestion"): Call(200),  # the advisory rating table (#121)
    # catalog image candidates (#260): with no image adapter configured a request is 409
    # `not_configured` for anyone; the list reads; a candidate the entry never had is 404 to
    # read and 204 to discard (idempotent). Ownership gates none of them (FR-AUTH-8).
    ("POST", f"{SC}/images/generate"): Call(409, {"slot": "cover"}),
    ("GET", f"{SC}/images/candidates"): Call(200),
    ("GET", f"{SC}/images/candidates/{{candidate_id}}/content"): Call(404, prepare=_no_candidate),
    ("GET", f"{SC}/images/candidates/{{candidate_id}}/url"): Call(404, prepare=_no_candidate),
    ("DELETE", f"{SC}/images/candidates/{{candidate_id}}"): Call(204, prepare=_no_candidate),
    ("GET", f"{SC}/image-usage"): Call(200),  # #269
    # -- scenarios: reads of a `private` scenario, as for catalog characters
    ("GET", SC): Call(200, queries=ENTRY_READS, prepare=_published),
    ("GET", f"{SC}/versions"): Call(200, prepare=_published),
    ("GET", f"{SC}/relations"): Call(200, prepare=_related),
    # -- personas (#114): writes, and the reads of an edited persona
    ("PATCH", PE): Call(200, {"description": "Calmer since the wedding."}),
    ("DELETE", PE): Call(204),
    ("PUT", f"{PE}/owner"): Call(200, {"principal": NEW_OWNER}),
    ("POST", f"{PE}/relations"): Call(201, {"principal": NEW_OWNER, "relation": "editor"}),
    ("DELETE", f"{PE}{RELATION}"): Call(204, prepare=_related),
    ("GET", PE): Call(200, prepare=_edited_persona),
    ("GET", f"{PE}/relations"): Call(200, prepare=_related),
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
    # -- engine profiles (#106): writes (a new version, a copy, a dry-run validation that
    # writes nothing, a delete) and the ownership routes
    ("PUT", P): Call(200, {"name": "Brisk Lite", "settings": EDITED}),
    ("POST", f"{P}/copy"): Call(201, {"name": "My copy"}),
    # #229: no story is pinned to an older version: an empty report, applied.
    ("POST", f"{P}/upgrade-pinned"): Call(200, {"ignore_warnings": True}),
    ("POST", f"{P}/validate"): Call(200, {"settings": EDITED}),
    ("DELETE", P): Call(200),
    ("PUT", f"{P}/owner"): Call(200, {"principal": NEW_OWNER}),
    ("POST", f"{P}/relations"): Call(201, {"principal": NEW_OWNER, "relation": "editor"}),
    ("DELETE", f"{P}{RELATION}"): Call(204, prepare=_related),
    # -- engine profiles: reads of an edited profile, at its latest version and at version 1
    ("GET", P): Call(200, queries=({}, {"version": "1"}), prepare=_edited),
    ("GET", f"{P}/versions"): Call(200, prepare=_edited),
    ("GET", f"{P}/guarantees"): Call(200, queries=({}, {"version": "1"}), prepare=_edited),
    ("GET", f"{P}/relations"): Call(200, prepare=_related),
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


QUERY_PREFIX = "query."
"""A ``prepare`` value under this prefix is a query parameter of the request (``request_query``),
not a path value."""


def request_query(query: dict[str, str], extra: dict[str, Any]) -> dict[str, Any]:
    """``query`` with ``extra``'s ``query.``-prefixed values added."""
    return {
        **query,
        **{k.removeprefix(QUERY_PREFIX): v for k, v in extra.items() if k.startswith(QUERY_PREFIX)},
    }


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
    "create_persona",
    "create_profile",
    "create_scenario",
    "create_story",
    "headers",
    "ok",
    "owner_of",
    "persona_body",
    "request_path",
    "request_query",
    "respond_body",
    "served_entity_routes",
]
