"""Helpers for epistemic seeding at genesis (#173): a profile keeping per-character partitions and
nested beliefs, the Sunday Dinner family of S-FAMILY with seeds the deterministic detectors
normalize, and reads of the beliefs, memories and exposures a creation wrote.

``partitions`` and ``nested_beliefs`` are #181's to deliver, so a test patches the delivered set
(``tests.nested_support.deliver_nested``) before it creates the profile. The M5 parts (I-27,
U-27, C-19, S-FAMILY) take their bodies from here."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.seeds.profiles import structured_m4_settings
from epistrel.core.seeds.ratings import EVERYONE_ID
from tests import genesis_support as g
from tests import scenario_support as scenarios
from tests.profile_support import PROFILES, merged

PARTITIONED: dict[str, Any] = {
    "character_knowledge": {
        "partitions": True,
        "testimony": "defaulted",
        "lazy_resolution": False,
        "nested_beliefs": {"enabled": True, "depth": 2},
    },
    "perception": {"level": "co_location"},
}
"""What the partitioned profile adds to ``Structured (M4)``'s settings."""

# The seeds: possession (``holds``) is what the detectors normalize without a model.
SALE = "Frank keeps the sale contract."
"""The house is being sold: the Father's secret (the world's, and the Mother's own)."""
SALE_DENIED = "Frank does not keep the sale contract."
"""The Brother's false belief about the sale."""
AFFAIR = "Linda keeps the love letters."
"""The Mother's affair: hers alone, ``secret``."""
RING = "Frank keeps the ring."
"""The world tells the Mother her husband is faithful (he keeps the ring)."""
RING_DENIED = "Frank does not keep the ring."
"""The Mother's own seed says the opposite: the world wins."""
RECIPES = "Linda keeps the recipe book."
"""Seeded public by the world and secret by the Father's role: the world's audience wins."""
KEY = "Ruth keeps the spare key."
"""The Sister's ``acquaintances`` seed: the family knows, the neighbour does not."""
CASSEROLE = "Ruth keeps the casserole dish."
"""What the opening narration states: an exposure for each role present."""
OPENING = f"Rain on the windows; the roast is late. {CASSEROLE}"
PERSONA: dict[str, Any] = {"name": "Sam", "age": 22, "description": "Wry and conflict-avoidant."}
TRAITS = ("wry", "conflict-avoidant")
NPC_ROLES = ("mother", "father", "brother", "sister")
NAMES = {"mother": "Linda", "father": "Frank", "brother": "Tom", "sister": "Ruth"}


def seed(seed_text: str, holder: str, audience: list[str], **fields: Any) -> dict[str, Any]:
    return {
        "text": seed_text,
        "holder": holder,
        "audience": audience,
        "disclosure": fields.pop("disclosure", "open"),
        "kind": fields.pop("kind", "canon"),
        **fields,
    }


def family() -> dict[str, Any]:
    """S-FAMILY's Sunday Dinner: four open NPC roles, a neighbour (a stranger to Sam) and the
    player's Third Sibling, the family a named group, relationships with a trust, the house's
    topology and the seeds of U-27 M5 (see the constants)."""
    r = scenarios.role
    roles = [
        r(
            "mother",
            starting_location="dining",
            seeded_memories=[
                seed(SALE, "mother", ["self"], disclosure="secret"),
                seed(AFFAIR, "mother", ["self"], disclosure="secret"),
                seed(RING_DENIED, "mother", ["self"], kind="belief", disclosure="guarded"),
            ],
            relationships=[{"to_role": "player", "name": "mother_of", "trust": 0.9}],
        ),
        r(
            "father",
            starting_location="dining",
            seeded_memories=[seed(RECIPES, "father", ["self", "player"], disclosure="secret")],
            relationships=[{"to_role": "mother", "name": "married_to"}],
        ),
        r(
            "brother",
            starting_location="dining",
            seeded_memories=[seed(SALE_DENIED, "brother", ["self"], kind="belief")],
            relationships=[{"to_role": "player", "name": "sibling_of"}],
        ),
        r(
            "sister",
            starting_location="kitchen",
            seeded_memories=[
                seed(KEY, "sister", ["self", "acquaintances"], disclosure="guarded"),
                {
                    **seed("She is sure Tom knows about the sale.", "sister", ["self"]),
                    "kind": "nested",
                    "nested": {"attributed_holder": "brother", "proposition": SALE},
                },
            ],
            relationships=[{"to_role": "player", "name": "sibling_of"}],
        ),
        r("neighbour", required=False, starting_location="kitchen"),
        r("player", kind="player", label="The Third Sibling", starting_location="dining"),
    ]
    return {
        "public_card": {
            "name": "Sunday Dinner",
            "public_description": "The whole family is home for the weekend.",
        },
        "metadata": {"labels": ["family"], "content_rating_id": EVERYONE_ID},
        "world": {
            "description": "A farmhouse outside a small river town.",
            "locations": [
                {"id": "house", "name": "The farmhouse"},
                {
                    "id": "dining",
                    "name": "The dining room",
                    "located_in": "house",
                    "adjacent_to": [{"location": "kitchen", "barrier": "door_closed"}],
                },
                {"id": "kitchen", "name": "The kitchen", "located_in": "house"},
            ],
            "entities": [
                {"id": name.replace(" ", "_"), "name": f"The {name}", "kind": "item"}
                for name in (
                    "sale contract",
                    "love letters",
                    "ring",
                    "recipe book",
                    "spare key",
                    "casserole dish",
                )
            ],
        },
        "cast": {
            "roles": roles,
            "groups": {"family": [*NPC_ROLES, "player"]},
        },
        "starting_state": {
            "seeded_memories": [
                seed(SALE, "world", ["father"], disclosure="secret"),
                seed(RING, "world", ["mother"]),
                seed(RECIPES, "world", ["public"]),
            ],
            "channels": [{"id": "the family phone", "participants": ["sister", "brother"]}],
            "opening": {
                "narration": OPENING,
                "starting_location": "dining",
                "roles_present": ["mother", "father", "brother", "player"],
            },
        },
    }


TEMPER: dict[str, Any] = {
    "name": "Sam",
    "age": 23,
    "description": "Short-tempered, flies off the handle.",
}
"""C-19 M5's persona."""
TEMPER_TRAITS = ("short-tempered", "flies off the handle")


def office() -> dict[str, Any]:
    """C-19 M5's scenario: Sam (the player) a coworker of Alice and Bob (the ``office`` group)
    and a stranger to Carol, all in the office."""
    r = scenarios.role
    return {
        "public_card": {"name": "Monday Standup", "public_description": "A small office."},
        "metadata": {"labels": ["work"], "content_rating_id": EVERYONE_ID},
        "world": {
            "description": "A small design studio above a bakery.",
            "locations": [{"id": "office", "name": "The office"}],
        },
        "cast": {
            "roles": [
                r("alice", starting_location="office"),
                r("bob", starting_location="office"),
                r("carol", starting_location="office"),
                r("player", kind="player", label="The new hire", starting_location="office"),
            ],
            "groups": {"office": ["alice", "bob", "player"]},
        },
        "starting_state": {
            "opening": {
                "narration": "The coffee machine gurgles.",
                "starting_location": "office",
                "roles_present": ["alice", "bob", "carol", "player"],
            }
        },
    }


def body_with(**changes: Any) -> dict[str, Any]:
    found = family()
    found.update(copy.deepcopy(changes))
    return found


async def partitioned_profile(client: httpx.AsyncClient, name: str = "Seeded partitions") -> str:
    """A profile running ``Structured (M4)`` with partitions and nested beliefs on (the caller
    has delivered them): its id."""
    created = await client.post(
        PROFILES,
        json={"name": name, "settings": merged(structured_m4_settings(), PARTITIONED)},
        headers=g.WRITE,
    )
    assert created.status_code == 201, created.text
    profile_id: str = created.json()["id"]
    return profile_id


async def cast(
    client: httpx.AsyncClient, definitions: Mapping[str, dict[str, Any]] | None = None
) -> dict[str, str]:
    """A catalog character per NPC role (named ``NAMES``, with ``definitions``' changes): role
    id → catalog id."""
    found: dict[str, str] = {}
    for (role, name), age in zip(NAMES.items(), (58, 60, 27, 25), strict=True):
        found[role] = await g.catalog_character_id(
            client,
            f"{name} Harrow",
            age=age,
            labels=["family"],
            definition=(definitions or {}).get(role),
        )
    return found


def request(
    scenario_id: str,
    characters: Mapping[str, str],
    profile_id: str,
    *,
    persona: Mapping[str, Any] | None = None,
    neighbour: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    """The creation of a story from the family scenario, every NPC role cast, the persona
    ``PERSONA`` (or ``persona``) for the player, under ``profile_id``, with the full report."""
    slots = {role: g.catalog_slot(c) for role, c in characters.items()}
    if neighbour:
        slots["neighbour"] = g.ad_hoc_slot("Mr Pike", 61, "The neighbour from across the lane.")
    return g.from_scenario(
        scenario_id,
        slots=slots,
        players={"player": g.player(**dict(persona or PERSONA))},
        engine_profile_id=profile_id,
        ignore_warnings=True,
        projection="full",
        **fields,
    )


def by_field(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """A ``full`` report's ``materialized`` entries by ``field_ref``."""
    return {e["field_ref"]: e for e in report["materialized"]}


async def rows_of(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


BELIEFS = (
    "SELECT b.id, b.holder_character_id AS holder, b.kind, b.polarity, b.confidence_level, "
    "b.disclosure, b.provenance, b.attributed_holder_id, b.attributed_confidence_level, "
    "b.held_from_turn, b.visibility, p.subject_id, p.object_id, p.literal, pr.name AS predicate, "
    "t.kind AS transition, t.antecedent_ref, t.valid_at, e.type AS act, e.payload AS act_payload "
    "FROM beliefs b JOIN propositions p ON p.id = b.proposition_id "
    "JOIN predicates pr ON pr.id = p.predicate_id "
    "JOIN belief_transitions t ON t.belief_id = b.id "
    "LEFT JOIN events e ON e.id::text = t.antecedent_ref->>'id' "
    "WHERE b.story_id = :s ORDER BY b.held_from_step, b.id"
)


async def beliefs(engine: AsyncEngine, story_id: str) -> list[Any]:
    """Every belief of the story with its proposition, its transition and the author act cited."""
    return await rows_of(engine, BELIEFS, s=story_id)


async def entity_ids(engine: AsyncEngine, story_id: str) -> dict[str, str]:
    """The story's entities by canonical name."""
    found = await rows_of(
        engine, "SELECT id, canonical_name FROM entities WHERE story_id = :s", s=story_id
    )
    return {str(r.canonical_name): str(r.id) for r in found}


def held(
    rows: list[Any], holder: str, subject: str, obj: str | None = None, *, kind: str = "first_order"
) -> list[Any]:
    """The rows of ``holder`` about ``subject`` (and ``obj``) of ``kind``."""
    return [
        r
        for r in rows
        if str(r.holder) == holder
        and str(r.subject_id) == subject
        and (obj is None or str(r.object_id) == obj)
        and r.kind == kind
    ]


__all__ = [
    "AFFAIR",
    "CASSEROLE",
    "KEY",
    "NAMES",
    "NPC_ROLES",
    "OPENING",
    "PARTITIONED",
    "PERSONA",
    "RECIPES",
    "RING",
    "RING_DENIED",
    "SALE",
    "SALE_DENIED",
    "TEMPER",
    "TEMPER_TRAITS",
    "TRAITS",
    "beliefs",
    "body_with",
    "by_field",
    "cast",
    "entity_ids",
    "family",
    "held",
    "office",
    "partitioned_profile",
    "request",
    "rows_of",
    "seed",
]
