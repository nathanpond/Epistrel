"""Helpers for a Lite story's materialisation from a scenario (#122): the Sunday Dinner body
with planted public phrases, the deferred items and inactive settings expected of it (worked
out from the fixture here, independently of ``core.genesis.materialize``), row counts of every
story-scoped table, and the rendered prompt's headings.

The milestone parts (I-18, I-27, U-27, C-15) compare through these helpers: a deferral's
capability names, a seed's kinds and the setting names include later milestones' terms
(docs/04 §13's capability lexicon), so they stay here, out of the parts' bodies."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import scenario_support as scenarios

PUBLIC_PHRASE = "a will nobody has read beside the orchard"
"""A phrase only the scenario's public description holds."""
LABEL_PHRASE = "harrowsaga"
"""A label only the scenario's metadata holds."""
STYLE_PHRASE = "gilded umber brushwork"
"""A phrase only the visual style holds."""
NEGATIVE_STYLE_PHRASE = "no lens flare ever"
PUBLIC_PHRASES = (PUBLIC_PHRASE, LABEL_PHRASE, STYLE_PHRASE, NEGATIVE_STYLE_PHRASE)
"""What no text prompt may carry (the public card, the labels, the visual style)."""

SCENARIO_HEADING = "## Scenario"
DIRECTOR_HEADING = "## Author notes (not facts)"
PREVAIL = (
    "Where these facts disagree with a character's own description, the scenario's facts prevail."
)
BELIEF_HEADING = "believes:"
"""How a holder's belief seeds are introduced in the scenario text."""

NEVER_DEFERRED = (
    "public_card",
    "metadata",
    "drivers.plot_hooks",
    "drivers.objectives",
    "drivers.director_guidance",
    "drivers.milestones",
    "drivers.endings",
    "recommendation",
    "visual_style",
)
"""Field paths no deferred item may name (public-card, director, recommendation, style)."""

UNUSED_SETTINGS = {
    "interaction_mode": "multi_character",
    "player_authority": "structured",
    "ambiguous_retcon_timing": "history_rewrite",
    "backfill_conflict_policy": "history_rewrite",
    "required_providers": "providers",
}
"""Each story setting a Lite story keeps inactive, with the capability it waits for."""


def sunday_dinner(locked: str, default: str) -> dict[str, Any]:
    """``scenario_support.COMPLETE`` (the Sunday Dinner family, every group filled: director
    guidance, hooks and objectives, settings, a recommended profile, a visual style) naming
    real role characters, with a phrase planted in its public description, a label and its
    visual style."""
    body = scenarios.complete_with(locked, default)
    body["public_card"]["public_description"] = f"Three siblings and {PUBLIC_PHRASE}."
    body["metadata"]["labels"] = [*body["metadata"]["labels"], LABEL_PHRASE]
    body["visual_style"] = {"prompt": STYLE_PHRASE, "negative_prompt": NEGATIVE_STYLE_PHRASE}
    return body


def _seed_capability(seed: Mapping[str, Any]) -> str:
    kind = seed.get("kind")
    if kind == "scheduled":
        return "clock"
    if kind in ("belief", "nested"):
        return "partitions"
    if seed.get("disclosure") == "secret" or "public" not in (seed.get("audience") or ()):
        return "partitions"
    return "structured"


def expected_deferred(body: Mapping[str, Any], *, player: bool = True) -> list[tuple[str, str]]:
    """``(field_ref, capability)`` of every item a Lite creation of ``body`` defers."""
    found: list[tuple[str, str]] = []
    world = body.get("world", {})
    found += [
        (f"world.locations[{i}]", "structured") for i in range(len(world.get("locations", [])))
    ]
    found += [(f"world.entities[{i}]", "structured") for i in range(len(world.get("entities", [])))]
    state = body.get("starting_state", {})
    for i, seed in enumerate(state.get("seeded_memories", [])):
        found.append((f"starting_state.seeded_memories[{i}]", _seed_capability(seed)))
    roles = body["cast"]["roles"]
    for r, role in enumerate(roles):
        for i, seed in enumerate(role.get("seeded_memories", [])):
            found.append((f"cast.roles[{r}].seeded_memories[{i}]", _seed_capability(seed)))
    for r, role in enumerate(roles):
        found += [
            (f"cast.roles[{r}].relationships[{i}]", "structured")
            for i in range(len(role.get("relationships", [])))
        ]
        if role.get("name_overrides") is not None:
            found.append((f"cast.roles[{r}].name_overrides", "structured"))
        if role.get("state_initial_values") is not None:
            found.append((f"cast.roles[{r}].state_initial_values", "states"))
    found += [
        (f"starting_state.states[{i}]", "states") for i in range(len(state.get("states", [])))
    ]
    if state.get("clock") is not None:
        found.append(("starting_state.clock", "clock"))
    found += [
        (f"starting_state.scheduled_events[{i}]", "clock")
        for i in range(len(state.get("scheduled_events", [])))
    ]
    for name in ("predicates", "closed_sets", "rules"):
        found += [
            (f"starting_state.{name}[{i}]", "structured") for i in range(len(state.get(name, [])))
        ]
    found += [
        (f"starting_state.channels[{i}]", "structured")
        for i in range(len(state.get("channels", [])))
    ]
    found += [
        (f"drivers.planned_reveals[{i}].truth", "structured")
        for i in range(len(body.get("drivers", {}).get("planned_reveals", [])))
    ]
    if player:
        found.append(("persona.audience", "partitions"))
    return sorted(found)


def counted(items: Sequence[tuple[str, str]]) -> dict[str, int]:
    """Items per capability (non-zero only)."""
    found: dict[str, int] = {}
    for _, capability in items:
        found[capability] = found.get(capability, 0) + 1
    return found


def expected_inactive(body: Mapping[str, Any], filled: Sequence[str]) -> dict[str, dict[str, Any]]:
    """``stories.settings.inactive`` of a Lite story created from ``body`` with the roles
    ``filled``: every unused setting it sets, and each filled role's participation."""
    defaults = body.get("story_settings_defaults", {})
    found = {
        name: {"value": defaults[name], "capability": capability}
        for name, capability in UNUSED_SETTINGS.items()
        if defaults.get(name) not in (None, [])
    }
    for role in body["cast"]["roles"]:
        if role.get("participation") is not None and role["id"] in filled:
            found[f"participation.{role['id']}"] = {
                "value": role["participation"],
                "capability": "multi_character",
            }
    return found


def deferred_pairs(report: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(field_ref, capability)`` of a ``full`` creation report's deferred items."""
    return sorted((d["field_ref"], d["capability_needed"]) for d in report["deferred"])


def inactive_of(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """A ``full`` creation report's inactive settings as ``{name: {value, capability}}``."""
    return {
        i["name"]: {"value": i["value"], "capability": i["capability"]} for i in report["inactive"]
    }


async def story_table_counts(engine: AsyncEngine) -> dict[str, int]:
    """Rows of every public table with a ``story_id`` column, and of ``stories``: what a
    creation may add to, enumerated from ``information_schema``."""
    found: dict[str, int] = {}
    async with engine.connect() as conn:
        tables = sorted(
            set(
                await conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'story_id'"
                    )
                )
            )
            | {"stories"}
        )
        for table in tables:
            found[table] = int(
                await conn.scalar(text(f'SELECT count(*) FROM "{table}"'))  # noqa: S608
                or 0
            )
    return found


def grown(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    """The tables whose row count changed, with the change."""
    return {t: after[t] - before.get(t, 0) for t in after if after[t] != before.get(t, 0)}


async def deferred_rows(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT field_ref, field_class, location, capability_needed, payload, "
                "operation_id, materialized_at FROM genesis_deferred WHERE story_id = :s "
                "ORDER BY field_ref"
            ),
            {"s": story_id},
        )
        return [dict(r._mapping) for r in rows]


def report_items(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A ``full`` report's deferred items as the table holds them, by field."""
    return sorted(
        (
            {
                "field_ref": d["field_ref"],
                "field_class": d["field_class"],
                "location": d["location"],
                "capability_needed": d["capability_needed"],
                "payload": d["payload"],
            }
            for d in report["deferred"]
        ),
        key=lambda d: d["field_ref"],
    )


def section(system: str, heading: str, *ends: str) -> str:
    """The part of a system prompt from ``heading`` to the first of ``ends`` after it."""
    start = system.index(heading)
    stops = [system.index(end, start + len(heading)) for end in ends if end in system[start + 1 :]]
    return system[start : min(stops)] if stops else system[start:]


def copy_of(body: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(body))


__all__ = [
    "BELIEF_HEADING",
    "DIRECTOR_HEADING",
    "LABEL_PHRASE",
    "NEVER_DEFERRED",
    "PREVAIL",
    "PUBLIC_PHRASE",
    "PUBLIC_PHRASES",
    "SCENARIO_HEADING",
    "STYLE_PHRASE",
    "UNUSED_SETTINGS",
    "copy_of",
    "counted",
    "deferred_pairs",
    "deferred_rows",
    "expected_deferred",
    "expected_inactive",
    "grown",
    "inactive_of",
    "report_items",
    "section",
    "story_table_counts",
    "sunday_dinner",
]
