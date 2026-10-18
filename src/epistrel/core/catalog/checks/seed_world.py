"""The publish-time reading of a scenario's knowledge (FR-SCN-5, FR-PROP-12; #149): what the
seed-normalization and fact-consistency checks share, built once per validation.

At publish no story exists, so the seeds are read against an **ephemeral** world
(``core.verify.seeds.SeedWorld``):

- **the registry**: the core vocabulary and the predicates the scenario declares, typed by
  #131's ``ScenarioRules`` and built by ``author_layer`` exactly as structured genesis registers
  them. A declaration genesis would refuse (a name a core predicate has, a predicate declared
  twice, a rule or closed set naming a predicate that is neither declared nor core, a rule
  cycle, a field the typed declaration does not have) is a ``seed_unnormalizable`` error at that
  field;
- **the entities**: each world location and entity (#139's ``scenario_entity_specs``); each role
  a ``locked`` or ``default`` character fills, as that character (the version the role uses: its
  catalog aliases, the role's name overrides); and each other role (an ``open`` one, the
  player's) as a placeholder named by its public label, with its role id and name overrides as
  aliases. Every role entity also answers to its role id ("the mother").

``World.readings`` holds each seed's ``SeedReading`` by its index in ``ScenarioDraft.seeds``
order, and ``World.backgrounds`` each filled character's definition prose read with the
character as its holder (the character layer of the fact merge).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from epistrel.core.catalog.checks.base import Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import filled
from epistrel.core.catalog.checks.character_documents import name_of
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.scenario_models import WORLD_HOLDER, ScenarioDraft, Seed
from epistrel.core.catalog.scenario_rules import dotted
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.registry import (
    Registry,
    ScenarioRules,
    author_layer,
    core_registry,
)
from epistrel.core.resolution.entities import (
    EntitySpec,
    character_spec,
    scenario_entity_specs,
)
from epistrel.core.verify.seeds import CANON, SeedReading, SeedWorld, read_seed

MEMO: Final = "seed_world"
UNNORMALIZABLE: Final = "seed_unnormalizable"
ROLE_ALIAS: Final = "role"
PLAYER: Final = "player"
BACKGROUND_FIELDS: Final = ("appearance", "personality", "values_motivations", "background_flavor")
"""A character definition's free-text fields: the character layer of the fact merge."""


@dataclass(frozen=True, slots=True)
class RuleProblem:
    """A story-rule declaration structured genesis would refuse: its field and why."""

    field: str
    reason: str
    message: str


@dataclass(slots=True)
class World:
    """What one validation read of the scenario's knowledge (see the module docstring)."""

    draft: ScenarioDraft
    seed_world: SeedWorld
    holders: Mapping[str, str]
    """Role id → the role's entity id."""
    open_npc: bool
    """Whether a non-player role waits for its character until creation (an ``open`` role, or
    one naming no character): a name only that character may have is resolvable only against
    the eventual cast."""
    rule_problems: list[RuleProblem] = field(default_factory=list)
    readings: list[tuple[tuple[str | int, ...], Seed, SeedReading]] = field(default_factory=list)
    backgrounds: list[tuple[int, str, SeedReading]] = field(default_factory=list)
    """``(role index, definition field, reading)`` of each filled character's prose."""


def is_canon(seed: Seed) -> bool:
    return (seed.kind or CANON) == CANON


def _rules(document: Mapping[str, Any], problems: list[RuleProblem]) -> Registry:
    state = document.get("starting_state")
    try:
        rules = ScenarioRules.parse(state if isinstance(state, Mapping) else {})
        layer = author_layer(None, rules, core_registry())
    except InvalidRequest as exc:
        details = exc.details or {}
        problems.append(
            RuleProblem(
                field=str(details.get("field") or "starting_state"),
                reason=str(details.get("reason") or "invalid"),
                message=exc.message,
            )
        )
        return core_registry()
    return layer.registry(core_registry())


def _slug_name(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").strip()


async def world_of(subject: Subject, ctx: ValidationContext) -> World:
    """The validation's ``World``, built on first use (see the module docstring)."""
    found = subject.memo.get(MEMO)
    if isinstance(found, World):
        return found
    draft = draft_of(subject)
    problems: list[RuleProblem] = []
    registry = _rules(subject.document, problems)
    by_index = {m.index: m for m in await filled(subject, ctx)}
    entities: list[tuple[str, EntitySpec]] = []
    holders: dict[str, str] = {}
    contents: dict[int, Mapping[str, Any]] = {}
    for index, role in enumerate(draft.cast.roles):
        entity_id = new_ulid()
        holders[role.id] = entity_id
        overrides = None if role.name_overrides is None else role.name_overrides.model_dump()
        member = by_index.get(index)
        content = None if member is None else member.document
        role_alias = (_slug_name(role.id), ROLE_ALIAS)
        if content is not None:
            contents[index] = content
            canonical = (overrides or {}).get("canonical") or name_of(content) or role.public_label
            spec = character_spec(
                entity_id, canonical, catalog_content=content, name_overrides=overrides
            )
        else:
            spec = character_spec(entity_id, role.public_label, name_overrides=overrides)
        entities.append(
            (
                entity_id,
                EntitySpec(
                    kind=spec.kind,
                    canonical_name=spec.canonical_name,
                    aliases=(*spec.aliases, role_alias),
                    gender=spec.gender,
                    descriptors=spec.descriptors,
                    character_id=entity_id,
                ),
            )
        )
    entities.extend((new_ulid(), spec) for spec in scenario_entity_specs(draft.world))
    world = World(
        draft=draft,
        seed_world=SeedWorld.of(registry, entities),
        holders=holders,
        open_npc=any(
            role.kind != PLAYER and (role.fill == "open" or role.character is None)
            for role in draft.cast.roles
        ),
        rule_problems=problems,
    )
    for loc, seed in draft.seeds():
        holder = None if seed.holder in (None, WORLD_HOLDER) else holders.get(seed.holder)
        world.readings.append((loc, seed, read_seed(seed.text, world.seed_world, holder=holder)))
    for index, content in contents.items():
        definition = content.get("definition")
        if not isinstance(definition, Mapping):
            continue
        holder = holders[draft.cast.roles[index].id]
        for name in BACKGROUND_FIELDS:
            text = definition.get(name)
            if isinstance(text, str) and text.strip():
                world.backgrounds.append(
                    (index, name, read_seed(text, world.seed_world, holder=holder))
                )
    subject.memo[MEMO] = world
    return world


def seed_field(loc: tuple[str | int, ...]) -> str:
    return dotted(loc)


__all__ = [
    "BACKGROUND_FIELDS",
    "UNNORMALIZABLE",
    "RuleProblem",
    "World",
    "is_canon",
    "seed_field",
    "world_of",
]
