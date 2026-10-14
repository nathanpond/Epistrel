"""Materialising a scenario into a Lite story, field class by field class (FR-SCN-9,11;
docs/02 §4.10; #122).

Every field of the scenario document has a class (#108's ``scenario_schema_classes``), and the
class decides what genesis does with it (``HANDLING``):

- ``render``: the Lite prompt carries it as text (``render.py``): the world description, the
  starting places, the roles present at the opening, the director's notes;
- ``render_and_defer``: rendered as text **and** deferred in its structured form, which Lite
  cannot hold: every knowledge seed, location, entity, relationship and name override (a
  planned reveal's hidden truth renders in the director notes only);
- ``defer``: not usable under Lite, kept as a ``genesis_deferred`` row naming the capability it
  needs: the story rules, predicates and closed sets, the State trackers and starting values,
  the starting clock and the scheduled events;
- ``setting``: stored in the story's settings, applied when its capability is delivered, else
  ``inactive`` (``settings_map``);
- ``consume``: read while the story is created and not kept as a story field (slots, eligibility,
  the creation rules, the policies, which become the story's own; the opening narration, which
  becomes turn 1; the recommendation, which #109's impact report reads);
- ``never``: the public card, the labels and the visual style: in no text prompt and never
  deferred (a public card is not story content; the style is M8's image prompts).

``handling_problems`` is the build-time check: a field with no handling, a handling for a field
that does not exist, or a public-card, director, recommendation or style field that would be
deferred, is a problem (``tests/test_genesis_materialize.py`` fails on any).

``materialize`` lists what a creation defers (one ``DeferredItem`` per item, one capability
each) and the settings it leaves inactive, from the scenario's stored document (each payload is
the item's original JSON, with the story character ids genesis resolved) and the resolved cast.
**Capabilities** (#106's module names): a seed is ``structured`` when its audience is
``public`` and it is not secret, ``partitions`` when its audience is limited or it is secret or
a belief, ``clock`` for a scheduled seed; a planned reveal's truth, a relationship, a location,
an entity, a name override and every rule, predicate and closed set need ``structured``; the
clock and scheduled events ``clock``; States and starting values ``states``; the player
persona's audience-limited seeding (``persona.audience``) ``partitions``.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from epistrel.core.catalog.report import EntryLocation
from epistrel.core.catalog.scenario_models import (
    WORLD_HOLDER,
    FieldClass,
    ScenarioDraft,
    Seed,
    Use,
    scenario_schema_classes,
)
from epistrel.core.genesis.models import DeferredItem, InactiveItem
from epistrel.core.genesis.settings_map import (
    MULTI_CHARACTER,
    PARTICIPATION,
    SETTING_CAPABILITIES,
    is_used,
)

STRUCTURED: Final = "structured"
PARTITIONS: Final = "partitions"
CLOCK: Final = "clock"
STATES: Final = "states"
LIMITED_KINDS: Final = frozenset({"belief", "nested"})


class Handling(StrEnum):
    """What genesis does with a scenario field (see the module docstring)."""

    render = "render"
    render_and_defer = "render_and_defer"
    defer = "defer"
    setting = "setting"
    consume = "consume"
    never = "never"


CLASS_HANDLING: Final[Mapping[FieldClass, Handling]] = {
    FieldClass.public_card: Handling.never,
    FieldClass.knowledge_seed: Handling.render_and_defer,
    FieldClass.story_rule: Handling.defer,
    FieldClass.starting_state: Handling.defer,
    FieldClass.director: Handling.render,
    FieldClass.configuration: Handling.consume,
    FieldClass.recommendation: Handling.consume,
    FieldClass.style: Handling.never,
}
"""Each class's handling; ``PATH_HANDLING`` names the fields that differ from their class's."""

PATH_HANDLING: Final[Mapping[str, Handling]] = {
    "world.description": Handling.render,
    "cast.roles[].starting_location": Handling.render,
    "starting_state.opening.starting_location": Handling.render,
    "starting_state.opening.roles_present": Handling.render,
    "starting_state.opening.narration": Handling.consume,
    "starting_state.scheduled_events": Handling.defer,
}
"""Fields handled other than their class: the world description and the starting places have a
complete Lite form (rendered only), the opening narration becomes turn 1, and the scheduled
events have no text form under Lite."""

NEVER_DEFERRED: Final = frozenset(
    {FieldClass.public_card, FieldClass.director, FieldClass.recommendation, FieldClass.style}
)
"""Classes never deferred (FR-SCN-11; docs/04 I-27)."""


def handling_of(path: str) -> Handling:
    """How genesis handles the field at ``path`` (``KeyError`` for an unknown field)."""
    field_class, use = scenario_schema_classes()[path]
    if path in PATH_HANDLING:
        return PATH_HANDLING[path]
    if field_class is FieldClass.configuration and use is Use.story_setting:
        return Handling.setting
    if path == "story_settings_defaults.required_providers":
        return Handling.setting
    return CLASS_HANDLING[field_class]


def handling() -> dict[str, Handling]:
    """Every scenario field's handling, by dotted path."""
    return {path: handling_of(path) for path in scenario_schema_classes()}


def handling_problems() -> list[str]:
    """The build-time check (see the module docstring): ``[]`` when every field is handled."""
    classes = scenario_schema_classes()
    problems: list[str] = [
        f"{path}: a path override names no field" for path in PATH_HANDLING if path not in classes
    ]
    for path, (field_class, _) in classes.items():
        if field_class not in CLASS_HANDLING:
            problems.append(f"{path}: class {field_class} has no handling")
            continue
        found = handling_of(path)
        deferred = found in (Handling.defer, Handling.render_and_defer)
        if field_class in NEVER_DEFERRED and deferred:
            problems.append(f"{path}: a {field_class} field is never deferred")
        if found is Handling.never and field_class not in (
            FieldClass.public_card,
            FieldClass.style,
        ):
            problems.append(f"{path}: only the public card and the style stay out of a story")
    settings = {
        path.removeprefix("story_settings_defaults.")
        for path, found in handling().items()
        if found is Handling.setting and path.startswith("story_settings_defaults.")
    }
    problems.extend(
        f"story_settings_defaults.{name}: a setting with no capability entry"
        for name in sorted(settings - set(SETTING_CAPABILITIES))
    )
    return problems


# -- the items ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CastRole:
    """A filled role as materialisation sees it: its role id, the story character filling it
    and, for the player, the persona copy."""

    role_id: str
    character_id: str
    is_player: bool = False
    persona: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Materialized:
    """What a creation defers and leaves inactive."""

    deferred: tuple[DeferredItem, ...]
    inactive: tuple[InactiveItem, ...]

    def settings(self) -> dict[str, dict[str, Any]]:
        """``stories.settings.inactive``: ``{name: {value, capability}}``."""
        return {
            item.name: {"value": copy.deepcopy(item.value), "capability": item.capability}
            for item in self.inactive
        }


def seed_capability(seed: Seed) -> str:
    """The capability a seed's structured form needs (see the module docstring)."""
    if seed.kind == "scheduled":
        return CLOCK
    if seed.kind in LIMITED_KINDS:
        return PARTITIONS
    if seed.disclosure == "secret" or "public" not in (seed.audience or ()):
        return PARTITIONS
    return STRUCTURED


def _class(pattern: str) -> FieldClass:
    """The class of the item at ``pattern`` (a field, or a group of fields sharing one)."""
    classes = scenario_schema_classes()
    if pattern in classes:
        return classes[pattern][0]
    found = {c for path, (c, _) in classes.items() if path.startswith(f"{pattern}.")}
    if len(found) != 1:
        raise ValueError(f"{pattern}: not one class ({sorted(found)})")
    return found.pop()


def _deferred_path(pattern: str) -> None:
    """Refuse an item whose fields the dispatcher does not defer (the build-time contract)."""
    classes = scenario_schema_classes()
    paths = [p for p in classes if p == pattern or p.startswith(f"{pattern}.")]
    handled = {handling_of(p) for p in paths}
    if not paths or not handled <= {Handling.defer, Handling.render_and_defer}:
        raise ValueError(f"{pattern}: not deferred by the dispatcher")


def _index(document: Any, *path: str | int) -> Any:
    node = document
    for part in path:
        node = node[part]
    return node


def _ref(*parts: str | int) -> str:
    found = ""
    for part in parts:
        if isinstance(part, int):
            found += f"[{part}]"
        else:
            found += f".{part}" if found else part
    return found


@dataclass(slots=True)
class _Builder:
    scenario_id: str
    version: int
    document: Mapping[str, Any]
    characters: Mapping[str, str]
    items: list[DeferredItem]

    def add(
        self,
        pattern: str,
        path: tuple[str | int, ...],
        capability: str,
        *,
        role: str | None = None,
        seed_id: str | None = None,
        roles: Sequence[str | None] = (),
    ) -> None:
        _deferred_path(pattern)
        field_ref = _ref(*path)
        resolved = {r: self.characters[r] for r in roles if r is not None and r in self.characters}
        self.items.append(
            DeferredItem(
                field_ref=field_ref,
                field_class=_class(pattern),
                location=EntryLocation(
                    entry=self.scenario_id,
                    version=self.version,
                    role=role,
                    field=field_ref,
                    seed_id=seed_id,
                ),
                capability_needed=capability,
                payload={
                    "value": copy.deepcopy(_index(self.document, *path)),
                    "characters": resolved,
                },
            )
        )


def _seeds(draft: ScenarioDraft) -> Iterator[tuple[str, tuple[str | int, ...], Seed, str | None]]:
    for index, seed in enumerate(draft.starting_state.seeded_memories):
        yield (
            "starting_state.seeded_memories[]",
            ("starting_state", "seeded_memories", index),
            seed,
            None,
        )
    for role_index, role in enumerate(draft.cast.roles):
        for index, seed in enumerate(role.seeded_memories):
            yield (
                "cast.roles[].seeded_memories[]",
                ("cast", "roles", role_index, "seeded_memories", index),
                seed,
                role.id,
            )


def materialize(
    scenario_id: str,
    version: int,
    document: Mapping[str, Any],
    draft: ScenarioDraft,
    cast: Sequence[CastRole],
) -> Materialized:
    """What a creation of ``draft`` (the stored ``document``, the creation's overrides applied
    to ``draft`` only) with ``cast`` defers and leaves inactive (see the module docstring)."""
    characters = {member.role_id: member.character_id for member in cast}
    build = _Builder(scenario_id, version, document, characters, [])
    world = draft.world
    for index, _ in enumerate(world.locations):
        build.add("world.locations[]", ("world", "locations", index), STRUCTURED)
    for index, _ in enumerate(world.entities):
        build.add("world.entities[]", ("world", "entities", index), STRUCTURED)
    for pattern, path, seed, holder_role in _seeds(draft):
        holder = seed.holder if seed.holder != WORLD_HOLDER else None
        build.add(
            pattern,
            path,
            seed_capability(seed),
            role=holder_role,
            seed_id=seed.id,
            roles=[holder, *(seed.audience or ())],
        )
    for role_index, role in enumerate(draft.cast.roles):
        where: tuple[str | int, ...] = ("cast", "roles", role_index)
        for index, link in enumerate(role.relationships):
            build.add(
                "cast.roles[].relationships[]",
                (*where, "relationships", index),
                STRUCTURED,
                role=role.id,
                roles=[role.id, link.to_role],
            )
        if role.name_overrides is not None:
            build.add(
                "cast.roles[].name_overrides",
                (*where, "name_overrides"),
                STRUCTURED,
                role=role.id,
                roles=[role.id],
            )
        if role.state_initial_values is not None:
            build.add(
                "cast.roles[].state_initial_values",
                (*where, "state_initial_values"),
                STATES,
                role=role.id,
                roles=[role.id],
            )
    state = draft.starting_state
    for index, _ in enumerate(state.states):
        build.add("starting_state.states", ("starting_state", "states", index), STATES)
    if state.clock is not None:
        build.add("starting_state.clock", ("starting_state", "clock"), CLOCK)
    for index, _ in enumerate(state.scheduled_events):
        build.add(
            "starting_state.scheduled_events", ("starting_state", "scheduled_events", index), CLOCK
        )
    for name in ("predicates", "closed_sets", "rules"):
        for index, _ in enumerate(getattr(state, name)):
            build.add(f"starting_state.{name}", ("starting_state", name, index), STRUCTURED)
    for index, reveal in enumerate(draft.drivers.planned_reveals):
        holder = reveal.truth.holder if reveal.truth.holder != WORLD_HOLDER else None
        build.add(
            "drivers.planned_reveals[].truth",
            ("drivers", "planned_reveals", index, "truth"),
            STRUCTURED,
            seed_id=reveal.truth.id,
            roles=[holder, *(reveal.truth.audience or ()), *reveal.allowed_roles],
        )
    for member in cast:
        if member.is_player and member.persona is not None:
            build.items.append(_persona_item(scenario_id, version, draft, member, characters))
    return Materialized(
        deferred=tuple(build.items), inactive=tuple(_inactive(scenario_id, version, draft, cast))
    )


def _persona_item(
    scenario_id: str,
    version: int,
    draft: ScenarioDraft,
    member: CastRole,
    characters: Mapping[str, str],
) -> DeferredItem:
    """The player persona's audience-limited seeding: its name and exact age known to the roles
    its relationships link it with, its gender and age band perceivable by others."""
    linked = sorted(
        {
            role.id
            for role in draft.cast.roles
            if role.id != member.role_id
            and (
                any(link.to_role == member.role_id for link in role.relationships)
                or any(
                    link.to_role == role.id
                    for own in draft.cast.roles
                    if own.id == member.role_id
                    for link in own.relationships
                )
            )
        }
    )
    persona = dict(member.persona or {})
    return DeferredItem(
        field_ref="persona.audience",
        field_class=FieldClass.knowledge_seed,
        location=EntryLocation(
            entry=scenario_id, version=version, role=member.role_id, field="persona.audience"
        ),
        capability_needed=PARTITIONS,
        payload={
            "value": persona,
            "characters": {member.role_id: member.character_id}
            | {r: characters[r] for r in linked if r in characters},
            "known_to": linked,
        },
    )


def _inactive(
    scenario_id: str, version: int, draft: ScenarioDraft, cast: Sequence[CastRole]
) -> Iterator[InactiveItem]:
    defaults = draft.story_settings_defaults
    for name, capability in SETTING_CAPABILITIES.items():
        if is_used(capability):
            continue
        value = getattr(defaults, name)
        if value is None or value == []:
            continue
        assert capability is not None  # noqa: S101 - is_used(None) is true
        yield InactiveItem(
            name=name,
            value=value,
            capability=capability,
            field_class=FieldClass.configuration,
            location=EntryLocation(
                entry=scenario_id, version=version, field=f"story_settings_defaults.{name}"
            ),
        )
    filled = {member.role_id for member in cast}
    if is_used(MULTI_CHARACTER):
        return
    for index, role in enumerate(draft.cast.roles):
        if role.participation is None or role.id not in filled:
            continue
        yield InactiveItem(
            name=f"{PARTICIPATION}.{role.id}",
            value=role.participation,
            capability=MULTI_CHARACTER,
            field_class=FieldClass.configuration,
            location=EntryLocation(
                entry=scenario_id,
                version=version,
                role=role.id,
                field=f"cast.roles[{index}].participation",
            ),
        )


__all__ = [
    "CLASS_HANDLING",
    "CLOCK",
    "NEVER_DEFERRED",
    "PARTITIONS",
    "PATH_HANDLING",
    "STATES",
    "STRUCTURED",
    "CastRole",
    "Handling",
    "Materialized",
    "handling",
    "handling_of",
    "handling_problems",
    "materialize",
    "seed_capability",
]
