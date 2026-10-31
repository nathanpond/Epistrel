"""The world clock: every story's built-in clock State and the one source of story-time
(FR-CLOCK-1,2,3,5,12,13,14; docs/02 §7.1; #193).

**The clock is a State.** Genesis declares ``world_clock`` (type ``clock``, owner ``story``,
``player_visible``; ``core.states.model.world_clock_definition``) in the story's ``genesis``
operation, so every story has one and its definition carries the clock's settings (the mode,
the anchor, the pacing table, the skip cap; ``ClockSettings``). Its value is an exact offset in
minutes from the story's start, the fold of its active ``delta`` changes (``core.states``), so a
truncation, an untruncation, a swap, a regenerate and a fork move it with everything else and
nothing here handles them.

- ``clock_at(conn, story_id, through)`` is the clock's value at a position: the advances at or
  before it on the active timeline (leaving out another variant's of its own turn, as a
  regenerated turn is not yet active), and the anchor (#195; ``None``: unanchored).
- ``advance(conn, op, story_id, *, minutes | to_offset, source, position, reason,
  estimate_meta=None)`` writes one advance in ``op``'s commit transaction (a ``state_changes``
  row, ``kind = delta``, ``source`` a clock source), then runs the post-advance callbacks
  (``core.world_clock.hooks``; #192's ``elapsed`` State rules) in the same transaction. A
  negative ``minutes``, or a ``to_offset`` before the clock's value at ``position``, is 422
  ``clock_backward`` and writes nothing: the clock never moves backward along the active
  timeline (FR-CLOCK-8).
- ``clock_before_slot(conn, story_id, turn, slot_k)`` is the clock as inter-turn slot
  ``turn⁺slot_k`` opens: through the end of ``turn`` and its earlier slots.
- ``story_time_at(conn, story_id, position)`` is **the** story-time entry point (FR-CLOCK-2):
  ``stories.story_minute_zero`` plus the clock's offset as ``position`` starts (the advances
  before it: an advance at a beat or a slot ends it, FR-CLOCK-17) when the story runs the clock
  (``clock.level`` ``on`` or ``prompt_only``), so the clock's offset and M4's story-minutes share
  one scale; otherwise M4's stand-in (``minute_zero + turn x 10``, #131, #139), kept only here
  for stories pinned to a clock-off profile version. ``time_basis(conn, story_id)`` is the same
  rule as a pure ``TimeBasis`` (``TimeBasis.at(position)``; ``through`` is ``clock_at``'s
  inclusive value) for callers that resolve many positions (#141's time grammar,
  normalization, retrieval windows); ``clock_off_basis()`` is the stand-in a caller with no
  story uses. No other module builds a ``TimeBasis`` (``tests/integration/
  test_story_time_source.py``). An operation's events are stamped as it commits, before its
  commit steps: an advance a turn writes in a step (#194's pacing) must be made known to the
  stamp of the beats after it (#194's).

**The anchor** (#195; ``core.world_clock.anchor``): the clock's absolute time, never more than
the story established. Each statement that establishes or refines it is a commitment, a fact on
``clock_anchor`` at its position with ``visibility = engine`` (``commit_anchor``; only an
objective narrator's, director's or author's statement anchors, #144), and ``anchor_at(conn,
story_id, position)`` is the fold of the active anchor facts through the position on the
author's starting anchor (the ``anchor`` setting; ``None``: unanchored), so a truncation
un-anchors and a fork copies. ``clockwork`` is the clock's calendar and bands. With the clock
on, ``time_basis`` carries the anchor and the bands, and its ``epoch`` is the one a full
date-and-time anchor implies (never otherwise: month and year granules decide nothing on an
unanchored story); ``stories.story_epoch`` stays the clock-off form.

**The module gate** (``clock.level``, FR-PROF-2): ``off`` makes the clock inert (no advance is
ever written, 409 ``module_off``; story-time is the stand-in), ``prompt_only`` (#203's Lite form)
writes advances and verifies nothing, ``on`` runs it all. The clock is exempt from
``states.level``.

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import states
from epistrel.core.enums import StateChangeKind, StateChangeSource, StateChangeStatus
from epistrel.core.errors import ClockBackward, InvalidRequest, ModuleOff, StoryNotFound
from epistrel.core.membership import Position, active_only
from epistrel.core.resolution.time import YEAR, TimeBasis, as_position
from epistrel.core.resolution.time import Advance as BasisAdvance
from epistrel.core.resolution.time import Position as AnyPosition
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.states.model import (
    CLOCK_SOURCES,
    COUNTED,
    WORLD_CLOCK,
    ClockSettings,
    StateChange,
    StateDefinition,
    Violation,
)
from epistrel.core.tables import stories
from epistrel.core.tables.states import state_changes, state_definitions
from epistrel.core.world_clock import hooks
from epistrel.core.world_clock.anchor import Anchor, Clockwork, Statement
from epistrel.core.world_clock.anchor import fold as fold_anchor

if TYPE_CHECKING:
    from epistrel.core.canon.facts import Fact, FactOperation
    from epistrel.core.enums import RecordProvenance
    from epistrel.core.events.operation import Operation

MODULE = "clock"
HEAD = Position(2**31 - 1)
"""A position after every turn and slot: the clock as the story stands."""
"""The profile module that runs the clock (``clock.level``)."""


@dataclass(frozen=True, slots=True)
class ClockValue:
    """The clock at a position: its offset in minutes from the story's start, its anchor
    (#195's ``Anchor.document()``; ``None``: unanchored) and the absolute time the anchor gives
    there (its known components and ranges; ``None``: none known)."""

    offset_minutes: int
    anchor: dict[str, Any] | None = None
    absolute: str | None = None

    def display(self) -> str:
        """The clock as a reader sees it: ``+HHh MMm`` from the start, followed by the anchor's
        known components and ranges when it has any (``+02h 10m (Saturday, between 05:00 and
        12:00 (morning))``); a date or minute the anchor does not fix is never shown."""
        offset = display(self.offset_minutes)
        return offset if self.absolute is None else f"{offset} ({self.absolute})"

    def document(self) -> dict[str, Any]:
        """``{offset, display}``, as ``respond`` and ``GET /clock`` return it."""
        return {"offset": self.offset_minutes, "display": self.display()}


@dataclass(frozen=True, slots=True)
class Advance:
    """A written advance: its change, the clock before and after it, where it sits, its
    source, and its story-minute."""

    change_id: str
    before: int
    after: int
    position: Position
    source: StateChangeSource
    valid_time_minutes: int


def display(offset_minutes: int) -> str:
    """``+HHh MMm``: an offset from the story's start (hours are not wrapped into days)."""
    hours, minutes = divmod(int(offset_minutes), 60)
    return f"+{hours:02d}h {minutes:02d}m"


# -- the gate ---------------------------------------------------------------------------------


async def clock_level(conn: AsyncConnection, story_id: str) -> str | None:
    """The level the story's effective configuration runs ``clock`` at (``None``: off, or a
    level this deployment has not delivered)."""
    from epistrel.core.profiles import schema  # here: the profiles import the settings schema
    from epistrel.core.profiles.effective import effective_configuration

    settings = (await effective_configuration(conn, story_id, validate=False)).settings
    level = schema.level_of(settings, MODULE)
    if level is None or not schema.is_delivered(MODULE, level):
        return None
    return level


async def require_clock(conn: AsyncConnection, story_id: str) -> str:
    """409 ``module_off`` unless the story runs the clock."""
    level = await clock_level(conn, story_id)
    if level is None:
        raise ModuleOff(
            "the story's configuration has the world clock off",
            {"story_id": story_id, "module": MODULE},
        )
    return level


# -- reading --------------------------------------------------------------------------------


async def clock_definition(
    conn: AsyncConnection, story_id: str, *, lock: bool = False
) -> StateDefinition:
    """The story's ``world_clock`` declaration (404 ``not_found`` for an unknown story)."""
    if lock:
        return await states.definition(conn, story_id, WORLD_CLOCK, lock=True)
    return await states.definition(conn, story_id, WORLD_CLOCK)


async def settings(conn: AsyncConnection, story_id: str) -> ClockSettings:
    """The clock's settings."""
    found = (await clock_definition(conn, story_id)).clock
    assert found is not None  # noqa: S101 - the clock's definition carries its settings
    return found


async def _advances(
    conn: AsyncConnection, story_id: str, *, view: states.View = states.View.ACTIVE
) -> list[BasisAdvance]:
    """The clock's counted changes in a view, in position order, as ``(key, variant,
    minutes)``."""
    c, d = state_changes.c, state_definitions.c
    clock_id = select(d.id).where(d.story_id == story_id, d.name == WORLD_CLOCK).scalar_subquery()
    where: list[ColumnElement[bool]] = [
        c.story_id == story_id,
        c.state_id == clock_id,
        c.status.in_(sorted(s.value for s in COUNTED)),
    ]
    if states.View(view) is states.View.ACTIVE:
        where.append(active_only(state_changes))
    found = await conn.execute(
        select(c.id, c.narrative_turn, c.turn_variant, c.slot_k, c.beat, c.step, c.delta).where(
            *where
        )
    )
    rows = sorted(found, key=lambda r: ((r.narrative_turn, r.slot_k, r.beat, r.step), str(r.id)))
    return [
        (
            (int(r.narrative_turn), int(r.slot_k), int(r.beat), int(r.step)),
            int(r.turn_variant),
            int(Decimal(r.delta)),
        )
        for r in rows
    ]


async def clock_at(
    conn: AsyncConnection,
    story_id: str,
    through: AnyPosition,
    *,
    view: states.View | str = states.View.ACTIVE,
) -> ClockValue:
    """The clock at ``through`` (see the module docstring); ``as_recorded`` folds every advance
    ever recorded. The anchor is the one established through ``through`` (#195)."""
    found = await settings(conn, story_id)
    basis = TimeBasis(advances=tuple(await _advances(conn, story_id, view=states.View(view))))
    offset = basis.through(through)
    anchor = await _fold(conn, story_id, found, as_position(through))
    if anchor.is_unanchored:
        return ClockValue(offset)
    return ClockValue(offset, anchor.document(), anchor.display(offset))


# -- the anchor (#195) ------------------------------------------------------------------------


def clockwork(found: ClockSettings) -> Clockwork:
    """The clock's calendar and time-of-day bands."""
    return Clockwork(found.calendar, tuple(found.bands))


def base_anchor(found: ClockSettings) -> Anchor:
    """The anchor before any statement: the author's starting ``anchor`` setting (the same
    document ``Anchor.document`` writes), or unanchored."""
    if found.anchor:
        return Anchor.from_document(found.anchor, clockwork(found))
    return Anchor.unanchored(clockwork(found))


def _statement_of(fact: Fact, works: Clockwork) -> tuple[Statement, int] | None:
    """An anchor fact's statement and offset, its weekday read by name in the calendar as it
    now stands (a calendar changed since has no such weekday: the statement keeps the rest)."""
    literal = fact.proposition.literal
    if literal is None or not isinstance(literal.value, str):
        return None
    try:
        raw = json.loads(literal.value)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("statement"), dict):
        return None
    statement = Statement.from_document(raw["statement"])
    if statement.weekday is not None:
        name = raw.get("weekday_name")
        names = works.weekdays
        weekday = names.index(name) if isinstance(name, str) and name in names else None
        statement = replace(statement, weekday=weekday)
    return statement, int(raw.get("offset", 0))


def _fact_position(fact: Fact) -> Position:
    return Position(
        int(fact.narrative_turn or 0),
        int(fact.turn_variant or 0),
        int(fact.slot_k or 0),
        int(fact.beat or 0),
    )


async def _fold(
    conn: AsyncConnection,
    story_id: str,
    found: ClockSettings,
    through: Position,
) -> Anchor:
    from epistrel.core.canon.facts import canon  # here: canon reads build on this package
    from epistrel.core.propositions.vocabulary import CLOCK_ANCHOR

    anchors = await canon(
        conn, story_id, predicate=CLOCK_ANCHOR, through_position=through, viewpoint=OMNISCIENT
    )
    ordered = sorted(anchors, key=lambda f: (_fact_position(f).key(), f.id))
    statements = []
    for fact in ordered:
        stated = _statement_of(fact, clockwork(found))
        if stated is not None:
            statements.append((stated[0], stated[1], _fact_position(fact)))
    return fold_anchor(statements, base_anchor(found))


async def anchor_at(conn: AsyncConnection, story_id: str, position: AnyPosition = HEAD) -> Anchor:
    """The clock's anchor through ``position`` (see the module docstring): the fold of the
    active ``clock_anchor`` facts at or before it. Its ``revision`` counts the statements that
    built it (#197 keys on it)."""
    found = await settings(conn, story_id)
    return await _fold(conn, story_id, found, as_position(position))


async def anchor_revision(
    conn: AsyncConnection, story_id: str, position: AnyPosition = HEAD
) -> int:
    """How many anchor statements are active through ``position``."""
    return (await anchor_at(conn, story_id, position)).revision


async def commit_anchor(
    conn: AsyncConnection,
    story_id: str,
    statement: Statement,
    *,
    operation: FactOperation,
    provenance: RecordProvenance,
    beat: int | None = None,
    slot_k: int | None = None,
) -> Fact | None:
    """Establish or refine the anchor with ``statement``, made at ``operation``'s position (its
    ``beat``), in its commit transaction (see the module docstring): a ``clock_anchor`` fact
    whose literal is the statement and the clock's offset there, with ``visibility = engine``.
    ``None`` (nothing written) when the story does not run the clock or the statement
    contradicts the anchor as it stands there (a statement verified against an anchor an
    earlier beat of the same turn narrowed)."""
    from epistrel.core.canon.facts import commit_fact
    from epistrel.core.propositions.model import (
        Granularity,
        Instant,
        LiteralKind,
        Proposition,
        TypedLiteral,
    )
    from epistrel.core.propositions.vocabulary import CLOCK_ANCHOR
    from epistrel.core.visibility import Visibility

    if await clock_level(conn, story_id) is None or operation.narrative_turn is None:
        return None
    position = Position(
        int(operation.narrative_turn),
        int(operation.turn_variant or 0),
        int(slot_k or 0),
        int(beat or 0),
    )
    basis = await time_basis(conn, story_id)
    offset = basis.at(position) - basis.minute_zero
    current = await anchor_at(conn, story_id, position)
    if not isinstance(current.refine(statement, offset, position), Anchor):
        return None
    clock = await clock_definition(conn, story_id)
    assert clock.id is not None  # noqa: S101 - a stored declaration has its id
    stored: dict[str, Any] = {"statement": statement.document(), "offset": offset}
    if statement.weekday is not None:
        stored["weekday_name"] = current.clockwork.weekdays[statement.weekday]
    literal = json.dumps(stored, separators=(",", ":"))
    return await commit_fact(
        conn,
        story_id,
        Proposition(
            subject_id=clock.id,
            predicate_id=CLOCK_ANCHOR,
            literal=TypedLiteral(kind=LiteralKind.STR, value=literal),
            valid_time=Instant.of(basis.at(position), Granularity.MINUTE),
        ),
        provenance=provenance,
        visibility=Visibility(scope="engine"),
        operation=operation,
        beat=beat,
        slot_k=slot_k,
    )


async def clock_before_slot(
    conn: AsyncConnection, story_id: str, turn: int, slot_k: int
) -> ClockValue:
    """The clock as slot ``turn⁺slot_k`` opens: through the end of ``turn`` (``slot_k`` 1) or
    of its slot ``slot_k - 1``."""
    if slot_k < 1:
        raise InvalidRequest("an inter-turn slot is 1 or later", {"field": "slot_k"})
    return await clock_at(conn, story_id, Position.end_of(turn, slot_k - 1))


async def _story_row(conn: AsyncConnection, story_id: str) -> Any:
    row = (
        await conn.execute(
            select(stories.c.story_minute_zero, stories.c.story_epoch).where(
                stories.c.id == story_id
            )
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return row


def clock_off_basis(minute_zero: int = 0, epoch: datetime | None = None) -> TimeBasis:
    """The clock-off stand-in basis (M4's rule) for a caller with no story's (a fixture's
    default, an eval case)."""
    return TimeBasis(minute_zero=minute_zero, epoch=epoch)


async def time_basis(conn: AsyncConnection, story_id: str) -> TimeBasis:
    """The story's time basis (see the module docstring): the clock's active advances, its
    anchor and bands when the story runs the clock, the stand-in otherwise."""
    row = await _story_row(conn, story_id)
    zero, epoch = int(row.story_minute_zero), row.story_epoch
    if await clock_level(conn, story_id) is None:
        return clock_off_basis(zero, epoch)
    found = await settings(conn, story_id)
    advances = tuple(await _advances(conn, story_id))
    anchor = await _fold(conn, story_id, found, HEAD)
    return TimeBasis(
        minute_zero=zero,
        epoch=anchor_epoch(anchor, zero),
        advances=advances,
        anchor=None if anchor.is_unanchored else anchor,
        bands=tuple(found.bands),
    )


def anchor_epoch(anchor: Anchor, minute_zero: int = 0) -> datetime | None:
    """The calendar instant of story-minute 0 a Gregorian anchor that fixes the full date and
    time implies (``None`` for any other anchor: FR-CLOCK-3 never picks one)."""
    if not anchor.clockwork.calendar.gregorian:
        return None
    found = anchor.epoch_minutes()
    if found is None:
        return None
    year, month, day, minute = found
    return datetime(year, month, day, tzinfo=UTC) + timedelta(minutes=minute - minute_zero)


async def story_time_at(conn: AsyncConnection, story_id: str, position: AnyPosition) -> int:
    """The story-minute of ``position`` (see the module docstring)."""
    return (await time_basis(conn, story_id)).at(position)


async def elapsed_since(conn: AsyncConnection, story_id: str) -> Callable[[Position], int]:
    """The minutes on the clock from a position to the clock as the story stands (FR-CAT-12's
    ageing, #193): 0 for every position when the story runs the clock off (the stand-in is
    not a clock, so nobody ages by it)."""
    if await clock_level(conn, story_id) is None:
        return lambda _: 0
    basis = TimeBasis(advances=tuple(await _advances(conn, story_id)))
    now = basis.at(HEAD)
    return lambda since: max(0, now - basis.through(since))


def full_years(minutes: int) -> int:
    """The full years in ``minutes`` of story-time (a year is 365.25 days)."""
    return max(0, minutes) // YEAR


# -- advancing -----------------------------------------------------------------------------


async def advance(
    conn: AsyncConnection,
    op: Operation,
    story_id: str,
    *,
    minutes: int | None = None,
    to_offset: int | None = None,
    source: StateChangeSource,
    position: Position,
    reason: str,
    estimate_meta: dict[str, Any] | None = None,
) -> Advance:
    """Advance the clock in ``op``'s commit transaction (see the module docstring)."""
    if (minutes is None) == (to_offset is None):
        raise InvalidRequest("an advance gives minutes or a target offset", {"field": "minutes"})
    source = StateChangeSource(source)
    if source not in CLOCK_SOURCES:
        raise InvalidRequest(
            f"{source.value!r} is not a clock source", {"field": "source", "reason": "source"}
        )
    await require_clock(conn, story_id)
    current = (await clock_at(conn, story_id, position)).offset_minutes
    wanted = int(minutes) if minutes is not None else int(to_offset or 0) - current
    if wanted < 0:
        attempted = current + wanted if minutes is not None else to_offset
        raise ClockBackward(
            "the world clock never moves backward: truncate or rewrite to reach earlier story-time",
            {"story_id": story_id, "current": current, "attempted": attempted},
        )
    applied = await states.apply_change(
        conn,
        op,
        StateChange(
            state=WORLD_CLOCK,
            reason=reason,
            source=source,
            kind=StateChangeKind.DELTA,
            delta=wanted,
            position=position,
            status=StateChangeStatus.COMMITTED,
            estimate_meta=estimate_meta,
        ),
    )
    if isinstance(applied, Violation):  # pragma: no cover - a forward advance breaks nothing
        raise ClockBackward(
            "the world clock never moves backward",
            {"story_id": story_id, "current": current, "attempted": current + wanted},
        )
    written = Advance(
        applied.change_id,
        current,
        current + wanted,
        applied.position,
        source,
        applied.valid_time_minutes,
    )
    # The elapsed State rules run after every advance (#192): importing them registers their
    # post-advance callback (here: they import this module).
    from epistrel.core.states import rules  # noqa: F401

    await hooks.run_post_advance(conn, op, story_id, written)
    return written


__all__ = [
    "HEAD",
    "MODULE",
    "Advance",
    "ClockSettings",
    "ClockValue",
    "advance",
    "anchor_at",
    "anchor_epoch",
    "anchor_revision",
    "base_anchor",
    "clock_at",
    "clock_before_slot",
    "clock_definition",
    "clock_level",
    "clock_off_basis",
    "clockwork",
    "commit_anchor",
    "display",
    "elapsed_since",
    "full_years",
    "require_clock",
    "settings",
    "story_time_at",
    "time_basis",
]
