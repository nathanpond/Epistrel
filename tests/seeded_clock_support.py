"""Helpers for States, the clock and scheduled events at genesis (#202): S-FAMILY's Sunday Dinner
of ``tests/seeded_partitions_support`` with a per-sibling allowance, a starting clock and the
18:00 dinner, a profile running both modules, and reads of what genesis wrote.

The M6 parts (I-27, S-FAMILY, U-27, S-STATE-SCHEMA's publish part) take their bodies from here."""

from __future__ import annotations

import copy
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.seeds.profiles import structured_m4_settings
from tests import genesis_support as g
from tests import seeded_partitions_support as sp
from tests import takeback_support
from tests.profile_support import PROFILES, merged

SEEDED: dict[str, Any] = {
    "states": {"level": "on"},
    "clock": {"level": "on", "scheduled_stops": True},
}
"""What the profile adds to the partitioned ``Structured (M4)``: States and the clock on."""
SIBLINGS = ("brother", "sister", "player")
ALLOWANCES = {"brother": 20, "sister": 15, "player": 10}
"""Each sibling's starting allowance (``state_initial_values``)."""
ALLOWANCE: dict[str, Any] = {
    "name": "allowance",
    "type": "number",
    "scope": "character",
    "constraints": {"non_negative": True},
    "initial_value": 0,
    "unit": "dollars",
    "aliases": ["pocket money"],
    "authorization": {"sources": ["role:mother", "role:father", "author", "consumer"]},
}
"""The per-sibling allowance: the parents (by role) may change it."""
GROCERIES: dict[str, Any] = {
    "name": "groceries",
    "type": "number",
    "initial_value": 120,
    "constraints": {"min": 0},
}
TOTAL: dict[str, Any] = {
    "name": "budget_left",
    "type": "number",
    "rules": {"kind": "derived", "expression": "groceries - 20"},
    "authorization": {"sources": ["rule", "author"]},
}
"""A derived State declared before the one it reads (genesis orders it after)."""
CLOCK: dict[str, Any] = {
    "anchor": {"year": 1987, "month": 10, "day": 25, "time": "14:00"},
    "max_unprompted_skip_minutes": 90,
    "time_awareness": "approximate",
    "stop_at_due_events": True,
}
"""Sunday 1987-10-25, 14:00; a 90-minute skip cap; the family knows the time roughly."""
START = 14 * 60
DINNER: dict[str, Any] = {
    "proposition": "the Sunday dinner",
    "planned": "18:00",
    "participants": ["mother", "father", "brother", "sister", "player"],
    "place": "dining",
}
"""The world's 18:00 dinner (four hours after the start)."""
DINNER_AT = 18 * 60 - START
ERRAND: dict[str, Any] = {
    "proposition": "the pharmacy errand",
    "planned": "15:00",
    "owner": "mother",
    "participants": ["mother"],
}
"""The Mother's own 15:00 errand: her plan, known to her alone (``self`` by default)."""


def family(**starting: Any) -> dict[str, Any]:
    """S-FAMILY's Sunday Dinner (``sp.family``) with the allowance, the starting clock and the
    dinner (``starting`` replaces starting-state groups)."""
    body = sp.family()
    for role in body["cast"]["roles"]:
        if role["id"] in ALLOWANCES:
            role["state_initial_values"] = {"allowance": ALLOWANCES[role["id"]]}
    body["starting_state"].update(
        {
            "states": [copy.deepcopy(ALLOWANCE)],
            "clock": copy.deepcopy(CLOCK),
            "scheduled_events": [copy.deepcopy(DINNER)],
        }
    )
    body["starting_state"].update(copy.deepcopy(starting))
    return body


async def seeded_profile(client: httpx.AsyncClient, name: str = "Seeded clock") -> str:
    """A profile running the partitioned ``Structured (M4)`` with States and the clock on (the
    caller has delivered them): its id, deleted after the test (``takeback_support``)."""
    created = await client.post(
        PROFILES,
        json={"name": name, "settings": merged(structured_m4_settings(), sp.PARTITIONED, SEEDED)},
        headers=g.WRITE,
    )
    assert created.status_code == 201, created.text
    profile_id: str = created.json()["id"]
    takeback_support.created_profile(profile_id)
    return profile_id


async def definitions(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The story's State definitions by name, the clock's settings columns included."""
    found = await sp.rows_of(
        engine,
        "SELECT name, type::text AS type, scope::text AS scope, created_by::text AS created_by, "
        'initial_value, "authorization", anchor, max_unprompted_skip_minutes, '
        "time_awareness::text AS time_awareness, stop_at_due_events "
        "FROM state_definitions WHERE story_id = :s",
        s=story_id,
    )
    return {str(r.name): r for r in found}


async def seeded_values(engine: AsyncEngine, story_id: str) -> dict[str | None, Any]:
    """The ``seed`` changes genesis wrote, by scope key: ``(state, kind, value, position)``."""
    found = await sp.rows_of(
        engine,
        "SELECT d.name, c.scope_key, c.kind::text AS kind, c.value, c.narrative_turn, "
        "c.source::text AS source FROM state_changes c JOIN state_definitions d "
        "ON d.id = c.state_id WHERE c.story_id = :s AND c.source = 'seed'",
        s=story_id,
    )
    return {r.scope_key: (r.name, r.kind, r.value, r.narrative_turn) for r in found}


async def events(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's scheduled events as it stands, each with its event's name."""
    from tests import clock_support

    return await clock_support.events(engine, story_id)


async def entity_name(engine: AsyncEngine, entity_id: str) -> str:
    (row,) = await sp.rows_of(
        engine, "SELECT canonical_name FROM entities WHERE id = :e", e=entity_id
    )
    return str(row.canonical_name)


async def deferred(engine: AsyncEngine, story_id: str) -> dict[str, tuple[str, bool]]:
    """The story's ``genesis_deferred`` rows: ``field_ref`` → ``(capability, materialized)``."""
    found = await sp.rows_of(
        engine,
        "SELECT field_ref, capability_needed, materialized_at FROM genesis_deferred "
        "WHERE story_id = :s",
        s=story_id,
    )
    return {
        str(r.field_ref): (str(r.capability_needed), r.materialized_at is not None) for r in found
    }


__all__ = [
    "ALLOWANCE",
    "ALLOWANCES",
    "CLOCK",
    "DINNER",
    "DINNER_AT",
    "ERRAND",
    "GROCERIES",
    "SEEDED",
    "SIBLINGS",
    "START",
    "TOTAL",
    "deferred",
    "definitions",
    "entity_name",
    "events",
    "family",
    "seeded_profile",
    "seeded_values",
]
