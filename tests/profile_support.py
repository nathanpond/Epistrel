"""Helpers for the engine-profile tests (#106, #109): request bodies, settings documents built
from the schema's own module registry, the documented dependency list, and what the impact
report and fingerprint tests compare.

Module and level names come from ``epistrel.core.profiles.schema`` (``MODULES``,
``DELIVERED``), never from literals, so a milestone part can name every module, delivered or
not, without using a later milestone's capability terms (the lexicon, docs/04 §13). For the
same reason the impact report's re-derivation key and the quality statuses a later milestone
produces are named here (``REDERIVATION``, ``QUALITY_STATUSES``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from itertools import combinations, product
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from tools.gates.spec import expand_ids

from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import CAPABILITY_MAP
from epistrel.core.profiles.schema import DependencyRule, Module, Requirement
from epistrel.core.seeds.profiles import LITE_PRESET_ID, STRUCTURED_M4_PROFILE_ID
from tests.conftest import REPO_ROOT
from tests.event_support import execute_in_tx, rows, scalar

PROFILES = "/engine-profiles"
SEEDED_PROFILES = [STRUCTURED_M4_PROFILE_ID, LITE_PRESET_ID]
"""The profiles every database at head holds, as a listing orders them (newest first: equal
creation times, so by id descending): the system-owned ``Structured (M4)`` and the Lite
preset."""
SETTINGS: dict[str, Any] = {
    "memory": {"rolling_summary": {"window_messages": 24, "compress_threshold_tokens": 3_000}},
    "pins": {"max_pinned": 20},
    "budgets": {"deadline_ms": 30_000},
    "models": {"generation": {"temperature": 0.6, "response_length": "short"}},
}
"""A user profile's settings: delivered modules only, a subset of their dials."""
EDITED: dict[str, Any] = {
    "memory": {"rolling_summary": {"window_messages": 12}},
    "budgets": {"model_calls": 14, "deadline_ms": 20_000},
}


def profile_body(name: str = "Brisk Lite", **fields: Any) -> dict[str, Any]:
    """A ``POST /engine-profiles`` body (``SETTINGS`` unless ``settings`` is given)."""
    return {"name": name, "settings": fields.pop("settings", SETTINGS), **fields}


def nest(path: str, value: Any) -> dict[str, Any]:
    """A settings document that sets one dotted path."""
    document: dict[str, Any] = {}
    current = document
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value
    return document


def merged(*documents: dict[str, Any]) -> dict[str, Any]:
    """The documents merged key by key (later ones win)."""
    out: dict[str, Any] = {}
    for document in documents:
        for key, value in document.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = merged(out[key], value)
            else:
                out[key] = value
    return out


def on_value(module: Module, level: str | None = None) -> Any:
    """A value of ``module``'s switch that turns it on (at ``level``, when named)."""
    if module.positive:
        return 1
    for value, named in module.levels:
        if level is None or named == level:
            return value
    raise AssertionError(f"{module.name} has no level {level}")


def off_value(module: Module) -> Any:
    """A value of ``module``'s switch that turns it off (the pipeline is never off: ``lite``)."""
    if module.positive:
        return 0
    if module.name == "pipeline":
        return "lite"
    options = schema.allowed_values(module.switch or "") or [False]
    return next(value for value in options if module.level(value) is None)


M3_DELIVERED: dict[str, frozenset[str]] = {
    "pipeline": frozenset({"lite"}),
    "rolling_summary": frozenset({schema.ON}),
    "pins": frozenset({schema.ON}),
    "polish": frozenset({schema.ON}),
    "budgets": frozenset({schema.ON}),
    "models": frozenset({schema.ON}),
}
"""What M3 delivered (#106): its parts assert what still holds of these as later milestones
deliver more."""


def undelivered_modules() -> list[Module]:
    """Every module this deployment has not delivered at any level."""
    return [m for m in schema.MODULES if m.name not in schema.DELIVERED]


def undelivered_levels() -> list[tuple[Module, str]]:
    """Every (delivered module, level) pair that is not delivered."""
    return [
        (m, level)
        for m in schema.MODULES
        if m.name in schema.DELIVERED
        for level in m.level_names
        if level not in schema.DELIVERED[m.name]
    ]


def naming(module: Module, level: str | None = None) -> dict[str, Any]:
    """A settings document that turns ``module`` on (at ``level``)."""
    assert module.switch is not None, module.name
    return nest(module.switch, on_value(module, level))


def undelivered() -> dict[str, Any]:
    """A settings document that switches on the first module (in schema order) this
    deployment has not delivered at any level: what a test may send as settings that cannot
    run, whichever milestone it is."""
    return naming(undelivered_modules()[0])


def undelivered_finding() -> dict[str, Any]:
    """The ``undelivered`` entry a refusal of ``undelivered()`` names."""
    module = undelivered_modules()[0]
    assert module.switch is not None, module.name
    return {
        "module": module.name,
        "level": module.level(on_value(module)),
        "setting": module.switch,
    }


def holding(requirement: Requirement) -> dict[str, Any]:
    """A document under which ``requirement`` holds."""
    module = schema.MODULE[requirement.module]
    return naming(module, requirement.levels[0] if requirement.levels else None)


def breaking(requirement: Requirement) -> dict[str, Any]:
    """A document under which ``requirement`` does not hold."""
    module = schema.MODULE[requirement.module]
    assert module.switch is not None, module.name
    return nest(module.switch, off_value(module))


def violating(rule: DependencyRule) -> dict[str, Any]:
    """Settings that switch ``rule``'s module on (and its condition) with every requirement off."""
    subject = holding(Requirement(rule.module, rule.levels))
    condition = holding(rule.when) if rule.when is not None else {}
    return merged(*(breaking(r) for r in rule.requires), subject, condition)


def satisfying(rule: DependencyRule) -> dict[str, Any]:
    """``violating(rule)`` with every requirement switched on."""
    return merged(violating(rule), *(holding(r) for r in rule.requires))


DOCUMENTED = re.compile(r"^(\d+)\. (.+)$")


def documented_rules() -> list[str]:
    """The numbered rule list in ``epistrel.core.profiles.schema``'s docstring, in order."""
    found: list[tuple[int, str]] = []
    for line in (schema.__doc__ or "").splitlines():
        match = DOCUMENTED.match(line.strip())
        if match:
            found.append((int(match.group(1)), match.group(2)))
    assert [n for n, _ in found] == list(range(1, len(found) + 1)), "a numbered list, from 1"
    return [text for _, text in found]


def requirement_text(req_id: str = "FR-PROF-4") -> str:
    """docs/03's line defining ``req_id`` (FR-PROF-4 unless named)."""
    spec = (REPO_ROOT / "docs" / "03-requirements-spec.md").read_text(encoding="utf-8")
    lines = spec.splitlines()
    (start,) = [i for i, line in enumerate(lines) if line.startswith(f"- **{req_id} (M).**")]
    found = [lines[start]]
    for line in lines[start + 1 :]:  # a requirement's sub-bullets, indented under it
        if not line.startswith("  "):
            break
        found.append(line)
    return "\n".join(found)


# -- the capability map (#129) --------------------------------------------------------------------

STRUCTURED_DOCUMENT: dict[str, Any] = {"pipeline": "structured"}
"""A settings document that runs the ``structured`` pipeline and nothing else."""

GROUP = re.compile(r"(?:FR|NFR)-[A-Z]+(?:-\d+(?:\.\.\d+)?(?:,\d+(?:\.\.\d+)?)*)?")
FAMILY = re.compile(r"^(?:FR|NFR)-[A-Z]+$")
FLOOR_POINTERS = frozenset({"FR-CONC", "FR-GUARD", "FR-CONC-4", "FR-LITE-5"})
"""What FR-PROF-13's listed clauses name without listing it: the families whose listed parts
follow in parentheses, the Lite read set it calls floor, and the Lite forms it refers to."""


def _units(groups: list[str]) -> set[str]:
    found: set[str] = set()
    for group in groups:
        found.update({group} if FAMILY.match(group) else expand_ids(group))
    return found


def prof_13_units() -> set[str]:
    """Every family and requirement id FR-PROF-13 lists against a module (its text up to the
    floor sentence), less ``FLOOR_POINTERS``."""
    text = requirement_text("FR-PROF-13").split("The **floor**")[0]
    body = text.split("**Capability map.**", 1)[1]
    return _units(GROUP.findall(body)) - FLOOR_POINTERS


def map_units() -> set[str]:
    """Every family and requirement id the capability map's entries list (the floor lists
    none)."""
    return _units([group for entry in CAPABILITY_MAP for group in entry.requirements])


M4_MODULES = frozenset(
    {
        *M3_DELIVERED,
        "episodic",
        "consolidation",
        "verification",
        "contradiction_checking",
        "commitment",
        "repair",
    }
)
"""The modules delivered by the end of M4 (#129): its parts keep asserting their own rules as
later milestones deliver more."""
M5_MODULES = frozenset(
    {
        "partitions",
        "nested_beliefs",
        "lazy_resolution",
        "testimony",
        "perception",
        "multi_character",
    }
)
"""The modules M5 delivers (#181)."""


def delivered_rules(among: frozenset[str] | None = None) -> list[DependencyRule]:
    """The dependency rules whose modules (and condition) are all delivered (and, with
    ``among``, all named in it)."""

    def ok(module: str) -> bool:
        return schema.is_delivered(module) and (among is None or module in among)

    return [
        rule
        for rule in schema.DEPENDENCIES
        if ok(rule.module)
        and all(ok(r.module) for r in rule.requires)
        and (rule.when is None or ok(rule.when.module))
    ]


def covering_array(factors: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """A small set of assignments (one value per factor) in which every pair of values of two
    factors appears together at least once (greedy over the full product)."""
    names = list(factors)
    wanted = {
        (a, va, b, vb) for a, b in combinations(names, 2) for va in factors[a] for vb in factors[b]
    }
    chosen: list[dict[str, Any]] = []
    candidates = [dict(zip(names, values, strict=True)) for values in product(*factors.values())]
    while wanted:
        best = max(candidates, key=lambda c: len(_pairs(c, names) & wanted))
        chosen.append(best)
        wanted -= _pairs(best, names)
    return chosen


def _pairs(assignment: dict[str, Any], names: list[str]) -> set[tuple[str, Any, str, Any]]:
    return {(a, assignment[a], b, assignment[b]) for a, b in combinations(names, 2)}


def covered_pairs(rows: list[dict[str, Any]]) -> Iterator[tuple[str, Any, str, Any]]:
    for row in rows:
        yield from _pairs(row, list(row))


# -- impact reports and fingerprints (#109) ------------------------------------------------------

REDERIVATION = "backfill"
"""An affected story's entry key for re-deriving its data (FR-PROF-7): an M7 term, so parts name
it through this constant."""
QUALITY_STATUSES = ("certified", "uncertified", "stale")
"""FR-PROF-16's quality statuses; M3 produces only the second."""
STATUS_COMPONENT = "QualityStatusValue"
"""The OpenAPI component of the status enum (its name holds a later milestone's term)."""
UNCERTIFIED = {"status": "uncertified"}
UNAVAILABLE_WARNINGS = ["capabilities_gained", "backfill_needed", "upgrade_unavailable"]
"""A report's warnings when a story cannot gain a data-producing capability."""
LITE_CAPABILITIES = ["budgets", "lite", "models", "pins", "polish", "rolling_summary"]
"""What the Lite preset enables: its pipeline by level, and every delivered module it turns on."""
POLISH = "repair.polish.enabled"
"""The polish pass's switch: a delivered module the tests mark data-producing."""


async def configuration_rows(db: AsyncEngine) -> dict[str, Any]:
    """Everything a profile change could write, to compare before and after one that must
    write nothing: profiles and their versions, each story's profile and pin, and the counts of
    operations, idempotency keys and configuration changes with the deployment's revision."""
    return {
        "profiles": [
            tuple(r)
            for r in await rows(
                db,
                "SELECT id, name, current_version, updated_at, deleted_at FROM engine_profiles "
                "ORDER BY id",
            )
        ],
        "versions": [
            tuple(r)
            for r in await rows(
                db, "SELECT profile_id, version FROM engine_profile_versions ORDER BY 1, 2"
            )
        ],
        "stories": [
            tuple(r)
            for r in await rows(
                db,
                "SELECT id, engine_profile_id, engine_profile_version_pin, config_revision, "
                "timeline_revision FROM stories ORDER BY id",
            )
        ],
        "operations": await scalar(db, "SELECT count(*) FROM operations"),
        "keys": await scalar(db, "SELECT count(*) FROM idempotency_keys"),
        "changes": await scalar(db, "SELECT count(*) FROM config_changes"),
        "revision": await scalar(db, "SELECT config_revision FROM deployment_state"),
    }


# -- a story's overrides (#113) -------------------------------------------------------------------

WINDOW = "memory.rolling_summary.window_messages"
"""The verbatim window a Lite prompt keeps (M2's ``lite_window_messages``)."""


async def store_overrides(engine: AsyncEngine, story_id: str, overrides: dict[str, Any]) -> None:
    """Give a story made with the event helpers (no REST call) its overrides of its profile's
    dials, straight into ``story_profile_overrides`` at revision 1."""
    await execute_in_tx(
        engine,
        (
            "INSERT INTO story_profile_overrides (story_id, settings, revision) "
            "VALUES (:s, CAST(:o AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE "
            "SET settings = excluded.settings, revision = story_profile_overrides.revision + 1",
            {"s": story_id, "o": json.dumps(overrides)},
        ),
    )
