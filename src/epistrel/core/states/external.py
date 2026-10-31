"""Externally owned State values as Core reads them (FR-EXT-2, FR-VERIFY-7, FR-STORE-17; #205).

An externally owned State's value is never folded from ``state_changes``: it is what the
provider answered, pinned once per turn and recorded as an ``external_read`` event (the
Orchestrator's ``orchestrator.providers.pinned.PinnedReads``), folded into the
``external_values`` projection (``core.projections.external``). Core never calls a provider and
never imports ``orchestrator.providers``: it reads what was pinned.

**Two sources, one answer** (``pinned_values``):

- **Inside a turn** the Orchestrator installs its pins as the current ``PinSource`` (``pinning``)
  for the whole turn, its commit included. Its reads are staged, not yet committed, so Core asks
  the source: the version pinned at or before the position read (``PinSource.pinned``). A State
  the turn has not pinned has no value (FR-EXT-2: no earlier turn's read stands in for this
  turn's, and nothing reads speculatively).
- **Outside a turn** (a later verification of a recorded position, an inspection), and inside
  one for a **frame-bounded read** of an earlier turn (``through`` before the pins' turn: #164's
  lazy resolution, #165's could-have-known replay; #207), Core reads the committed
  ``external_values``, never the current pin: the latest version at or before the position, of
  an operation on the active timeline (``core.membership.active_operation``, so a truncated or
  replaced turn's read no longer answers, and a regenerate's own replaced variant is left out).

**Inspection** (``inspect``, #207; ``GET /stories/{id}/inspect/external-facts``, FR-ADMIN-5):
the story's ``external_read`` events on the active timeline with the facts citing them, an
omniscient raw read (the caller applies ``core.epistemic.inspection.omniscient_read``).

Pure but for ``pinned_values``'s and ``inspect``'s reads; logs nothing (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import ExternalValueSource, StateOwner
from epistrel.core.membership import (
    Position,
    active_operation,
    position_key,
    position_literal,
)
from epistrel.core.states.model import StateDefinition, Value, from_json
from epistrel.core.tables import operations
from epistrel.core.tables.states import STORY_SCOPE_KEY, external_values

if TYPE_CHECKING:
    from epistrel.core.canon.facts import Fact

INSPECT_LIMIT_DEFAULT = 100
INSPECT_LIMIT_MAX = 500
"""``inspect``'s page size and its cap (#178's inspection reads keep the same)."""

Key = tuple[str, str | None]
"""An external value within a story: ``(state id, scope key)`` (``None``: story-scoped)."""


@dataclass(frozen=True, slots=True)
class ExternalPin:
    """One version of an externally owned State's value: the State, the scope key, the typed
    value (``None``: unknown), where it was read, its version and source, and its
    ``external_read`` event (``None`` while the turn that read it has not committed)."""

    state_id: str
    scope_key: str | None
    value: Value | None
    position: Position
    version: int = 1
    source: ExternalValueSource = ExternalValueSource.READ
    event_id: str | None = None


class PinSource(Protocol):
    """A turn's pinned reads as Core reads them (``orchestrator.providers.pinned.PinnedReads``)."""

    @property
    def turn(self) -> int:
        """The narrative turn the pins were read for."""
        ...

    def pinned(self, state_id: str, scope_key: str | None, through: Position | None) -> Any:
        """The ``ExternalPin`` of ``(state_id, scope_key)`` pinned at or before ``through``
        (``None``: the latest), or ``None`` when the turn has pinned none."""
        ...


_PINS: ContextVar[PinSource | None] = ContextVar("epistrel_external_pins", default=None)


@contextmanager
def pinning(source: PinSource) -> Iterator[PinSource]:
    """Make ``source`` the current turn's pins for the block (see the module docstring)."""
    token = _PINS.set(source)
    try:
        yield source
    finally:
        _PINS.reset(token)


def current() -> PinSource | None:
    """The current turn's pins, or ``None`` outside a turn."""
    return _PINS.get()


def external(states: Sequence[StateDefinition]) -> list[StateDefinition]:
    """The externally owned ones among ``states``."""
    return [s for s in states if s.owner is StateOwner.EXTERNAL and s.id is not None]


def _at_or_before(position: Position, through: Position | None) -> bool:
    return through is None or position.at_or_before(through)


async def pinned_values(
    conn: AsyncConnection,
    story_id: str,
    states: Sequence[StateDefinition],
    keys: Sequence[Key],
    *,
    through: Position | None = None,
) -> dict[Key, ExternalPin]:
    """The value of each of ``keys`` (externally owned ``states``) at or before ``through``
    (``None``: the latest): the current turn's pin, or outside a turn the latest committed
    version on the active timeline (see the module docstring). A key with none is absent."""
    by_id = {s.id: s for s in external(states)}
    wanted = [k for k in keys if k[0] in by_id]
    if not wanted:
        return {}
    source = current()
    if source is not None and (through is None or through.turn >= source.turn):
        found: dict[Key, ExternalPin] = {}
        for key in wanted:
            pin = source.pinned(key[0], key[1], through)
            if isinstance(pin, ExternalPin):
                found[key] = pin
        return found
    v = external_values.c
    query = (
        select(external_values)
        .join(operations, operations.c.operation_id == v.operation_id)
        .where(
            v.story_id == story_id,
            v.state_id.in_(sorted({k[0] for k in wanted})),
            active_operation(operations),
        )
    )
    if through is not None:
        query = query.where(position_key(external_values, "position") <= position_literal(through))
    rows = await conn.execute(query)
    latest: dict[Key, tuple[tuple[int, ...], ExternalPin]] = {}
    want = set(wanted)
    for row in rows:
        scope_key = None if row.scope_key == STORY_SCOPE_KEY else str(row.scope_key)
        key = (str(row.state_id), scope_key)
        if key not in want:
            continue
        position = Position.of_columns(dict(row._mapping), "position")
        assert position is not None  # noqa: S101 - the column group is NOT NULL
        order = (*position.key(), int(row.version))
        if key in latest and latest[key][0] >= order:
            continue
        latest[key] = (
            order,
            ExternalPin(
                state_id=key[0],
                scope_key=scope_key,
                value=from_json(by_id[key[0]].type, row.value),
                position=position,
                version=int(row.version),
                source=ExternalValueSource(row.source),
                event_id=str(row.event_id),
            ),
        )
    return {key: pin for key, (_, pin) in latest.items() if _at_or_before(pin.position, through)}


# -- external facts (#207) ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExternalFactItem:
    """One ``external_read`` (#205) with the facts that reference it: the event, the provider
    and the value's name, the State and scope key (``None``: story-scoped), the value read
    (``None``: unknown), when it was read, its turn and variant, its source (``live``,
    ``last_known``, ``unknown``) and every fact version citing it (``source_event_ids``)."""

    event_id: str
    provider: str
    value_name: str
    state_id: str | None
    scope_key: str | None
    value: Any
    read_at: str | None
    turn: int | None
    variant: int | None
    source: str
    facts: tuple[Fact, ...]


@dataclass(frozen=True, slots=True)
class ExternalFactPage:
    story_id: str
    revision: int
    items: tuple[ExternalFactItem, ...]
    next_after_id: str | None


async def inspect(
    db: AsyncEngine,
    story_id: str,
    *,
    as_of: int | None = None,
    limit: int = INSPECT_LIMIT_DEFAULT,
    after_id: str | None = None,
) -> ExternalFactPage:
    """The story's snapshotted external facts (FR-ADMIN-5, FR-EXT-2,4; #207): its
    ``external_read`` events on the active timeline, positioned at or before turn ``as_of``, by
    id after ``after_id``, ``limit`` a page, each with the facts that cite it. A raw read: no
    provider is called. No partitions gate applies (a value read is no character's knowledge),
    so a story with no reads answers an empty page. 404 for an unknown story."""
    from epistrel.core.canon import facts as canon_facts
    from epistrel.core.concurrency import check_expected_revision
    from epistrel.core.events.types import EXTERNAL_READ
    from epistrel.core.tables import events

    limit = max(1, min(INSPECT_LIMIT_MAX, limit))
    e = events.c
    query = (
        select(e.id, e.payload, e.narrative_turn, e.turn_variant)
        .select_from(events.join(operations, operations.c.operation_id == e.operation_id))
        .where(e.story_id == story_id, e.type == EXTERNAL_READ, active_operation(operations))
        .order_by(e.id)
        .limit(limit + 1)
    )
    if after_id is not None:
        query = query.where(e.id > after_id)
    if as_of is not None:
        query = query.where(e.narrative_turn <= as_of)
    async with db.connect() as conn:
        revision = await check_expected_revision(conn, story_id, None)
        rows = list(await conn.execute(query))
        page, more = rows[:limit], len(rows) > limit
        cited = await canon_facts.citing(conn, story_id, [str(r.id) for r in page])
    by_event: dict[str, list[Fact]] = {}
    for fact in cited:
        for event_id in fact.source_event_ids:
            by_event.setdefault(event_id, []).append(fact)
    items = []
    for row in page:
        payload = dict(row.payload or {})
        items.append(
            ExternalFactItem(
                event_id=str(row.id),
                provider=str(payload.get("provider", "")),
                value_name=str(payload.get("value_name", "")),
                state_id=payload.get("state_id"),
                scope_key=payload.get("scope_key"),
                value=payload.get("value"),
                read_at=payload.get("read_at"),
                turn=row.narrative_turn,
                variant=row.turn_variant,
                source=str(payload.get("source", "")),
                facts=tuple(by_event.get(str(row.id), ())),
            )
        )
    return ExternalFactPage(
        story_id, revision, tuple(items), items[-1].event_id if more and items else None
    )


__all__ = [
    "INSPECT_LIMIT_DEFAULT",
    "INSPECT_LIMIT_MAX",
    "ExternalFactItem",
    "ExternalFactPage",
    "ExternalPin",
    "Key",
    "PinSource",
    "current",
    "external",
    "inspect",
    "pinned_values",
    "pinning",
]
