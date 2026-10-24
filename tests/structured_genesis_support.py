"""Helpers for a structured story's genesis from a scenario (#149): the Sunday Dinner body with
seeds the deterministic detectors normalize, the capability names a structured genesis leaves
deferred, and reads of what it wrote.

The M4 parts (I-27, I-20) take these from here: a deferral's capability names and the seed
kinds include later milestones' terms (docs/04 §13's capability lexicon), so they stay out of
the parts' bodies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.testing.profiles import structured_m4
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios

WORLD_CANON = "Tom Harrow keeps the deed."
"""A world seed the possession detector normalizes: ``holds(Tom, the deed)``."""
ROLE_DENIAL = "Tom Harrow does not keep the deed."
"""A role seed (the brother's) contradicting ``WORLD_CANON``: it loses the merge (``polarity``)."""
WORLD_PROSE = "The farm has been in the family for four generations."
"""``COMPLETE``'s world seed: free text no detector normalizes, so a memory only."""
AUNT_PROSE = "She keeps the only key to the old barn."
"""``COMPLETE``'s aunt's seed, evident to whoever meets her: a memory everyone may hold."""
DIRECTOR_PHRASES = (
    scenarios.GUIDANCE_SECRET,
    "A stranger asks for the deed.",
    "Find out who sold the orchard.",
    "Only after the will is read.",
    "The will is read.",
    "The farm is split.",
)
"""Text only the director's fields hold (guidance, a hook, an objective, a reveal's conditions,
a milestone, an ending)."""

LATER_CAPABILITIES = frozenset({"partitions", "clock", "states"})
"""What a structured story of M4 still defers (per-character knowledge, the clock, trackers)."""
STRUCTURED = "structured"
NON_CANON_KINDS = ("belief", "nested", "scheduled")


def public_seed(seed_text: str, holder: str = "world") -> dict[str, Any]:
    return {
        "text": seed_text,
        "holder": holder,
        "audience": ["public"],
        "disclosure": "open",
        "kind": "canon",
    }


def structured_dinner(locked: str, default: str) -> dict[str, Any]:
    """``lg.sunday_dinner`` with a world seed the detectors normalize (``WORLD_CANON``), a
    brother's seed that contradicts it (``ROLE_DENIAL``), and the deed an ``item``."""
    body = lg.sunday_dinner(locked, default)
    body["world"]["entities"][0]["kind"] = "item"  # what ``holds`` takes as its object
    body["starting_state"]["seeded_memories"].append(public_seed(WORLD_CANON))
    body["cast"]["roles"][1]["seeded_memories"].append(public_seed(ROLE_DENIAL, "brother"))
    return body


def structured_request(scenario_id: str, mother: str, **fields: Any) -> dict[str, Any]:
    """The creation of a story from ``scenario_id`` pinned to the seeded profile's version 1
    (``Structured (M4)``), ``mother`` cast in the open role, with the full report."""
    from tests import genesis_support as g

    return g.from_scenario(
        scenario_id,
        slots={"mother": g.catalog_slot(mother)},
        engine_profile_id=structured_m4(),
        engine_profile_version=1,
        ignore_warnings=True,
        projection="full",
        **fields,
    )


def by_field(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """A ``full`` report's ``materialized`` entries by ``field_ref``."""
    return {e["field_ref"]: e for e in report["materialized"]}


def is_seed(seed: Mapping[str, Any]) -> bool:
    """A seed whose structured form M4 materializes: a canon one shared with everyone."""
    return (seed.get("kind") or "canon") not in NON_CANON_KINDS


async def rows_of(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def story_rows_naming(engine: AsyncEngine, story_id: str, phrase: str) -> dict[str, int]:
    """How many rows of each store a structured story's knowledge lives in name ``phrase``:
    facts (with their propositions and literals), memories, entities and their aliases, and the
    story's predicates; non-zero counts only."""
    found: dict[str, int] = {}
    queries = {
        "facts": "SELECT count(*) FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND (f::text ILIKE :p OR p::text ILIKE :p)",
        "propositions": "SELECT count(*) FROM propositions WHERE story_id = :s AND "
        "propositions::text ILIKE :p",
        "memories": "SELECT count(*) FROM memories WHERE story_id = :s AND memories::text ILIKE :p",
        "entities": "SELECT count(*) FROM entities WHERE story_id = :s AND entities::text ILIKE :p",
        "entity_aliases": "SELECT count(*) FROM entity_aliases a JOIN entities e "
        "ON e.id = a.entity_id WHERE e.story_id = :s AND a::text ILIKE :p",
        "predicates": "SELECT count(*) FROM predicates WHERE story_id = :s AND "
        "predicates::text ILIKE :p",
    }
    async with engine.connect() as conn:
        for name, sql in queries.items():
            count = int(await conn.scalar(text(sql), {"s": story_id, "p": f"%{phrase}%"}) or 0)
            if count:
                found[name] = count
    return found


__all__ = [
    "AUNT_PROSE",
    "DIRECTOR_PHRASES",
    "LATER_CAPABILITIES",
    "ROLE_DENIAL",
    "STRUCTURED",
    "WORLD_CANON",
    "WORLD_PROSE",
    "by_field",
    "is_seed",
    "public_seed",
    "rows_of",
    "story_rows_naming",
    "structured_dinner",
    "structured_request",
]
