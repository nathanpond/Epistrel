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
     ``engine`` (omniscient only), under per-character partitions too and whatever its
     ``kind`` (a ``belief`` or ``nested`` truth is read as canon, never seeded as beliefs;
     #558, #574);
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
from dataclasses import dataclass, field, replace
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import Visibility, commit_fact
from epistrel.core.catalog.report import Entry, EntryLocation, Outcome
from epistrel.core.catalog.scenario_models import WORLD_HOLDER, FieldClass
from epistrel.core.enums import MemoryKind, RecordProvenance
from epistrel.core.errors import InvalidRequest
from epistrel.core.events.operation import Operation
from epistrel.core.genesis import epistemic, merge
from epistrel.core.genesis.epistemic import AuthorAct, EpistemicPlan, MemberMemory, SeededBelief
from epistrel.core.genesis.materialize import PARTITIONS, STRUCTURED
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
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, LOCATED_IN
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
from epistrel.core.verify.seeds import CANON, SeedWorld, read_seed, seed_facts, seed_of
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
_LOCATION = re.compile(r"^world\.locations\[\d+\]$")
_CHANNEL = re.compile(r"^starting_state\.channels\[\d+\]$")
PERSONA_ITEM: Final = "persona.audience"
LIMITED_KINDS: Final = frozenset({"belief", "nested"})


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
    is_player: bool = False


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
    epistemic: EpistemicPlan | None = None
    """The partitions branch's beliefs, memories and opening exposures (#173; ``None``: the
    story keeps no partitions)."""
    aliases: dict[str, str] = field(default_factory=dict)
    """A duplicate fact's merge key → the key of the one fact written for it (#173)."""
    settings: Any = None
    """The story's effective settings (what the belief writes check their modules against)."""

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
    partitions: bool = False
    nested: bool = False
    groups: Mapping[str, Sequence[str]] = field(default_factory=dict)
    roles: epistemic.Cast | None = None
    beliefs: list[tuple[_Item, SeededBelief]] = field(default_factory=list)
    member_memories: list[tuple[_Item, MemberMemory]] = field(default_factory=list)
    epistemic: EpistemicPlan = field(default_factory=EpistemicPlan)

    def believe(self, tracked: _Item, belief: SeededBelief) -> None:
        n = sum(1 for _, b in self.beliefs if b.act == belief.act and b.holder == belief.holder)
        key = belief.key or f"{belief.act}@{belief.holder}#{n}"
        self.beliefs.append((tracked, replace(belief, key=key)))

    def consumed(self, item: DeferredItem) -> bool:
        """Whether this genesis materializes ``item``'s capability."""
        return item.capability_needed == STRUCTURED or (
            self.partitions and item.capability_needed == PARTITIONS
        )

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


def _cast(build: _Builder) -> epistemic.Cast:
    """The roles as audiences resolve them (#173): filled roles, the player's, the scenario's
    named groups and every relationship's two roles."""
    if build.roles is None:
        links: list[tuple[str, str]] = []
        for item in build.items:
            value = _value(item)
            role = item.location.role
            if _RELATIONSHIP.match(item.field_ref) and isinstance(value, Mapping) and role:
                links.append((role, str(value.get("to_role"))))
        build.roles = epistemic.Cast.of(
            {r: m.character_id for r, m in build.by_role.items()},
            players=[m.role_id for m in build.cast if m.is_player],
            groups=build.groups,
            relationships=links,
        )
    return build.roles


def _audience(build: _Builder, value: Mapping[str, Any]) -> epistemic.Audience:
    holder_role = value.get("holder")
    role = holder_role if isinstance(holder_role, str) and holder_role != WORLD_HOLDER else None
    return epistemic.resolve([str(a) for a in value.get("audience") or ()], role, _cast(build))


def _seed_visibility(
    build: _Builder, value: Mapping[str, Any], holder: str | None, item: DeferredItem
) -> Visibility:
    if item.field_ref.startswith("drivers."):
        return Visibility(scope="engine")
    audience = [str(a) for a in value.get("audience") or ()]
    if value.get("disclosure") != "secret" and SHARED & set(audience):
        return Visibility(scope="objective" if holder is None else "public")
    holder_role = value.get("holder")
    del holder_role
    named = set(_audience(build, value).characters)
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
        if item.field_ref.startswith("drivers.") and kind in LIMITED_KINDS:
            # #574: a planned reveal's truth stays with the director until a reveal beat (C-25
            # M5, #558), whatever its kind: it is an engine-scoped canon fact, never a belief.
            kind = CANON
        holder_role = value.get("holder")
        holder = (
            None if holder_role in (None, WORLD_HOLDER) else _characters(item).get(str(holder_role))
        )
        if build.partitions and kind in LIMITED_KINDS and holder is not None:
            _belief_seed(build, item, tracked, value, holder, kind)
            continue
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
        elif build.partitions and visibility.scope == "characters" and build.consumed(item):
            _canon_beliefs(build, item, tracked, value, holder, layer, keys, text)
            shared = True  # every member holds it: the item is materialized
        if build.consumed(item) and (keys or shared):
            build.plan.materialized.add(item.field_ref)
        else:
            tracked.add("deferred")


def _act(build: _Builder, item: DeferredItem, value: Mapping[str, Any], holder: str | None) -> str:
    """The item's author act (one ``seed_materialized`` event), keyed by its field ref."""
    build.epistemic.act(
        AuthorAct(
            field_ref=item.field_ref,
            seed_id=None if value.get("id") is None else str(value.get("id")),
            holder=holder,
            audience=tuple(str(a) for a in value.get("audience") or ()),
            disclosure=None if value.get("disclosure") is None else str(value.get("disclosure")),
        )
    )
    return item.field_ref


def _unfilled(tracked: _Item, audience: epistemic.Audience) -> None:
    if audience.dropped:
        tracked.add(epistemic.UNFILLED)


def _canon_beliefs(
    build: _Builder,
    item: DeferredItem,
    tracked: _Item,
    value: Mapping[str, Any],
    holder: str | None,
    layer: SeedLayer,
    keys: Sequence[str],
    text: str,
) -> None:
    """A limited canon seed's beliefs and memories: a ``known`` belief of each fact in every
    member's partition (the seed's disclosure for its holders, ``open`` for the others)."""
    audience = _audience(build, value)
    _unfilled(tracked, audience)
    act = _act(build, item, value, holder)
    disclosure = str(value.get("disclosure") or "open")
    facts = {f.seed_id: f for f in build.layers[layer] if f.seed_id in keys}
    for member in audience.characters:
        holds = holder is None or member == holder
        for key in keys:
            proposition = facts[key].proposition
            if proposition is None:  # pragma: no cover - a seed's facts are propositions
                continue
            build.believe(
                tracked,
                SeededBelief(
                    act=act,
                    holder=member,
                    proposition=proposition,
                    level=epistemic.Level.KNOWN,
                    disclosure=disclosure if holds else "open",
                    layer=layer,
                    fact_key=key,
                ),
            )
        build.member_memories.append(
            (tracked, MemberMemory(act=act, member=member, text=text, facts=tuple(keys)))
        )


def _belief_seed(
    build: _Builder,
    item: DeferredItem,
    tracked: _Item,
    value: Mapping[str, Any],
    holder: str,
    kind: str,
) -> None:
    """A ``belief`` or ``nested`` seed: beliefs only (see ``core.genesis.epistemic``)."""
    assert build.world is not None  # noqa: S101
    attributed: str | None = None
    attributed_level: epistemic.Level | None = None
    text = str(value.get("text") or "")
    read = text
    if kind == "nested":
        content = value.get("nested")
        if not build.nested or not isinstance(content, Mapping):
            tracked.add("deferred")
            return
        attributed = _characters(item).get(str(content.get("attributed_holder")))
        if attributed is None:
            attributed = next(
                (
                    m.character_id
                    for m in build.cast
                    if m.role_id == content.get("attributed_holder") and not m.is_player
                ),
                None,
            )
        if attributed is None or attributed == holder:
            tracked.add(epistemic.UNFILLED)
            tracked.add("deferred")
            return
        attributed_level = epistemic.level_of(
            content.get("attributed_confidence"), epistemic.Level.KNOWN
        )
        read = str(content.get("proposition") or "")
    reading = read_seed(read, build.world, holder=holder)
    if reading.unresolved or not reading.propositions:
        build.error(
            f"{item.field_ref}.{'nested.proposition' if kind == 'nested' else 'text'}",
            item.location,
            reading.unresolved[0] if reading.unresolved else "no_proposition",
            UNNORMALIZABLE_RULE,
        )
        return
    audience = _audience(build, value)
    _unfilled(tracked, audience)
    act = _act(build, item, value, holder)
    level = epistemic.level_of(value.get("confidence"), epistemic.Level.BELIEVED)
    disclosure = str(value.get("disclosure") or "open")
    for member in audience.characters:
        if member == attributed:
            continue
        for proposition in reading.propositions:
            build.believe(
                tracked,
                SeededBelief(
                    act=act,
                    holder=member,
                    proposition=proposition.model_copy(
                        update={"valid_time": Interval(start=None, end=None)}
                    )
                    if value.get("valid_time") is None
                    else proposition,
                    level=level,
                    disclosure=disclosure if member == holder else "open",
                    layer=SeedLayer.ROLE,
                    attributed=attributed,
                    attributed_level=attributed_level,
                ),
            )
        build.member_memories.append((tracked, MemberMemory(act=act, member=member, text=text)))
    tracked.add("nested" if attributed is not None else "belief")
    build.plan.materialized.add(item.field_ref)


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
        update_: dict[str, Any] = {"valid_time": Interval(start=None, end=None)}
        trust = value.get("trust")
        if isinstance(trust, int | float) and not isinstance(trust, bool):
            # #162's seeded trust: ``testimony.seed_trust`` reads the fact's qualifier.
            update_["qualifiers"] = {**found.qualifiers, "trust": float(trust)}
        proposition = found.model_copy(update=update_)
        keys = build.add_facts(
            tracked, item.field_ref, SeedLayer.WORLD, [proposition], Visibility(scope="public")
        )
        build.plan.materialized.add(item.field_ref)
        if build.partitions:
            _relationship_beliefs(build, item, tracked, value, owner, proposition, keys)


def _relationship_beliefs(
    build: _Builder,
    item: DeferredItem,
    tracked: _Item,
    value: Mapping[str, Any],
    owner: str,
    proposition: Proposition,
    keys: Sequence[str],
) -> None:
    """A ``known`` belief of the relationship for each role that knows it (its ``audience``,
    default its two roles), ``open``."""
    knowing = [str(r) for r in value.get("audience") or (owner, value.get("to_role"))]
    audience = epistemic.resolve(knowing, None, _cast(build))
    if value.get("audience"):  # its two roles by default: the player's is no news
        _unfilled(tracked, audience)
    if not audience.characters:
        return
    act = _act(build, item, {"audience": knowing, "disclosure": "open"}, None)
    for member in audience.characters:
        build.believe(
            tracked,
            SeededBelief(
                act=act,
                holder=member,
                proposition=proposition,
                level=epistemic.Level.KNOWN,
                disclosure="open",
                layer=SeedLayer.WORLD,
                fact_key=keys[0] if keys else None,
            ),
        )


def _topology(build: _Builder) -> None:
    """Each location's ``located_in`` and ``adjacent_to`` (#161's topology; #173): objective
    canon between the places' entities, an adjacency once per pair (the lower id first), its
    barrier the fact's qualifier."""
    pairs: set[tuple[str, str]] = set()
    for item in build.items:
        if not _LOCATION.match(item.field_ref) or item.capability_needed != STRUCTURED:
            continue
        value = _value(item)
        if not isinstance(value, Mapping):
            continue
        place = build.places.get(str(value.get("id")))
        if place is None:
            continue
        found: list[Proposition] = []
        outer = build.places.get(str(value.get("located_in")))
        if value.get("located_in") is not None and outer is not None:
            found.append(
                Proposition(
                    subject_id=place,
                    predicate_id=LOCATED_IN,
                    object_id=outer,
                    valid_time=Interval(start=None, end=None),
                )
            )
        for adjacent in value.get("adjacent_to") or ():
            other = build.places.get(str(adjacent.get("location")))
            if other is None or other == place:
                continue
            lo, hi = sorted((place, other))
            if (lo, hi) in pairs:
                continue
            pairs.add((lo, hi))
            barrier = adjacent.get("barrier")
            found.append(
                Proposition(
                    subject_id=lo,
                    predicate_id=CORE_BY_NAME["adjacent_to"].id,
                    object_id=hi,
                    qualifiers={} if barrier is None else {"barrier": str(barrier)},
                    valid_time=Interval(start=None, end=None),
                )
            )
        if not found:
            continue
        tracked = next(t for t in build.tracked if t.field_ref == item.field_ref)
        build.add_facts(tracked, f"topology:{item.field_ref}", SeedLayer.WORLD, found, Visibility())


def _channels(build: _Builder) -> None:
    """Each open channel (``starting_state.channels``; #161, #173): ``on_channel(character,
    channel id)`` objective canon for each participant filling a role."""
    for item in build.items:
        if not _CHANNEL.match(item.field_ref) or item.capability_needed != STRUCTURED:
            continue
        value = _value(item)
        if not isinstance(value, Mapping):
            continue
        tracked = build.track(item, item.field_ref, item.location)
        chars = _characters(item)
        found = [
            Proposition(
                subject_id=chars[str(role)],
                predicate_id=CORE_BY_NAME["on_channel"].id,
                literal=TypedLiteral(kind=LiteralKind.STR, value=str(value.get("id"))),
                valid_time=Interval(start=None, end=None),
            )
            for role in value.get("participants") or ()
            if str(role) in chars
        ]
        if not found:
            tracked.add("deferred")
            continue
        build.add_facts(tracked, item.field_ref, SeedLayer.WORLD, found, Visibility())
        build.plan.materialized.add(item.field_ref)


def _persona(build: _Builder) -> None:
    """The persona's acquaintances believe its name, exact age and traits (#173); a stranger
    holds none of it. No canon comes from the persona's description."""
    for item in build.items:
        if item.field_ref != PERSONA_ITEM or not build.partitions:
            continue
        value = _value(item)
        player = item.location.role
        if not isinstance(value, Mapping) or player is None or player not in build.by_role:
            continue
        member = build.by_role[player]
        tracked = build.track(item, item.field_ref, item.location)
        cast = _cast(build)
        audience = epistemic.resolve(sorted(cast.acquaintances(player)), None, cast)
        act = _act(build, item, {"audience": [epistemic.ACQUAINTANCES]}, member.character_id)
        traits = epistemic.traits_of(str(value.get("description") or ""))
        age = value.get("age")
        for holder in audience.characters:
            for belief in epistemic.identity_beliefs(
                act,
                holder,
                member.character_id,
                str(value.get("name") or member.name),
                int(age) if isinstance(age, int) else member.true_age,
                traits,
            ):
                build.believe(tracked, belief)
            description = str(value.get("description") or "")
            if description.strip():
                build.member_memories.append(
                    (tracked, MemberMemory(act=act, member=holder, text=description))
                )
        tracked.add("belief" if audience.characters else "deferred")
        build.plan.materialized.add(item.field_ref)


def _opening(build: _Builder, narration: str | None, present: Sequence[str]) -> None:
    """Turn 1's exposures: the narration's propositions, for the present non-player roles."""
    assert build.world is not None  # noqa: S101
    if not build.partitions or not narration:
        return
    witnesses = tuple(
        build.by_role[r].character_id
        for r in dict.fromkeys(present)
        if r in build.by_role and not build.by_role[r].is_player
    )
    reading = read_seed(narration, build.world)
    build.epistemic.opening = reading.propositions
    build.epistemic.witnesses = witnesses if reading.propositions else ()


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
            if build.partitions and member.is_player and name == "description":
                continue  # #173: a persona's description is its acquaintances' beliefs only
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
    settings: Any = None,
    groups: Mapping[str, Sequence[str]] | None = None,
    opening: tuple[str | None, Sequence[str]] = (None, ()),
) -> Plan:
    """The structured materialization of ``items`` (a creation's ``genesis_deferred`` items)
    for ``cast``, with ``placements`` (role id → a world location's scenario id) and whether the
    story keeps episodic memory (see the module docstring). With ``settings`` keeping
    partitions, the partitions branch (``core.genesis.epistemic``) runs too, with the
    scenario's named ``groups`` and its ``opening`` (narration, roles present). Pure."""
    build = _Builder(scenario_id, version, story_id, items, cast, episodic)
    build.by_role = {member.role_id: member for member in cast}
    build.partitions = epistemic.partitions_on(settings)
    build.nested = build.partitions and epistemic.nested_on(settings)
    build.groups = dict(groups or {})
    build.plan.settings = settings
    _entities(build)
    layer = _rules(build)
    entities = [(m.character_id, m.spec) for m in cast] + list(build.plan.entities)
    build.world = SeedWorld.of(build.registry, entities)
    if layer is not None:
        _closed_sets(build, layer)
    _seeds(build)
    _relationships(build)
    _topology(build)
    _channels(build)
    _placements(build, placements or {})
    _backgrounds(build)
    _persona(build)
    _opening(build, *opening)
    result = _merged(build)
    kept = {fact.seed_id for fact in result.kept}
    candidates = [
        (fact.seed_id, fact.proposition, build.visibility[fact.seed_id])
        for layer_facts in build.layers.values()
        for fact in layer_facts
        if fact.seed_id in kept and fact.proposition is not None
    ]
    merged: dict[str, tuple[str, Visibility]] = {k: (k, v) for k, _, v in candidates}
    lost_audience: set[str] = set()
    if build.partitions:
        merged, lost = epistemic.dedupe_facts(candidates)
        for loser, winner in lost:
            lost_audience.add(loser)
            tracked = build.owner.get(loser)
            if tracked is not None:
                tracked.add(epistemic.VISIBILITY_CONFLICT)
                if tracked.loser is None:
                    tracked.winner = seed_of(winner)
                    tracked.loser = seed_of(loser)
                    tracked.rule = epistemic.VISIBILITY_CONFLICT
    for key, proposition, _ in candidates:
        winner, visibility = merged[key]
        tracked = build.owner.get(key)
        if winner != key:
            build.plan.aliases[key] = winner
            if tracked is not None and key not in lost_audience:
                tracked.add("canon")
            continue
        build.plan.facts.append(PlannedFact(key, proposition, visibility))
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
    if build.partitions:
        _beliefs(build, kept, lost_audience)
    _identity(build)
    build.plan.entries = [t.entry() for t in build.tracked if t.outcomes]
    return build.plan


def _beliefs(build: _Builder, kept: set[str], lost_audience: set[str]) -> None:
    """The partitions branch's beliefs after the fact merge: a canon copy whose fact was dropped
    (or whose audience lost) is not written; the rest go through the per-holder merge."""
    candidates = [
        (tracked, b)
        for tracked, b in build.beliefs
        if b.fact_key is None or (b.fact_key in kept and b.fact_key not in lost_audience)
    ]
    owner = {b.key: tracked for tracked, b in candidates}
    beliefs, losses = epistemic.merge_beliefs([b for _, b in candidates], registry=build.registry)
    for loss in losses:
        tracked = owner[loss.loser.key]
        tracked.add("merge_conflict" if loss.code == "merge_conflict" else loss.code)  # type: ignore[arg-type]
        if tracked.loser is None:
            winner = owner[loss.winner.key]
            tracked.winner, tracked.loser, tracked.rule = (
                str(winner.location.seed_id or winner.field_ref),
                str(tracked.location.seed_id or tracked.field_ref),
                loss.rule,
            )
    beliefs = epistemic.numbered(beliefs)
    held = {(b.act, b.holder) for b in beliefs}
    for b in beliefs:
        tracked = owner[b.key]
        tracked.add("nested" if b.attributed is not None else "belief")
    build.epistemic.beliefs = beliefs
    if build.episodic:
        for tracked, memory in build.member_memories:
            if (memory.act, memory.member) not in held:
                continue
            keys = tuple(b.key for b in beliefs if (b.act, b.holder) == (memory.act, memory.member))
            facts = tuple(build.plan.aliases.get(k, k) for k in memory.facts if k in kept)
            build.epistemic.memories.append(replace(memory, beliefs=keys, facts=facts))
            tracked.add("memory")
    used = {b.act for b in beliefs}
    build.epistemic.acts = [a for a in build.epistemic.acts if a.field_ref in used]
    build.plan.epistemic = build.epistemic


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
        facts = [
            written[found.aliases.get(k, k)]
            for k in memory.facts
            if found.aliases.get(k, k) in written
        ]
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
    if found.epistemic is not None:
        await epistemic.write(
            conn,
            story_id,
            operation,
            found.epistemic,
            facts={k: written[found.aliases.get(k, k)] for k in {*written, *found.aliases}},
            settings=found.settings,
            registry=registry,
        )
    if found.materialized:
        capabilities = [STRUCTURED] if found.epistemic is None else [STRUCTURED, PARTITIONS]
        await conn.execute(
            update(genesis_deferred)
            .where(
                genesis_deferred.c.story_id == story_id,
                genesis_deferred.c.capability_needed.in_(capabilities),
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
