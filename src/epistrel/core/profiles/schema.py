"""An engine profile's settings: the module schema, its tags, what is delivered, and the
dependency graph (FR-PROF-2,3,4,6,16; FR-CONC-15; docs/02 §4.11; #106).

**One full model.** ``ProfileSettings`` holds every capability module FR-PROF-2 names, delivered
or not: the pipeline, memory (the rolling summary, episodic retrieval, consolidation, hot-set
budgets), pinned memories, character knowledge, perception, verification, commitment, the world
clock, States, multi-character rendering, repair and polish, budgets, and models (per-purpose
routing and the generation defaults). Every module defaults to off, so a profile names only
what it changes. A stored profile version may be sparse: it is validated over the model's
defaults (#113 replaces them with the deployment's ``profile_defaults``).

**Three tags on every setting.** Each leaf is declared
``Annotated[<type>, Tag.<behavioral|data_producing>, ConfigClass.<interpretive|prospective>,
Quality.<relevant|irrelevant>]``:

- ``Tag`` follows FR-PROF-6: a *data-producing* setting enables or raises a module that writes
  structured data (partitions, nested beliefs, perception routing, extraction, commitment, the
  clock, States, consolidation, and the switch from ``lite`` to ``structured``). Every other
  setting is *behavioral* (thresholds, gates, budgets, models, generation parameters, repair
  limits).
- ``ConfigClass`` follows FR-CONC-15 (#64): what changes how already-generated work is verified,
  routed or committed is *interpretive*; models, budgets, thresholds and everything else that
  only affects later work is *prospective*. Every setting delivered in M3 is prospective.
- ``Quality`` follows FR-PROF-16: module levels, thresholds and gates, budgets, models,
  generation parameters, the rolling summary's dials and repair limits are *relevant* to a
  configuration's fingerprint; pinned memories are not.

``missing_tags`` is the build-time check (``test_u_28__schema``): it names every leaf without
exactly one tag of each kind.

**Modules and levels.** ``MODULES`` names each capability module (the names #109 reports as
capabilities and #122's genesis defers by), the setting that switches it, the level each value
of that setting means, and the settings it owns. ``DELIVERED`` maps each module this deployment
has delivered to its delivered levels; in M3 that is the ``lite`` pipeline, the rolling summary,
pinned memories, the polish pass, budgets and models. M4 to M6 flip entries here and nothing
else. ``delivered_schema`` derives, from the full model, the JSON Schema of only the delivered
modules and levels: the OpenAPI document and ``GET /engine-profiles/schema`` publish that one.

**The dependency graph (FR-PROF-4).** ``DEPENDENCIES`` declares, as data, every rule FR-PROF-4
states, delivered or not. ``FR_PROF_4`` holds each clause of the requirement verbatim, and each
rule names the clause it comes from. The clause "every module except the rolling summary, the
prompt-only clock and States of FR-LITE-5, budgets, and models requires the ``structured``
pipeline" gives one rule per module it constrains. It does not constrain the pipeline itself,
pinned memories (which FR-LITE-3 makes available under every profile), or the polish pass, whose
Lite form is the bounded regeneration of a reply that broke an output policy (FR-GUARD-6,
ADR-085). The rules, in ``DEPENDENCIES`` order (``test_u_28__schema`` compares this list with
it):

1. episodic requires pipeline=structured
2. consolidation requires pipeline=structured
3. partitions requires pipeline=structured
4. nested_beliefs requires pipeline=structured
5. lazy_resolution requires pipeline=structured
6. testimony requires pipeline=structured
7. perception requires pipeline=structured
8. verification requires pipeline=structured
9. contradiction_checking requires pipeline=structured
10. commitment requires pipeline=structured
11. clock=on requires pipeline=structured
12. time_claims requires pipeline=structured
13. scheduled_stops requires pipeline=structured
14. states=on requires pipeline=structured
15. multi_character requires pipeline=structured
16. repair requires pipeline=structured
17. partitions requires perception=co_location|full
18. perception requires partitions
19. nested_beliefs requires partitions
20. lazy_resolution requires partitions
21. testimony requires partitions
22. time_claims requires clock, verification
23. scheduled_stops requires clock
24. clock requires commitment when pipeline=structured
25. contradiction_checking requires verification=declared|extraction|entailment
26. consolidation requires episodic
"""

from __future__ import annotations

import copy
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema
from pydantic.fields import FieldInfo

from epistrel.core.settings_schema import (
    DEFAULT_GUARDRAIL_RETRY_BUDGET,
    DEFAULT_LITE_WINDOW_MESSAGES,
    DEFAULT_MAX_PINNED_MEMORIES,
    DEFAULT_TURN_DEADLINE_S,
    MAX_CALLS_PER_TURN,
    MAX_GUARDRAIL_RETRY_BUDGET,
    MAX_LENGTH_TOKENS,
    MAX_LITE_SUMMARY_MAX_TOKENS,
    MAX_LITE_WINDOW_MESSAGES,
    MAX_PINNED_MEMORIES_LIMIT,
    MAX_TURN_DEADLINE_S,
    MIN_CALLS_PER_TURN,
    MIN_LITE_SUMMARY_MAX_TOKENS,
    ConfigClass,
    Length,
    leaves,
)


class Tag(StrEnum):
    """FR-PROF-6: whether changing a setting changes what is stored."""

    behavioral = "behavioral"
    data_producing = "data_producing"


class Quality(StrEnum):
    """FR-PROF-16: whether a setting is part of a configuration's fingerprint."""

    relevant = "relevant"
    irrelevant = "irrelevant"


BEHAVIORAL = Tag.behavioral
DATA_PRODUCING = Tag.data_producing
INTERPRETIVE = ConfigClass.interpretive
PROSPECTIVE = ConfigClass.prospective
RELEVANT = Quality.relevant
IRRELEVANT = Quality.irrelevant

ON = "on"
"""The level of a module that is simply on (a boolean switch, or budgets and models)."""

DEFAULT_COMPRESS_THRESHOLD_TOKENS = 6_000
"""The Lite preset's rolling-summary threshold (#106's AC1). #113 maps it onto M2's
``lite_summary_max_tokens`` (the merge cap, whose M2 default is 2,000)."""
DEFAULT_DEADLINE_MS = int(DEFAULT_TURN_DEADLINE_S * 1000)
MAX_DEADLINE_MS = int(MAX_TURN_DEADLINE_S * 1000)
MODEL_NAME_MAX = 200
PURPOSES_MAX = 50
PURPOSE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
"""A model purpose's name as ``models.purposes`` keys it (``generation``, ``guardrail_check``)."""

PurposeName = Annotated[str, Field(pattern=PURPOSE_PATTERN)]


class ProfileModel(BaseModel):
    """Base of every part of ``ProfileSettings``: closed, immutable, documented."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)


# -- memory ---------------------------------------------------------------------------------------


class RollingSummary(ProfileModel):
    """The rolling auto-summary (FR-LITE-2)."""

    enabled: Annotated[bool, BEHAVIORAL, PROSPECTIVE, RELEVANT] = True
    """Fold messages older than the window into summary segments, and show the summary in the
    prompt."""
    window_messages: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=DEFAULT_LITE_WINDOW_MESSAGES, ge=1, le=MAX_LITE_WINDOW_MESSAGES
    )
    """How many of the latest messages the prompt keeps verbatim."""
    compress_threshold_tokens: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=DEFAULT_COMPRESS_THRESHOLD_TOKENS,
        ge=MIN_LITE_SUMMARY_MAX_TOKENS,
        le=MAX_LITE_SUMMARY_MAX_TOKENS,
    )
    """The summary's size, in tokens, above which its segments are compressed further."""


class Episodic(ProfileModel):
    """Episodic retrieval (FR-MEM-1, FR-RET)."""

    enabled: Annotated[bool, BEHAVIORAL, PROSPECTIVE, RELEVANT] = False
    k: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(default=8, ge=1, le=100)
    """How many memories one retrieval returns."""
    hybrid_search: Annotated[bool, BEHAVIORAL, PROSPECTIVE, RELEVANT] = True
    reranking: Annotated[bool, BEHAVIORAL, PROSPECTIVE, RELEVANT] = False


class Consolidation(ProfileModel):
    """Consolidation and reflections (FR-MEM-3,6)."""

    enabled: Annotated[bool, DATA_PRODUCING, PROSPECTIVE, RELEVANT] = False
    cadence_turns: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=20, ge=1, le=1_000
    )
    """Turns between consolidation runs."""


class HotSet(ProfileModel):
    """The hot-set budgets of the structured pipeline's memories (FR-MEM-8)."""

    memories_per_character: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=2_000, ge=1, le=100_000
    )
    vectors_per_story: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=50_000, ge=1, le=10_000_000
    )


class Memory(ProfileModel):
    """The memory modules (FR-PROF-2)."""

    rolling_summary: RollingSummary = Field(default_factory=RollingSummary)
    episodic: Episodic = Field(default_factory=Episodic)
    consolidation: Consolidation = Field(default_factory=Consolidation)
    hot_set: HotSet = Field(default_factory=HotSet)


class Pins(ProfileModel):
    """Pinned memories (FR-LITE-3): the story's author-pinned texts, always in the prompt."""

    enabled: Annotated[bool, BEHAVIORAL, PROSPECTIVE, IRRELEVANT] = True
    """Include the story's pinned memories in the prompt."""
    max_pinned: Annotated[int, BEHAVIORAL, PROSPECTIVE, IRRELEVANT] = Field(
        default=DEFAULT_MAX_PINNED_MEMORIES, ge=1, le=MAX_PINNED_MEMORIES_LIMIT
    )
    """The most pinned memories a story may hold."""


# -- character knowledge, perception, verification, commitment ----------------------------------


class NestedBeliefs(ProfileModel):
    """Nested beliefs (FR-EPI-2, FR-BELIEF-15): off, or a depth of at least 2."""

    enabled: Annotated[bool, DATA_PRODUCING, INTERPRETIVE, RELEVANT] = False
    depth: Annotated[int, DATA_PRODUCING, INTERPRETIVE, RELEVANT] = Field(default=2, ge=2, le=5)


class CharacterKnowledge(ProfileModel):
    """Shared context, or per-character partitions with their dials (FR-PROF-2)."""

    partitions: Annotated[bool, DATA_PRODUCING, INTERPRETIVE, RELEVANT] = False
    nested_beliefs: NestedBeliefs = Field(default_factory=NestedBeliefs)
    lazy_resolution: Annotated[bool, BEHAVIORAL, INTERPRETIVE, RELEVANT] = False
    testimony: Annotated[
        Literal["off", "model", "defaulted"], BEHAVIORAL, INTERPRETIVE, RELEVANT
    ] = "off"
    """Testimony adjudication (FR-BELIEF-3): ``model`` or ``defaulted``."""


class Perception(ProfileModel):
    """Perception routing (FR-TURN-2,9,10,13)."""

    level: Annotated[
        Literal["none", "co_location", "full"], DATA_PRODUCING, INTERPRETIVE, RELEVANT
    ] = "none"


class Verification(ProfileModel):
    """Claim verification, a ladder: off, declared claims only, plus claim extraction, plus
    entailment escalation (FR-VERIFY); and contradiction checking (FR-PROP-10)."""

    level: Annotated[
        Literal["off", "declared", "extraction", "entailment"],
        DATA_PRODUCING,
        INTERPRETIVE,
        RELEVANT,
    ] = "off"
    """Raising it to ``extraction`` writes the claims extraction finds, so the ladder is
    data-producing."""
    extraction_mode: Annotated[
        Literal["gated", "always"], DATA_PRODUCING, INTERPRETIVE, RELEVANT
    ] = "gated"
    entailment_gate: Annotated[float, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=0.5, ge=0.0, le=1.0
    )
    """The predicate-resolution confidence below which a verdict escalates (FR-VERIFY-3)."""
    contradiction_checking: Annotated[bool, BEHAVIORAL, INTERPRETIVE, RELEVANT] = False


class Commitment(ProfileModel):
    """Commitment (FR-COMMIT) and its salience detection."""

    enabled: Annotated[bool, DATA_PRODUCING, INTERPRETIVE, RELEVANT] = False
    salience: Annotated[Literal["rules", "rules_and_model"], BEHAVIORAL, PROSPECTIVE, RELEVANT] = (
        "rules"
    )


class Clock(ProfileModel):
    """The world clock (FR-CLOCK): off, its prompt-only Lite form (FR-LITE-5), or on."""

    level: Annotated[
        Literal["off", "prompt_only", "on"], DATA_PRODUCING, INTERPRETIVE, RELEVANT
    ] = "off"
    time_claims: Annotated[bool, BEHAVIORAL, INTERPRETIVE, RELEVANT] = False
    """Verify time claims against the clock."""
    scheduled_stops: Annotated[bool, BEHAVIORAL, INTERPRETIVE, RELEVANT] = False
    """Stop the clock at scheduled events."""


class States(ProfileModel):
    """States and external data (FR-STATE, FR-EXT): off, prompt-only (FR-LITE-5), or on."""

    level: Annotated[
        Literal["off", "prompt_only", "on"], DATA_PRODUCING, INTERPRETIVE, RELEVANT
    ] = "off"


class MultiCharacter(ProfileModel):
    """One combined render, or per-character renders (FR-MULTI)."""

    renders: Annotated[Literal["combined", "per_character"], BEHAVIORAL, PROSPECTIVE, RELEVANT] = (
        "combined"
    )


# -- repair, budgets, models ----------------------------------------------------------------------


class Polish(ProfileModel):
    """The polish pass. Under ``lite`` it is the bounded regeneration of a reply that broke an
    output policy, before the safe fallback is used (FR-GUARD-6; the story setting
    ``guardrail_retry_budget`` until profiles drive the Lite turn)."""

    enabled: Annotated[bool, BEHAVIORAL, PROSPECTIVE, RELEVANT] = True
    max_regenerations: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=DEFAULT_GUARDRAIL_RETRY_BUDGET, ge=0, le=MAX_GUARDRAIL_RETRY_BUDGET
    )
    """How many times such a reply is generated again."""


class Repair(ProfileModel):
    """Repair and polish (FR-PROF-2)."""

    max_attempts: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(default=0, ge=0, le=10)
    """Repair attempts of the structured pipeline's repair ladder (FR-REPAIR)."""
    polish: Polish = Field(default_factory=Polish)


class Budgets(ProfileModel):
    """The per-turn model-call budget and deadline (NFR-PERF-6)."""

    model_calls: Annotated[int | None, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=None, ge=MIN_CALLS_PER_TURN, le=MAX_CALLS_PER_TURN
    )
    """The most model calls one turn may make, the reserved guardrail calls included, as M2's
    ``max_model_calls_per_turn``: a cap below one generation plus those calls refuses every
    turn. ``null``: NFR-PERF-6's formula (9 + the guardrail calls in M3)."""
    deadline_ms: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=DEFAULT_DEADLINE_MS, ge=1, le=MAX_DEADLINE_MS
    )
    """A turn's wall-clock deadline, in milliseconds (M2's ``turn_deadline``)."""


class RouteParams(ProfileModel):
    """Generation parameters for one purpose's calls; ``null`` inherits."""

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_LENGTH_TOKENS)


class PurposeRoute(ProfileModel):
    """One model purpose's routing: its model and parameters; ``null`` inherits from the
    deployment's model configuration (``/admin/model-config``). Endpoints and keys are never
    part of a profile."""

    model: str | None = Field(default=None, min_length=1, max_length=MODEL_NAME_MAX)
    params: RouteParams | None = None


class Generation(ProfileModel):
    """The generation defaults (FR-LITE-4): temperature, top_p and response length."""

    temperature: Annotated[float | None, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=0.8, ge=0.0, le=2.0
    )
    top_p: Annotated[float | None, BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default=0.95, ge=0.0, le=1.0
    )
    response_length: Annotated[Length, BEHAVIORAL, PROSPECTIVE, RELEVANT] = "medium"
    """``short``, ``medium``, ``long``, or a token cap."""


class Models(ProfileModel):
    """Per-purpose model routing (FR-MODEL) and the generation defaults."""

    purposes: Annotated[dict[PurposeName, PurposeRoute], BEHAVIORAL, PROSPECTIVE, RELEVANT] = Field(
        default_factory=dict, max_length=PURPOSES_MAX
    )
    """Routing by purpose; a purpose left out inherits the deployment's configuration."""
    generation: Generation = Field(default_factory=Generation)


# -- the settings --------------------------------------------------------------------------------


class ProfileSettings(ProfileModel):
    """An engine profile's settings, by capability module (FR-PROF-2)."""

    pipeline: Annotated[Literal["lite", "structured"], DATA_PRODUCING, PROSPECTIVE, RELEVANT] = (
        "lite"
    )
    """The pipeline every turn runs: ``lite`` (FR-LITE)."""
    memory: Memory = Field(default_factory=Memory)
    pins: Pins = Field(default_factory=Pins)
    character_knowledge: CharacterKnowledge = Field(default_factory=CharacterKnowledge)
    perception: Perception = Field(default_factory=Perception)
    verification: Verification = Field(default_factory=Verification)
    commitment: Commitment = Field(default_factory=Commitment)
    clock: Clock = Field(default_factory=Clock)
    states: States = Field(default_factory=States)
    multi_character: MultiCharacter = Field(default_factory=MultiCharacter)
    repair: Repair = Field(default_factory=Repair)
    budgets: Budgets = Field(default_factory=Budgets)
    models: Models = Field(default_factory=Models)


# -- the three tags (the build-time check) -------------------------------------------------------

TAG_KINDS: tuple[tuple[str, type[StrEnum]], ...] = (
    ("Tag", Tag),
    ("ConfigClass", ConfigClass),
    ("Quality", Quality),
)


@dataclass(frozen=True, slots=True)
class SettingTags:
    """One setting's three tags."""

    tag: Tag
    change_class: ConfigClass
    quality: Quality


def missing_tags(model: type[BaseModel] = ProfileSettings) -> dict[str, tuple[str, ...]]:
    """``{dotted path: kinds}`` for every leaf of ``model`` that lacks exactly one tag of each
    kind (``Tag``, ``ConfigClass``, ``Quality``); empty when every setting is tagged."""
    found: dict[str, tuple[str, ...]] = {}
    for path, info in leaves(model):
        lacking = tuple(
            name
            for name, kind in TAG_KINDS
            if sum(isinstance(item, kind) for item in info.metadata) != 1
        )
        if lacking:
            found[path] = lacking
    return found


def tags_of(info: FieldInfo) -> SettingTags:
    """A tagged leaf's three tags (``missing_tags`` guarantees there is one of each)."""
    values = {kind: next(i for i in info.metadata if isinstance(i, kind)) for _, kind in TAG_KINDS}
    return SettingTags(
        tag=Tag(values[Tag]),
        change_class=ConfigClass(values[ConfigClass]),
        quality=Quality(values[Quality]),
    )


def setting_tags(model: type[BaseModel] = ProfileSettings) -> dict[str, SettingTags]:
    """Every leaf of ``model`` with its tags, by dotted path."""
    return {path: tags_of(info) for path, info in leaves(model)}


# -- modules and levels --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Module:
    """One capability module (FR-PROF-2): ``switch`` is the setting that turns it on, and
    ``levels`` the level each of its values means (any other value is off). A module with no
    switch (budgets, models) is always on; a ``positive`` switch is on above zero. ``owns``
    lists the settings (dotted prefixes) that belong to the module, and ``owns_at`` those that
    belong to one of its levels only."""

    name: str
    switch: str | None
    levels: tuple[tuple[object, str], ...] = ()
    owns: tuple[str, ...] = ()
    owns_at: tuple[tuple[str, tuple[str, ...]], ...] = ()
    positive: bool = False

    def level(self, value: Any) -> str | None:
        """The level ``value`` (of the switch) means; ``None``: off."""
        if self.switch is None:
            return ON
        if self.positive:
            return ON if isinstance(value, int) and value > 0 else None
        for known, level in self.levels:
            if type(value) is type(known) and value == known:
                return level
        return None

    @property
    def level_names(self) -> tuple[str, ...]:
        if self.switch is None or self.positive:
            return (ON,)
        return tuple(dict.fromkeys(level for _, level in self.levels))


def _flag(name: str, switch: str, *owns: str) -> Module:
    return Module(name, switch, ((True, ON),), owns=owns or (switch,))


def _ladder(name: str, switch: str, *levels: str, owns: tuple[str, ...] = ()) -> Module:
    return Module(name, switch, tuple((level, level) for level in levels), owns=owns or (switch,))


MODULES: tuple[Module, ...] = (
    Module(
        "pipeline",
        "pipeline",
        (("lite", "lite"), ("structured", "structured")),
        owns=("pipeline",),
        owns_at=(("structured", ("memory.hot_set",)),),
    ),
    _flag("rolling_summary", "memory.rolling_summary.enabled", "memory.rolling_summary"),
    _flag("episodic", "memory.episodic.enabled", "memory.episodic"),
    _flag("consolidation", "memory.consolidation.enabled", "memory.consolidation"),
    _flag("pins", "pins.enabled", "pins"),
    _flag("partitions", "character_knowledge.partitions", "character_knowledge"),
    _flag(
        "nested_beliefs",
        "character_knowledge.nested_beliefs.enabled",
        "character_knowledge.nested_beliefs",
    ),
    _flag("lazy_resolution", "character_knowledge.lazy_resolution"),
    _ladder("testimony", "character_knowledge.testimony", "model", "defaulted"),
    _ladder("perception", "perception.level", "co_location", "full", owns=("perception",)),
    Module(
        "verification",
        "verification.level",
        (("declared", "declared"), ("extraction", "extraction"), ("entailment", "entailment")),
        owns=("verification",),
        owns_at=(
            ("extraction", ("verification.extraction_mode",)),
            ("entailment", ("verification.entailment_gate",)),
        ),
    ),
    _flag("contradiction_checking", "verification.contradiction_checking"),
    _flag("commitment", "commitment.enabled", "commitment"),
    Module("clock", "clock.level", (("prompt_only", "prompt_only"), ("on", ON)), owns=("clock",)),
    _flag("time_claims", "clock.time_claims"),
    _flag("scheduled_stops", "clock.scheduled_stops"),
    Module(
        "states", "states.level", (("prompt_only", "prompt_only"), ("on", ON)), owns=("states",)
    ),
    _ladder(
        "multi_character", "multi_character.renders", "per_character", owns=("multi_character",)
    ),
    Module("repair", "repair.max_attempts", owns=("repair.max_attempts",), positive=True),
    _flag("polish", "repair.polish.enabled", "repair.polish"),
    Module("budgets", None, owns=("budgets",)),
    Module("models", None, owns=("models",)),
)
"""Every capability module, in schema order. Its ``name`` is the capability name #109 reports
and #122's genesis defers by; the pipeline's levels (``lite``, ``structured``) name it too."""

MODULE: Mapping[str, Module] = types.MappingProxyType({m.name: m for m in MODULES})

DELIVERED: Mapping[str, frozenset[str]] = types.MappingProxyType(
    {
        "pipeline": frozenset({"lite"}),
        "rolling_summary": frozenset({ON}),
        "pins": frozenset({ON}),
        "polish": frozenset({ON}),
        "budgets": frozenset({ON}),
        "models": frozenset({ON}),
    }
)
"""The modules this deployment has delivered, each with its delivered levels (M3: the ``lite``
pipeline, the rolling summary, pinned memories, the polish pass, budgets and models). A module
not listed is not delivered at any level."""


def _owners() -> list[tuple[str, str, str | None]]:
    """``(prefix, module, level)`` for every owned prefix, longest first."""
    found: list[tuple[str, str, str | None]] = []
    for module in MODULES:
        found.extend((prefix, module.name, None) for prefix in module.owns)
        for level, prefixes in module.owns_at:
            found.extend((prefix, module.name, level) for prefix in prefixes)
    return sorted(found, key=lambda item: len(item[0]), reverse=True)


_OWNERS = _owners()


def owner_of(path: str) -> tuple[str, str | None] | None:
    """The ``(module, level)`` a setting (dotted path) belongs to; ``None`` for a group that
    belongs to several (``memory``, ``repair``, ``models``' parent)."""
    for prefix, module, level in _OWNERS:
        if path == prefix or path.startswith(f"{prefix}."):
            return module, level
    return None


def is_delivered(module: str, level: str | None = None) -> bool:
    """Whether ``module`` is delivered (at ``level``, when one is named)."""
    levels = DELIVERED.get(module, frozenset())
    return bool(levels) if level is None else level in levels


LEAF_PATHS: tuple[str, ...] = tuple(path for path, _ in leaves(ProfileSettings))
SWITCHES: Mapping[str, Module] = types.MappingProxyType(
    {m.switch: m for m in MODULES if m.switch is not None}
)


def _leaf_exposed(path: str) -> bool:
    owner = owner_of(path)
    return owner is not None and is_delivered(*owner)


def exposed(path: str) -> bool:
    """Whether a setting or group (dotted path) is part of the delivered schema: a leaf whose
    module (or module level) is delivered, or a group with such a leaf."""
    return any(
        _leaf_exposed(leaf) for leaf in LEAF_PATHS if leaf == path or leaf.startswith(f"{path}.")
    )


def allowed_values(path: str) -> list[Any] | None:
    """The values a delivered switch may take (its off values and its delivered levels'), in
    schema order; ``None`` for a setting that is not a level switch."""
    module = SWITCHES.get(path)
    if module is None or module.positive or not module.levels:
        return None
    options = _literal_options(path)
    if options is None:
        return None
    return [
        value
        for value in options
        if (level := module.level(value)) is None or is_delivered(module.name, level)
    ]


def _field_at(path: str) -> FieldInfo:
    model: type[BaseModel] = ProfileSettings
    info: FieldInfo | None = None
    for part in path.split("."):
        info = model.model_fields[part]
        nested = info.annotation
        if isinstance(nested, type) and issubclass(nested, BaseModel):
            model = nested
    assert info is not None  # noqa: S101 - a path has at least one part
    return info


def _literal_options(path: str) -> list[Any] | None:
    annotation = _field_at(path).annotation
    if annotation is bool:
        return [False, True]
    options = list(get_args(annotation))
    return options or None


def value_at(settings: BaseModel, path: str) -> Any:
    """The value of ``settings`` at a dotted path."""
    value: Any = settings
    for part in path.split("."):
        value = getattr(value, part)
    return value


def level_of(settings: ProfileSettings, module: str) -> str | None:
    """The level ``module`` runs at under ``settings``; ``None``: off."""
    found = MODULE[module]
    return found.level(None if found.switch is None else value_at(settings, found.switch))


def enabled_modules(settings: ProfileSettings) -> dict[str, str]:
    """Every module that is on under ``settings``, with its level, in schema order (#109's
    ``capabilities`` are these names, and the pipeline's level)."""
    found: dict[str, str] = {}
    for module in MODULES:
        level = level_of(settings, module.name)
        if level is not None:
            found[module.name] = level
    return found


# -- data-producing increases (FR-PROF-6,8; #109) -------------------------------------------------


def level_rank(module: str, level: str | None) -> int:
    """Where ``level`` sits on ``module``'s ladder: ``-1`` when off, then each level in the
    order the module declares them (``lite`` before ``structured``, ``declared`` before
    ``extraction``)."""
    if level is None:
        return -1
    names = MODULE[module].level_names
    return names.index(level) if level in names else 0


def _switch_tag(module: Module) -> Tag | None:
    return None if module.switch is None else tags_of(_field_at(module.switch)).tag


DATA_PRODUCING_MODULES: frozenset[str] = frozenset(
    m.name for m in MODULES if _switch_tag(m) is Tag.data_producing
)
"""Modules whose switch is tagged data-producing: turning one on, or raising its level, makes the
engine write structured data that a story's existing history lacks (FR-PROF-6). In M3 none of
them is delivered. Read at call time, so a test may mark a delivered module data-producing
(``epistrel.testing.profiles.data_producing``)."""

DATA_PRODUCING_DIALS: Mapping[str, str] = types.MappingProxyType(
    {
        path: owner[0]
        for path, tags in setting_tags().items()
        if tags.tag is Tag.data_producing
        and path not in SWITCHES
        and (owner := owner_of(path)) is not None
    }
)
"""Data-producing settings that are not a module's switch, with their module: raising one
(``verification.extraction_mode`` from ``gated`` to ``always``, a deeper nested-belief depth)
adds data too while the module is on."""


def _raised(path: str, before: Any, after: Any) -> bool:
    """Whether ``after`` is a higher value of the setting at ``path`` than ``before``: later
    in its declared options (``False`` before ``True``), or a larger number."""
    options = _literal_options(path)
    if options is not None and before in options and after in options:
        return options.index(after) > options.index(before)
    numbers = (int, float)
    if isinstance(before, numbers) and isinstance(after, numbers):
        return after > before
    return False


def data_increases(before: ProfileSettings, after: ProfileSettings) -> list[str]:
    """The modules whose data a change from ``before`` to ``after`` adds, in schema order: each
    data-producing module (``DATA_PRODUCING_MODULES``) switched on or raised to a higher level,
    and each module that stays on while one of its data-producing dials rises."""
    found: list[str] = []
    for module in MODULES:
        old, new = level_of(before, module.name), level_of(after, module.name)
        if module.name in DATA_PRODUCING_MODULES and level_rank(module.name, new) > level_rank(
            module.name, old
        ):
            found.append(module.name)
            continue
        if old is None or new is None:
            continue
        if any(
            owner == module.name and _raised(path, value_at(before, path), value_at(after, path))
            for path, owner in DATA_PRODUCING_DIALS.items()
        ):
            found.append(module.name)
    return found


# -- the dependency graph (FR-PROF-4) -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Clause:
    """One clause of FR-PROF-4, quoted verbatim from docs/03."""

    key: str
    text: str


FR_PROF_4: tuple[Clause, ...] = (
    Clause(
        "structured",
        "every module except the rolling summary, the prompt-only clock and States of "
        "FR-LITE-5, budgets, and models requires the `structured` pipeline",
    ),
    Clause("partitions", "per-character partitions require perception at `co_location` or `full`"),
    Clause("perception", "perception other than `none` requires partitions"),
    Clause(
        "partition_dials",
        "nested beliefs, lazy belief resolution, and testimony adjudication require "
        "per-character partitions",
    ),
    Clause("time_claims", "time-claim verification requires the clock and verification"),
    Clause("scheduled_stops", "scheduled-event stops require the clock"),
    Clause("clock", "in the `structured` pipeline the clock requires commitment"),
    Clause("contradiction", "contradiction checking requires at least declared-claim verification"),
    Clause("consolidation", "consolidation requires episodic memory"),
)


@dataclass(frozen=True, slots=True)
class Requirement:
    """``module`` on, at one of ``levels`` (any level when empty)."""

    module: str
    levels: tuple[str, ...] = ()

    def met(self, settings: ProfileSettings) -> bool:
        level = level_of(settings, self.module)
        return level is not None and (not self.levels or level in self.levels)

    def describe(self) -> str:
        return self.module + (f"={'|'.join(self.levels)}" if self.levels else "")


@dataclass(frozen=True, slots=True)
class DependencyRule:
    """``module`` (at one of ``levels``, any when empty; and only ``when`` that holds) requires
    every one of ``requires``. ``clause`` is the FR-PROF-4 clause it implements."""

    clause: str
    module: str
    requires: tuple[Requirement, ...]
    levels: tuple[str, ...] = ()
    when: Requirement | None = None

    def applies(self, settings: ProfileSettings) -> bool:
        if not Requirement(self.module, self.levels).met(settings):
            return False
        return self.when is None or self.when.met(settings)

    def unmet(self, settings: ProfileSettings) -> tuple[Requirement, ...]:
        """The requirements ``settings`` does not meet, when the rule applies to them."""
        if not self.applies(settings):
            return ()
        return tuple(r for r in self.requires if not r.met(settings))

    def describe(self) -> str:
        subject = Requirement(self.module, self.levels).describe()
        needs = ", ".join(r.describe() for r in self.requires)
        condition = f" when {self.when.describe()}" if self.when is not None else ""
        return f"{subject} requires {needs}{condition}"


STRUCTURED = Requirement("pipeline", ("structured",))
_VERIFIED = ("declared", "extraction", "entailment")


def _needs_structured(module: str, *levels: str) -> DependencyRule:
    return DependencyRule("structured", module, (STRUCTURED,), levels=levels)


DEPENDENCIES: tuple[DependencyRule, ...] = (
    _needs_structured("episodic"),
    _needs_structured("consolidation"),
    _needs_structured("partitions"),
    _needs_structured("nested_beliefs"),
    _needs_structured("lazy_resolution"),
    _needs_structured("testimony"),
    _needs_structured("perception"),
    _needs_structured("verification"),
    _needs_structured("contradiction_checking"),
    _needs_structured("commitment"),
    _needs_structured("clock", ON),
    _needs_structured("time_claims"),
    _needs_structured("scheduled_stops"),
    _needs_structured("states", ON),
    _needs_structured("multi_character"),
    _needs_structured("repair"),
    DependencyRule(
        "partitions", "partitions", (Requirement("perception", ("co_location", "full")),)
    ),
    DependencyRule("perception", "perception", (Requirement("partitions"),)),
    DependencyRule("partition_dials", "nested_beliefs", (Requirement("partitions"),)),
    DependencyRule("partition_dials", "lazy_resolution", (Requirement("partitions"),)),
    DependencyRule("partition_dials", "testimony", (Requirement("partitions"),)),
    DependencyRule(
        "time_claims", "time_claims", (Requirement("clock"), Requirement("verification"))
    ),
    DependencyRule("scheduled_stops", "scheduled_stops", (Requirement("clock"),)),
    DependencyRule("clock", "clock", (Requirement("commitment"),), when=STRUCTURED),
    DependencyRule(
        "contradiction", "contradiction_checking", (Requirement("verification", _VERIFIED),)
    ),
    DependencyRule("consolidation", "consolidation", (Requirement("episodic"),)),
)
"""Every FR-PROF-4 rule (see the module docstring), delivered modules or not. Validation reads
it at call time, so a test may add a rule (``epistrel.testing.profiles``)."""


# -- the delivered schema ------------------------------------------------------------------------


def _inline(node: Any, defs: Mapping[str, Any]) -> Any:
    """``node`` with every ``$ref`` into ``defs`` replaced by its definition."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            resolved = _inline(copy.deepcopy(defs[ref.rsplit("/", 1)[-1]]), defs)
            siblings = {k: _inline(v, defs) for k, v in node.items() if k != "$ref"}
            return {**resolved, **siblings}
        return {key: _inline(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    return node


def _prune(node: dict[str, Any], prefix: str) -> dict[str, Any]:
    properties: dict[str, Any] = node.get("properties", {})
    for name in list(properties):
        path = f"{prefix}{name}"
        if not exposed(path):
            del properties[name]
            continue
        child = properties[name]
        if "properties" in child:
            child.pop("default", None)  # a group's default would name what is pruned
            _prune(child, f"{path}.")
        if "patternProperties" in child:
            child["additionalProperties"] = False
        allowed = allowed_values(path)
        if allowed is not None and "enum" in child:
            child["enum"] = allowed
    if "required" in node:
        node["required"] = [name for name in node["required"] if name in properties]
    return node


def delivered_schema() -> dict[str, Any]:
    """The JSON Schema of ``ProfileSettings`` with only the delivered modules and levels, self-
    contained (no ``$ref``): what the OpenAPI document and ``GET /engine-profiles/schema``
    publish. A setting it leaves out is refused (422 ``invalid_profile``)."""
    full = ProfileSettings.model_json_schema(mode="validation")
    defs = full.pop("$defs", {})
    return _prune(_inline(full, defs), "")


SETTINGS_SCHEMA: dict[str, Any] = delivered_schema()
SettingsDocument = Annotated[dict[str, Any], WithJsonSchema(SETTINGS_SCHEMA)]
"""A profile's settings as JSON: any delivered module, any subset of its settings (what it
leaves out takes the default). Documented as the delivered schema; validated by
``validate.require_valid``."""


def delivered_values(settings: ProfileSettings) -> dict[str, Any]:
    """``settings`` as JSON with every delivered setting at its value and nothing undelivered:
    what an effective-configuration read shows (#109)."""

    def keep(node: Mapping[str, Any], prefix: str) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for name, value in node.items():
            path = f"{prefix}{name}"
            if not exposed(path):
                continue
            nested = isinstance(value, Mapping) and path not in LEAF_PATHS
            found[name] = keep(value, f"{path}.") if nested else value
        return found

    return keep(settings.model_dump(mode="json"), "")


def delivered_tags() -> dict[str, SettingTags]:
    """The tags of every delivered setting, by dotted path."""
    return {path: tags for path, tags in setting_tags().items() if _leaf_exposed(path)}


def delivered_levels() -> dict[str, list[str]]:
    """``DELIVERED`` as JSON: each delivered module and its levels, in schema order."""
    return {
        module.name: [level for level in module.level_names if level in DELIVERED[module.name]]
        for module in MODULES
        if module.name in DELIVERED
    }


# -- reading a stored document -------------------------------------------------------------------


def walk(document: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """``(dotted path, value)`` for every key of a settings document that names a setting or
    a group of ``ProfileSettings``, depth first; a group's members follow it."""
    for key, value in document.items():
        path = f"{prefix}{key}"
        yield path, value
        if isinstance(value, Mapping) and path not in LEAF_PATHS:
            yield from walk(value, f"{path}.")


@dataclass(frozen=True, slots=True)
class Purposes:
    """The model purposes a profile may route (the Orchestrator's registry, #51, passed in by
    the Engine: Core does not import the Orchestrator). ``reserved`` purposes are declared for a
    later milestone, so naming one is naming something undelivered."""

    delivered: frozenset[str]
    reserved: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def of(cls, delivered: Sequence[str], reserved: Sequence[str] = ()) -> Purposes:
        return cls(frozenset(delivered), frozenset(reserved))


__all__ = [
    "BEHAVIORAL",
    "DATA_PRODUCING",
    "DATA_PRODUCING_DIALS",
    "DATA_PRODUCING_MODULES",
    "DELIVERED",
    "DEPENDENCIES",
    "FR_PROF_4",
    "INTERPRETIVE",
    "IRRELEVANT",
    "LEAF_PATHS",
    "MODULE",
    "MODULES",
    "ON",
    "PROSPECTIVE",
    "PURPOSE_PATTERN",
    "RELEVANT",
    "SETTINGS_SCHEMA",
    "SWITCHES",
    "Budgets",
    "Clause",
    "DependencyRule",
    "Generation",
    "Models",
    "Module",
    "ProfileSettings",
    "PurposeRoute",
    "Purposes",
    "Quality",
    "Requirement",
    "RouteParams",
    "SettingTags",
    "SettingsDocument",
    "Tag",
    "allowed_values",
    "data_increases",
    "delivered_levels",
    "delivered_schema",
    "delivered_tags",
    "delivered_values",
    "enabled_modules",
    "exposed",
    "is_delivered",
    "level_of",
    "level_rank",
    "missing_tags",
    "owner_of",
    "setting_tags",
    "tags_of",
    "value_at",
    "walk",
]
