"""Helpers for the scenario catalog tests (#108): request bodies, REST calls on scenarios, row
snapshots, and the fixture documents.

The milestone parts (C-15, U-26) take their field names and documents from here: their bodies
are checked against the capability lexicon (docs/04 §13), and a scenario's field names include
later milestones' terms (the starting State and its clock, predicates, participation modes)."""

from __future__ import annotations

import copy
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.auth_support import bearer

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
    """A ``POST /catalog/scenarios`` body: a public card, a world and two open roles (one the
    player's); ``groups`` replace whole groups."""
    body: dict[str, Any] = {
        "public_card": {
            "name": name,
            "public_description": "The whole family is home for the weekend.",
        },
        "metadata": {"labels": ["family"]},
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
    }
    body.update(groups)
    return body


COMPLETE: dict[str, Any] = {
    "public_card": {
        "name": "The Harrow Inheritance",
        "public_description": "Three siblings, one farm, and a will nobody has read.",
        "media": {"gallery": [], "videos": []},
    },
    "metadata": {"labels": ["Family", "dark theme"]},
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
                "eligibility": {"labels": ["warm"], "age_range": [45, 75]},
                "seeded_memories": [
                    {
                        "text": f"Last winter {ROLE_SECRET}.",
                        "holder": "mother",
                        "audience": ["self"],
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
                "fill": "open",
                "participation": "reactive",
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
                    "audience": ["self"],
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
"""A scenario that fills every group: what the lifecycle and projection parts publish."""

PRIVATE_PHRASES = (WORLD_SECRET, ROLE_SECRET, GUIDANCE_SECRET)


def complete(**changes: Any) -> dict[str, Any]:
    """A deep copy of ``COMPLETE`` with ``changes`` applied to its groups."""
    body = copy.deepcopy(COMPLETE)
    body.update(changes)
    return body


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
    "CARD_ROLE_KEYS",
    "COMPLETE",
    "FULL_KEYS",
    "GROUPS",
    "GUIDANCE_SECRET",
    "PRINCIPAL",
    "PRIVATE_PHRASES",
    "READ",
    "ROLE_SECRET",
    "VERSION_KEYS",
    "WORLD_SECRET",
    "WRITE",
    "complete",
    "create",
    "entry_row",
    "error_of",
    "fields_of",
    "headers",
    "publish",
    "published",
    "read",
    "role",
    "save",
    "scenario",
    "seed",
    "set_cast_limits",
    "version_rows",
]
