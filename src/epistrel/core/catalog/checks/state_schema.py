"""The state-schema check (FR-SCN-5, FR-STATE-3; #111 registers it, #202 implements it): the
States a scenario tracks, their initial values, the starting clock and the scheduled events are
valid, so a story created from it starts with them in place.

It needs the ``states`` module: where the deployment has not delivered it the runner lists it
``unavailable`` and never runs it. Each problem is a ``rejected`` entry at its field (a publish
fails 422 ``publish_invalid`` with the report), read the way genesis reads the scenario
(``core.catalog.starting_state``):

- **each State** (``starting_state.states[i]``), through #185's ``validate_definition`` with
  every ``role:<slug>`` source mapped to a placeholder character: its shape, enum values, its
  initial value against its constraints, the reserved name ``world_clock``, #188's authorization
  and transfer shapes and #192's rule shapes. Code ``state_schema_invalid`` (``source``: the
  declaration's ``reason``), or ``state_unpermitted`` for ``rules`` whose sources omit ``rule``.
  An ``authorization.sources`` ``role:<slug>`` or an ``entity_id`` naming no role or world item
  is ``unknown_role`` / ``unknown_entity``; a name declared twice is ``duplicate``;
- **derived States** (#192): every input a story-scoped number State of the scenario
  (``rules_input``), and no cycle among them (``rules_cycle``);
- **each role's initial values** (``cast.roles[i].state_initial_values.<name>``): a
  character-scoped State of the scenario (``unknown_state``, ``not_character_scoped``) and a
  value its type and constraints admit (the constraint's name);
- **the clock** (``starting_state.clock``): #193's settings shapes and #195's anchor;
- **scheduled events** (``starting_state.scheduled_events[i]``): their roles and place exist;
- **closed sets** (#191's refusal, moved here): a ``starting_state.closed_sets`` entry over an
  owner-lookup predicate (``state_value``, the clock's, #207's external values), whether its
  members are objects or a ``key_set``, is ``owner_lookup_closed_set``: the owner answers those,
  never an author's closed set.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.report import Entry, Outcome
from epistrel.core.catalog.scenario_models import WORLD_HOLDER, ScenarioDraft
from epistrel.core.catalog.starting_state import (
    clock_settings,
    definition_of,
    derived_inputs,
    planned_at,
    role_sources,
    starting_anchor,
    unknown_roles,
)
from epistrel.core.enums import StateScope
from epistrel.core.errors import InvalidRequest, StateUnpermitted
from epistrel.core.states.expressions import find_cycle
from epistrel.core.states.model import (
    NUMERIC_TYPES,
    StateDefinition,
    coerce,
    validate_definition,
    violation,
)

INVALID: Final = "state_schema_invalid"
UNPERMITTED: Final = "state_unpermitted"
OWNER_LOOKUP_CLOSED_SET: Final = "owner_lookup_closed_set"
RULE: Final = "a scenario's States, starting clock and scheduled events are valid (FR-STATE-3)"
CLOSED_RULE: Final = (
    "an owner-lookup predicate is answered by its owner, never by an author's closed set"
)
_PLACEHOLDER: Final = "01J0000000000000000000ROLE"


def _placeholder(index: int) -> str:
    return f"{_PLACEHOLDER[:-3]}{index:03d}"


def _reason(exc: InvalidRequest) -> tuple[str, str | None]:
    details = exc.details or {}
    return str(details.get("reason") or "invalid"), (
        None if details.get("field") is None else str(details["field"]).removeprefix("body.")
    )


class _Report:
    def __init__(self, subject: Subject) -> None:
        self.subject = subject
        self.entries: list[Entry] = []

    def add(
        self,
        code: str,
        field: str,
        source: str,
        *,
        text: str | None = None,
        role: str | None = None,
        rule: str = RULE,
    ) -> None:
        self.entries.append(
            self.subject.entry(
                code, Outcome.rejected, field=field, text=text, source=source, role=role, rule=rule
            )
        )


def _states(draft: ScenarioDraft, report: _Report) -> dict[str, StateDefinition]:
    roles = [role.id for role in draft.cast.roles]
    characters = {role: _placeholder(i) for i, role in enumerate(roles)}
    world = [*(loc.id for loc in draft.world.locations), *(e.id for e in draft.world.entities)]
    entities = {**characters, **{item: _placeholder(500 + i) for i, item in enumerate(world)}}
    found: dict[str, StateDefinition] = {}
    seen: set[str] = set()
    for index, state in enumerate(draft.starting_state.states):
        at = f"starting_state.states[{index}]"
        broken = False
        if state.name in seen:
            report.add(INVALID, f"{at}.name", "duplicate", text=state.name)
            broken = True
        seen.add(state.name)
        for role in unknown_roles(role_sources(state), roles):
            report.add(INVALID, f"{at}.authorization.sources", "unknown_role", text=f"role:{role}")
            broken = True
        if state.entity_id is not None and state.entity_id not in entities:
            report.add(INVALID, f"{at}.entity_id", "unknown_entity", text=state.entity_id)
            broken = True
        if broken:
            continue
        try:
            checked = validate_definition(
                definition_of(state, characters=characters, entities=entities)
            )
        except StateUnpermitted:
            report.add(UNPERMITTED, f"{at}.authorization.sources", "source", text=state.name)
            continue
        except InvalidRequest as exc:
            reason, field = _reason(exc)
            report.add(INVALID, at if field is None else f"{at}.{field}", reason, text=state.name)
            continue
        found[state.name] = checked
    _derived(draft, found, report)
    return found


def _derived(draft: ScenarioDraft, found: Mapping[str, StateDefinition], report: _Report) -> None:
    graph: dict[str, frozenset[str]] = {}
    for index, state in enumerate(draft.starting_state.states):
        inputs = derived_inputs(state)
        if inputs is None or state.name not in found:
            continue
        graph[state.name] = inputs
        for name in sorted(inputs):
            read = found.get(name)
            if read is None or read.scope is not StateScope.STORY or read.type not in NUMERIC_TYPES:
                report.add(
                    INVALID,
                    f"starting_state.states[{index}].rules",
                    "rules_input",
                    text=name,
                )
    cycle = find_cycle(graph)
    if cycle is not None:
        index = next(i for i, s in enumerate(draft.starting_state.states) if s.name == cycle[0])
        report.add(
            INVALID, f"starting_state.states[{index}].rules", "rules_cycle", text=" -> ".join(cycle)
        )


def _initial_values(
    draft: ScenarioDraft, states: Mapping[str, StateDefinition], report: _Report
) -> None:
    declared = {s.name for s in draft.starting_state.states}
    for index, role in enumerate(draft.cast.roles):
        for name, raw in (role.state_initial_values or {}).items():
            at = f"cast.roles[{index}].state_initial_values.{name}"
            definition = states.get(name)
            if definition is None:
                if name not in declared:
                    report.add(INVALID, at, "unknown_state", text=name, role=role.id)
                continue  # a declaration its own entry already refuses
            if definition.scope is not StateScope.CHARACTER:
                report.add(INVALID, at, "not_character_scoped", text=name, role=role.id)
                continue
            try:
                value = coerce(definition.type, raw, field=at)
            except InvalidRequest as exc:
                report.add(INVALID, at, _reason(exc)[0], text=name, role=role.id)
                continue
            broken = violation(definition, value)
            if broken is not None:
                report.add(INVALID, at, broken, text=name, role=role.id)


def _clock_and_events(draft: ScenarioDraft, report: _Report) -> None:
    start = draft.starting_state
    try:
        settings = clock_settings(start.clock)
    except InvalidRequest as exc:
        reason, field = _reason(exc)
        report.add(INVALID, f"starting_state.clock.{field or 'anchor'}", reason)
        settings = None
    roles = [role.id for role in draft.cast.roles]
    places = {loc.id for loc in draft.world.locations}
    anchor = None if settings is None else starting_anchor(settings)
    for index, event in enumerate(start.scheduled_events):
        at = f"starting_state.scheduled_events[{index}]"
        owner = None if event.owner in (None, WORLD_HOLDER) else event.owner
        named = [("owner", owner), ("subject", event.subject)]
        named += [(f"participants[{i}]", p) for i, p in enumerate(event.participants or ())]
        for field, role in named:
            if unknown_roles([role], roles):
                report.add(INVALID, f"{at}.{field}", "unknown_role", text=role)
        if event.place is not None and event.place not in places:
            report.add(INVALID, f"{at}.place", "unknown_place", text=event.place)
        if anchor is not None:
            try:
                planned_at(event.planned, anchor)
            except InvalidRequest as exc:
                report.add(INVALID, f"{at}.planned", _reason(exc)[0], text=str(event.planned))


def owner_lookup_predicates() -> frozenset[str]:
    """The names of the core predicates an owner answers (``registry.OWNER_LOOKUPS``: the
    States' ``state_value``, the clock's, #207's external values)."""
    # Imported for their registrations (each module registers its owner lookups on import).
    from epistrel.core.propositions.registry import OWNER_LOOKUPS
    from epistrel.core.propositions.vocabulary import CORE_PREDICATES
    from epistrel.core.states import verify as _states_verify
    from epistrel.core.world_clock import verify as _clock_verify

    del _states_verify, _clock_verify
    return frozenset(p.name for p in CORE_PREDICATES if p.id in OWNER_LOOKUPS)


def _closed_sets(draft: ScenarioDraft, report: _Report) -> None:
    owned = owner_lookup_predicates()
    for index, closed in enumerate(draft.starting_state.closed_sets):
        predicate: Any = closed.get("predicate")
        if isinstance(predicate, str) and predicate in owned:
            report.add(
                OWNER_LOOKUP_CLOSED_SET,
                f"starting_state.closed_sets[{index}].predicate",
                "owner_lookup",
                text=predicate,
                rule=CLOSED_RULE,
            )


def problems(subject: Subject, draft: ScenarioDraft) -> list[Entry]:
    """Every entry the check reports for ``draft`` (see the module docstring)."""
    report = _Report(subject)
    states = _states(draft, report)
    _initial_values(draft, states, report)
    _clock_and_events(draft, report)
    _closed_sets(draft, report)
    return report.entries


class StateSchemaCheck(Check):
    """See the module docstring."""

    name = "state_schema"
    capability = "states"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        return Findings(entries=problems(subject, draft_of(subject)))


CHECK = StateSchemaCheck()

__all__ = [
    "CHECK",
    "INVALID",
    "OWNER_LOOKUP_CLOSED_SET",
    "UNPERMITTED",
    "StateSchemaCheck",
    "owner_lookup_predicates",
    "problems",
]
