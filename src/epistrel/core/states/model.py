"""The pure half of story States (FR-STATE-1,2,4,6; #185): the declaration, its shape rules,
typed values, constraints and the fold. Nothing here reads or writes the database;
``core.states`` stores what these functions decide.

**The declaration** (``StateDefinition``, FR-STATE-1,2): one shape for both owners. A State has a
``name`` (a letter, then letters, digits and ``_``; at most 80), a ``type`` (``number``,
``boolean``, ``enum``, ``text``), a ``scope`` (``story`` or ``character``), ``constraints``
(``min``, ``max``, ``non_negative`` for numbers, ``allowed_values`` for an enum, and the
``tolerance`` a numeric claim is compared within, #191), an ``initial_value`` (``None``: undefined
until set), the ``aliases`` and ``unit`` prose may name it by, the ``entity_id`` a
possession-style State tracks, a ``visibility``, an ``owner`` (``story``, or ``external`` with its
``provider_id`` and no ``rules``), ``silent``, the effect rules (``rules``, ``authorization``,
``transfer``; #188's), ``reservation_expiry_minutes`` (#216's) and who declared it
(``created_by``). ``validate_definition`` applies the shape rules and raises 422
``validation_error`` with ``details.reason``; the name ``world_clock`` is reserved for the
built-in clock (#193, ``reason: reserved_state``).

**The world clock** (#193, FR-CLOCK-1,3,5) is the one State of type ``clock``: story-owned,
story-scoped, non-negative, with initial value 0. Its value is an exact offset in minutes from
the story's start, changed only by ``delta`` changes from a clock source (``CLOCK_SOURCES``),
and its declaration carries the clock's settings (``ClockSettings``: the mode, the anchor, the
pacing table, the skip cap and the other settings #194 to #200 read). Only a ``builtin``
declaration of ``world_clock`` has that type; ``world_clock_definition()`` is genesis's.

**Values.** A number is a ``Decimal`` (never a float) of at most 15 significant digits, so its
JSON form round-trips exactly; a boolean is a ``bool``; an enum is one of the definition's
``allowed_values``; text is at most 200 characters. ``Undefined`` is a State with no value yet (no
initial value and no ``set``).

**The fold** (FR-STATE-4). A State's value in a view is ``fold(initial_value, changes)``: its
changes in position order (``(turn, slot_k, beat, step)``, then id, which is creation order),
``set`` replacing the value and ``delta``/``transfer_in``/``transfer_out`` adding their signed
``delta``. Only ``committed`` and ``converted`` changes count (``COUNTED``). A delta on an
undefined value leaves it undefined. Which changes are in a view (active, or as recorded, through
a position) is the caller's; the fold is the same function everywhere, so the projection, the
reads and the property tests agree by construction.

**Constraints** (FR-STATE-6's bounds half). ``violation`` names the first constraint a value
breaks (``type``, ``min``, ``max``, ``non_negative``, ``allowed_values``, ``max_length``), or
``None``. ``check_change`` is what ``core.states.apply_change`` decides before it writes: a turn's
change asserted by a reactive character (FR-MULTI-9) is refused as ``reactive_asserter``, and a
change whose folded result breaks a constraint is refused with that constraint.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.enums import (
    ClockMode,
    StateChangeKind,
    StateChangeSource,
    StateChangeStatus,
    StateContingency,
    StateCreatedBy,
    StateOwner,
    StateScope,
    StateType,
    StateVisibility,
    TimeAwareness,
)
from epistrel.core.epistemic.roles import REACTIVE
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position

WORLD_CLOCK: Final = "world_clock"
"""The built-in clock's name (#193): reserved, refused to every other declaration."""
RESERVED_NAMES: frozenset[str] = frozenset({WORLD_CLOCK})
"""Names ``define_state`` refuses with ``reason: reserved_state`` (#193 extends the guard)."""
NAME_MAX = 80
NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
TEXT_MAX = 200
"""The longest ``text`` value (FR-STATE-1's short text)."""
REASON_MAX = 200
ALIAS_MAX = 80
UNIT_MAX = 40
DIGITS_MAX = 15
"""A number's most significant digits: its JSON form (a float on the way out) round-trips."""

COUNTED: frozenset[StateChangeStatus] = frozenset(
    {StateChangeStatus.COMMITTED, StateChangeStatus.CONVERTED}
)
"""The statuses that count toward a value; the others are kept for #216 to #218."""

ADDITIVE: frozenset[StateChangeKind] = frozenset(
    {StateChangeKind.DELTA, StateChangeKind.TRANSFER_IN, StateChangeKind.TRANSFER_OUT}
)
"""The kinds that add their signed ``delta``."""

INTERPRETIVE_MEMBERS: tuple[str, ...] = (
    "constraints",
    "authorization",
    "transfer",
    "rules",
    "visibility",
)
"""Definition members whose change re-checks a turn validated under the old ones (FR-CONC-16)."""
PROSPECTIVE_MEMBERS: tuple[str, ...] = ("aliases", "unit", "silent", "reservation_expiry_minutes")
"""Definition members whose change applies to what starts after it."""
IMMUTABLE_MEMBERS: tuple[str, ...] = (
    "name",
    "type",
    "scope",
    "owner",
    "provider_id",
    "entity_id",
    "initial_value",
    "created_by",
)
"""Definition members an update refuses (``reason: immutable_field``): the State's identity,
and its fold's base (changing ``initial_value`` would rewrite every view's history)."""

NUMERIC_TYPES: frozenset[StateType] = frozenset({StateType.NUMBER, StateType.CLOCK})
"""The types whose value is a number (the clock's is its offset in minutes)."""

CLOCK_SOURCES: frozenset[StateChangeSource] = frozenset(
    {
        StateChangeSource.PACING,
        StateChangeSource.DECLARED_SKIP,
        StateChangeSource.ESTIMATE,
        StateChangeSource.PLAYER,
        StateChangeSource.AUTHOR,
        StateChangeSource.CONSUMER,
        StateChangeSource.EXTERNAL,
    }
)
"""The sources a world clock advance may have (FR-CLOCK-12)."""

CLOCK_INTERPRETIVE_MEMBERS: tuple[str, ...] = (
    "mode",
    "max_unprompted_skip_minutes",
    "stop_at_due_events",
    "time_awareness",
    "bands",
    "calendar",
)
"""The clock settings ``PATCH /clock`` changes as interpretive configuration (#193)."""
CLOCK_PROSPECTIVE_MEMBERS: tuple[str, ...] = ("pacing_table",)
"""The clock setting ``PATCH /clock`` changes as prospective configuration (#193)."""
CLOCK_MEMBERS: tuple[str, ...] = (
    "mode",
    "anchor",
    "pacing_table",
    "max_unprompted_skip_minutes",
    "stop_at_due_events",
    "grace_minutes",
    "time_statement_tolerance_minutes",
    "elapsed_tolerance_factor",
    "time_awareness",
    "bands",
    "calendar",
)
"""Every clock setting, each stored in the ``state_definitions`` column of its name."""

DEFAULT_PACING_TABLE: tuple[tuple[int, int | None, int], ...] = (
    (0, 5, 0),
    (5, 51, 1),
    (51, None, 2),
)
"""``(from_words, to_words (exclusive; None: no end), minutes)`` rows (#194 reads them)."""
DEFAULT_BANDS: tuple[tuple[str, str, str], ...] = (
    ("night", "00:00", "05:00"),
    ("morning", "05:00", "12:00"),
    ("afternoon", "12:00", "17:00"),
    ("evening", "17:00", "21:00"),
    ("night", "21:00", "24:00"),
)
"""FR-CLOCK-3's time-of-day bands, ``(name, from, to)`` (#195 reads them)."""
_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")

Value = Decimal | bool | str


class UndefinedType:
    """The value of a State that has none yet (see the module docstring)."""

    _instance: UndefinedType | None = None

    def __new__(cls) -> UndefinedType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Undefined"

    def __bool__(self) -> bool:
        return False


Undefined: Final = UndefinedType()


class Constraints(BaseModel):
    """A State's bounds (FR-STATE-1). ``tolerance`` is how far a numeric claim may be from the
    value and still match it (#191; 0 by default)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min: Decimal | None = None
    max: Decimal | None = None
    non_negative: bool = False
    allowed_values: tuple[str, ...] | None = None
    tolerance: Decimal = Field(default=Decimal(0), ge=0)

    def document(self) -> dict[str, Any]:
        """The stored ``constraints`` document (only what is set)."""
        found: dict[str, Any] = {}
        if self.min is not None:
            found["min"] = to_json(self.min)
        if self.max is not None:
            found["max"] = to_json(self.max)
        if self.non_negative:
            found["non_negative"] = True
        if self.allowed_values is not None:
            found["allowed_values"] = list(self.allowed_values)
        if self.tolerance:
            found["tolerance"] = to_json(self.tolerance)
        return found


class ClockSettings(BaseModel):
    """The world clock's settings (FR-CLOCK-3,5,8,9; #193): its ``mode``, its ``anchor``
    (``None``: unanchored; #195's), the ``pacing_table`` (#194), the unprompted-skip cap
    (#199), the due-event stop and its grace (#196), the time-statement tolerance and the
    elapsed-time tolerance factor (#197), the characters' ``time_awareness`` (#200), the
    time-of-day ``bands`` and the ``calendar``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ClockMode = ClockMode.PACED
    anchor: dict[str, Any] | None = None
    pacing_table: tuple[tuple[int, int | None, int], ...] = DEFAULT_PACING_TABLE
    max_unprompted_skip_minutes: int = Field(default=1440, ge=0)
    stop_at_due_events: bool = True
    grace_minutes: int = Field(default=30, ge=0)
    time_statement_tolerance_minutes: int = Field(default=15, ge=0)
    elapsed_tolerance_factor: Decimal = Field(default=Decimal(2), ge=1)
    time_awareness: TimeAwareness = TimeAwareness.EXACT
    bands: tuple[tuple[str, str, str], ...] = DEFAULT_BANDS
    calendar: Literal["gregorian"] = "gregorian"

    def document(self) -> dict[str, Any]:
        """Every setting as JSON (each the ``state_definitions`` column of its name)."""
        return {
            "mode": self.mode.value,
            "anchor": None if self.anchor is None else dict(self.anchor),
            "pacing_table": [list(row) for row in self.pacing_table],
            "max_unprompted_skip_minutes": self.max_unprompted_skip_minutes,
            "stop_at_due_events": self.stop_at_due_events,
            "grace_minutes": self.grace_minutes,
            "time_statement_tolerance_minutes": self.time_statement_tolerance_minutes,
            "elapsed_tolerance_factor": to_json(self.elapsed_tolerance_factor),
            "time_awareness": self.time_awareness.value,
            "bands": [list(band) for band in self.bands],
            "calendar": self.calendar,
        }


def check_clock_settings(settings: ClockSettings) -> ClockSettings:
    """The shape rules of the clock's settings (422 ``validation_error``)."""
    table = settings.pacing_table
    if not table or table[0][0] != 0:
        raise _invalid("the pacing table starts at 0 words", "pacing_table", "shape")
    for index, (start, end, minutes) in enumerate(table):
        last = index == len(table) - 1
        if minutes < 0 or start < 0:
            raise _invalid("pacing rows are not negative", "pacing_table", "shape")
        if last != (end is None):
            raise _invalid(
                "only the pacing table's last row is open-ended", "pacing_table", "shape"
            )
        if end is not None and (end <= start or table[index + 1][0] != end):
            raise _invalid("the pacing table's rows are contiguous", "pacing_table", "shape")
    if not settings.bands:
        raise _invalid("the clock names its time-of-day bands", "bands", "required")
    for name, begins, ends in settings.bands:
        if not name.strip() or len(name) > ALIAS_MAX:
            raise _invalid(f"a band's name is 1 to {ALIAS_MAX} characters", "bands", "name")
        if not (_HHMM.match(begins) and _HHMM.match(ends)) or begins == "24:00" or begins >= ends:
            raise _invalid("a band runs from one HH:MM to a later one", "bands", "times")
    if not _digits_ok(settings.elapsed_tolerance_factor):
        raise _invalid(
            f"a number has at most {DIGITS_MAX} significant digits",
            "elapsed_tolerance_factor",
            "precision",
        )
    return settings


class StateDefinition(BaseModel):
    """A State's declaration (see the module docstring); ``id`` is set once it is stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: StateType
    scope: StateScope = StateScope.STORY
    constraints: Constraints = Field(default_factory=Constraints)
    initial_value: Decimal | bool | str | None = None
    aliases: tuple[str, ...] = ()
    unit: str | None = None
    entity_id: str | None = None
    visibility: StateVisibility = StateVisibility.PLAYER_VISIBLE
    owner: StateOwner = StateOwner.STORY
    provider_id: str | None = None
    silent: bool = False
    rules: dict[str, Any] = Field(default_factory=dict)
    authorization: dict[str, Any] = Field(default_factory=dict)
    transfer: dict[str, Any] = Field(default_factory=dict)
    reservation_expiry_minutes: int | None = None
    created_by: StateCreatedBy = StateCreatedBy.CONSUMER
    clock: ClockSettings | None = None
    """The world clock's settings (``None`` for every other State)."""
    id: str | None = None

    def document(self) -> dict[str, Any]:
        """Every member as JSON (what ``update_definition`` patches and records); the clock's
        settings are members of their own (``CLOCK_MEMBERS``)."""
        return {
            **({} if self.clock is None else self.clock.document()),
            "name": self.name,
            "type": self.type.value,
            "scope": self.scope.value,
            "constraints": self.constraints.document(),
            "initial_value": None if self.initial_value is None else to_json(self.initial_value),
            "aliases": list(self.aliases),
            "unit": self.unit,
            "entity_id": self.entity_id,
            "visibility": self.visibility.value,
            "owner": self.owner.value,
            "provider_id": self.provider_id,
            "silent": self.silent,
            "rules": dict(self.rules),
            "authorization": dict(self.authorization),
            "transfer": dict(self.transfer),
            "reservation_expiry_minutes": self.reservation_expiry_minutes,
            "created_by": self.created_by.value,
        }

    def interpretive(self) -> list[Any]:
        """The members a turn's checks interpret, as JSON (``config_dependencies``' hash)."""
        document = self.document()
        members = INTERPRETIVE_MEMBERS + (() if self.clock is None else CLOCK_INTERPRETIVE_MEMBERS)
        return [self.name, *(document[m] for m in members)]


@dataclass(frozen=True, slots=True)
class StateChange:
    """One change ``core.states.apply_change`` is asked to write (see ``StateChangeKind``).
    ``asserter_role`` is the asserting character's role for the turn (#176) when ``source`` is
    ``turn`` (``None`` for every other source); ``position`` defaults to the operation's turn
    (0 for a position-less operation) and ``valid_time_minutes`` to the story-minute there."""

    state: str
    reason: str
    source: StateChangeSource
    kind: StateChangeKind = StateChangeKind.SET
    value: Any = None
    delta: Decimal | int | None = None
    scope_key: str | None = None
    asserter_role: str | None = None
    position: Position | None = None
    valid_time_minutes: int | None = None
    contingency: StateContingency = StateContingency.UNCONDITIONAL
    status: StateChangeStatus = StateChangeStatus.COMMITTED
    prerequisite_effect_ids: tuple[str, ...] = ()
    estimate_meta: Mapping[str, Any] | None = None
    """An estimated clock advance's estimate and reason (#193, #197)."""


@dataclass(frozen=True, slots=True)
class Violation:
    """A refused change (nothing written): the ``constraint`` it breaks (or
    ``reactive_asserter``), the value it would have left and the value it found."""

    constraint: str
    attempted: Value | UndefinedType
    current: Value | UndefinedType


@dataclass(frozen=True, slots=True)
class ChangeRow:
    """What the fold reads of one stored change."""

    id: str
    kind: StateChangeKind
    value: Value | None
    delta: Decimal | None
    status: StateChangeStatus
    position: Position

    def order(self) -> tuple[tuple[int, int, int, int], str]:
        return (self.position.key(), self.id)


@dataclass(frozen=True, slots=True)
class Folded:
    """A fold's answer: the value, the position of the last counted change, and how many
    counted changes it folded."""

    value: Value | UndefinedType
    since: Position | None
    count: int


# -- values -----------------------------------------------------------------------------------


def _invalid(message: str, field: str, reason: str) -> InvalidRequest:
    return InvalidRequest(message, {"field": field, "reason": reason})


def number(raw: Any) -> Decimal | None:
    """``raw`` as a number (``None`` when it is not one: a bool is not)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        found = raw
    elif isinstance(raw, int):
        found = Decimal(raw)
    elif isinstance(raw, float):
        try:
            found = Decimal(str(raw))
        except InvalidOperation:
            return None
    else:
        return None
    if not found.is_finite():
        return None
    return found


def _digits_ok(value: Decimal) -> bool:
    return len(value.normalize().as_tuple().digits) <= DIGITS_MAX


def coerce(state_type: StateType, raw: Any, *, field: str = "value") -> Value:
    """``raw`` as a value of ``state_type`` (422 ``validation_error``, ``reason: type``); the
    clock's is a whole number of minutes."""
    if state_type in NUMERIC_TYPES:
        found = number(raw)
        if found is None:
            raise _invalid("a number State takes a number", field, "type")
        if state_type is StateType.CLOCK and found != found.to_integral_value():
            raise _invalid("the clock moves in whole minutes", field, "whole_minutes")
        if not _digits_ok(found):
            raise _invalid(
                f"a number has at most {DIGITS_MAX} significant digits", field, "precision"
            )
        return found
    if state_type is StateType.BOOLEAN:
        if not isinstance(raw, bool):
            raise _invalid("a boolean State takes true or false", field, "type")
        return raw
    if not isinstance(raw, str):
        raise _invalid(f"a {state_type.value} State takes a string", field, "type")
    return raw


def to_json(value: Value) -> Any:
    """A value as stored in ``jsonb``: an integral number as an int, any other as a float (its
    at most 15 digits round-trip), a bool or a string as they are."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def from_json(state_type: StateType, raw: Any) -> Value | None:
    """A stored ``jsonb`` value as a typed value (``None`` stays ``None``)."""
    if raw is None:
        return None
    if state_type in NUMERIC_TYPES:
        found = number(raw)
        if found is None:
            raise ValueError(f"stored value {raw!r} is not a number")
        return found
    return raw  # type: ignore[no-any-return]


def violation(definition: StateDefinition, value: Value | UndefinedType) -> str | None:
    """The first constraint ``value`` breaks under ``definition``, or ``None``; an undefined
    value breaks none."""
    if isinstance(value, UndefinedType):
        return None
    kind, c = definition.type, definition.constraints
    if kind in NUMERIC_TYPES:
        if not isinstance(value, Decimal):
            return "type"
        if c.non_negative and value < 0:
            return "non_negative"
        if c.min is not None and value < c.min:
            return "min"
        if c.max is not None and value > c.max:
            return "max"
        return None
    if kind is StateType.BOOLEAN:
        return None if isinstance(value, bool) else "type"
    if not isinstance(value, str):
        return "type"
    if kind is StateType.ENUM:
        return None if value in (c.allowed_values or ()) else "allowed_values"
    return "max_length" if len(value) > TEXT_MAX else None


# -- the declaration ------------------------------------------------------------------------------


def validate_definition(
    definition: StateDefinition | Mapping[str, Any], *, builtin: bool = False
) -> StateDefinition:
    """``definition`` checked against the shape rules (see the module docstring), its values
    typed; 422 ``validation_error`` with ``details.reason`` otherwise. ``builtin`` admits a
    reserved name (#193's world clock)."""
    if not isinstance(definition, StateDefinition):
        try:
            definition = StateDefinition.model_validate(dict(definition))
        except ValueError as exc:
            errors = getattr(exc, "errors", None)
            if callable(errors):
                raise InvalidRequest.from_errors(errors()) from None
            raise _invalid(str(exc), "body", "invalid") from None
    d = definition
    if not NAME_PATTERN.match(d.name):
        raise _invalid(
            "a State's name is a letter, then letters, digits and underscores (at most 80)",
            "name",
            "name",
        )
    if d.name in RESERVED_NAMES and not builtin:
        raise _invalid(f"{d.name!r} is reserved for the built-in clock", "name", "reserved_state")
    if (d.type is StateType.CLOCK) != (d.name == WORLD_CLOCK) or (
        d.type is StateType.CLOCK and not builtin
    ):
        raise _invalid("only the built-in world_clock is of type clock", "type", "reserved_state")
    if (d.clock is not None) != (d.type is StateType.CLOCK):
        raise _invalid("only the world clock has clock settings", "clock", "not_clock")
    if d.clock is not None:
        check_clock_settings(d.clock)
        if d.scope is not StateScope.STORY or d.initial_value != 0 or d.rules:
            raise _invalid(
                "the world clock is story-scoped, starts at 0 and has no rules", "clock", "shape"
            )
    if d.owner is StateOwner.EXTERNAL:
        if not d.provider_id:
            raise _invalid(
                "an externally owned value names its provider", "provider_id", "required"
            )
        if d.rules:
            raise _invalid("an externally owned value has no rules", "rules", "external_rules")
    elif d.provider_id is not None:
        raise _invalid(
            "only an externally owned value names a provider", "provider_id", "story_owned"
        )
    c = d.constraints
    if d.type is StateType.ENUM:
        values = c.allowed_values or ()
        if not values or any(not v.strip() for v in values):
            raise _invalid(
                "an enum State lists its allowed values", "constraints.allowed_values", "required"
            )
        if len(set(values)) != len(values):
            raise _invalid(
                "an enum's allowed values are unique", "constraints.allowed_values", "duplicate"
            )
        if any(len(v) > TEXT_MAX for v in values):
            raise _invalid(
                f"an allowed value is at most {TEXT_MAX} characters",
                "constraints.allowed_values",
                "too_long",
            )
    elif c.allowed_values is not None:
        raise _invalid(
            "only an enum State lists allowed values", "constraints.allowed_values", "not_enum"
        )
    if d.type not in NUMERIC_TYPES:
        for name in ("min", "max"):
            if getattr(c, name) is not None:
                raise _invalid(
                    f"only a number State has a {name}", f"constraints.{name}", "not_number"
                )
        if c.non_negative:
            raise _invalid(
                "only a number State is non-negative", "constraints.non_negative", "not_number"
            )
        if c.tolerance:
            raise _invalid(
                "only a number State has a tolerance", "constraints.tolerance", "not_number"
            )
    else:
        for name in ("min", "max", "tolerance"):
            bound = getattr(c, name)
            if bound is not None and not _digits_ok(bound):
                raise _invalid(
                    f"a number has at most {DIGITS_MAX} significant digits",
                    f"constraints.{name}",
                    "precision",
                )
        if c.min is not None and c.max is not None and c.min > c.max:
            raise _invalid("min is above max", "constraints.min", "bounds")
    initial: Value | None = None
    if d.initial_value is not None:
        initial = coerce(d.type, d.initial_value, field="initial_value")
        broken = violation(d, initial)
        if broken is not None:
            raise _invalid(
                f"the initial value breaks the State's {broken} constraint",
                "initial_value",
                broken,
            )
    aliases = tuple(a.strip() for a in d.aliases)
    if any(not a for a in aliases):
        raise _invalid("an alias is not empty", "aliases", "empty")
    if any(len(a) > ALIAS_MAX for a in aliases):
        raise _invalid(f"an alias is at most {ALIAS_MAX} characters", "aliases", "too_long")
    if len({a.casefold() for a in aliases}) != len(aliases):
        raise _invalid("aliases are unique", "aliases", "duplicate")
    unit = d.unit.strip() if d.unit is not None else None
    if unit is not None and not 1 <= len(unit) <= UNIT_MAX:
        raise _invalid(f"a unit is 1 to {UNIT_MAX} characters", "unit", "length")
    if d.reservation_expiry_minutes is not None and d.reservation_expiry_minutes <= 0:
        raise _invalid(
            "a reservation expiry is a positive number of minutes",
            "reservation_expiry_minutes",
            "positive",
        )
    return d.model_copy(update={"initial_value": initial, "aliases": aliases, "unit": unit})


def world_clock_definition() -> StateDefinition:
    """The built-in clock as genesis declares it (FR-CLOCK-1; #193): story-owned and
    story-scoped, visible to the player, starting at offset 0, never negative, with the default
    settings (``paced``, unanchored)."""
    return StateDefinition(
        name=WORLD_CLOCK,
        type=StateType.CLOCK,
        scope=StateScope.STORY,
        constraints=Constraints(non_negative=True),
        initial_value=Decimal(0),
        unit="minutes",
        visibility=StateVisibility.PLAYER_VISIBLE,
        owner=StateOwner.STORY,
        created_by=StateCreatedBy.GENESIS,
        clock=ClockSettings(),
    )


# -- the fold -------------------------------------------------------------------------------------


def step(
    current: Value | UndefinedType,
    kind: StateChangeKind,
    value: Value | None,
    delta: Decimal | None,
) -> Value | UndefinedType:
    """The value one change leaves (see the module docstring)."""
    if kind is StateChangeKind.SET:
        if value is None:
            raise ValueError("a set names its value")
        return value
    if isinstance(current, UndefinedType) or delta is None:
        return Undefined
    if not isinstance(current, Decimal):
        raise ValueError("a delta applies to a number")
    return current + delta


def fold(initial: Value | None, changes: Iterable[ChangeRow]) -> Folded:
    """The value ``changes`` leave on ``initial`` (see the module docstring)."""
    current: Value | UndefinedType = Undefined if initial is None else initial
    since: Position | None = None
    count = 0
    for change in sorted(changes, key=ChangeRow.order):
        if change.status not in COUNTED:
            continue
        current = step(current, change.kind, change.value, change.delta)
        since = change.position
        count += 1
    return Folded(current, since, count)


def check_change(
    definition: StateDefinition,
    change: StateChange,
    attempted: Value | UndefinedType,
    current: Value | UndefinedType,
    later: Sequence[Value | UndefinedType] = (),
) -> Violation | None:
    """Whether ``change`` may be written (``None``) or the ``Violation`` it is refused with:
    a reactive character's turn change (FR-MULTI-9), or a counted change whose result
    (``attempted``), or a later value it would change (``later``), breaks a constraint."""
    if change.source is StateChangeSource.TURN and change.asserter_role == REACTIVE:
        return Violation("reactive_asserter", attempted, current)
    if change.status not in COUNTED:
        return None
    for value in (attempted, *later):
        broken = violation(definition, value)
        if broken is not None:
            return Violation(broken, attempted, current)
    return None


__all__ = [
    "ADDITIVE",
    "CLOCK_INTERPRETIVE_MEMBERS",
    "CLOCK_MEMBERS",
    "CLOCK_PROSPECTIVE_MEMBERS",
    "CLOCK_SOURCES",
    "COUNTED",
    "DEFAULT_BANDS",
    "DEFAULT_PACING_TABLE",
    "IMMUTABLE_MEMBERS",
    "INTERPRETIVE_MEMBERS",
    "NUMERIC_TYPES",
    "PROSPECTIVE_MEMBERS",
    "RESERVED_NAMES",
    "TEXT_MAX",
    "WORLD_CLOCK",
    "ChangeRow",
    "ClockSettings",
    "Constraints",
    "Folded",
    "StateChange",
    "StateDefinition",
    "Undefined",
    "UndefinedType",
    "Value",
    "Violation",
    "check_change",
    "check_clock_settings",
    "coerce",
    "fold",
    "from_json",
    "number",
    "step",
    "to_json",
    "validate_definition",
    "violation",
    "world_clock_definition",
]
