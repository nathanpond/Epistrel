"""Helpers for the scenario catalog tests (#108, #111): request bodies, REST calls on scenarios,
row snapshots, the fixture documents, the catalog characters a fixture's roles name, and the form
a ``full`` read shows a document in.

The milestone parts (C-15, U-26) take their field names and documents from here: their bodies
are checked against the capability lexicon (docs/04 §13), and a scenario's field names include
later milestones' terms (the starting State and its clock, predicates, participation modes)."""

from __future__ import annotations

import copy
import json
import re
import types
import typing
from collections.abc import Mapping
from typing import Any

import httpx2 as httpx
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.catalog.scenario_models import ScenarioDraft, ScenarioModel
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.seeds.ratings import EVERYONE_ID
from tests.auth_support import bearer
from tests.character_support import complete as complete_character

BASE = "/catalog/scenarios"
PRINCIPAL = "p-scenarios"
WRITE = {**bearer(), "X-Epistrel-Principal": PRINCIPAL}
READ = bearer()

CARD_KEYS = frozenset(
    {
        "id",
        "version",
        "listing_status",
        "status",
        "companion_of",
        "name",
        "public_description",
        "media",
        "labels",
        "content_rating",
        "roles",
    }
)
"""A scenario card's keys (#108 AC5): the envelope and the public card, nothing else."""
CARD_ROLE_KEYS = frozenset({"id", "public_label", "setup_description", "kind", "fill"})
"""What a card shows of each role."""
GROUPS = (
    "public_card",
    "metadata",
    "world",
    "cast",
    "starting_state",
    "drivers",
    "story_settings_defaults",
    "recommendation",
    "visual_style",
    "creation_rules",
)
FULL_KEYS = frozenset(
    {
        "id",
        "status",
        "listing_status",
        "owner_principal",
        "version",
        "current_version",
        "created_at",
        "updated_at",
        "published_at",
        "companion_of",
        "schema_version",
        *GROUPS,
    }
)
"""A scenario's ``full`` keys (a draft read adds ``draft_revision``)."""
VERSION_KEYS = frozenset({"version", "published_at", "published_by"})

WORLD_SECRET = "the orchard was sold twice, once to each brother"
"""A phrase only the world description holds."""
ROLE_SECRET = "she burned the second deed before anyone woke"
"""A phrase only a role's seeded memory holds."""
GUIDANCE_SECRET = "let the storm cut the power before dessert"
"""A phrase only the director guidance holds."""
LOCKED_CHARACTER = "01J9Z8Q4N5V6W7X8Y9Z0A1B2D3"
"""The catalog character ``COMPLETE``'s locked role names, at version 3. No character has this
id: a save never checks a role's character, but a publish does (#111), so a test that publishes
``COMPLETE`` names real characters (``cast_characters``, ``complete_with``)."""
DEFAULT_CHARACTER = "01J9Z8Q4N5V6W7X8Y9Z0A1B2D4"
"""The catalog character ``COMPLETE``'s default role is pre-filled with (no version: the latest
published at story creation); like ``LOCKED_CHARACTER``, no character has it."""
LOCKED_VERSIONS = 3
"""The version ``COMPLETE``'s locked role pins: ``cast_characters`` publishes that many."""
OPENING = "Rain on the windows; the roast is late."
"""The opening narration of ``scenario()``'s body (a published scenario needs one, #111)."""
M3_UNAVAILABLE = ("fact_consistency", "seed_normalization", "state_schema")
"""The checks a validation lists ``unavailable`` on every M3 deployment (#111): they need the
structured pipeline or the module tracking starting values, which M3 has not delivered."""


def role(
    role_id: str,
    *,
    kind: str = "npc",
    fill: str = "open",
    label: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """One role of a scenario body."""
    return {
        "id": role_id,
        "public_label": label or role_id.replace("_", " ").title(),
        "setup_description": f"The {role_id} is home for the weekend.",
        "kind": kind,
        "fill": fill,
        **fields,
    }


def seed(seed_text: str, holder: str = "world", **fields: Any) -> dict[str, Any]:
    """A complete seed (holder, audience and disclosure declared)."""
    return {
        "text": seed_text,
        "holder": holder,
        "audience": ["self"],
        "disclosure": "secret",
        **fields,
    }


def scenario(name: str = "Sunday Dinner", **groups: Any) -> dict[str, Any]:
    """A ``POST /catalog/scenarios`` body that publishes as it is: a public card, the Everyone
    rating, a world, two open roles (one the player's) and an opening narration; ``groups``
    replace whole groups."""
    body: dict[str, Any] = {
        "public_card": {
            "name": name,
            "public_description": "The whole family is home for the weekend.",
        },
        "metadata": {"labels": ["family"], "content_rating_id": EVERYONE_ID},
        "world": {
            "description": "A farmhouse outside a small river town.",
            "locations": [{"id": "kitchen", "name": "The kitchen"}],
        },
        "cast": {
            "roles": [
                role("mother", starting_location="kitchen"),
                role("player", kind="player", label="The eldest"),
            ]
        },
        "starting_state": {"opening": {"narration": OPENING}},
    }
    body.update(groups)
    return body


COMPLETE: dict[str, Any] = {
    "public_card": {
        "name": "The Harrow Inheritance",
        "public_description": "Three siblings, one farm, and a will nobody has read.",
        "media": {"gallery": [], "videos": []},
    },
    "metadata": {"labels": ["Family", "dark theme"], "content_rating_id": EVERYONE_ID},
    "world": {
        "description": f"A river farm in late autumn; {WORLD_SECRET}.",
        "locations": [
            {"id": "kitchen", "name": "The kitchen", "description": "Warm, crowded."},
            {"id": "orchard", "name": "The orchard"},
        ],
        "entities": [
            {"id": "deed", "name": "The orchard deed", "aliases": ["the deed"]},
        ],
    },
    "cast": {
        "roles": [
            {
                "id": "mother",
                "public_label": "Mother",
                "setup_description": "Her children are home for the weekend.",
                "kind": "npc",
                "required": True,
                "fill": "open",
                "eligibility": {
                    "labels": ["warm"],
                    "content_ratings": [EVERYONE_ID],
                    "age_range": [45, 75],
                },
                "seeded_memories": [
                    {
                        "text": f"Last winter {ROLE_SECRET}.",
                        "holder": "mother",
                        "audience": ["self", "family"],
                        "disclosure": "secret",
                        "kind": "canon",
                    }
                ],
                "relationships": [{"to_role": "player", "name": "mother_of"}],
                "state_initial_values": {"composure": 7},
                "starting_location": "kitchen",
                "participation": "primary",
                "name_overrides": {"aliases": ["Mum"]},
            },
            {
                "id": "brother",
                "public_label": "The younger brother",
                "setup_description": "He arrived first and has already opened the wine.",
                "kind": "npc",
                "required": False,
                "fill": "locked",
                "character": {"catalog_id": LOCKED_CHARACTER, "version": 3},
                "seeded_memories": [
                    {
                        "text": "A second buyer walked the orchard in the spring.",
                        "holder": "brother",
                        "audience": ["self", "acquaintances"],
                        "disclosure": "guarded",
                        "kind": "belief",
                        "valid_time": {"from": "1987-04-01"},
                    }
                ],
                "relationships": [
                    {"to_role": "mother", "name": "son_of"},
                    {"to_role": "player", "name": "sibling_of"},
                ],
                "starting_location": "orchard",
                "participation": "reactive",
                "name_overrides": {"canonical": "Tom Harrow", "aliases": ["Tommy"]},
            },
            {
                "id": "aunt",
                "public_label": "Aunt Ivy",
                "setup_description": "She drove up from the city with a casserole.",
                "kind": "npc",
                "required": False,
                "fill": "default",
                "character": {"catalog_id": DEFAULT_CHARACTER},
                "seeded_memories": [
                    {
                        "text": "She keeps the only key to the old barn.",
                        "holder": "aunt",
                        "audience": ["self", "perceivable"],
                        "disclosure": "open",
                    }
                ],
                "relationships": [{"to_role": "mother", "name": "sister_of"}],
                "participation": "silent",
            },
            {
                "id": "player",
                "public_label": "The eldest",
                "setup_description": "Back home for the first time in a year.",
                "kind": "player",
                "fill": "open",
            },
        ],
        "max_characters_at_start": 6,
    },
    "starting_state": {
        "seeded_memories": [
            {
                "text": "The farm has been in the family for four generations.",
                "holder": "world",
                "audience": ["public"],
                "disclosure": "open",
            }
        ],
        "states": [{"name": "harvest_mood", "initial": "tense"}],
        "clock": {"start": "1987-10-24T18:00:00", "time_awareness": "loose"},
        "scheduled_events": [{"at": "1987-10-24T21:00:00", "what": "the reading of the will"}],
        "predicates": [{"name": "owns", "cardinality": "multi"}],
        "closed_sets": [],
        "rules": [{"text": "Nobody leaves before the will is read."}],
        "opening": {
            "narration": "Rain on the windows; the roast is late.",
            "starting_location": "kitchen",
            "roles_present": ["mother", "player"],
        },
    },
    "drivers": {
        "plot_hooks": ["A stranger asks for the deed."],
        "objectives": ["Find out who sold the orchard."],
        "director_guidance": f"Slow burn; {GUIDANCE_SECRET}.",
        "planned_reveals": [
            {
                "truth": {
                    "text": "The brother forged the second sale.",
                    "holder": "brother",
                    "audience": ["self", "aunt"],
                    "disclosure": "secret",
                },
                "allowed_roles": ["player"],
                "conditions": "Only after the will is read.",
            }
        ],
        "milestones": ["The will is read."],
        "endings": ["The farm is split.", "The farm is sold."],
    },
    "story_settings_defaults": {
        "interaction_mode": "sequential",
        "turn_concurrency": "queue",
        "player_authority": "own_character",
        "default_output_viewpoint": "mother",
        "ambiguous_retcon_timing": "ask_author",
        "backfill_conflict_policy": "last_mention",
        "audit_retention_days": 30,
        "policies": [
            {
                "name": "No real addresses",
                "applies_to": ["text_output"],
                "rule": {"type": "wordlist", "words": ["Elm Street"]},
                "message": "Keep the farm's address vague.",
            }
        ],
        "required_providers": ["weather"],
    },
    "recommendation": {"engine_profile_id": "lite", "capabilities": ["per_character_partitions"]},
    "visual_style": {"prompt": "oil painting, muted autumn palette", "negative_prompt": "text"},
    "creation_rules": {
        "overridable": ["story_settings_defaults.turn_concurrency", "cast.max_characters_at_start"],
        "bounds": {"audit_retention_days": {"min": 7}},
    },
}
"""A scenario that fills every group: what the lifecycle and projection parts publish. Its cast
has a role of each fill mode (an open one, one locked to a pinned catalog character, one
pre-filled by default) and the player's. Its seeds use every disclosure, and every seed but the
world's names several audience tokens (levels, a role and a named group). Its role characters
are ids no character has: ``complete_with`` names real ones, which a publish needs (#111)."""

CARD_ROLES: list[dict[str, Any]] = [
    {
        "id": "mother",
        "public_label": "Mother",
        "setup_description": "Her children are home for the weekend.",
        "kind": "npc",
        "fill": "open",
    },
    {
        "id": "brother",
        "public_label": "The younger brother",
        "setup_description": "He arrived first and has already opened the wine.",
        "kind": "npc",
        "fill": "locked",
    },
    {
        "id": "aunt",
        "public_label": "Aunt Ivy",
        "setup_description": "She drove up from the city with a casserole.",
        "kind": "npc",
        "fill": "default",
    },
    {
        "id": "player",
        "public_label": "The eldest",
        "setup_description": "Back home for the first time in a year.",
        "kind": "player",
        "fill": "open",
    },
]
"""What a card of ``COMPLETE`` shows of its roles, in order (#108 AC5): written out rather than
derived from the fixture."""

PRIVATE_PHRASES = (WORLD_SECRET, ROLE_SECRET, GUIDANCE_SECRET)


def complete(**changes: Any) -> dict[str, Any]:
    """A deep copy of ``COMPLETE`` with ``changes`` applied to its groups."""
    body = copy.deepcopy(COMPLETE)
    body.update(changes)
    return body


def complete_with(
    locked: str, default: str, *, locked_version: int = LOCKED_VERSIONS, **changes: Any
) -> dict[str, Any]:
    """``complete(**changes)`` whose locked role names catalog character ``locked`` (pinned at
    ``locked_version``) and whose default role names ``default``."""
    body = complete(**changes)
    roles = body["cast"]["roles"]
    roles[1]["character"] = {"catalog_id": locked, "version": locked_version}
    roles[2]["character"] = {"catalog_id": default}
    return body


async def published_character(
    client: httpx.AsyncClient, name: str, *, versions: int = 1, **fields: Any
) -> str:
    """A catalog character created and published ``versions`` times (each an unchanged draft
    publishing as the next version): its id. Its body is a complete character (#115:
    ``tests.character_support.complete``) with ``fields`` as further keys."""
    body = complete_character(name, public_description=f"{name}, a fixture.", **fields)
    created = await client.post("/catalog/characters", json=body, headers=WRITE)
    assert created.status_code == 201, created.text
    character_id: str = created.json()["id"]
    for revision in range(1, versions + 1):
        done = await client.post(
            f"/catalog/characters/{character_id}/publish",
            json={"draft_revision": revision},
            headers=WRITE,
        )
        assert done.status_code == 201, done.text
    return character_id


async def cast_characters(client: httpx.AsyncClient) -> tuple[str, str]:
    """The two catalog characters ``COMPLETE``'s filled roles need to publish (#111): the
    locked one published ``LOCKED_VERSIONS`` times (its role pins that version), then the
    default one, published once. ``complete_with(*found)`` names them."""
    locked = await published_character(client, "Tom Harrow", versions=LOCKED_VERSIONS)
    return locked, await published_character(client, "Ivy Harrow")


async def plant_character(
    db: AsyncEngine,
    character_id: str,
    content: Mapping[str, Any],
    *,
    rating_version_id: str | None = None,
) -> int:
    """Publish ``content`` as the next version of catalog character ``character_id``, as #115's
    shape will hold it (its labels and aliases have no request field yet; #114's interim draft
    takes ``metadata.content_rating_id`` and ``definition.true_age``), recorded under
    ``rating_version_id``: the version's number."""
    async with db.begin() as conn:
        version = int(
            await conn.scalar(
                text(
                    "SELECT coalesce(max(version), 0) + 1 FROM catalog.character_versions "
                    "WHERE catalog_id = :c"
                ),
                {"c": character_id},
            )
            or 1
        )
        await conn.execute(
            text(
                "INSERT INTO catalog.character_versions (catalog_id, version, content, "
                "content_rating_version_id, published_at, published_by) "
                "VALUES (:c, :v, CAST(:doc AS jsonb), :r, now(), 'p-plant')"
            ),
            {"c": character_id, "v": version, "doc": json.dumps(content), "r": rating_version_id},
        )
        await conn.execute(
            text(
                "UPDATE catalog.character_catalog SET status = 'published', "
                "current_version = :v, updated_at = now() WHERE id = :c"
            ),
            {"c": character_id, "v": version},
        )
    return version


# -- the document as a ``full`` read shows it -------------------------------------------------


def seeds_of(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every seed of a document in its stored form (each list present): the world's seeded
    memories, each role's, and each planned reveal's truth."""
    found: list[dict[str, Any]] = list(document["starting_state"]["seeded_memories"])
    for slot in document["cast"]["roles"]:
        found.extend(slot["seeded_memories"])
    found.extend(reveal["truth"] for reveal in document["drivers"]["planned_reveals"])
    return found


def _without_seed_ids(document: Mapping[str, Any]) -> list[Any]:
    """Take each seed's ``id`` out of ``document`` (in place); the ids taken, in seed order."""
    return [found.pop("id", None) for found in seeds_of(document)]


def document_of(shown: Mapping[str, Any]) -> dict[str, Any]:
    """The document a ``full`` read (or a create's or save's answer) shows: ``schema_version``
    and every group, each seed without the ``id`` the server assigned it. Every seed has one, a
    ULID, and no two seeds share one."""
    document = copy.deepcopy({key: shown[key] for key in ("schema_version", *GROUPS)})
    ids = _without_seed_ids(document)
    assert all(isinstance(i, str) and re.fullmatch(ULID_PATTERN, i) for i in ids), ids
    assert len(set(ids)) == len(ids), ids
    return document


def _held_model(annotation: Any) -> tuple[type[ScenarioModel], bool] | None:
    """``(model, is_list)`` when a field holds an object of the scenario document (``M`` or
    ``M | None``) or a list of them; ``None`` for a field holding a value."""
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(members) != 1:
            return None
        annotation = members[0]
    if isinstance(annotation, type) and issubclass(annotation, ScenarioModel):
        return annotation, False
    if typing.get_origin(annotation) is list:
        (item,) = typing.get_args(annotation)
        if isinstance(item, type) and issubclass(item, ScenarioModel):
            return item, True
    return None


def _filled(model: type[ScenarioModel], sent: Mapping[str, Any]) -> dict[str, Any]:
    """``sent``, one object of the document, with each field it leaves out at ``model``'s
    declared default: the values it sets are kept as sent (labels lower-cased)."""
    unknown = set(sent) - set(model.model_fields)
    assert not unknown, (model.__name__, unknown)
    filled: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name not in sent:
            assert not info.is_required(), (model.__name__, name)
            default = info.get_default(call_default_factory=True)
            filled[name] = (
                default.model_dump(mode="json") if isinstance(default, BaseModel) else default
            )
            continue
        value = sent[name]
        held = _held_model(info.annotation)
        if held is not None and value is not None:
            child, is_list = held
            value = [_filled(child, item) for item in value] if is_list else _filled(child, value)
        elif name == "labels":
            value = [label.strip().lower() for label in value]
        filled[name] = value
    return filled


def stored_form(body: Mapping[str, Any]) -> dict[str, Any]:
    """What ``document_of`` gives for a scenario created from ``body`` (its groups): every
    value the body sets, as sent, and every field it leaves out at its declared default; labels
    are stored lower-cased. Only the defaults are read from the model, and none of its
    validators run, so a save that changed a value sent cannot change this expectation too."""
    document = _filled(ScenarioDraft, body)
    _without_seed_ids(document)
    return document


def headers(key: str | None = None, principal: str = PRINCIPAL) -> dict[str, str]:
    """Write headers, with an ``Idempotency-Key`` when ``key`` is given."""
    found = {**bearer(), "X-Epistrel-Principal": principal}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


async def create(client: httpx.AsyncClient, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await client.post(BASE, json=body if body is not None else scenario(), headers=WRITE)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def save(
    client: httpx.AsyncClient, scenario_id: str, revision: int, **groups: Any
) -> httpx.Response:
    return await client.patch(
        f"{BASE}/{scenario_id}", json={"draft_revision": revision, **groups}, headers=WRITE
    )


async def publish(
    client: httpx.AsyncClient, scenario_id: str, revision: int, *, key: str | None = None
) -> httpx.Response:
    return await client.post(
        f"{BASE}/{scenario_id}/publish", json={"draft_revision": revision}, headers=headers(key)
    )


async def read(client: httpx.AsyncClient, scenario_id: str, **params: Any) -> httpx.Response:
    query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
    return await client.get(f"{BASE}/{scenario_id}", params=query, headers=READ)


async def published(
    client: httpx.AsyncClient, body: dict[str, Any] | None = None
) -> tuple[str, int]:
    """A created and published scenario: its id and its draft revision after the publish."""
    created = await create(client, body)
    done = await publish(client, created["id"], created["draft_revision"])
    assert done.status_code == 201, done.text
    return created["id"], int(done.json()["draft_revision"])


async def set_cast_limits(client: httpx.AsyncClient, **values: int | None) -> None:
    """Set the deployment's starting-cast settings (``max_characters_at_start_default`` and
    ``_ceiling``) through ``/admin/settings``."""
    response = await client.put(
        "/admin/settings",
        json={f"max_characters_at_start_{name}": value for name, value in values.items()},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text


async def version_rows(engine: AsyncEngine, scenario_id: str) -> list[str]:
    """The scenario's ``scenario_versions`` rows as JSON text, by version."""
    async with engine.connect() as conn:
        found = await conn.scalars(
            text(
                "SELECT row_to_json(v)::text FROM catalog.scenario_versions v "
                "WHERE catalog_id = :c ORDER BY version"
            ),
            {"c": scenario_id},
        )
        return list(found)


async def entry_row(engine: AsyncEngine, scenario_id: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM catalog.scenario_catalog WHERE id = :c"), {"c": scenario_id}
            )
        ).one_or_none()


def error_of(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def fields_of(response: httpx.Response) -> list[str]:
    """Every field a 422 ``validation_error`` names (``details.errors``), in order."""
    return [item["field"] for item in error_of(response)["details"]["errors"]]


__all__ = [
    "BASE",
    "CARD_KEYS",
    "CARD_ROLES",
    "CARD_ROLE_KEYS",
    "COMPLETE",
    "DEFAULT_CHARACTER",
    "FULL_KEYS",
    "GROUPS",
    "GUIDANCE_SECRET",
    "LOCKED_CHARACTER",
    "LOCKED_VERSIONS",
    "M3_UNAVAILABLE",
    "OPENING",
    "PRINCIPAL",
    "PRIVATE_PHRASES",
    "READ",
    "ROLE_SECRET",
    "VERSION_KEYS",
    "WORLD_SECRET",
    "WRITE",
    "cast_characters",
    "complete",
    "complete_with",
    "create",
    "document_of",
    "entry_row",
    "error_of",
    "fields_of",
    "headers",
    "plant_character",
    "publish",
    "published",
    "published_character",
    "read",
    "role",
    "save",
    "scenario",
    "seed",
    "seeds_of",
    "set_cast_limits",
    "stored_form",
    "version_rows",
]
