"""Structured genesis: a scenario's knowledge becomes canon, memories, entities and story rules
(FR-SCN-5,9,11, FR-PROP-4,12; docs/02 §4.10; #149).

A story whose effective configuration runs the ``structured`` pipeline (#129's
``level_at_least(settings, "pipeline", "structured")``) is created like a Lite one: the same
cast, settings, turn 1 and ``genesis_deferred`` rows (#122's ``materialize``, one per item).
Then this module **materializes the rows whose capability the story has** (``structured``),
from each row's ``payload.value`` (the item's original JSON) and ``payload.characters`` (the
story character ids genesis resolved), never re-parsing the scenario, and sets their
``materialized_at`` in the genesis transaction. Rows needing ``partitions``, ``clock`` or
``states`` stay ``NULL``, so the deferral report shrinks as capabilities arrive.
``deferred_items`` reads a story's rows back as ``DeferredItem`` values: the reader a later
switch to ``structured`` before any turn (#150) plans from.

It runs in two steps, so a refusal writes nothing:

1. ``plan`` (pure, before the transaction) decides everything:

   - **entities**: each world location (a ``place``) and entity, from #139's
     ``scenario_entity_specs``, with its id fixed now; a role's name overrides are already its
     character's aliases (#139, every profile), so their rows are only marked materialized;
   - **story rules**: the predicates, closed sets and rules, typed by #131's ``ScenarioRules``
     and laid out by ``author_layer`` with fixed ids (so seeds normalize against them);
   - **seeds** (the world's, each role's, each planned reveal's truth): the seed's text read by
     ``core.verify.seeds.read_seed`` (#148's detectors, then #140's ``normalize``) with its
     holder in the context window. A ``canon`` seed (the default kind) naming an entity the
     cast and the world do not have refuses the creation (422 ``seed_unnormalizable``: the
     author asked for canon). ``belief``, ``nested`` and ``scheduled`` seeds stay deferred;
   - **visibility** from the audience: ``public`` or ``perceivable`` (and not ``secret``) is
     ``objective`` for a world seed and ``public`` for a role's; otherwise the characters it
     names (``self``: the holder; a role id; ``acquaintances``: the roles a relationship links
     to the holder), and ``engine`` when none resolves. A planned reveal's hidden truth is
     ``engine`` (omniscient only) until per-character partitions arrive;
   - **relationships**: each role relationship's name through the registry and #140's alias
     table ("mother_of" is ``parent_of``), between the two roles' characters; one whose other
     role is unfilled or whose name is no registered predicate stays deferred;
   - **the opening placement**: each filled role's ``starting_location``, and the opening's
     place for the roles it has present, as ``located_at`` (#137's scene projections read it);
   - **the character layer**: each character's definition prose (a catalog character's
     appearance, personality, values and background; an ad-hoc character's definition; a
     player's persona description) read with the character as holder;
   - **the fact merge** (U-27, FR-SCN-7): every proposition above, closed sets first, through
     #140's ``merge_layers`` (world → role → character). A losing proposition is not written
     and its item is reported ``merge_conflict {winner, loser, rule}``; ``strict`` refuses the
     creation with ``creation_conflicts`` (the caller's ``refuse``);
   - **memories**: a seed whose visibility is ``objective`` or ``public`` gets one memory with
     ``provenance = seed``, ``semantic`` (its propositions) when it normalized, ``episodic``
     (free text) otherwise; a seed every fact of which lost the merge gets none (it is dropped
     entirely). A role-limited, secret or hidden seed gets none (memories have no
     visibility filter in M4); with ``memory.episodic`` off, the memory is deferred instead
     (a ``genesis_deferred`` row under capability ``episodic``);
   - **names and ages**: each character's ``name`` (``public``) and true ``age`` (``engine``)
     as facts, outside the merge (the catalog's and the persona's values win).

2. ``write`` (a commit step of the genesis operation, after its events) records the entities,
   registers the author layer (``register_author`` with the planned ids), commits each fact
   with ``commit_fact`` (``provenance = seed``, ``authority = author``, never locked), writes
   the seed memories (``core.memory.records.write_seed``; a fact's memory is caused by the
   fact's event, a free-text memory by ``story_created``) and stamps ``materialized_at``.

The director's notes (guidance, hooks, objectives, milestones, endings, a reveal's conditions)
are never read here: they enter the director's context only (FR-SCN-11). Log lines carry
identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import Visibility, commit_fact
from epistrel.core.catalog.report import Entry, EntryLocation, Outcome
from epistrel.core.catalog.scenario_models import WORLD_HOLDER, FieldClass
from epistrel.core.enums import MemoryKind, RecordProvenance
from epistrel.core.errors import InvalidRequest
from epistrel.core.events.operation import Operation
from epistrel.core.genesis import merge
from epistrel.core.genesis.materialize import STRUCTURED
from epistrel.core.genesis.models import DeferredItem, MaterializedEntry, MaterializedOutcome
from epistrel.core.ids import new_ulid
from epistrel.core.memory.records import SeedMemory, write_seed
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from epistrel.core.propositions.registry import (
    AuthorLayer,
    Registry,
    ScenarioRules,
    author_layer,
    core_registry,
    load,
    register_author,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.entities import (
    NARRATOR,
    EntitySpec,
    ResolutionContext,
    scenario_entity_specs,
)
from epistrel.core.tables import events, genesis_deferred
from epistrel.core.verify.merge import MergeResult, SeedFact, SeedLayer
from epistrel.core.verify.normalize import ClaimSketch, normalize
from epistrel.core.verify.rules import ClosedSetRecord
from epistrel.core.verify.seeds import CANON, SeedWorld, read_seed, seed_facts
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

EPISODIC: Final = "episodic"
"""The capability a seed memory waits for when the story keeps no episodic memory (#106's
module name)."""
UNNORMALIZABLE: Final = "seed_unnormalizable"
DEFERRED_KINDS: Final = frozenset({"belief", "nested", "scheduled"})
"""Seed kinds whose structured form needs a capability M4 does not deliver."""
SHARED: Final = frozenset({"public", "perceivable"})
SELF: Final = "self"
ACQUAINTANCES: Final = "acquaintances"
UNNORMALIZABLE_RULE: Final = (
    "a canon seed normalizes to registered predicates with entities of the story (FR-PROP-12)"
)
RULES_RULE: Final = "a story rule declares a predicate the registry can hold (FR-PROP-4)"
BACKGROUND_FIELDS: Final = ("appearance", "personality", "values_motivations", "background_flavor")

_SEED = re.compile(r"(\.seeded_memories\[\d+\]|^drivers\.planned_reveals\[\d+\]\.truth)$")
_RULE = re.compile(r"^starting_state\.(predicates|closed_sets|rules)\[(\d+)\]$")
_RELATIONSHIP = re.compile(r"^cast\.roles\[\d+\]\.relationships\[\d+\]$")


# -- inputs ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CastInput:
    """A story character as the plan reads it: its id (its entity's), its role, its name and
    true age, its entity (#139's ``character_spec``) and its definition prose by field."""

    character_id: str
    role_id: str
    name: str
    true_age: int
    spec: EntitySpec
    background: tuple[tuple[str, str], ...] = ()


def background_of(
    catalog_content: Mapping[str, Any] | None,
    *,
    definition: str | None = None,
    persona_description: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """A character's definition prose, by field: a catalog version's free-text fields, an ad-hoc
    character's definition, or a player's persona description."""
    found: list[tuple[str, str]] = []
    content = catalog_content or {}
    own = content.get("definition")
    if isinstance(own, Mapping):
        for name in BACKGROUND_FIELDS:
            text = own.get(name)
            if isinstance(text, str) and text.strip():
                found.append((name, text))
    elif definition and definition.strip():
        found.append(("definition", definition))
    if persona_description and persona_description.strip():
        found.append(("description", persona_description))
    return tuple(found)


# -- the plan --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedFact:
    """A fact to commit: the merge key it came from, its proposition and visibility."""

    key: str
    proposition: Proposition
    visibility: Visibility


@dataclass(frozen=True, slots=True)
class PlannedMemory:
    """A seed memory to write; ``facts`` are the merge keys of its propositions."""

    kind: MemoryKind
    text: str
    visibility: Visibility
    holder: str | None
    facts: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()


@dataclass(slots=True)
class Plan:
    """What ``write`` records, and what the creation report says of it (see the module
    docstring)."""

    entities: list[tuple[str, EntitySpec]] = field(default_factory=list)
    rules: ScenarioRules = field(default_factory=ScenarioRules)
    predicate_ids: dict[str, str] = field(default_factory=dict)
    references: dict[str, str] = field(default_factory=dict)
    """A closed set's scenario references (role and entity ids) → entity ids."""
    facts: list[PlannedFact] = field(default_factory=list)
    memories: list[PlannedMemory] = field(default_factory=list)
    materialized: set[str] = field(default_factory=set)
    """The ``field_ref`` of each ``structured`` row this genesis materializes."""
    deferred: list[DeferredItem] = field(default_factory=list)
    """Rows this genesis adds: a seed memory waiting for ``episodic``."""
    entries: list[MaterializedEntry] = field(default_factory=list)
    errors: list[Entry] = field(default_factory=list)

    @property
    def conflicts(self) -> int:
        """How many items lost the fact merge (``counts.conflicts``)."""
        return sum(1 for e in self.entries if "merge_conflict" in e.outcomes)


@dataclass(slots=True)
class _Item:
    """One planned item while the plan is built."""

    item: DeferredItem | None
    field_ref: str
    location: EntryLocation
    outcomes: list[MaterializedOutcome] = field(default_factory=list)
    winner: str | None = None
    loser: str | None = None
    rule: str | None = None

    def add(self, outcome: MaterializedOutcome) -> None:
        if outcome not in self.outcomes:
            self.outcomes.append(outcome)

    def entry(self) -> MaterializedEntry:
        return MaterializedEntry(
            field_ref=self.field_ref,
            location=self.location,
            outcomes=list(self.outcomes),
            winner=self.winner,
            loser=self.loser,
            rule=self.rule,
        )


@dataclass(slots=True)
class _Builder:
    scenario_id: str
    version: int
    story_id: str
    items: Sequence[DeferredItem]
    cast: Sequence[CastInput]
    episodic: bool
    plan: Plan = field(default_factory=Plan)
    by_role: dict[str, CastInput] = field(default_factory=dict)
    places: dict[str, str] = field(default_factory=dict)
    """A world location's or entity's scenario id → its entity id."""
    registry: Registry = field(default_factory=core_registry)
    world: SeedWorld | None = None
    layers: dict[SeedLayer, list[SeedFact]] = field(
        default_factory=lambda: {layer: [] for layer in SeedLayer}
    )
    visibility: dict[str, Visibility] = field(default_factory=dict)
    """A merge key → its fact's visibility."""
    owner: dict[str, _Item] = field(default_factory=dict)
    """A merge key → the item it came from."""
    tracked: list[_Item] = field(default_factory=list)
    memories: list[tuple[_Item, PlannedMemory]] = field(default_factory=list)

    def track(self, item: DeferredItem | None, field_ref: str, location: EntryLocation) -> _Item:
        found = _Item(item, field_ref, location)
        self.tracked.append(found)
        return found

    def error(self, field_ref: str, location: EntryLocation, reason: str, rule: str) -> None:
        self.plan.errors.append(
            Entry(
                code=UNNORMALIZABLE,
                location=location.model_copy(update={"field": field_ref}),
                outcome=Outcome.rejected,
                source=reason,
                rule=rule,
            )
        )

    def location(self, *, role: str | None = None, field_ref: str) -> EntryLocation:
        return EntryLocation(
            entry=self.scenario_id, version=self.version, role=role, field=field_ref
        )

    def add_facts(
        self,
        tracked: _Item,
        key: str,
        layer: SeedLayer,
        found: Sequence[Proposition],
        visibility: Visibility,
    ) -> list[str]:
        keys: list[str] = []
        for fact in seed_facts(key, layer, found):
            self.layers[layer].append(fact)
            self.visibility[fact.seed_id] = visibility
            self.owner[fact.seed_id] = tracked
            keys.append(fact.seed_id)
        return keys


def _value(item: DeferredItem) -> Any:
    return item.payload.get("value")


def _characters(item: DeferredItem) -> Mapping[str, str]:
    found = item.payload.get("characters")
    return found if isinstance(found, Mapping) else {}


def _entities(build: _Builder) -> None:
    for item in build.items:
        if item.capability_needed != STRUCTURED:
            continue
        value = _value(item)
        if not isinstance(value, Mapping):
            continue
        if item.field_ref.startswith("world.locations["):
            specs = scenario_entity_specs({"locations": [value]})
        elif item.field_ref.startswith("world.entities["):
            specs = scenario_entity_specs({"entities": [value]})
        elif item.field_ref.endswith(".name_overrides"):
            # Already the character's aliases (#139 writes them in every profile).
            build.track(item, item.field_ref, item.location).add("entity")
            build.plan.materialized.add(item.field_ref)
            continue
        else:
            continue
        for spec in specs:
            entity_id = new_ulid()
            build.plan.entities.append((entity_id, spec))
            build.places[str(value.get("id"))] = entity_id
        build.track(item, item.field_ref, item.location).add("entity")
        build.plan.materialized.add(item.field_ref)


def _rules(build: _Builder) -> AuthorLayer | None:
    lists: dict[str, list[tuple[int, DeferredItem]]] = {
        "predicates": [],
        "closed_sets": [],
        "rules": [],
    }
    for item in build.items:
        match = _RULE.match(item.field_ref)
        if match is not None and item.capability_needed == STRUCTURED:
            lists[match.group(1)].append((int(match.group(2)), item))
    if not any(lists.values()):
        return None
    where = build.location(field_ref="starting_state")
    try:
        rules = ScenarioRules.parse(
            {
                name: [_value(i) for _, i in sorted(found, key=lambda x: x[0])]
                for name, found in lists.items()
            }
        )
        layer = author_layer(build.story_id, rules, core_registry())
    except InvalidRequest as exc:
        details = exc.details or {}
        build.error(
            str(details.get("field") or "starting_state"),
            where,
            str(details.get("reason") or "invalid"),
            RULES_RULE,
        )
        return None
    build.plan.rules = rules
    build.plan.predicate_ids = {p.name: p.id for p in layer.predicates}
    build.registry = layer.registry(core_registry())
    for found in lists.values():
        for _, item in found:
            build.track(item, item.field_ref, item.location).add("rule")
            build.plan.materialized.add(item.field_ref)
    return layer


def _closed_sets(build: _Builder, layer: AuthorLayer) -> None:
    """Each author closed set, resolved to entity ids, as a merge input above the world's
    seeds (an unknown reference refuses the creation)."""
    refs: dict[str, str] = {
        **{role: member.character_id for role, member in build.by_role.items()},
        **build.places,
    }
    for index, closed in enumerate(build.plan.rules.closed_sets):
        field_ref = f"starting_state.closed_sets[{index}]"
        missing = [r for r in closed.entity_refs() if r not in refs]
        if missing:
            build.error(
                field_ref, build.location(field_ref=field_ref), "unresolved_entity", RULES_RULE
            )
            continue
        for ref in closed.entity_refs():
            build.plan.references[ref] = refs[ref]
        key = f"closed:{field_ref}"
        record = ClosedSetRecord(
            id=key,
            predicate_id=layer.names.get(closed.predicate) or CORE_BY_NAME[closed.predicate].id,
            subject_id=refs[closed.subject],
            members=closed.resolved_members(refs.__getitem__),
            key=dict(closed.key),
            valid_from=closed.valid_from,
            valid_to=closed.valid_to,
            key_set=closed.key_set,
        )
        build.layers[SeedLayer.WORLD].append(SeedFact(key, SeedLayer.WORLD, closed_set=record))


def _linked(build: _Builder, role: str) -> set[str]:
    """The roles a relationship links to ``role``, either way (``acquaintances``)."""
    found: set[str] = set()
    for item in build.items:
        if not _RELATIONSHIP.match(item.field_ref):
            continue
        value = _value(item)
        owner = item.location.role
        if not isinstance(value, Mapping) or owner is None:
            continue
        other = str(value.get("to_role"))
        if owner == role:
            found.add(other)
        elif other == role:
            found.add(owner)
    return found


def _seed_visibility(
    build: _Builder, value: Mapping[str, Any], holder: str | None, item: DeferredItem
) -> Visibility:
    if item.field_ref.startswith("drivers."):
        return Visibility(scope="engine")
    audience = [str(a) for a in value.get("audience") or ()]
    if value.get("disclosure") != "secret" and SHARED & set(audience):
        return Visibility(scope="objective" if holder is None else "public")
    named: set[str] = set()
    holder_role = value.get("holder")
    for token in audience:
        if token == SELF and holder is not None:
            named.add(holder)
        elif token == ACQUAINTANCES and isinstance(holder_role, str):
            named.update(
                build.by_role[r].character_id
                for r in _linked(build, holder_role)
                if r in build.by_role
            )
        elif token in build.by_role:
            named.add(build.by_role[token].character_id)
    if not named:
        return Visibility(scope="engine")
    return Visibility(scope="characters", characters=tuple(sorted(named)))


def _seeds(build: _Builder) -> None:
    assert build.world is not None  # noqa: S101 - built before the seeds
    for item in build.items:
        if not _SEED.search(item.field_ref):
            continue
        value = _value(item)
        if not isinstance(value, Mapping):
            continue
        tracked = build.track(item, item.field_ref, item.location)
        kind = str(value.get("kind") or CANON)
        holder_role = value.get("holder")
        holder = (
            None if holder_role in (None, WORLD_HOLDER) else _characters(item).get(str(holder_role))
        )
        if kind in DEFERRED_KINDS:
            tracked.add("deferred")
            continue
        seed_id = str(value.get("id") or item.field_ref)
        text = str(value.get("text") or "")
        visibility = _seed_visibility(build, value, holder, item)
        reading = read_seed(text, build.world, holder=holder)
        if reading.unresolved:
            build.error(
                f"{item.field_ref}.text",
                item.location,
                reading.unresolved[0],
                UNNORMALIZABLE_RULE,
            )
            continue
        layer = SeedLayer.ROLE if holder is not None else SeedLayer.WORLD
        keys = build.add_facts(tracked, seed_id, layer, reading.propositions, visibility)
        shared = visibility.scope in ("objective", "public")
        if shared:
            memory = PlannedMemory(
                kind=MemoryKind.SEMANTIC if keys else MemoryKind.EPISODIC,
                text=text,
                visibility=visibility,
                holder=holder,
                facts=tuple(keys),
                participants=tuple(
                    dict.fromkeys(
                        i
                        for p in reading.propositions
                        for i in (p.subject_id, p.object_id)
                        if i is not None
                    )
                ),
            )
            build.memories.append((tracked, memory))
        if item.capability_needed == STRUCTURED and (keys or shared):
            build.plan.materialized.add(item.field_ref)
        else:
            tracked.add("deferred")


def _relationships(build: _Builder) -> None:
    assert build.world is not None  # noqa: S101
    context = ResolutionContext(index=build.world.index)
    for item in build.items:
        if not _RELATIONSHIP.match(item.field_ref):
            continue
        value = _value(item)
        owner = item.location.role
        tracked = build.track(item, item.field_ref, item.location)
        chars = _characters(item)
        if not isinstance(value, Mapping) or owner is None:
            tracked.add("deferred")
            continue
        subject, other = chars.get(owner), chars.get(str(value.get("to_role")))
        if subject is None or other is None:
            tracked.add("deferred")
            continue
        found = normalize(
            ClaimSketch(
                relation_text=str(value.get("name") or ""), subject_id=subject, object_id=other
            ),
            NARRATOR,
            0,
            registry=build.registry,
            context=context,
            basis=build.world.basis,
        )
        if not isinstance(found, Proposition):
            tracked.add("deferred")
            continue
        proposition = found.model_copy(update={"valid_time": Interval(start=None, end=None)})
        build.add_facts(
            tracked, item.field_ref, SeedLayer.WORLD, [proposition], Visibility(scope="public")
        )
        build.plan.materialized.add(item.field_ref)


def _placements(build: _Builder, placements: Mapping[str, str]) -> None:
    assert build.world is not None  # noqa: S101
    context = ResolutionContext(index=build.world.index)
    for role, place in placements.items():
        member = build.by_role.get(role)
        where = build.places.get(place)
        if member is None or where is None:
            continue
        found = normalize(
            ClaimSketch(
                relation_text="located_at", subject_id=member.character_id, object_id=where
            ),
            NARRATOR,
            0,
            registry=build.registry,
            context=context,
            basis=build.world.basis,
        )
        if not isinstance(found, Proposition):
            continue
        field_ref = f"placement.{role}"
        tracked = build.track(None, field_ref, build.location(role=role, field_ref=field_ref))
        build.add_facts(tracked, field_ref, SeedLayer.WORLD, [found], Visibility(scope="objective"))


def _backgrounds(build: _Builder) -> None:
    assert build.world is not None  # noqa: S101
    for member in build.cast:
        for name, text in member.background:
            reading = read_seed(text, build.world, holder=member.character_id)
            if not reading.propositions:
                continue
            key = f"{member.role_id}:{name}"
            field_ref = f"characters.{member.role_id}.{name}"
            tracked = build.track(
                None, field_ref, build.location(role=member.role_id, field_ref=field_ref)
            )
            build.add_facts(
                tracked, key, SeedLayer.CHARACTER, reading.propositions, Visibility(scope="public")
            )


def _merged(build: _Builder) -> MergeResult:
    result, conflicts = merge.merge_structured(build.layers, registry=build.registry)
    for conflict in conflicts:
        tracked = build.owner.get(conflict.key)
        if tracked is None:  # a closed set lost: rules sit above every seed
            continue
        tracked.add("merge_conflict")
        if tracked.loser is None:
            tracked.winner, tracked.loser, tracked.rule = (
                conflict.winner,
                conflict.loser,
                conflict.rule,
            )
    return result


def _identity(build: _Builder) -> None:
    """Each character's name (``public``) and true age (``engine``), outside the merge."""
    name = CORE_BY_NAME["name"].id
    age = CORE_BY_NAME["age"].id
    for member in build.cast:
        build.plan.facts.append(
            PlannedFact(
                f"name:{member.role_id}",
                Proposition(
                    subject_id=member.character_id,
                    predicate_id=name,
                    literal=TypedLiteral(kind=LiteralKind.STR, value=member.name),
                    valid_time=Interval.of(0, None),
                ),
                Visibility(scope="public"),
            )
        )
        build.plan.facts.append(
            PlannedFact(
                f"age:{member.role_id}",
                Proposition(
                    subject_id=member.character_id,
                    predicate_id=age,
                    literal=TypedLiteral(kind=LiteralKind.INT, value=member.true_age),
                    valid_time=Interval.of(0, None),
                ),
                Visibility(scope="engine"),
            )
        )


def plan(
    scenario_id: str,
    version: int,
    story_id: str,
    items: Sequence[DeferredItem],
    cast: Sequence[CastInput],
    *,
    placements: Mapping[str, str] | None = None,
    episodic: bool = True,
) -> Plan:
    """The structured materialization of ``items`` (a creation's ``genesis_deferred`` items)
    for ``cast``, with ``placements`` (role id → a world location's scenario id) and whether the
    story keeps episodic memory (see the module docstring). Pure."""
    build = _Builder(scenario_id, version, story_id, items, cast, episodic)
    build.by_role = {member.role_id: member for member in cast}
    _entities(build)
    layer = _rules(build)
    entities = [(m.character_id, m.spec) for m in cast] + list(build.plan.entities)
    build.world = SeedWorld.of(build.registry, entities)
    if layer is not None:
        _closed_sets(build, layer)
    _seeds(build)
    _relationships(build)
    _placements(build, placements or {})
    _backgrounds(build)
    result = _merged(build)
    kept = {fact.seed_id for fact in result.kept}
    for fact in (f for layer_facts in build.layers.values() for f in layer_facts):
        if fact.seed_id in kept and fact.proposition is not None:
            build.plan.facts.append(
                PlannedFact(fact.seed_id, fact.proposition, build.visibility[fact.seed_id])
            )
            tracked = build.owner.get(fact.seed_id)
            if tracked is not None:
                tracked.add("canon")
    for tracked, memory in build.memories:
        if memory.facts and not any(k in kept for k in memory.facts):
            continue  # every fact it states lost the merge: dropped entirely (U-27)
        memory = PlannedMemory(
            kind=memory.kind,
            text=memory.text,
            visibility=memory.visibility,
            holder=memory.holder,
            facts=tuple(k for k in memory.facts if k in kept),
            participants=memory.participants,
        )
        if episodic:
            build.plan.memories.append(memory)
            tracked.add("memory")
            continue
        tracked.add("deferred")
        if tracked.item is not None:
            build.plan.deferred.append(
                DeferredItem(
                    field_ref=tracked.item.field_ref,
                    field_class=tracked.item.field_class,
                    location=tracked.item.location,
                    capability_needed=EPISODIC,
                    payload=copy.deepcopy(tracked.item.payload),
                )
            )
    _identity(build)
    build.plan.entries = [t.entry() for t in build.tracked if t.outcomes]
    return build.plan


def by_outcome(entries: Iterable[MaterializedEntry]) -> dict[str, int]:
    """``counts.materialized.by_outcome``."""
    found: dict[str, int] = {}
    for entry in entries:
        for outcome in entry.outcomes:
            found[outcome] = found.get(outcome, 0) + 1
    return found


# -- the write -------------------------------------------------------------------------------


async def _story_created(conn: AsyncConnection, operation_id: str) -> str | None:
    found = await conn.scalar(
        select(events.c.id).where(
            events.c.operation_id == operation_id, events.c.type == "story_created"
        )
    )
    return None if found is None else str(found)


async def write(conn: AsyncConnection, story_id: str, operation: Operation, found: Plan) -> None:
    """Record ``found`` in the genesis operation's commit (see the module docstring)."""
    for entity_id, spec in found.entities:
        await store.add_entity(
            conn,
            story_id,
            spec.kind,
            spec.canonical_name,
            aliases=spec.aliases,
            entity_id=entity_id,
            gender=spec.gender,
            descriptors=spec.descriptors,
            operation_id=operation.operation_id,
        )
    if not found.rules.empty:
        await register_author(
            conn,
            story_id,
            found.rules,
            resolve=lambda ref: found.references.get(ref, ref),
            ids=found.predicate_ids,
        )
    registry = await load(conn, story_id)
    written: dict[str, Any] = {}
    for planned in found.facts:
        written[planned.key] = await commit_fact(
            conn,
            story_id,
            planned.proposition,
            provenance=RecordProvenance.SEED,
            visibility=planned.visibility,
            operation=operation,
            registry=registry,
        )
    origin = await _story_created(conn, operation.operation_id)
    memories: list[SeedMemory] = []
    for memory in found.memories:
        facts = [written[k] for k in memory.facts if k in written]
        causes = tuple(dict.fromkeys(e for f in facts for e in f.source_event_ids))
        if not causes and origin is not None:
            causes = (origin,)
        memories.append(
            SeedMemory(
                kind=memory.kind,
                text=memory.text,
                visibility=memory.visibility,
                source_event_ids=causes,
                holder_character_id=memory.holder,
                proposition_ids=tuple(dict.fromkeys(f.proposition_id for f in facts)),
                participants=memory.participants,
            )
        )
    if memories:
        await write_seed(conn, operation, memories)
    if found.materialized:
        await conn.execute(
            update(genesis_deferred)
            .where(
                genesis_deferred.c.story_id == story_id,
                genesis_deferred.c.capability_needed == STRUCTURED,
                genesis_deferred.c.field_ref.in_(sorted(found.materialized)),
            )
            .values(materialized_at=func.transaction_timestamp())
        )
    log.info(
        "structured genesis",
        story_id=story_id,
        entities=len(found.entities),
        facts=len(found.facts),
        memories=len(memories),
        materialized=len(found.materialized),
        conflicts=found.conflicts,
    )


# -- the reader ------------------------------------------------------------------------------


async def deferred_items(conn: AsyncConnection, story_id: str) -> list[DeferredItem]:
    """A story's ``genesis_deferred`` rows not yet materialized, as ``DeferredItem`` values (in
    ``field_ref`` order): what ``plan`` takes, for a switch to ``structured`` before any turn."""
    rows = await conn.execute(
        select(
            genesis_deferred.c.field_ref,
            genesis_deferred.c.field_class,
            genesis_deferred.c.location,
            genesis_deferred.c.capability_needed,
            genesis_deferred.c.payload,
        )
        .where(
            genesis_deferred.c.story_id == story_id,
            genesis_deferred.c.materialized_at.is_(None),
        )
        .order_by(genesis_deferred.c.field_ref, genesis_deferred.c.id)
    )
    return [
        DeferredItem(
            field_ref=str(row.field_ref),
            field_class=FieldClass(row.field_class),
            location=EntryLocation.model_validate(row.location),
            capability_needed=str(row.capability_needed),
            payload=dict(row.payload),
        )
        for row in rows
    ]


__all__ = [
    "EPISODIC",
    "UNNORMALIZABLE",
    "CastInput",
    "Plan",
    "PlannedFact",
    "PlannedMemory",
    "background_of",
    "by_outcome",
    "deferred_items",
    "plan",
    "write",
]
