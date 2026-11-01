"""The State API (FR-STATE-3,9, FR-STORE-31, FR-RESP-12, FR-AUTH-2; docs/02 §7.1; #186).

**Reads.** ``read_states(db, story_id, viewpoint=, actor_character_id=)`` lists the story's
States for one output viewpoint, with the world clock under ``clock`` only (#193's
``read_clock``); ``read_state(db, story_id, name, viewpoint=, actor_character_id=, as_of=)``
reads one, folded through a checkpoint: ``N`` (turn N's end, without its inter-turn slots) or
``N⁺k`` (with slots 1..k; ``core.timeline.parse_as_of``). The viewpoint resolves as history's
does (``core.viewpoint``: named, else the actor, else the story's default, else 422
``viewpoint_unresolved``). A definition's visibility maps onto ADR-167's scopes
(``player_visible`` → ``public``, ``hidden`` → ``engine``) and is read through
``core.visibility``, so a character viewpoint is never given a hidden State: it is left out of
the list entirely (its own scoped value too), and named, it is 404 exactly as an unknown name
(owner decision 2026-10-28). ``omniscient`` reads everything. The story's ``player_view``
never widens a States read (FR-RESP-12: hidden States are private reads). With
``states.level`` off both reads are 409 ``module_off``.

**Writes between turns.** ``set_state`` (``PUT …/states/{name}``) and ``define_state``
(``POST …/states``) are inter-turn slot operations (``core.world_clock.slots.open_inter_turn``,
source ``consumer``): positioned at commit, under the story lock, at ``N⁺k`` after the last
completed turn ``N``, bumping the timeline revision, so a checkpoint ``N`` excludes them and a
truncation to ``N`` or earlier discards them (a definition with its slot: its name is free
again). Each records a non-audit event (``state_set``, ``state_defined``), so a fork copies its
slot. Like a turn, each takes the story's turn slot first (``core.turn_gate``): with a turn in
flight it waits for it under ``turn_concurrency: queue`` (up to the turn deadline) and is 423
``story_busy`` under ``reject``. A refusal writes nothing; the common ones are checked before
the operation opens, and the commit checks again:

- 404 for an unknown State; 422 ``validation_error`` (``reason: reserved_state``) for
  ``world_clock`` (use ``POST …/clock/advance``); 409 ``module_off`` with ``states.level`` off
  (``prompt_only`` and ``on`` both take API sets);
- 422 ``state_readonly_external`` for an externally owned State (FR-EXT-8; #209, #210 write
  those); 422 ``state_derived`` for a derived one (``rules.kind = derived``, #192);
- 422 ``state_unpermitted`` (``reason: source``) when the State's ``authorization.sources``
  omit ``consumer`` (an empty or absent list is #188's default, which admits it);
- 422 ``state_constraint_violation {constraint, attempted, current}``.

API sets are not subject to #188's render caps. ``update_state_definition`` (``PATCH
…/states/{name}/definition``) is #185's ``update_definition``: a story configuration change
(``config_revision`` + 1, one ``config_changes`` row per member), no timeline revision.

``check_settable`` and ``consumer_change`` are shared with ingest's ``state_change`` (#58).
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import states, turn_gate
from epistrel.core.enums import (
    Provenance,
    StateChangeKind,
    StateChangeSource,
    StateCreatedBy,
    StateOwner,
    StateScope,
    StateVisibility,
)
from epistrel.core.errors import (
    InvalidRequest,
    StateConstraintViolation,
    StateDerived,
    StateNameTaken,
    StateNotFound,
    StateReadonlyExternal,
    StateUnpermitted,
    StoryNotFound,
)
from epistrel.core.events.types import StateDefined as StateDefinedEvent
from epistrel.core.events.types import StateSet as StateSetEvent
from epistrel.core.membership import Position, active_only
from epistrel.core.settings_schema import StorySettings
from epistrel.core.states.authorize import source_permitted
from epistrel.core.states.model import (
    REASON_MAX,
    WORLD_CLOCK,
    StateChange,
    StateDefinition,
    UndefinedType,
    Value,
    Violation,
    check_change,
    step,
    to_json,
    validate_definition,
)
from epistrel.core.tables import characters, operations, stories
from epistrel.core.tables.states import state_definitions
from epistrel.core.timeline import boundary_label, parse_as_of
from epistrel.core.viewpoint import resolve_viewpoint
from epistrel.core.visibility import Visibility, visible_to
from epistrel.core.world_clock.service import ClockView, read_clock
from epistrel.core.world_clock.slots import open_inter_turn, slot_position

CONSUMER = "consumer"
"""The source an API or ingested write has (``authorization.sources``' name for it)."""
DERIVED = "derived"
"""``rules.kind`` of a derived State (#192)."""
SCOPES: dict[StateVisibility, Visibility] = {
    StateVisibility.PLAYER_VISIBLE: Visibility(scope="public"),
    StateVisibility.HIDDEN: Visibility(scope="engine"),
}
"""A definition's visibility as ADR-167's scope (the one visibility rule, ``core.visibility``)."""

JsonScalar = StrictBool | StrictInt | StrictFloat | StrictStr
"""A State value as JSON: a number, a boolean or a string."""


# -- requests ---------------------------------------------------------------------------------


class StateSetRequest(BaseModel):
    """``PUT /stories/{id}/states/{name}``: a new ``value``, or a signed ``delta`` for a number
    State, for one character's value of a per-character State (``scope_key``), with a
    ``reason``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"delta": -5, "reason": "bought a lantern"}]},
    )

    value: JsonScalar | None = None
    delta: StrictInt | StrictFloat | None = None
    scope_key: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=REASON_MAX)

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if (self.value is None) == (self.delta is None):
            raise ValueError("give a value or a delta, not both")
        return self


class StateConstraintsBody(BaseModel):
    """A State's bounds: ``min``, ``max`` and ``non_negative`` for a number, the
    ``allowed_values`` of an enum, and a number's ``tolerance``."""

    model_config = ConfigDict(extra="forbid")

    min: StrictInt | StrictFloat | None = None
    max: StrictInt | StrictFloat | None = None
    non_negative: StrictBool = False
    allowed_values: list[str] | None = None
    tolerance: StrictInt | StrictFloat | None = None


class StateDeclaration(BaseModel):
    """A State the consumer declares (FR-STATE-1,2,3): its ``name``, ``type`` and ``scope``,
    its ``constraints`` and ``initial_value`` (absent: undefined until set), the ``aliases`` and
    ``unit`` prose names it by, the ``entity_id`` it tracks, its ``visibility`` and ``owner``
    (``external`` with its ``provider_id``), ``silent``, and its effect rules (``rules``,
    ``authorization``, ``transfer``; #188) and ``reservation_expiry_minutes``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    type: Literal["number", "boolean", "enum", "text"]
    scope: Literal["story", "character"] = "story"
    constraints: StateConstraintsBody = Field(default_factory=StateConstraintsBody)
    initial_value: JsonScalar | None = None
    aliases: list[str] = Field(default_factory=list)
    unit: str | None = None
    entity_id: str | None = None
    visibility: Literal["player_visible", "hidden"] = "player_visible"
    owner: Literal["story", "external"] = "story"
    provider_id: str | None = None
    silent: StrictBool = False
    rules: dict[str, Any] = Field(default_factory=dict)
    authorization: dict[str, Any] = Field(default_factory=dict)
    transfer: dict[str, Any] = Field(default_factory=dict)
    reservation_expiry_minutes: StrictInt | None = None

    def definition(self) -> dict[str, Any]:
        """The declaration as ``core.states`` takes it (declared by the consumer)."""
        document = self.model_dump(exclude_none=True)
        document["constraints"] = self.constraints.model_dump(exclude_none=True)
        document["created_by"] = StateCreatedBy.CONSUMER.value
        return document


class StateDefineRequest(BaseModel):
    """``POST /stories/{id}/states``: the ``definition`` of a State to declare."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "definition": {
                        "name": "gold",
                        "type": "number",
                        "constraints": {"non_negative": True},
                        "initial_value": 10,
                        "unit": "coins",
                    }
                }
            ]
        },
    )

    definition: StateDeclaration


# -- results ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateRead:
    """One State as a viewpoint reads it: its declaration's identity, its ``value`` (a
    story-scoped State) or ``values`` per character (a per-character one; ``None``: undefined),
    and the position of the last change folded into it (``None``: the initial value)."""

    name: str
    type: str
    scope: str
    visibility: str
    owner: str
    unit: str | None
    value: Any = None
    values: dict[str, Any] | None = None
    since_position: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class StatesView:
    """``GET /stories/{id}/states``: the States the viewpoint may read, the world clock, the
    story's revision and the viewpoint the read was produced for."""

    states: list[StateRead]
    clock: ClockView
    timeline_revision: int
    output_viewpoint: str


@dataclass(frozen=True, slots=True)
class StateView:
    """``GET /stories/{id}/states/{name}``: the State through ``as_of`` (``None``: the head)."""

    state: StateRead
    as_of: str | None
    timeline_revision: int
    output_viewpoint: str


@dataclass(frozen=True, slots=True)
class StateWritten:
    """An API set: the value it found and the one it left (``None``: undefined), its slot
    (``position``, ``N⁺k``; ``slot`` the full position), its operation and change, and the
    story's revision after it."""

    state: str
    old_value: Any
    new_value: Any
    position: str
    slot: dict[str, int]
    operation_id: str
    change_id: str
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class StateDeclared:
    """An API declaration: the stored definition, its slot, its operation and the story's
    revision after it."""

    definition: dict[str, Any]
    position: str
    slot: dict[str, int]
    operation_id: str
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class StateDefinitionWrite:
    """A definition update: the definition as it now stands, the story's configuration
    revision (moved by a change) and its unchanged timeline revision."""

    definition: dict[str, Any]
    config_revision: int
    timeline_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- reading ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Story:
    timeline_revision: int
    head: int
    default_viewpoint: str | None
    cast: tuple[str, ...]


async def _story(conn: AsyncConnection, story_id: str) -> _Story:
    row = (
        await conn.execute(
            select(
                stories.c.timeline_revision, stories.c.next_turn_index, stories.c.settings
            ).where(stories.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    cast = tuple(
        str(c)
        for c in await conn.scalars(
            select(characters.c.id)
            .where(characters.c.story_id == story_id)
            .order_by(characters.c.id)
        )
    )
    return _Story(
        timeline_revision=int(row.timeline_revision),
        head=int(row.next_turn_index) - 1,
        default_viewpoint=StorySettings.from_row(row.settings).default_output_viewpoint,
        cast=cast,
    )


def _viewpoint(story: _Story, viewpoint: str | None, actor_character_id: str | None) -> str:
    if actor_character_id is not None and actor_character_id not in story.cast:
        raise InvalidRequest(
            "the actor is not a character of the story",
            {"field": "query.actor_character_id", "unknown_actor": actor_character_id},
        )
    return resolve_viewpoint(
        viewpoint,
        actor_character_id=actor_character_id,
        story_default=story.default_viewpoint,
        cast_ids=story.cast,
        field="query.viewpoint",
    )


def readable(state: StateDefinition, viewpoint: str) -> bool:
    """Whether ``viewpoint`` may read ``state`` (``core.visibility``, ADR-167's scopes)."""
    return visible_to(SCOPES[state.visibility], viewpoint)


def _json(value: Value | UndefinedType | None) -> Any:
    if value is None or isinstance(value, UndefinedType):
        return None
    return to_json(value)


async def _read(
    conn: AsyncConnection,
    story_id: str,
    state: StateDefinition,
    cast: tuple[str, ...],
    through: Position | None,
) -> StateRead:
    keys: tuple[str | None, ...] = cast if state.scope is StateScope.CHARACTER else (None,)
    found: dict[str | None, Any] = {}
    since: Position | None = None
    for key in keys:
        value = await states.value_of(conn, story_id, state.name, key, through=through)
        if isinstance(value, UndefinedType):
            found[key] = None
            continue
        found[key] = _json(value.value)
        if value.since is not None and (since is None or since.key() < value.since.key()):
            since = value.since
    base: dict[str, Any] = {
        "name": state.name,
        "type": state.type.value,
        "scope": state.scope.value,
        "visibility": state.visibility.value,
        "owner": state.owner.value,
        "unit": state.unit,
        "since_position": None if since is None else since.document(),
    }
    if state.scope is StateScope.CHARACTER:
        return StateRead(**base, values={str(k): v for k, v in found.items()})
    return StateRead(**base, value=found[None])


async def read_states(
    db: AsyncEngine,
    story_id: str,
    *,
    viewpoint: str | None = None,
    actor_character_id: str | None = None,
) -> StatesView:
    """The story's States for one viewpoint, as the story stands (see the module docstring)."""
    async with db.connect() as conn:
        story = await _story(conn, story_id)
        resolved = _viewpoint(story, viewpoint, actor_character_id)
        await states.require_module(conn, story_id)
        found = [
            await _read(conn, story_id, state, story.cast, None)
            for state in await states.definitions(conn, story_id)
            if state.name != WORLD_CLOCK and readable(state, resolved)
        ]
    return StatesView(
        states=found,
        clock=await read_clock(db, story_id),
        timeline_revision=story.timeline_revision,
        output_viewpoint=resolved,
    )


async def _declared_by(conn: AsyncConnection, story_id: str, state_id: str) -> Position:
    """Where the operation that declared the State sits (genesis: ``0``)."""
    d, o = state_definitions.c, operations.c
    row = (
        await conn.execute(
            select(
                func.coalesce(o.narrative_turn, 0).label("turn"),
                func.coalesce(o.slot_k, 0).label("slot_k"),
            )
            .select_from(state_definitions.join(operations, o.operation_id == d.operation_id))
            .where(d.story_id == story_id, d.id == state_id)
        )
    ).one_or_none()
    if row is None:
        return Position(0)
    return Position(int(row.turn), 0, int(row.slot_k))


def _unknown(story_id: str, name: str) -> StateNotFound:
    return StateNotFound(f"the story has no State {name!r}", {"story_id": story_id, "state": name})


async def read_state(
    db: AsyncEngine,
    story_id: str,
    name: str,
    *,
    viewpoint: str | None = None,
    actor_character_id: str | None = None,
    as_of: str | None = None,
) -> StateView:
    """One State for one viewpoint through ``as_of`` (``N`` or ``N+k``; the head when
    ``None``): see the module docstring. A State the viewpoint may not read, the world clock
    and a State declared after ``as_of`` are 404 as an unknown name is."""
    through = None if as_of is None else parse_as_of(as_of)
    async with db.connect() as conn:
        story = await _story(conn, story_id)
        resolved = _viewpoint(story, viewpoint, actor_character_id)
        await states.require_module(conn, story_id)
        if through is not None and through.turn > story.head:
            raise InvalidRequest(
                "as_of is a turn of the story, from 0 to its head",
                {"field": "query.as_of", "head": story.head},
            )
        if name == WORLD_CLOCK:
            raise _unknown(story_id, name)
        state = await states.definition(conn, story_id, name)
        if not readable(state, resolved):
            raise _unknown(story_id, name)
        if through is not None:
            assert state.id is not None  # noqa: S101 - a stored definition has its id
            declared = await _declared_by(conn, story_id, state.id)
            if declared.key()[:2] > through.key()[:2]:
                raise _unknown(story_id, name)
        read = await _read(conn, story_id, state, story.cast, through)
    label = None if through is None else boundary_label(through.turn, through.slot_k)
    return StateView(
        state=read,
        as_of=label,
        timeline_revision=story.timeline_revision,
        output_viewpoint=resolved,
    )


# -- the checks a consumer's write passes -----------------------------------------------------


def _reserved() -> InvalidRequest:
    return InvalidRequest(
        f"{WORLD_CLOCK!r} is the built-in clock: advance it with {states.CLOCK_ADVANCE_ROUTE}",
        {"field": "state", "reason": "reserved_state", "use": states.CLOCK_ADVANCE_ROUTE},
    )


def sources_admit(state: StateDefinition, source: str) -> bool:
    """Whether ``state``'s ``authorization.sources`` admit ``source`` (#188's rule,
    ``core.states.authorize.source_permitted``: an absent list is the default, which admits
    every source)."""
    return source_permitted(state, source)


async def check_settable(
    conn: AsyncConnection, story_id: str, name: str, *, source: str = CONSUMER
) -> StateDefinition:
    """The State ``name`` a consumer may set, or the refusal (see the module docstring): 404,
    ``reserved_state``, ``module_off``, ``state_readonly_external``, ``state_derived``,
    ``state_unpermitted``."""
    if name == WORLD_CLOCK:
        raise _reserved()
    await states.require_module(conn, story_id)
    state = await states.definition(conn, story_id, name)
    details = {"story_id": story_id, "state": name}
    if state.owner is StateOwner.EXTERNAL:
        raise StateReadonlyExternal(
            f"State {name!r} is owned by its provider: it is not set through the State API",
            details,
        )
    if state.rules.get("kind") == DERIVED:
        raise StateDerived(f"State {name!r} is derived from other States", details)
    if not sources_admit(state, source):
        raise StateUnpermitted(
            f"State {name!r} does not admit changes from the {source}",
            {**details, "reason": "source", "source": source},
        )
    return state


def consumer_change(
    name: str,
    *,
    value: Any,
    delta: Any,
    scope_key: str | None,
    reason: str,
    position: Position | None = None,
) -> StateChange:
    """The ``StateChange`` a consumer's set or delta asks for."""
    kind = StateChangeKind.SET if delta is None else StateChangeKind.DELTA
    return StateChange(
        state=name,
        reason=reason,
        source=StateChangeSource.CONSUMER,
        kind=kind,
        value=value,
        delta=None if delta is None else _number(delta),
        scope_key=scope_key,
        position=position,
    )


def _number(raw: Any) -> Decimal | int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else Decimal(str(raw))


def refused(name: str, violation: Violation) -> StateConstraintViolation:
    """422 ``state_constraint_violation`` for a ``Violation``."""
    return StateConstraintViolation(
        f"the change would leave {name!r} breaking its {violation.constraint} constraint",
        {
            "state": name,
            "constraint": violation.constraint,
            "attempted": _json(violation.attempted),
            "current": _json(violation.current),
        },
    )


async def _precheck_value(
    conn: AsyncConnection, story_id: str, state: StateDefinition, change: StateChange
) -> None:
    """The change's shape and constraints against the head (the slot is after it; the commit
    checks again under the story lock)."""
    states._scope_ok(state, change.scope_key)
    await states._scope_key_ok(conn, story_id, change.scope_key)
    value, delta = states._typed(state, change)
    found = await states.value_of(conn, story_id, state.name, change.scope_key)
    current = found if isinstance(found, UndefinedType) else found.value
    if change.kind is not StateChangeKind.SET and isinstance(current, UndefinedType):
        raise InvalidRequest(
            f"State {state.name!r} has no value yet: set it before changing it by a delta",
            {"field": "delta", "reason": "undefined"},
        )
    attempted = step(current, change.kind, value, delta)
    violation = check_change(state, change, attempted, current)
    if violation is not None:
        raise refused(state.name, violation)


# -- the story's turn slot ---------------------------------------------------------------------


@contextlib.asynccontextmanager
async def turn_slot(db: AsyncEngine, story_id: str, operation_id: str) -> AsyncIterator[None]:
    """Hold the story's turn slot for ``operation_id`` while the block runs, as a turn would
    (#63): with a turn in flight (or queued), wait for it under ``queue`` up to the turn
    deadline, or 423 ``story_busy`` under ``reject`` (and past the deadline). The operation
    gives the slot back in its commit (``holds_turn_slot``); a block that fails before gives it
    back here."""
    concurrency = await turn_gate.read_concurrency(db, story_id)
    deadline = time.monotonic() + concurrency.turn_deadline
    while True:
        held = await turn_gate.acquire(db, story_id, operation_id, lease_s=concurrency.lease_s)
        if held is None:
            break
        if concurrency.mode == "queue" and time.monotonic() < deadline:
            await turn_gate.wakeups.wait(story_id, turn_gate.POLL_S)
            continue
        raise held.busy(story_id)
    try:
        async with turn_gate.renewing(db, story_id, operation_id, concurrency.lease_s):
            yield
    except BaseException:
        await turn_gate.release_now(db, story_id, operation_id)
        raise


# -- writing -----------------------------------------------------------------------------------


async def set_state(
    db: AsyncEngine,
    story_id: str,
    name: str,
    request: StateSetRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> StateWritten:
    """The consumer's set of State ``name`` at the next inter-turn slot (see the module
    docstring)."""
    from epistrel.core.ids import new_ulid
    from epistrel.core.stories import require_writable  # here: stories declares States

    await require_writable(db, story_id)
    change = consumer_change(
        name,
        value=request.value,
        delta=request.delta,
        scope_key=request.scope_key,
        reason=request.reason,
    )
    async with db.connect() as conn:
        state = await check_settable(conn, story_id, name)
        await _precheck_value(conn, story_id, state, change)
    written: list[states.Applied] = []

    async def write(conn: AsyncConnection) -> None:
        await check_settable(conn, story_id, name)
        applied = await states.apply_change(conn, op, consumer_change_at(change, slot_position(op)))
        if isinstance(applied, Violation):
            raise refused(name, applied)
        written.append(applied)

    operation_id = new_ulid()
    async with turn_slot(db, story_id, operation_id):
        op = open_inter_turn(
            db,
            story_id,
            source=Provenance.CONSUMER,
            principal=principal,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
        )
        op.holds_turn_slot = True
        async with op:
            op.append(
                "state_set",
                StateSetEvent(source=CONSUMER, kind=change.kind.value),
                provenance=Provenance.CONSUMER,
            )
            op.add_commit_step(write)
            await op.commit()
    applied = written[0]
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return StateWritten(
        state=name,
        old_value=_json(applied.old_value),
        new_value=_json(applied.new_value),
        position=boundary_label(applied.position.turn, applied.position.slot_k),
        slot=applied.position.document(),
        operation_id=op.operation_id,
        change_id=applied.change_id,
        timeline_revision=op.timeline_revision,
    )


def consumer_change_at(change: StateChange, position: Position) -> StateChange:
    """``change`` at its slot's position."""
    return dataclasses.replace(change, position=position)


async def _check_declarable(
    conn: AsyncConnection, story_id: str, declaration: Mapping[str, Any]
) -> StateDefinition:
    checked = validate_definition(declaration)
    await states.require_module(conn, story_id)
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
    return checked


async def define_state(
    db: AsyncEngine,
    story_id: str,
    request: StateDefineRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> StateDeclared:
    """The consumer's declaration of a State at the next inter-turn slot (see the module
    docstring): 409 ``state_name_taken`` for a name an active State holds, 422
    ``validation_error`` for a shape error or ``world_clock`` (``reason: reserved_state``), 409
    ``module_off``."""
    from epistrel.core.ids import new_ulid
    from epistrel.core.stories import require_writable  # here: stories declares States

    await require_writable(db, story_id)
    declaration = request.definition.definition()
    async with db.connect() as conn:
        await _check_declarable(conn, story_id, declaration)
    stored: list[StateDefinition] = []

    async def write(conn: AsyncConnection) -> None:
        stored.append(await states.define_state(conn, op, story_id, declaration))

    operation_id = new_ulid()
    async with turn_slot(db, story_id, operation_id):
        op = open_inter_turn(
            db,
            story_id,
            source=Provenance.CONSUMER,
            principal=principal,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
        )
        op.holds_turn_slot = True
        async with op:
            op.append(
                "state_defined", StateDefinedEvent(source=CONSUMER), provenance=Provenance.CONSUMER
            )
            op.add_commit_step(write)
            await op.commit()
    position = slot_position(op)
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return StateDeclared(
        definition=stored[0].document(),
        position=boundary_label(position.turn, position.slot_k),
        slot=position.document(),
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
    )


async def update_state_definition(
    db: AsyncEngine, story_id: str, name: str, patch: Mapping[str, Any], *, principal: str
) -> StateDefinitionWrite:
    """Change State ``name``'s definition (a JSON merge patch over its mutable members): #185's
    ``update_definition``, a story configuration change. ``world_clock``'s settings change
    through ``PATCH /stories/{id}/clock`` (422 ``reserved_state`` here)."""
    if name == WORLD_CLOCK:
        raise InvalidRequest(
            f"{WORLD_CLOCK!r} is the built-in clock: change its settings with "
            "PATCH /stories/{story_id}/clock",
            {
                "field": "state",
                "reason": "reserved_state",
                "use": "PATCH /stories/{story_id}/clock",
            },
        )
    async with db.begin() as conn:
        await _story(conn, story_id)
        await states.require_module(conn, story_id)
        before = await states.definition(conn, story_id, name)
        after = await states.update_definition(
            conn, story_id, name, dict(patch), principal=principal
        )
        row = (
            await conn.execute(
                select(stories.c.config_revision, stories.c.timeline_revision).where(
                    stories.c.id == story_id
                )
            )
        ).one()
    old, new = before.document(), after.document()
    return StateDefinitionWrite(
        definition=new,
        config_revision=int(row.config_revision),
        timeline_revision=int(row.timeline_revision),
        changed=old != new,
    )


__all__ = [
    "CONSUMER",
    "SCOPES",
    "StateConstraintsBody",
    "StateDeclaration",
    "StateDeclared",
    "StateDefineRequest",
    "StateDefinitionWrite",
    "StateRead",
    "StateSetRequest",
    "StateView",
    "StateWritten",
    "StatesView",
    "check_settable",
    "consumer_change",
    "consumer_change_at",
    "define_state",
    "read_state",
    "read_states",
    "readable",
    "refused",
    "set_state",
    "sources_admit",
    "turn_slot",
    "update_state_definition",
]
