"""A State's deterministic rules, applied (FR-STATE-7; docs/02 §7.1; #192).

"Updated by rules" is limited to constraints plus two triggers (``core.states.expressions``
holds their shapes and the derived arithmetic); there is no scripting language, no model call
and no background job, and a replay (the projection's fold over the stored changes)
reproduces the same values with nothing re-run.

- **Elapsed** (``{kind: elapsed, per_minutes, delta, bounds_clamp}``): ``apply_elapsed`` is a
  post-advance callback (``core.world_clock.hooks``) registered when this module is imported
  (``core.world_clock.advance`` imports it). After each committed clock advance it writes,
  in the advance's transaction and at its position (so the ticks follow the advance's
  operation's membership), one ``delta`` change with ``source = rule`` per whole
  ``per_minutes`` the clock has passed since the rule's last tick. Each tick's
  ``valid_time_minutes`` is the story-minute it falls due, so the last tick is the latest
  active ``rule`` change's (``last_applied_minute``, derived from the stored rows, never kept
  elsewhere). Ticks count from when the rule applies: the clock as the State was declared, or,
  for a rule set by ``update_definition``, the clock at that configuration change's position
  (``core.states.change_position``), whichever is later than the last tick. A tick that would
  break a bound moves only up to it with ``bounds_clamp``, and is held (a ``delta`` of 0)
  without; either way it is recorded, so it is not tried again.
- **Derived** (``{kind: derived, expression}``): a derived State stores no change. Its value is
  folded from its inputs on every read (``core.states.value_of``, ``current_values``,
  ``folded_at``): the expression governing the read's position (``core.states.rule_versions``:
  the declaration's, then each update's from its configuration change's position) over the
  inputs' values there, in the read's view. So it equals its expression over its inputs at
  every position, whatever changed an input (an earlier slot, a truncation, an untruncation,
  a swap, a fork), and nothing can make it stale. ``check_definition`` refuses, at declaration
  and on ``update_definition``, a derived State that reads anything but the story's active
  story-scoped number States (422 ``validation_error``, ``reason: rules_input``) or that closes a
  cycle (``reason: rules_cycle``). A derived State refuses every change
  (``core.states.apply_change``: ``Violation("derived")``; the State API's 422
  ``state_derived``), and it has no row in the ``state_values`` projection.

Rules run only with ``states.level`` on or ``prompt_only`` (``module_on("states")``); nothing
here logs (invariant 3).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import states, world_clock
from epistrel.core.enums import StateChangeKind, StateChangeSource, StateScope
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position, active_only
from epistrel.core.states.expressions import find_cycle
from epistrel.core.states.model import (
    NUMERIC_TYPES,
    StateChange,
    StateDefinition,
    UndefinedType,
    Violation,
    derived_rule,
    elapsed_rule,
    violation,
)
from epistrel.core.tables import operations, stories
from epistrel.core.tables.states import state_changes, state_definitions
from epistrel.core.world_clock import hooks

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation
    from epistrel.core.world_clock import Advance

ELAPSED_REASON = "story-time passed"
"""The ``reason`` of a rule's changes (identifiers only: no story text)."""


# -- definitions --------------------------------------------------------------------------------


def _graph(defined: list[StateDefinition]) -> dict[str, frozenset[str]]:
    """Each derived State's inputs, by name."""
    found: dict[str, frozenset[str]] = {}
    for state in defined:
        rule = derived_rule(state)
        if rule is not None:
            found[state.name] = rule.inputs
    return found


async def check_definition(
    conn: AsyncConnection, story_id: str, definition: StateDefinition
) -> None:
    """A derived ``definition``'s inputs and cycles, against the story's active States (see the
    module docstring); ``definition`` may replace the active State of its name (an update)."""
    rule = derived_rule(definition)
    defined = [s for s in await states.definitions(conn, story_id) if s.name != definition.name]
    graph = _graph(defined)
    if rule is None:
        return
    by_name = {s.name: s for s in defined}
    for name in sorted(rule.inputs):
        found = by_name.get(name)
        if found is None or found.scope is not StateScope.STORY or found.type not in NUMERIC_TYPES:
            raise InvalidRequest(
                f"a derived State reads story-scoped number States: {name!r} is not one",
                {"field": "rules", "reason": "rules_input", "state": name},
            )
    graph[definition.name] = rule.inputs
    cycle = find_cycle(graph)
    if cycle is not None:
        raise InvalidRequest(
            "derived States would read each other in a cycle: " + " -> ".join(cycle),
            {"field": "rules", "reason": "rules_cycle", "cycle": cycle},
        )


# -- elapsed rules -------------------------------------------------------------------------------


async def _minute_zero(conn: AsyncConnection, story_id: str) -> int:
    found = await conn.scalar(select(stories.c.story_minute_zero).where(stories.c.id == story_id))
    return int(found or 0)


async def _last_tick(conn: AsyncConnection, story_id: str, state: StateDefinition) -> int | None:
    """The story-minute of the State's latest active rule change (``None``: no tick yet)."""
    c = state_changes.c
    found = await conn.scalar(
        select(func.max(c.valid_time_minutes)).where(
            c.story_id == story_id,
            c.state_id == state.id,
            c.source == StateChangeSource.RULE.value,
            active_only(state_changes),
        )
    )
    return None if found is None else int(found)


async def _declared_at(conn: AsyncConnection, story_id: str, state: StateDefinition) -> Position:
    d, o = state_definitions.c, operations.c
    row = (
        await conn.execute(
            select(
                func.coalesce(o.narrative_turn, 0).label("turn"),
                func.coalesce(o.slot_k, 0).label("slot_k"),
            )
            .select_from(state_definitions.join(operations, o.operation_id == d.operation_id))
            .where(d.story_id == story_id, d.id == state.id)
        )
    ).one()
    return Position.end_of(int(row.turn), int(row.slot_k))


def _bounded(state: StateDefinition, current: Decimal, delta: Decimal, clamp: bool) -> Decimal:
    """The delta one tick applies: all of it, or (when it would break a bound) up to the bound
    with ``clamp`` and nothing without."""
    if violation(state, current + delta) is None:
        return delta
    if not clamp:
        return Decimal(0)
    c = state.constraints
    low = c.min
    if c.non_negative:
        low = Decimal(0) if low is None else max(low, Decimal(0))
    target = current + delta
    if c.max is not None and target > c.max:
        target = c.max
    if low is not None and target < low:
        target = low
    moved = target - current
    return moved if (moved > 0) == (delta > 0) else Decimal(0)


async def apply_elapsed(
    conn: AsyncConnection, op: Operation, story_id: str, advance: Advance
) -> None:
    """The post-advance callback (see the module docstring)."""
    if await states.state_level(conn, story_id) is None:
        return
    ruled = [(s, r) for s in await states.definitions(conn, story_id) if (r := elapsed_rule(s))]
    if not ruled:
        return
    zero = await _minute_zero(conn, story_id)
    for state, rule in ruled:
        # Ticks count from when the rule applies: the declaration, or the configuration change
        # that set the rule (#192), and after the last tick.
        applies = (await states.rule_versions(conn, story_id, state))[-1][0]
        if applies is None:
            applies = await _declared_at(conn, story_id, state)
        last = (await world_clock.clock_at(conn, story_id, applies)).offset_minutes
        stored = await _last_tick(conn, story_id, state)
        if stored is not None:
            last = max(last, stored - zero)
        ticks = max(0, (advance.after - last) // rule.per_minutes)
        if not ticks:
            continue
        found = await states.value_of(conn, story_id, state.name, through=advance.position)
        if isinstance(found, UndefinedType) or not isinstance(found.value, Decimal):
            continue
        current = found.value
        for tick in range(1, ticks + 1):
            delta = _bounded(state, current, rule.delta, rule.bounds_clamp)
            written = await states.apply_change(
                conn,
                op,
                StateChange(
                    state=state.name,
                    reason=ELAPSED_REASON,
                    source=StateChangeSource.RULE,
                    kind=StateChangeKind.DELTA,
                    delta=delta,
                    position=advance.position,
                    valid_time_minutes=zero + last + tick * rule.per_minutes,
                ),
            )
            if isinstance(written, Violation):  # pragma: no cover - _bounded keeps it in bounds
                break
            current = current + delta


hooks.register_post_advance(apply_elapsed)

__all__ = [
    "ELAPSED_REASON",
    "apply_elapsed",
    "check_definition",
]
