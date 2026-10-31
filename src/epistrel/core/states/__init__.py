"""Story States: typed single values that follow the timeline (FR-STATE-1,2,4,6; docs/02 §7.1;
#185).

A State is declared once (``state_definitions``) and changed by rows of ``state_changes``, each
tied to the operation that wrote it. Its value in any view is the fold of its changes in that
view (``model.fold``), so a truncation, an untruncation, a regenerate, a swap and a fork apply to
States with no code of their own: ``core.membership`` moves the rows with their operations
(``core.tables.states`` registers both tables), and the ``state_values`` projection
(``core.projections.states``) is refreshed in the same transaction. The pure rules (the
declaration's shape, typed values, constraints and the fold) are ``core.states.model``'s.

- ``define_state(conn, op, story_id, definition)`` stores a declaration in ``op``'s commit
  transaction (a commit step), after the shape rules (``model.validate_definition``): 409
  ``state_name_taken`` for a name an active State of the story holds (one a truncation
  discarded is free again, #186; ``check_active_names`` refuses the edit that would restore
  it over a newer one), 422 for an ``entity_id`` that is not one of the story's entities.
  An externally owned State (#204) names a registered provider whose manifest owns its
  ``external_value_name`` with the same type (422 ``validation_error``, ``reason``
  ``unknown_provider``, ``unknown_value`` or ``type_mismatch``).
- ``update_definition(conn, story_id, name, patch, *, principal)`` changes a declaration in place
  (owner decision 2026-10-30): a JSON merge patch over its members, under the story row lock, as
  a story configuration change. Each changed member records one ``config_changes`` row
  (``setting`` ``state:<name>.<member>``) at one new ``config_revision``; no timeline revision
  moves. ``constraints``, ``authorization``, ``transfer``, ``rules`` and ``visibility`` are
  ``interpretive`` (a structured turn depends on them: ``config_dependencies``'
  ``state_definitions``), ``aliases``, ``unit``, ``silent`` and ``reservation_expiry_minutes``
  ``prospective``; the State's identity and its fold's base are immutable (422
  ``validation_error``, ``reason: immutable_field``). New constraints the current value (or the
  initial value) already breaks are 422 ``state_constraint_violation``; an archived story is 409
  ``story_archived``.
- ``apply_change(conn, op, change)`` writes one change in ``op``'s commit transaction, or answers
  the ``Violation`` it is refused with and writes nothing (FR-STATE-6's bounds half; the
  in-character repair is #187's): a turn change asserted by a reactive character
  (``reactive_asserter``, FR-MULTI-9), or one whose folded value at its position, or at any later
  active change, breaks a constraint. A change for an unknown State is 404; a scope key that is
  missing, superfluous or not one of the story's characters, a value of the wrong type and a
  delta on a non-number are 422 ``validation_error``.
- ``value_of(conn, story_id, name, scope_key=None, *, through=None, view="active")`` folds the
  State's changes in a view: ``active`` (the timeline as it stands) or ``as_recorded`` (every
  change ever recorded, whatever its membership), through a ``core.membership.Position``
  (``Position.end_of(N)`` is checkpoint ``N``, without its inter-turn slots;
  ``Position.end_of(N, k)`` is ``N⁺k``). It answers ``Undefined`` for a State with no value.
- A derived State (``rules.kind = derived``; FR-STATE-7, #192) stores no change: ``value_of``,
  ``current_values`` and ``folded_at`` fold it from its inputs on every read, with the
  expression governing the read's position (``rule_versions``/``rules_at``: the declaration's,
  then each ``update_definition`` from its configuration change's position,
  ``change_position``), so it never goes stale; ``apply_change`` refuses any change to it
  (``Violation("derived")``), it has no ``state_values`` row, and no update makes a State
  derived or underived (422 ``reason: rules_kind``). ``core.states.rules`` checks inputs and
  cycles and applies the elapsed rules.

**The module gate** (``states.level``, FR-PROF-2): at ``off`` every write here is 409
``module_off`` (nothing written, so the projection holds no row for the story); ``prompt_only``
(#203's Lite form) allows declarations, updates and every change but a ``turn``'s; ``on`` allows
all. The built-in ``world_clock`` (#193) is exempt. Reads are not gated (#186 gates its routes).

**Erasure.** The tables are indexed for erasure in their write transactions (a character-scoped
change declares its character; reasons, aliases, units and text values are indexed by position),
and every write passes the resurrection guard first (``core.erasure.guard.check_content``).
Erasing a character deletes the changes scoped to it (``core.erasure.erase``).

``valid_time_at`` is the one place a change's story-minute comes from: the M4 stand-in basis
until #193 points it at ``world_clock.story_time_at``. Log lines carry identifiers and counts
only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    ColumnElement,
    and_,
    delete,
    func,
    insert,
    literal,
    select,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import providers
from epistrel.core.config_changes import CONFIG_CHANGED, ConfigScope, record_config_change
from epistrel.core.enums import (
    StateChangeKind,
    StateChangeSource,
    StateOwner,
    StateScope,
    StateType,
    StateVisibility,
    StoryStatus,
)
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import state_change_record, state_definition_record
from epistrel.core.errors import (
    InvalidRequest,
    ModuleOff,
    StateConstraintViolation,
    StateNameTaken,
    StateNotFound,
    StoryArchived,
    StoryNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, active_only, active_operation, insert_derived
from epistrel.core.settings_schema import ConfigClass, merge_patch
from epistrel.core.states.expressions import DerivedRule, evaluate, parse_rules
from epistrel.core.states.model import (
    ADDITIVE,
    CLOCK_INTERPRETIVE_MEMBERS,
    CLOCK_MEMBERS,
    CLOCK_PROSPECTIVE_MEMBERS,
    CLOCK_SOURCES,
    COUNTED,
    IMMUTABLE_MEMBERS,
    INTERPRETIVE_MEMBERS,
    NUMERIC_TYPES,
    PROSPECTIVE_MEMBERS,
    REASON_MAX,
    RESERVED_NAMES,
    TEXT_MAX,
    WORLD_CLOCK,
    ChangeRow,
    ClockSettings,
    Constraints,
    Folded,
    StateChange,
    StateDefinition,
    Undefined,
    UndefinedType,
    Value,
    Violation,
    check_change,
    coerce,
    derived_rule,
    fold,
    from_json,
    number,
    step,
    to_json,
    validate_definition,
    violation,
)
from epistrel.core.tables import characters, entities, events, operations, stories
from epistrel.core.tables.states import (
    STORY_SCOPE_KEY,
    state_changes,
    state_definitions,
    state_values,
)

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation

MODULE = "states"
"""The profile module that gates State writes (``states.level``)."""
ON = "on"
SETTING_PREFIX = "state:"
"""``config_changes.setting`` of a definition member: ``state:<name>.<member>``."""


DERIVED = "derived"
"""The ``Violation`` constraint of a change to a derived State from anything but its rule."""


class View(StrEnum):
    """Which changes a read folds: the ``active`` timeline's, or every one ``as_recorded``."""

    ACTIVE = "active"
    AS_RECORDED = "as_recorded"


@dataclass(frozen=True, slots=True)
class StateValue:
    """A State's value in a view: the value, the position of the last change folded into it
    (``None``: the initial value), how many changes it folds, and its scope key."""

    value: Value
    since: Position | None
    change_count: int
    scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class Applied:
    """A written change: its id, the value it found and the one it left, where it sits and its
    story-minute."""

    change_id: str
    state_id: str
    old_value: Value | UndefinedType
    new_value: Value | UndefinedType
    position: Position
    valid_time_minutes: int


# -- the gate ---------------------------------------------------------------------------------


async def state_level(conn: AsyncConnection, story_id: str) -> str | None:
    """The level the story's effective configuration runs ``states`` at (``None``: off, or a
    level this deployment has not delivered)."""
    from epistrel.core.profiles import schema  # here: the profiles import the settings schema
    from epistrel.core.profiles.effective import effective_configuration

    settings = (await effective_configuration(conn, story_id, validate=False)).settings
    level = schema.level_of(settings, MODULE)
    if level is None or not schema.is_delivered(MODULE, level):
        return None
    return level


async def require_module(conn: AsyncConnection, story_id: str, *, turn: bool = False) -> str:
    """409 ``module_off`` unless the story runs ``states`` (at ``on`` for a turn's change)."""
    level = await state_level(conn, story_id)
    if level is None:
        raise ModuleOff(
            "the story's configuration has States off", {"story_id": story_id, "module": MODULE}
        )
    if turn and level != ON:
        raise ModuleOff(
            "a turn changes States only with states.level on",
            {"story_id": story_id, "module": MODULE, "level": level},
        )
    return level


def _exempt(name: str) -> bool:
    return name == WORLD_CLOCK


CLOCK_ADVANCE_ROUTE = "POST /stories/{story_id}/clock/advance"
"""Where the world clock is moved forward (the State API never sets it)."""


def _clock_change_ok(change: StateChange) -> None:
    """The world clock moves only forward, by an advance from a clock source (#193): a ``set``
    (of any value, so never backward) or another source is 422 ``reserved_state``."""
    if change.kind is not StateChangeKind.DELTA or change.source not in CLOCK_SOURCES:
        raise InvalidRequest(
            f"{WORLD_CLOCK!r} is the built-in clock: advance it with {CLOCK_ADVANCE_ROUTE}",
            {"field": "state", "reason": "reserved_state", "use": CLOCK_ADVANCE_ROUTE},
        )


# -- valid time -----------------------------------------------------------------------------------


async def valid_time_at(conn: AsyncConnection, story_id: str, position: Position) -> int:
    """The story-minute of ``position``: ``world_clock.story_time_at`` (#193)."""
    from epistrel.core import world_clock  # here: the clock is a State of this module's

    return await world_clock.story_time_at(conn, story_id, position)


# -- reading definitions and changes ------------------------------------------------------------


def _definition_of(row: Any) -> StateDefinition:
    kind = StateType(row.type)
    raw = dict(row.constraints or {})
    constraints = Constraints(
        min=number(raw["min"]) if "min" in raw else None,
        max=number(raw["max"]) if "max" in raw else None,
        non_negative=bool(raw.get("non_negative", False)),
        allowed_values=tuple(raw["allowed_values"]) if "allowed_values" in raw else None,
        tolerance=number(raw.get("tolerance", 0)) or Decimal(0),
    )
    return StateDefinition(
        id=str(row.id),
        name=str(row.name),
        type=kind,
        scope=row.scope,
        constraints=constraints,
        initial_value=from_json(kind, row.initial_value),
        aliases=tuple(row.aliases or ()),
        unit=row.unit,
        entity_id=None if row.entity_id is None else str(row.entity_id),
        visibility=row.visibility,
        owner=row.owner,
        provider_id=row.provider_id,
        external_value_name=row.external_value_name,
        silent=bool(row.silent),
        rules=dict(row.rules or {}),
        authorization=dict(row.authorization or {}),
        transfer=dict(row.transfer or {}),
        reservation_expiry_minutes=row.reservation_expiry_minutes,
        created_by=row.created_by,
        clock=_clock_of(row) if kind is StateType.CLOCK else None,
    )


def _clock_of(row: Any) -> ClockSettings:
    return ClockSettings.model_validate(
        {m: getattr(row, m) for m in CLOCK_MEMBERS if getattr(row, m) is not None}
    )


async def definitions(
    conn: AsyncConnection, story_id: str, *, view: View = View.ACTIVE
) -> list[StateDefinition]:
    """The story's States in a view, by name."""
    d = state_definitions.c
    where: list[ColumnElement[bool]] = [d.story_id == story_id]
    if View(view) is View.ACTIVE:
        where.append(active_only(state_definitions))
    found = await conn.execute(select(state_definitions).where(*where).order_by(d.name))
    return [_definition_of(r) for r in found]


async def definition(
    conn: AsyncConnection,
    story_id: str,
    name: str,
    *,
    view: View = View.ACTIVE,
    lock: bool = False,
) -> StateDefinition:
    """The story's State ``name`` in a view (404 when it has none)."""
    d = state_definitions.c
    query = select(state_definitions).where(d.story_id == story_id, d.name == name)
    if View(view) is View.ACTIVE:
        query = query.where(active_only(state_definitions))
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise StateNotFound(
            f"the story has no State {name!r}", {"story_id": story_id, "state": name}
        )
    return _definition_of(row)


def _change_of(kind_of: StateType, row: Any) -> ChangeRow:
    return ChangeRow(
        id=str(row.id),
        kind=StateChangeKind(row.kind),
        value=from_json(kind_of, row.value),
        delta=None if row.delta is None else Decimal(row.delta),
        status=row.status,
        position=Position(
            int(row.narrative_turn),
            int(row.turn_variant),
            int(row.slot_k),
            int(row.beat),
            int(row.step),
        ),
    )


def _through(position: Position) -> Any:
    c = state_changes.c
    return tuple_(c.narrative_turn, c.slot_k, c.beat, c.step) <= tuple_(
        *(literal(v) for v in position.key())
    )


async def _changes(
    conn: AsyncConnection,
    story_id: str,
    state: StateDefinition,
    scope_key: str | None,
    *,
    view: View = View.ACTIVE,
    through: Position | None = None,
) -> list[ChangeRow]:
    c = state_changes.c
    where: list[ColumnElement[bool]] = [c.story_id == story_id, c.state_id == state.id]
    where.append(c.scope_key.is_(None) if scope_key is None else c.scope_key == scope_key)
    if View(view) is View.ACTIVE:
        where.append(active_only(state_changes))
    if through is not None:
        where.append(_through(through))
    found = await conn.execute(select(state_changes).where(*where))
    return sorted((_change_of(state.type, r) for r in found), key=ChangeRow.order)


def _scope_ok(state: StateDefinition, scope_key: str | None) -> None:
    if state.scope is StateScope.CHARACTER and scope_key is None:
        raise InvalidRequest(
            f"State {state.name!r} is per character: name the character",
            {"field": "scope_key", "reason": "required"},
        )
    if state.scope is StateScope.STORY and scope_key is not None:
        raise InvalidRequest(
            f"State {state.name!r} belongs to the story: it takes no scope key",
            {"field": "scope_key", "reason": "story_scoped"},
        )


async def value_of(
    conn: AsyncConnection,
    story_id: str,
    name: str,
    scope_key: str | None = None,
    *,
    through: Position | None = None,
    view: View | str = View.ACTIVE,
) -> StateValue | UndefinedType:
    """The State's value in a view (see the module docstring)."""
    chosen = View(view)
    state = await definition(conn, story_id, name, view=chosen)
    _scope_ok(state, scope_key)
    if derived_rule(state) is not None:
        return await _derived_value(conn, story_id, state, through=through, view=chosen)
    folded = fold(
        state.initial_value,
        await _changes(conn, story_id, state, scope_key, view=chosen, through=through),
    )
    if isinstance(folded.value, UndefinedType):
        return Undefined
    return StateValue(folded.value, folded.since, folded.count, scope_key)


# -- derived States and rule versions (FR-STATE-7; #192) -------------------------------------


async def change_position(conn: AsyncConnection, story_id: str, seq: int) -> Position:
    """Where a configuration change recorded at event ``seq`` sits on the active timeline: the
    end of the latest turn or inter-turn slot that recorded an event before it (``end_of(0)``
    when none did). A position after it is governed by the change; one at or before it is not."""
    e, o = events.c, operations.c
    slot = func.coalesce(e.slot_k, 0)
    row = (
        await conn.execute(
            select(e.narrative_turn, slot.label("slot_k"))
            .select_from(events.join(operations, o.operation_id == e.operation_id))
            .where(
                e.story_id == story_id,
                e.seq < seq,
                e.narrative_turn.is_not(None),
                active_operation(operations),
            )
            .order_by(e.narrative_turn.desc(), slot.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return Position.end_of(0)
    return Position.end_of(int(row.narrative_turn), int(row.slot_k))


async def rule_versions(
    conn: AsyncConnection, story_id: str, state: StateDefinition
) -> list[tuple[Position | None, dict[str, Any]]]:
    """The State's ``rules`` along the timeline, oldest first: its declaration's (``None``:
    from the declaration), then each ``update_definition`` of them from that configuration
    change's position (``change_position``; read from its ``config_changed`` event)."""
    e = events.c
    setting = f"{SETTING_PREFIX}{state.name}.rules"
    changed = list(
        await conn.execute(
            select(e.seq, e.payload)
            .where(
                e.story_id == story_id,
                e.type == CONFIG_CHANGED,
                e.payload["setting"].astext == setting,
            )
            .order_by(e.seq)
        )
    )
    if not changed:
        return [(None, dict(state.rules))]
    versions: list[tuple[Position | None, dict[str, Any]]] = [
        (None, dict(changed[0].payload.get("old") or {}))
    ]
    for row in changed:
        at = await change_position(conn, story_id, int(row.seq))
        versions.append((at, dict(row.payload.get("new") or {})))
    return versions


def rules_at(
    versions: Sequence[tuple[Position | None, dict[str, Any]]], through: Position | None
) -> dict[str, Any]:
    """The ``rules`` that govern ``through`` (``None``: the head): the latest version whose
    change sits before it."""
    found = versions[0][1]
    for at, chosen in versions:
        if at is None or through is None or through.key() > at.key():
            found = chosen
    return found


async def _derived_value(
    conn: AsyncConnection,
    story_id: str,
    state: StateDefinition,
    *,
    through: Position | None,
    view: View,
) -> StateValue | UndefinedType:
    """A derived State's value through ``through`` in ``view``: its expression (the version
    governing that position) over its inputs' values there, folded on every read, so it is
    never stale; undefined while an input has no value or a division is by zero. Its ``since``
    is its latest input's and its count the inputs' counts."""
    rule = parse_rules(rules_at(await rule_versions(conn, story_id, state), through))
    if not isinstance(rule, DerivedRule):
        return Undefined
    values: dict[str, Decimal] = {}
    since: Position | None = None
    count = 0
    for name in sorted(rule.inputs):
        try:
            found = await value_of(conn, story_id, name, through=through, view=view)
        except StateNotFound:
            return Undefined
        if isinstance(found, UndefinedType) or not isinstance(found.value, Decimal):
            return Undefined
        values[name] = found.value
        count += found.change_count
        if found.since is not None and (since is None or found.since.key() > since.key()):
            since = found.since
    result = evaluate(rule.node, values)
    if result is None:
        return Undefined
    return StateValue(result, since, count, None)


# -- the projection ----------------------------------------------------------------------

Key = tuple[str, str | None]
"""A State's value within a story: ``(state id, scope key)``."""


def _projected(
    story_id: str, state_id: str, scope_key: str | None, folded: Folded
) -> dict[str, Any]:
    assert folded.since is not None  # noqa: S101 - a row exists only for a counted change
    return {
        "story_id": story_id,
        "state_id": state_id,
        "scope_key": STORY_SCOPE_KEY if scope_key is None else scope_key,
        "value": None if isinstance(folded.value, UndefinedType) else to_json(folded.value),
        **folded.since.columns("since"),
        "change_count": folded.count,
    }


def derive(
    story_id: str,
    states: Sequence[StateDefinition],
    rows: Iterable[tuple[str, str | None, ChangeRow]],
) -> dict[Key, dict[str, Any]]:
    """The ``state_values`` rows of active ``states`` from their active change rows
    (``(state id, scope key, change)``): one per key with a counted change (pure)."""
    by_id = {s.id: s for s in states if s.id is not None}
    grouped: dict[Key, list[ChangeRow]] = {}
    for state_id, scope_key, change in rows:
        if state_id in by_id:
            grouped.setdefault((state_id, scope_key), []).append(change)
    found: dict[Key, dict[str, Any]] = {}
    for (state_id, scope_key), changes in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        folded = fold(by_id[state_id].initial_value, changes)
        if folded.count:
            found[(state_id, scope_key)] = _projected(story_id, state_id, scope_key, folded)
    return found


async def _active_rows(
    conn: AsyncConnection,
    story_id: str,
    states: Sequence[StateDefinition],
    keys: Sequence[Key] | None,
) -> list[tuple[str, str | None, ChangeRow]]:
    types = {s.id: s.type for s in states}
    c = state_changes.c
    where: list[ColumnElement[bool]] = [c.story_id == story_id, active_only(state_changes)]
    if keys is not None:
        if not keys:
            return []
        where.append(c.state_id.in_(sorted({k[0] for k in keys})))
    wanted = None if keys is None else set(keys)
    found: list[tuple[str, str | None, ChangeRow]] = []
    for row in await conn.execute(select(state_changes).where(*where)):
        key = (str(row.state_id), None if row.scope_key is None else str(row.scope_key))
        if key[0] not in types or (wanted is not None and key not in wanted):
            continue
        found.append((key[0], key[1], _change_of(types[key[0]], row)))
    return found


async def fold_state(conn: AsyncConnection, story_id: str) -> list[dict[str, Any]]:
    """Every ``state_values`` row of the story, folded from its active changes (the rebuild)."""
    states = await definitions(conn, story_id)
    return list(derive(story_id, states, await _active_rows(conn, story_id, states, None)).values())


async def current_values(
    conn: AsyncConnection, story_id: str, states: Sequence[StateDefinition] | None = None
) -> dict[tuple[str, str | None], Value | UndefinedType]:
    """Each State's folded value now, on the active timeline (``core.membership.excluding``
    honoured), by ``(name, scope key)``: only the keys a counted change has folded (any other
    key is its State's initial value). #187's dry-run reads its snapshot here."""
    if states is None:
        states = await definitions(conn, story_id)
    by_id = {s.id: s for s in states if s.id is not None}
    grouped: dict[Key, list[ChangeRow]] = {}
    for state_id, scope_key, change in await _active_rows(conn, story_id, states, None):
        grouped.setdefault((state_id, scope_key), []).append(change)
    found: dict[tuple[str, str | None], Value | UndefinedType] = {}
    for (state_id, scope_key), changes in grouped.items():
        state = by_id[state_id]
        folded = fold(state.initial_value, changes)
        if folded.count:
            found[(state.name, scope_key)] = folded.value
    for state in states:
        if derived_rule(state) is not None:
            derived = await _derived_value(conn, story_id, state, through=None, view=View.ACTIVE)
            if isinstance(derived, StateValue) and derived.change_count:
                found[(state.name, None)] = derived.value
    return found


async def folded_at(
    conn: AsyncConnection,
    story_id: str,
    states: Sequence[StateDefinition],
    *,
    through: Position | None = None,
) -> dict[Key, Folded]:
    """Each of ``states``' folded value on the active timeline through ``through`` (``None``:
    now), by ``(state id, scope key)``: only the keys a change was recorded for (any other key
    holds its State's initial value). #191's owner lookup reads its values here."""
    by_id = {s.id: s for s in states if s.id is not None}
    if not by_id:
        return {}
    c = state_changes.c
    where: list[ColumnElement[bool]] = [
        c.story_id == story_id,
        c.state_id.in_(sorted(by_id)),
        active_only(state_changes),
    ]
    if through is not None:
        where.append(_through(through))
    grouped: dict[Key, list[ChangeRow]] = {}
    for row in await conn.execute(select(state_changes).where(*where)):
        key = (str(row.state_id), None if row.scope_key is None else str(row.scope_key))
        grouped.setdefault(key, []).append(_change_of(by_id[key[0]].type, row))
    found = {key: fold(by_id[key[0]].initial_value, rows) for key, rows in grouped.items()}
    for state_id, state in by_id.items():
        if derived_rule(state) is not None:
            derived = await _derived_value(conn, story_id, state, through=through, view=View.ACTIVE)
            if isinstance(derived, StateValue):
                found[(state_id, None)] = Folded(derived.value, derived.since, derived.change_count)
    return found


async def turn_effects(
    conn: AsyncConnection,
    story_id: str,
    operation_ids: Sequence[str],
    *,
    omniscient: bool,
    with_ids: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """The State changes each of ``operation_ids`` committed (#187, FR-RESP-7), in position
    order, as the response and history return them: ``{state, scope_key, old_value, new_value,
    reason, source, contingency, status}`` (plus the ``beat``; ``with_ids``: also the
    ``change_id`` and the row's ``kind``, #188), a ``hidden`` State's only when ``omniscient``
    (C-12's rule; visibility only, no audience filter)."""
    if not operation_ids:
        return {}
    c, d = state_changes.c, state_definitions.c
    rows = await conn.execute(
        select(state_changes, d.name.label("state_name"), d.visibility.label("state_visibility"))
        .select_from(
            state_changes.join(
                state_definitions,
                (d.id == c.state_id) & (d.story_id == c.story_id),
            )
        )
        .where(c.story_id == story_id, c.operation_id.in_(list(operation_ids)))
        .order_by(c.narrative_turn, c.slot_k, c.beat, c.step, c.id)
    )
    found: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not omniscient and row.state_visibility != StateVisibility.PLAYER_VISIBLE:
            continue
        found.setdefault(str(row.operation_id), []).append(
            {
                "state": str(row.state_name),
                "scope_key": None if row.scope_key is None else str(row.scope_key),
                "old_value": row.old_value,
                "new_value": row.new_value,
                "reason": str(row.reason),
                "source": str(getattr(row.source, "value", row.source)),
                "contingency": str(getattr(row.contingency, "value", row.contingency)),
                "status": str(getattr(row.status, "value", row.status)),
                "beat": int(row.beat),
                **(
                    {"change_id": str(row.id), "kind": str(getattr(row.kind, "value", row.kind))}
                    if with_ids
                    else {}
                ),
            }
        )
    return found


def _comparable(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: (str(number(v)) if k == "value" and number(v) is not None else v)
        for k, v in row.items()
        if k in state_values.c
    }


async def refresh(conn: AsyncConnection, story_id: str, keys: Iterable[Key]) -> int:
    """Re-derive the ``state_values`` rows of ``keys`` and write the difference; answers the
    rows changed."""
    chosen = sorted(set(keys), key=lambda k: (k[0], k[1] or ""))
    if not chosen:
        return 0
    states = await definitions(conn, story_id)
    wanted = derive(story_id, states, await _active_rows(conn, story_id, states, chosen))
    v = state_values.c
    changed = 0
    for state_id, scope_key in chosen:
        stored_key = STORY_SCOPE_KEY if scope_key is None else scope_key
        where = (v.story_id == story_id, v.state_id == state_id, v.scope_key == stored_key)
        found = (await conn.execute(select(state_values).where(*where))).one_or_none()
        old = None if found is None else _comparable(dict(found._mapping))
        new = wanted.get((state_id, scope_key))
        if old is not None and (new is None or _comparable(new) != old):
            await conn.execute(delete(state_values).where(*where))
            changed += 1
            old = None
        if new is not None and old is None:
            await conn.execute(insert(state_values).values(**new))
            changed += 1
    return changed


async def follow_operations(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str]
) -> int:
    """A membership flip of ``operation_ids`` (``core.membership``'s follower, registered by
    ``core.projections.states``): the values their changes and definitions touch are re-derived
    in the flip's transaction."""
    ids = sorted(set(operation_ids))
    if not ids:
        return 0
    c, d = state_changes.c, state_definitions.c
    keys: dict[str, set[Key]] = {}
    where: list[ColumnElement[bool]] = [c.operation_id.in_(ids)]
    if story_id is not None:
        where.append(c.story_id == story_id)
    for r in await conn.execute(
        select(c.story_id, c.state_id, c.scope_key).where(*where).distinct()
    ):
        keys.setdefault(str(r.story_id), set()).add(
            (str(r.state_id), None if r.scope_key is None else str(r.scope_key))
        )
    defined: list[ColumnElement[bool]] = [d.operation_id.in_(ids)]
    if story_id is not None:
        defined.append(d.story_id == story_id)
    for defined_row in await conn.execute(select(d.story_id, d.id).where(*defined)):
        story, state_id = str(defined_row.story_id), str(defined_row.id)
        v = state_values.c
        for held in await conn.execute(
            select(v.scope_key).where(v.story_id == story, v.state_id == state_id)
        ):
            keys.setdefault(story, set()).add(
                (state_id, None if held.scope_key == STORY_SCOPE_KEY else str(held.scope_key))
            )
        for changed_row in await conn.execute(
            select(c.scope_key).where(c.story_id == story, c.state_id == state_id).distinct()
        ):
            scope_key = changed_row.scope_key
            keys.setdefault(story, set()).add(
                (state_id, None if scope_key is None else str(scope_key))
            )
    changed = 0
    for story, found in sorted(keys.items()):
        changed += await refresh(conn, story, found)
    return changed


async def check_active_names(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str]
) -> None:
    """After a flip made ``operation_ids`` active again (an untruncation, a swap): 409
    ``state_name_taken`` when a State they declared shares its name with another active State
    of its story (#186: a name is held by one active State; the flip's transaction rolls back).
    The deferred ``ex_state_definitions_active_name`` constraint is the backstop."""
    ids = sorted(set(operation_ids))
    if not ids:
        return
    d = state_definitions.c
    other = state_definitions.alias("other")
    where: list[ColumnElement[bool]] = [
        d.operation_id.in_(ids),
        active_only(state_definitions),
    ]
    if story_id is not None:
        where.append(d.story_id == story_id)
    clash = (
        await conn.execute(
            select(d.story_id, d.name)
            .join(
                other,
                and_(
                    other.c.story_id == d.story_id,
                    other.c.name == d.name,
                    other.c.id != d.id,
                    active_only(other),
                ),
            )
            .where(*where)
            .order_by(d.name)
            .limit(1)
        )
    ).one_or_none()
    if clash is not None:
        raise StateNameTaken(
            f"the story already has an active State {clash.name!r}: the edit would restore "
            "another State of that name",
            {"story_id": str(clash.story_id), "state": clash.name},
        )


# -- defining -------------------------------------------------------------------------------


def _texts(state: StateDefinition) -> tuple[str, ...]:
    texts = [state.name, *state.aliases]
    if state.unit:
        texts.append(state.unit)
    if isinstance(state.initial_value, str) and state.type is StateType.TEXT:
        texts.append(state.initial_value)
    return tuple(texts)


def _definition_row(story_id: str, state: StateDefinition, op: Operation) -> dict[str, Any]:
    document = state.document()
    return {
        "id": state.id,
        "story_id": story_id,
        "name": state.name,
        "type": state.type.value,
        "scope": state.scope.value,
        "constraints": document["constraints"],
        "initial_value": document["initial_value"],
        "aliases": list(state.aliases),
        "unit": state.unit,
        "entity_id": state.entity_id,
        "visibility": state.visibility.value,
        "owner": state.owner.value,
        "provider_id": state.provider_id,
        "external_value_name": state.external_value_name,
        "silent": state.silent,
        "rules": dict(state.rules),
        "authorization": dict(state.authorization),
        "transfer": dict(state.transfer),
        "reservation_expiry_minutes": state.reservation_expiry_minutes,
        "created_by": state.created_by.value,
        **({} if state.clock is None else _clock_columns(state)),
        "operation_id": op.operation_id,
        "narrative_turn": op.narrative_turn,
        "turn_variant": op.turn_variant,
    }


async def _index_definition(conn: AsyncConnection, story_id: str, state: StateDefinition) -> None:
    await index_records(
        conn,
        [
            state_definition_record(
                SimpleNamespace(
                    id=state.id,
                    story_id=story_id,
                    name=state.name,
                    aliases=list(state.aliases),
                    unit=state.unit,
                    type=state.type.value,
                    initial_value=None
                    if state.initial_value is None
                    else to_json(state.initial_value),
                )
            )
        ],
    )


async def define_state(
    conn: AsyncConnection,
    op: Operation,
    story_id: str,
    state: StateDefinition | Mapping[str, Any],
    *,
    builtin: bool = False,
) -> StateDefinition:
    """Store a State declaration in ``op``'s commit transaction (see the module docstring);
    answers it with its id. ``builtin`` admits the reserved world clock (#193)."""
    checked = validate_definition(state, builtin=builtin)
    if op.story_id != story_id:
        raise InvalidRequest(
            "a State is declared by an operation of its own story", {"field": "story_id"}
        )
    if not (builtin and _exempt(checked.name)):
        await require_module(conn, story_id)
    d = state_definitions.c
    taken = await conn.scalar(
        select(d.id)
        .where(
            d.story_id == story_id,
            d.name == checked.name,
            active_only(state_definitions),
        )
        .limit(1)
    )
    if taken is not None:
        raise StateNameTaken(
            f"the story already has a State {checked.name!r}",
            {"story_id": story_id, "state": checked.name},
        )
    if checked.owner is StateOwner.EXTERNAL:
        # One declaration path for both owners (FR-STATE-2): an external one names a registered
        # provider whose manifest owns the value, with the same type (#204).
        assert checked.provider_id is not None  # noqa: S101 - validate_definition requires it
        assert checked.external_value_name is not None  # noqa: S101 - defaulted there
        await providers.check_external_value(
            conn, checked.provider_id, checked.external_value_name, checked.type.value
        )
    if checked.entity_id is not None:
        known = await conn.scalar(
            select(entities.c.id).where(
                entities.c.story_id == story_id, entities.c.id == checked.entity_id
            )
        )
        if known is None:
            raise InvalidRequest(
                "entity_id names none of the story's entities",
                {"field": "entity_id", "reason": "unknown_entity"},
            )
    await guard.check_content(
        conn,
        story_id,
        [
            guard.Content(
                _texts(checked),
                frozenset(),
                op.provenance in guard.CONSUMER_PROVENANCE,
            )
        ],
        base_revision=op.base_revision,
        path=__name__,
    )
    from epistrel.core.states import rules  # here: the rules apply this module's writes

    await rules.check_definition(conn, story_id, checked)
    stored = checked.model_copy(update={"id": new_ulid()})
    await insert_derived(conn, state_definitions, [_definition_row(story_id, stored, op)])
    await _index_definition(conn, story_id, stored)
    return stored


# -- updating a definition ---------------------------------------------------------------


def _members(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(interpretive, prospective)``: the members an update of State ``name`` may change."""
    if name == WORLD_CLOCK:
        return CLOCK_INTERPRETIVE_MEMBERS, CLOCK_PROSPECTIVE_MEMBERS
    return INTERPRETIVE_MEMBERS, PROSPECTIVE_MEMBERS


def _member_class(member: str, interpretive: Sequence[str]) -> ConfigClass:
    return ConfigClass.interpretive if member in interpretive else ConfigClass.prospective


def _clock_columns(state: StateDefinition) -> dict[str, Any]:
    """The clock's settings as their ``state_definitions`` columns."""
    assert state.clock is not None  # noqa: S101 - only the clock has settings
    return {
        **state.clock.document(),
        "elapsed_tolerance_factor": state.clock.elapsed_tolerance_factor,
    }


async def _scope_values(
    conn: AsyncConnection, story_id: str, state: StateDefinition
) -> list[Value | UndefinedType]:
    """Every scope key's current value on the active timeline (one with no counted change holds
    the initial value, which the shape rules check)."""
    found: dict[str | None, list[ChangeRow]] = {}
    for _, scope_key, change in await _active_rows(conn, story_id, [state], None):
        found.setdefault(scope_key, []).append(change)
    return [fold(state.initial_value, changes).value for changes in found.values()]


async def update_definition(
    conn: AsyncConnection,
    story_id: str,
    name: str,
    patch: Mapping[str, Any],
    *,
    principal: str,
) -> StateDefinition:
    """Change State ``name``'s declaration (see the module docstring); answers the declaration
    as it now stands. A patch that changes nothing writes nothing. The world clock's members
    are its settings (``CLOCK_INTERPRETIVE_MEMBERS``, ``CLOCK_PROSPECTIVE_MEMBERS``; #193's
    ``PATCH /clock``); its State members are fixed."""
    interpretive, prospective = _members(name)
    for member in patch:
        if member in IMMUTABLE_MEMBERS or (
            name == WORLD_CLOCK and member not in (*interpretive, *prospective)
        ):
            raise InvalidRequest(
                f"a State's {member} cannot change", {"field": member, "reason": "immutable_field"}
            )
        if member not in (*interpretive, *prospective):
            raise InvalidRequest(
                f"{member!r} is not a State definition member",
                {"field": member, "reason": "unknown"},
            )
    row = (
        await conn.execute(
            select(stories.c.config_revision, stories.c.status)
            .where(stories.c.id == story_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    current = await definition(conn, story_id, name, lock=True)
    if not _exempt(name):
        await require_module(conn, story_id)
    before = current.document()
    after = merge_patch(before, patch)
    for member in IMMUTABLE_MEMBERS:
        after[member] = before[member]
    # A State never becomes, or stops being, derived (#192): a derived State stores no change.
    if (current.rules.get("kind") == DERIVED) != (
        (after.get("rules") or {}).get("kind") == DERIVED
    ):
        raise InvalidRequest(
            f"State {name!r} cannot become, or stop being, derived: declare a new State",
            {"field": "rules", "reason": "rules_kind"},
        )
    builtin = name in RESERVED_NAMES
    clock = {m: after.pop(m) for m in CLOCK_MEMBERS if m in after}
    if clock:
        after["clock"] = {m: v for m, v in clock.items() if v is not None}
    wanted = validate_definition(
        {k: v for k, v in after.items() if v is not None or k in ("initial_value",)},
        builtin=builtin,
    ).model_copy(update={"id": current.id})
    wanted_document = wanted.document()
    changed = [m for m in (*interpretive, *prospective) if wanted_document[m] != before[m]]
    if not changed:
        return current
    if row.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )
    if "rules" in changed:
        from epistrel.core.states import rules  # here: the rules apply this module's writes

        await rules.check_definition(conn, story_id, wanted)
    if "constraints" in changed:
        for value in await _scope_values(conn, story_id, current):
            broken = violation(wanted, value)
            if broken is not None:
                raise StateConstraintViolation(
                    f"the current value of {name!r} breaks the new {broken} constraint",
                    {
                        "state": name,
                        "constraint": broken,
                        "attempted": to_json(value)
                        if not isinstance(value, UndefinedType)
                        else None,
                        "current": to_json(value) if not isinstance(value, UndefinedType) else None,
                    },
                )
    config_revision = int(row.config_revision) + 1
    await conn.execute(
        update(stories).where(stories.c.id == story_id).values(config_revision=config_revision)
    )
    await conn.execute(
        update(state_definitions)
        .where(state_definitions.c.id == current.id)
        .values(**{m: _stored(wanted, m) for m in changed})
    )
    for member in changed:
        await record_config_change(
            conn,
            config_revision=config_revision,
            setting=f"{SETTING_PREFIX}{name}.{member}",
            old=before[member],
            new=wanted_document[member],
            actor=principal,
            change_class=_member_class(member, interpretive),
            scope=ConfigScope.STORY,
            scope_id=story_id,
        )
    await _index_definition(conn, story_id, wanted)
    return wanted


def _stored(state: StateDefinition, member: str) -> Any:
    if member in CLOCK_MEMBERS and state.clock is not None:
        return _clock_columns(state)[member]
    document = state.document()
    if member == "aliases":
        return list(state.aliases)
    return document[member]


# -- changing a value --------------------------------------------------------------------


def operation_position(op: Operation) -> Position:
    """Where ``op``'s writes sit by default: its inter-turn slot once its commit positioned it
    (``core.world_clock.slots``), else its turn (0 for a position-less operation)."""
    if op.slot and op.slot_k is not None and op.narrative_turn is not None:
        return Position(op.narrative_turn, op.turn_variant or 0, op.slot_k)
    return Position(op.narrative_turn or 0, op.turn_variant or 0)


def _position(op: Operation, change: StateChange) -> Position:
    if change.position is not None:
        return change.position
    return Position(op.narrative_turn or 0, op.turn_variant or 0)


def _typed(state: StateDefinition, change: StateChange) -> tuple[Value | None, Decimal | None]:
    if change.kind is StateChangeKind.SET:
        if change.delta is not None:
            raise InvalidRequest("a set takes a value, not a delta", {"field": "delta"})
        value = coerce(state.type, change.value)
        if state.type is StateType.TEXT and isinstance(value, str) and len(value) > TEXT_MAX:
            raise InvalidRequest(
                f"a text State holds at most {TEXT_MAX} characters",
                {"field": "value", "reason": "too_long"},
            )
        return value, None
    if change.kind not in ADDITIVE:  # pragma: no cover - every kind is one or the other
        raise InvalidRequest(f"unknown change kind {change.kind}", {"field": "kind"})
    if state.type not in NUMERIC_TYPES:
        raise InvalidRequest(
            f"a delta applies to a number State, not {state.type.value!r}",
            {"field": "delta", "reason": "not_number"},
        )
    if change.value is not None:
        raise InvalidRequest("a delta takes no value", {"field": "value"})
    delta = coerce(state.type, change.delta, field="delta")
    assert isinstance(delta, Decimal)  # noqa: S101 - coerce answers a Decimal for a number
    return None, delta


async def _scope_key_ok(conn: AsyncConnection, story_id: str, scope_key: str | None) -> None:
    if scope_key is None:
        return
    known = await conn.scalar(
        select(characters.c.id).where(
            characters.c.story_id == story_id, characters.c.id == scope_key
        )
    )
    if known is None:
        raise InvalidRequest(
            "scope_key names none of the story's characters",
            {"field": "scope_key", "reason": "unknown_character"},
        )


async def apply_change(
    conn: AsyncConnection, op: Operation, change: StateChange
) -> Applied | Violation:
    """Write ``change`` in ``op``'s commit transaction, or answer why it is refused (see the
    module docstring)."""
    story_id = op.story_id
    if story_id is None:
        raise InvalidRequest("a State change belongs to a story's operation", {"field": "op"})
    state = await definition(conn, story_id, change.state)
    if _exempt(state.name):
        from epistrel.core import world_clock  # here: the clock is a State of this module's

        _clock_change_ok(change)
        await world_clock.require_clock(conn, story_id)
    else:
        await require_module(conn, story_id, turn=change.source is StateChangeSource.TURN)
    _scope_ok(state, change.scope_key)
    await _scope_key_ok(conn, story_id, change.scope_key)
    if len(change.reason) > REASON_MAX or not change.reason.strip():
        raise InvalidRequest(
            f"a change gives a reason of 1 to {REASON_MAX} characters",
            {"field": "reason", "reason": "length"},
        )
    value, delta = _typed(state, change)
    position = _position(op, change)
    existing = await _changes(conn, story_id, state, change.scope_key)
    new_id = new_ulid()
    earlier = [r for r in existing if r.position.key() <= position.key()]
    later = [r for r in existing if r.position.key() > position.key()]
    current = fold(state.initial_value, earlier).value
    counted = change.status in COUNTED
    attempted = step(current, change.kind, value, delta) if counted else current
    following: list[Value | UndefinedType] = []
    running = attempted
    for r in later:
        if r.status in COUNTED:
            running = step(running, r.kind, r.value, r.delta)
            following.append(running)
    if derived_rule(state) is not None:
        # A derived State stores no change: its value is folded from its inputs (#192).
        return Violation(DERIVED, attempted, current)
    refused = check_change(state, change, attempted, current, following)
    if refused is not None:
        return refused
    if counted and change.kind in ADDITIVE and isinstance(current, UndefinedType):
        raise InvalidRequest(
            f"State {state.name!r} has no value yet: set it before changing it by a delta",
            {"field": "delta", "reason": "undefined"},
        )
    texts = [change.reason]
    if isinstance(value, str) and state.type is StateType.TEXT:
        texts.append(value)
    await guard.check_content(
        conn,
        story_id,
        [
            guard.Content(
                tuple(texts),
                frozenset({change.scope_key} if change.scope_key else ()),
                change.source in (StateChangeSource.CONSUMER, StateChangeSource.PLAYER)
                or op.provenance in guard.CONSUMER_PROVENANCE,
            )
        ],
        base_revision=op.base_revision,
        path=__name__,
    )
    minutes = (
        change.valid_time_minutes
        if change.valid_time_minutes is not None
        else await valid_time_at(conn, story_id, position)
    )
    row = {
        "id": new_id,
        "story_id": story_id,
        "state_id": state.id,
        "scope_key": change.scope_key,
        "kind": change.kind.value,
        "value": None if value is None else to_json(value),
        "delta": delta,
        "old_value": None if isinstance(current, UndefinedType) else to_json(current),
        "new_value": None if isinstance(attempted, UndefinedType) else to_json(attempted),
        "reason": change.reason,
        "source": change.source.value,
        "contingency": change.contingency.value,
        "status": change.status.value,
        "prerequisite_effect_ids": list(change.prerequisite_effect_ids),
        "operation_id": op.operation_id,
        "narrative_turn": position.turn,
        "turn_variant": position.variant,
        "slot_k": position.slot_k,
        "beat": position.beat,
        "step": position.step,
        "valid_time_minutes": minutes,
        "tx_from": await conn.scalar(select(func.transaction_timestamp())),
        "estimate_meta": None if change.estimate_meta is None else dict(change.estimate_meta),
    }
    await insert_derived(conn, state_changes, [row])
    await index_records(conn, [state_change_record(SimpleNamespace(**row))])
    assert state.id is not None  # noqa: S101 - a stored definition has its id
    await refresh(conn, story_id, [(state.id, change.scope_key)])
    return Applied(new_id, state.id, current, attempted, position, minutes)


async def numeric_states(conn: AsyncConnection, story_id: str) -> frozenset[str]:
    """The names of the story's active ``number`` States (a ``state:<name>`` tie-break's)."""
    return frozenset(
        d.name for d in await definitions(conn, story_id) if d.type is StateType.NUMBER
    )


async def validate_state_tie_break(
    conn: AsyncConnection, story_id: str, value: object, *, field: str
) -> None:
    """422 ``validation_error`` (``reason: tie_break_state_invalid``) when ``value`` is a
    ``state:<name>`` tie-break naming no numeric State of the story (FR-MULTI-8; #188): on the
    settings PATCH and at genesis, after the scenario's States exist (#202)."""
    from epistrel.core.settings_schema import TIE_BREAK_STATE_INVALID, state_tie_break_name

    name = state_tie_break_name(value)
    if name is None or name in await numeric_states(conn, story_id):
        return
    message = "a state:<name> tie-break names a number State of the story"
    raise InvalidRequest(
        message,
        {
            "field": field,
            "reason": TIE_BREAK_STATE_INVALID,
            "errors": [{"field": field, "message": message, "type": "value_error"}],
        },
    )


async def apply_transfer(
    conn: AsyncConnection, op: Operation, own: StateChange, other: StateChange | None
) -> list[Applied] | Violation:
    """Both sides of one transfer (FR-STATE-12; #188), written together: ``own`` and ``other``,
    each a ``transfer_out`` or ``transfer_in`` row with its signed delta (``other`` ``None``: the
    other side is untracked or external, and the tracked side alone is written). Either side's
    ``Violation`` writes neither (a savepoint)."""
    savepoint = await conn.begin_nested()
    done: list[Applied] = []
    for change in (own, other):
        if change is None:
            continue
        found = await apply_change(conn, op, change)
        if isinstance(found, Violation):
            await savepoint.rollback()
            return found
        done.append(found)
    await savepoint.commit()
    return done


__all__ = [
    "CLOCK_ADVANCE_ROUTE",
    "DERIVED",
    "MODULE",
    "Applied",
    "ChangeRow",
    "ClockSettings",
    "Constraints",
    "Folded",
    "Key",
    "StateChange",
    "StateDefinition",
    "StateValue",
    "Undefined",
    "UndefinedType",
    "View",
    "Violation",
    "apply_change",
    "apply_transfer",
    "change_position",
    "check_active_names",
    "current_values",
    "define_state",
    "definition",
    "definitions",
    "derive",
    "fold",
    "fold_state",
    "folded_at",
    "follow_operations",
    "numeric_states",
    "operation_position",
    "refresh",
    "require_module",
    "rule_versions",
    "rules_at",
    "state_level",
    "turn_effects",
    "update_definition",
    "valid_time_at",
    "validate_state_tie_break",
    "value_of",
]
