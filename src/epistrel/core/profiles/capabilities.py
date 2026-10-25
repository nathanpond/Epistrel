"""The capability map: which requirement areas hold under which modules (FR-PROF-12,13; #129).

**Guarantees follow capabilities.** FR-PROF-13 lists, area by area, the modules (and levels)
each requirement area depends on; every area it does not list is the **floor**, which holds
under every profile. ``CAPABILITY_MAP`` is that list as data, one ``CapabilityEntry`` per
bullet and per sub-clause of the requirement, in its order:

- ``slug``: the entry's stable id, ``<area>[_<level>]`` (#132's guarantee table and #109's impact
  report name entries by it);
- ``area``: the clause, in the requirement's words;
- ``requirements``: the requirement groups it covers, as docs/03 writes them (``FR-TURN`` for a
  whole family, ``FR-MEM-1,2,4,5,7,8`` for some of its ids, ``FR-CONC-11..13`` for a range). An
  id may sit in several entries (FR-TURN-9 is the structured pipeline's and perception's); a
  group that holds only part of a requirement says which part in ``notes``;
- ``requires``: the modules it needs, each on at a level at least the one named (``Needs``);
  empty for the required-field repair clause, which applies wherever its field is required;
- ``story_settings``: the story settings (``StorySettings`` fields, and a character's
  ``participation``) that only matter where the entry holds (#181): per-character renders'
  interaction mode, order, tie-break, interjections and participation, and the player's
  authority, whose structured effects run with them (FR-PROF-13's "player authority → story
  setting"). Each is stored and reported whatever the profile;
- ``floor``: true for the one entry FR-PROF-13 calls the floor, whose requirements are every
  area no other entry lists (``epistrel.tools.capability_map`` computes that complement from
  docs/03 when it renders ``docs/capability-map.md``).

An entry **holds** for a configuration when every module it requires is on at its level
(``holds``). A module, or level, this deployment has not delivered is never on, so an area that
needs one does not hold anywhere yet. The rendered map adds, per entry, the milestones its
requirements are assigned to (docs/03 §22).

**Module gating.** Every module's code path asks the story's effective configuration (#113)
whether it runs, through the two helpers here, and never assumes itself on:

- ``module_on(effective, name)``: the module is on (at any level);
- ``level_at_least(effective, name, level)``: it is on at ``level`` or above, in the order its
  schema declares (``lite`` < ``structured``; ``declared`` < ``extraction`` < ``entailment``).
  The pipeline is asked this way: ``level_at_least(effective, "pipeline", "structured")``.

Both take the validated ``ProfileSettings``; an unknown module or level raises ``ValueError``
(a typo is a bug, not an off module), and a module or level that is not delivered is off.

**Capabilities as reported.** ``capability_names`` is what every respond response, effective
configuration read and impact report lists as the capabilities enabled (FR-PROF-16): each module
that is on, by name, or as ``<module>:<level>`` when its level is not simply on
(``verification:entailment``); the pipeline by its level (``lite``, ``structured``).
``module_names`` gives the bare names (the pipeline by its level), which a scenario's
recommendation and an impact report's capabilities gained and lost use.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass

from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import ProfileSettings

PIPELINE = "pipeline"
STRUCTURED = "structured"


def _module(name: str) -> schema.Module:
    found = schema.MODULE.get(name)
    if found is None:
        raise ValueError(f"{name!r} is not a profile module")
    return found


def module_on(effective: ProfileSettings, name: str) -> bool:
    """Whether module ``name`` is on (at any delivered level) under ``effective``."""
    _module(name)
    level = schema.level_of(effective, name)
    return level is not None and schema.is_delivered(name, level)


def level_at_least(effective: ProfileSettings, name: str, level: str) -> bool:
    """Whether module ``name`` runs at ``level`` or a higher one under ``effective``; ``False``
    when ``level`` is not delivered."""
    module = _module(name)
    if level not in module.level_names:
        raise ValueError(f"{name!r} has no level {level!r}: {list(module.level_names)}")
    if not schema.is_delivered(name, level):
        return False
    current = schema.level_of(effective, name)
    if current is None or not schema.is_delivered(name, current):
        return False
    return schema.level_rank(name, current) >= schema.level_rank(name, level)


@dataclass(frozen=True, slots=True)
class Needs:
    """A module on, at ``level`` or above (any level that is on when ``None``; exactly
    ``level`` when ``only``, for a module whose levels are alternatives rather than a ladder:
    testimony's ``model`` or ``defaulted``)."""

    module: str
    level: str | None = None
    only: bool = False

    def met(self, effective: ProfileSettings) -> bool:
        if self.level is None:
            return module_on(effective, self.module)
        if self.only:
            return module_on(effective, self.module) and (
                schema.level_of(effective, self.module) == self.level
            )
        return level_at_least(effective, self.module, self.level)

    def describe(self) -> str:
        if self.level is None:
            return f"`{self.module}`"
        if self.only or self.level == self.top():
            return f"`{self.module}` at `{self.level}`"
        return f"`{self.module}` at `{self.level}` or above"

    def top(self) -> str:
        """The highest level of the module's ladder."""
        return _module(self.module).level_names[-1]


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    """One area of FR-PROF-13 (see the module docstring)."""

    slug: str
    area: str
    requirements: tuple[str, ...]
    requires: tuple[Needs, ...]
    notes: str = ""
    floor: bool = False
    story_settings: tuple[str, ...] = ()

    def holds(self, effective: ProfileSettings) -> bool:
        """Whether the area applies under ``effective``: every module it needs is on."""
        return all(need.met(effective) for need in self.requires)

    @property
    def delivered(self) -> bool:
        """Whether every module (and level) the area needs is delivered."""
        return all(
            schema.is_delivered(need.module, need.level)
            if need.level is not None
            else schema.is_delivered(need.module)
            for need in self.requires
        )


def _entry(
    slug: str,
    area: str,
    requirements: str,
    *needs: Needs,
    notes: str = "",
    story_settings: tuple[str, ...] = (),
) -> CapabilityEntry:
    groups = tuple(group.strip() for group in requirements.split(";") if group.strip())
    return CapabilityEntry(slug, area, groups, needs, notes, story_settings=story_settings)


MULTI_CHARACTER_SETTINGS = (
    "interaction_mode",
    "sequence_order",
    "simultaneous_tie_break",
    "reactive_interjections",
    "participation",
    "player_authority",
)
"""The story settings of per-character renders (FR-MULTI-5..9, FR-RESP-11; #174 to #180)."""


STRUCTURED_PIPELINE = Needs(PIPELINE, STRUCTURED)
PARTITIONS = Needs("partitions")

CAPABILITY_MAP: tuple[CapabilityEntry, ...] = (
    _entry(
        "structured_pipeline",
        "The `structured` pipeline",
        "FR-TURN; FR-BITEMP; FR-PROP; FR-RET; FR-EMBED; FR-MEM-1,2,4,5,7,8; FR-STORE-3; "
        "FR-RESP-8; FR-DIRECT; FR-REWRITE; FR-CONC-3,7,11..13; FR-GUARD-4,5",
        STRUCTURED_PIPELINE,
        notes="FR-STORE-3: its projections of beliefs, relationships and placement. FR-CONC: "
        "the structured read set and rewrite parts (the Lite read set of FR-CONC-4 is floor). "
        "FR-GUARD: its memory parts.",
    ),
    _entry(
        "partitions",
        "Per-character partitions",
        "FR-EPI; FR-BELIEF; NFR-PRIV-4; FR-MEM-11; FR-RESP-11; FR-COMMIT-4,11,13,14",
        PARTITIONS,
        notes="NFR-PRIV-4: structural isolation; a character's render then carries no rolling "
        "summary and no text pins, which are not viewpoint-scoped (the narrator's render keeps "
        "them). FR-RESP-11: its viewpoint checks. FR-COMMIT: "
        "FR-COMMIT-4's observation, FR-COMMIT-11's could-have-known flag, FR-COMMIT-13's "
        "knocker-side commitment for a modeled character, and FR-COMMIT-14's knowledge "
        "obligations and epistemic checks (its position obligations hold with shared "
        "knowledge too).",
    ),
    _entry(
        "partitions_nested",
        "Per-character partitions: nested beliefs",
        "FR-EPI-2; FR-BELIEF-15",
        PARTITIONS,
        Needs("nested_beliefs"),
        notes="FR-EPI-2: its nested clause. Off, no nested belief is recorded, and claims about "
        "another character's beliefs get no nested-belief check.",
    ),
    _entry(
        "partitions_lazy",
        "Per-character partitions: lazy resolution",
        "FR-BELIEF-5,6,9",
        PARTITIONS,
        Needs("lazy_resolution"),
        notes="Off, every witness's transition is resolved when the exposure is recorded.",
    ),
    _entry(
        "partitions_testimony",
        "Per-character partitions: model adjudication of testimony",
        "FR-BELIEF-3",
        PARTITIONS,
        Needs("testimony", "model", only=True),
        notes="At `defaulted`, every acceptance follows the deterministic default of trust, "
        "priors, and consistency.",
    ),
    _entry(
        "perception_co_location",
        "Perception at `co_location` or `full`",
        "FR-TURN-9,10; FR-RESP-13",
        Needs("perception", "co_location"),
        notes="FR-RESP-13: the audience filter.",
    ),
    _entry(
        "perception_full",
        "Perception at `full`",
        "FR-TURN-2,13",
        Needs("perception", "full"),
        notes="FR-TURN-2: its envelope/content split and qualifiers.",
    ),
    _entry(
        "verification",
        "Verification, at its level",
        "FR-VERIFY; FR-PROP-10; FR-REPAIR",
        Needs("verification", "declared"),
        notes="FR-VERIFY-13 and FR-VERIFY-3 need the higher levels (the next two entries).",
    ),
    _entry(
        "verification_extraction",
        "Verification with claim extraction",
        "FR-VERIFY-13",
        Needs("verification", "extraction"),
        notes="FR-VERIFY-13: its model extraction backstop.",
    ),
    _entry(
        "verification_entailment",
        "Verification with entailment escalation",
        "FR-VERIFY-3",
        Needs("verification", "entailment"),
        notes="FR-VERIFY-3: its entailment escalation.",
    ),
    _entry("commitment", "Commitment", "FR-COMMIT", Needs("commitment")),
    _entry(
        "clock",
        "The world clock",
        "FR-CLOCK",
        Needs("clock", "on"),
        notes="Under `lite`, the clock applies only in the prompt-only form of FR-LITE-5.",
    ),
    _entry(
        "states_external",
        "States and external data",
        "FR-STATE; FR-EXT",
        Needs("states", "on"),
        notes="Under `lite`, States apply only in the prompt-only form of FR-LITE-5, and no "
        "external-data provider is used.",
    ),
    _entry(
        "per_character_renders",
        "Per-character renders",
        "FR-MULTI",
        Needs("multi_character", "per_character"),
        notes="Player authority is a story setting (`player_authority`, FR-RESP-11): its "
        "structured effects run under the `structured` pipeline, and under `lite` it is a "
        "prompt instruction only.",
        story_settings=MULTI_CHARACTER_SETTINGS,
    ),
    _entry("consolidation", "Consolidation", "FR-MEM-3,6,9,10", Needs("consolidation")),
    _entry(
        "required_field_repair",
        "Repair of a missing or invalid structured field",
        "FR-CAT-12; FR-STATE-5,6; FR-CLOCK-16",
        notes="A character's age (FR-CAT-12), a State effect (FR-STATE-5,6), a time stop "
        "(FR-CLOCK-16): wherever that field is required, whether or not verification is on.",
    ),
    CapabilityEntry(
        "floor",
        "The floor",
        (),
        (),
        notes="Every requirement area not listed above, including FR-STORE event storage and "
        "the transcript (FR-PROF-11), the rest of FR-CONC, FR-AUTH, FR-ADMIN including "
        "erasure, budgets (NFR-PERF-6,7), the catalog, and content ratings with their prompt "
        "guidance and the rest of FR-GUARD (FR-RATE, FR-GUARD): under every profile.",
        floor=True,
    ),
)
"""FR-PROF-13, one entry per bullet and sub-clause (see the module docstring)."""

CAPABILITY: Mapping[str, CapabilityEntry] = types.MappingProxyType(
    {entry.slug: entry for entry in CAPABILITY_MAP}
)


def holding(effective: ProfileSettings) -> tuple[str, ...]:
    """The slugs of every entry that holds under ``effective``, in map order (the floor
    always does)."""
    return tuple(entry.slug for entry in CAPABILITY_MAP if entry.holds(effective))


def capability_name(module: str, level: str) -> str:
    """How a module on at ``level`` is reported: the pipeline by its level, a module at its
    plain ``on`` level by its name, any other as ``<module>:<level>``."""
    if module == PIPELINE:
        return level
    return module if level == schema.ON else f"{module}:{level}"


def capability_names(effective: ProfileSettings) -> tuple[str, ...]:
    """The capabilities ``effective`` enables, as reported (see the module docstring), sorted."""
    return tuple(
        sorted(
            capability_name(name, level)
            for name, level in schema.enabled_modules(effective).items()
        )
    )


def module_names(effective: ProfileSettings) -> tuple[str, ...]:
    """The modules ``effective`` turns on, by name (the pipeline by its level), sorted."""
    return tuple(
        sorted(
            level if name == PIPELINE else name
            for name, level in schema.enabled_modules(effective).items()
        )
    )


__all__ = [
    "CAPABILITY",
    "CAPABILITY_MAP",
    "MULTI_CHARACTER_SETTINGS",
    "PIPELINE",
    "STRUCTURED",
    "CapabilityEntry",
    "Needs",
    "capability_name",
    "capability_names",
    "holding",
    "level_at_least",
    "module_names",
    "module_on",
]
