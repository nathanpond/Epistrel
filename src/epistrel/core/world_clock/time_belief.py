"""A character's time belief: characters know the time the way people do (FR-CLOCK-10,11,15,
FR-TURN-6; docs/02 §7.1; #200).

**What a character believes** (``time_belief(conn, story, character, position)`` →
``TimeBelief``) is computed, never stored, until something overrides it:

- **derived from the story's ``time_awareness``** (the ``world_clock`` definition, #193; basis
  ``awareness``): ``exact`` is the clock's absolute range at the position (#195's
  ``Anchor.absolute_at``: an unanchored clock leaves every time open); ``approximate`` is that
  range widened by an hour either way (the band and the hour, ±1); ``none`` is no estimate at
  all, only the time the character has perceived passing.
- **an override**: the character's latest open first-order belief on the core ``current_time``
  predicate (viewpoint-free: its subject is the clock State, its literal the time as read or
  told, its valid-time the instant; written only through ``transition()``, ADR-168) with an
  ``observation`` (a watch or clock beat; basis ``observation``) or ``testimony`` (#162; basis
  ``testimony``) antecedent. A ``time_is`` belief counts the same way: one the character
  observed (a wall clock a ``do`` or a narration it perceived shows: #164's observation sink) or
  accepted as testimony (#162 writes the proposition it heard). The override is carried
  forward by the time the character perceived since its valid-time: a stopped watch read at
  17:30 as "three" is 15:00 then and 15:20 twenty perceived minutes later.
- **a gap** (basis ``gap``): after a span canon held the character ``unconscious`` (asleep or
  passed out; ``core.epistemic.scene.unconscious_spans``), the derived belief as it stood when
  the span began, carried forward by the time perceived since: Marcus, out from 20:00 to 23:00,
  wakes believing it is still eight. A later override wins over it.

**Perceived elapsed time** (``perceived``) is the clock's advance less every span the character
perceived no time passing (``core.epistemic.scene.perception_gaps``: unconscious, or placed
nowhere once placed at all); ``TimeBelief.perceived_elapsed`` is the time perceived since the
character's current placement began (its scene), else since the story's start.

**Held schedules** (``TimeBelief.schedules``): the open canon scheduled events (#196) the
character holds (its owner, a shared owner or a participant), each at the time the character
holds it (its own ``scheduled`` belief about the event when it has one, #198's sincere error
included; else the canon planned time), then its belief-only schedules (a ``scheduled`` belief
about no event it holds: FR-CLOCK-9, never an event, it shapes only this).

``time_belief`` makes the reads; ``derive`` is the pure computation over them (the no-leak
property drives it).

**The belief relation** (``verdict``, pure): a clock check (``time_is``, ``time_of_day_band``,
``elapsed_since``, ``scheduled_status``) said by the character is checked against its belief,
never the clock (#166's belief dimension; ``core.world_clock.verify`` registers the hook,
``core.verify.rules.check_with`` asks it for a ``belief(A)`` holder):

- a time or a band is ``supported`` (consistent) when it is admissible under the belief (a
  stated time widened by ``time_statement_tolerance_minutes``), ``contradicted`` (inconsistent)
  when it is not. A time said to the minute ("12:07", "4:52 exactly"; not on a quarter hour)
  needs a precise belief (``exact`` awareness, or a timepiece read): otherwise it is
  inconsistent. With no estimate (``none``), a hedged guess ("must be around noon", "about
  four") is consistent, a low-confidence guess, and a flat time is inconsistent;
- ``elapsed_since`` holds within ``elapsed_tolerance_factor`` of the time the character
  perceived since then ("hours" after twenty perceived minutes is inconsistent);
- ``scheduled_status`` is read against a held schedule at the character's believed time
  (``due`` within the tolerance either side of it, ``not_due`` before, ``past`` after it and its
  grace); an event it does not hold is ``uncertain`` (unknown).

The verdict's evidence names its ``owner_ref`` ``time_belief:<basis>:<awareness>`` (a
contradicted one is the character's own contrary knowledge: a lie against it is a lie). A
hedged guess is never canonized and writes no belief for its speaker (FR-EPI-14); hearers
receive it as testimony.

**The context** (FR-CLOCK-11; ``TimeBelief.text``): the belief as text (a minute only where the
character can tell it: a range derived from awareness names about the hour), the time
perceived in the scene and each held schedule with the time remaining as the character believes
it ("Jenni's meeting: 17:00, about 5½ hours from now"). The objective clock reaches only the
director's context and verification: ``hidden_canon`` is the canon a character's context and its
``belief(A)`` record set leave out (every fact on ``CLOCK_PREDICATES``, and every canon
``scheduled`` fact the character does not hold; ADR-167).

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.membership import Position
from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.vocabulary import (
    CLOCK_PREDICATES,
    CURRENT_TIME,
    ELAPSED_SINCE,
    SCHEDULED,
    SCHEDULED_STATUS,
    TIME_IS,
    TIME_OF_DAY_BAND,
)
from epistrel.core.verify.rules import (
    Evidence,
    Rule,
    Verdict,
    VerdictKind,
    VerdictReason,
    uncertain,
)
from epistrel.core.world_clock.anchor import (
    Anchor,
    Clockwork,
    Statement,
    band_statement,
    said_precisely,
    statement_of,
)

Basis = Literal["awareness", "observation", "testimony", "gap"]
AWARENESS: Basis = "awareness"
OBSERVATION: Basis = "observation"
TESTIMONY: Basis = "testimony"
GAP: Basis = "gap"

APPROXIMATE_SPREAD = 60
"""``approximate`` awareness: the band and the hour, an hour either way."""
REF_PREFIX = "time_belief:"
"""The ``owner_ref`` of a belief-relation verdict: ``time_belief:<basis>:<awareness>``."""
DUE = "due"
NOT_DUE = "not_due"
PAST = "past"
POSSIBLY_DUE = "possibly_due"

Interval = tuple[int, int]


# -- the belief ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeldSchedule:
    """A scheduled event the character holds: its id (the canon ``scheduled`` proposition), its
    entity (the proposition's object, what a ``scheduled_status`` claim names), its name and
    the clock offset it is planned at as the character holds it."""

    event_id: str
    entity_id: str | None
    name: str
    planned: int
    grace: int = 30
    at: str | None = None
    """The time of day it is planned for, when the clock's anchor fixes the minute there."""


@dataclass(frozen=True)
class TimeBelief:
    """What ``character_id`` believes the time is at a position (see the module docstring).

    ``times`` are the admissible minutes of the day it believes (half-open, merged; ``None``: no
    estimate); ``skew`` how far its believed time runs from the clock (minutes, believed minus
    objective; 0 when unknown); ``precise`` whether it can tell the minute; ``offset`` and
    ``now`` the clock's offset and story-minute at the position (the director's, never shown
    to the character); ``gaps`` the spans it perceived no time passing, through ``now``."""

    character_id: str
    awareness: str
    basis: Basis
    offset: int
    now: int
    minute_zero: int
    times: tuple[Interval, ...] | None
    skew: int = 0
    precise: bool = False
    perceived_elapsed: int = 0
    gaps: tuple[Interval, ...] = ()
    schedules: tuple[HeldSchedule, ...] = ()
    clockwork: Clockwork = field(default_factory=Clockwork)
    tolerance: int = 15
    factor: float = 2.0
    belief_id: str | None = None

    @property
    def ref(self) -> str:
        return f"{REF_PREFIX}{self.basis}:{self.awareness}"

    @property
    def believed_offset(self) -> int:
        """The clock offset the character takes "now" to be."""
        return self.offset + self.skew

    def perceived(self, start: int, end: int | None = None) -> int:
        """The minutes the character perceived passing from story-minute ``start`` to ``end``
        (``now`` by default)."""
        return perceived_between(start, self.now if end is None else end, self.gaps)

    def estimate(self) -> int | None:
        """The minute of the day it believes, the middle of its range (``None``: none)."""
        if not self.times:
            return None
        day = self.clockwork.day_minutes
        if self.times == ((0, day),):
            return None
        lo, hi = self.times[0][0], self.times[-1][1]
        if len(self.times) > 1 and self.times[0][0] == 0 and self.times[-1][1] == day:
            # A range wrapping past midnight.
            lo, hi = self.times[-1][0], self.times[0][1] + day
        return ((lo + hi - 1) // 2) % day

    def payload(self) -> dict[str, Any]:
        """The belief as the read set and the report see it (no text)."""
        return {
            "character_id": self.character_id,
            "basis": self.basis,
            "awareness": self.awareness,
            "times": None if self.times is None else [list(t) for t in self.times],
            "skew": self.skew,
            "precise": self.precise,
            "perceived_elapsed": self.perceived_elapsed,
        }

    # -- the context's text --------------------------------------------------------------

    def time_text(self) -> str:
        """The believed time as the character's context says it (never the clock's)."""
        cw = self.clockwork
        day = cw.day_minutes
        if self.times is None or self.times == ((0, day),):
            return "you have no clear sense of what time it is"
        exact = all(hi - lo == 1 for lo, hi in self.times)
        if exact:
            said = " or ".join(cw.clock(lo) for lo, _ in self.times)
            bands = sorted({b for lo, _ in self.times for b in cw.bands_of(lo, lo + 1)})
        else:
            middle = self.estimate()
            if middle is not None and self.basis in (AWARENESS, GAP):
                # The band and the hour: never a minute the character cannot tell.
                middle = (round(middle / 60.0) * 60) % day
            said = "about " + ("?" if middle is None else cw.clock(middle))
            bands = [] if middle is None else cw.bands_of(middle, middle + 1)
        band = f" ({', '.join(bands)})" if bands else ""
        how = {
            AWARENESS: "",
            OBSERVATION: ", by the last timepiece you read",
            TESTIMONY: ", as you were told",
            GAP: ", as far as you know (you were not awake to notice time passing)",
        }[self.basis]
        return f"it is {said}{band}{how}"

    def lines(self) -> tuple[str, ...]:
        """``CharacterContext.time_belief``'s lines: the belief, the time perceived in the scene
        and each held schedule with the time left as the character believes it."""
        found = [f"As far as you know, {self.time_text()}."]
        if self.perceived_elapsed > 0:
            found.append(f"You have been here {duration(self.perceived_elapsed)}.")
        believed = self.believed_offset
        for s in self.schedules:
            at = self._schedule_time(s)
            left = s.planned - believed
            if self.times is None:
                when = f"{s.name}: {at}"
            elif left >= 0:
                when = f"{s.name}: {at}, {duration(left)} from now"
            else:
                when = f"{s.name}: {at}, {duration(-left)} ago"
            found.append(f"{when}.")
        return tuple(found)

    def text(self) -> str:
        return "\n".join(self.lines())

    def _schedule_time(self, schedule: HeldSchedule) -> str:
        """A schedule's planned time of day as the character holds it, read through the
        character's own sense of the time (its believed time plus the time left)."""
        if schedule.at is not None:
            return schedule.at
        middle = self.estimate()
        if middle is None:
            return "later"
        day = self.clockwork.day_minutes
        return self.clockwork.clock((middle + schedule.planned - self.believed_offset) % day)


def duration(minutes: int) -> str:
    """A span as a person says it: "about 20 minutes", "about an hour", "about 5½ hours"."""
    minutes = max(0, int(minutes))
    if minutes < 55:
        rounded = max(5, round(minutes / 5.0) * 5) if minutes >= 5 else minutes
        unit = "minute" if rounded == 1 else "minutes"
        return f"about {rounded} {unit}"
    halves = round(minutes / 30.0)
    hours, half = divmod(halves, 2)
    if hours == 1 and not half:
        return "about an hour"
    if hours >= 24:
        days = round(minutes / 1440.0)
        return "about a day" if days == 1 else f"about {days} days"
    return f"about {hours}{'½' if half else ''} hours"


# -- pure helpers --------------------------------------------------------------------------------


def perceived_between(start: int, end: int, gaps: Iterable[Interval]) -> int:
    """``end - start`` less every gap's overlap with it (never below 0)."""
    if end <= start:
        return 0
    lost = sum(max(0, min(end, hi) - max(start, lo)) for lo, hi in gaps)
    return max(0, end - start - lost)


def _merge(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    found: list[list[int]] = []
    for lo, hi in sorted(i for i in intervals if i[0] < i[1]):
        if found and lo <= found[-1][1]:
            found[-1][1] = max(found[-1][1], hi)
        else:
            found.append([lo, hi])
    return tuple((a, b) for a, b in found)


def _wrap(lo: int, hi: int, day: int) -> list[Interval]:
    if hi - lo >= day:
        return [(0, day)]
    lo_m, hi_m = lo % day, hi % day
    if hi_m == 0:
        hi_m = day
    if lo_m < hi_m:
        return [(lo_m, hi_m)]
    return [(lo_m, day), (0, hi_m)]


def shifted(times: Sequence[Interval], minutes: int, day: int) -> tuple[Interval, ...]:
    """``times`` (minutes of the day) moved ``minutes`` later, wrapping at midnight."""
    found: list[Interval] = []
    for lo, hi in times:
        found += _wrap(lo + minutes, hi + minutes, day)
    return _merge(found)


def widened(times: Sequence[Interval], minutes: int, day: int) -> tuple[Interval, ...]:
    """``times`` widened ``minutes`` either side, wrapping at midnight."""
    found: list[Interval] = []
    for lo, hi in times:
        found += _wrap(lo - minutes, hi + minutes, day)
    return _merge(found)


def _intersects(a: Sequence[Interval], b: Sequence[Interval]) -> bool:
    return any(max(x, y) < min(p, q) for x, p in a for y, q in b)


def _anchor_times(anchor: Anchor, offset: int) -> tuple[Interval, ...]:
    found = anchor.absolute_at(offset)
    return _merge((s.lo, s.hi) for s in found.segments)


def _quarter(times: Sequence[Interval]) -> bool:
    """Every stated minute falls on a quarter hour ("three", "half past three")."""
    return all(lo % 15 == 0 for lo, _ in times)


def said_time(text: str, clockwork: Clockwork) -> tuple[Statement | None, bool, bool]:
    """``(statement, hedged, precise)`` of a time as said: the statement ``statement_of``
    reads (``None``: none), whether it was said as a guess or approximately ("must be around
    noon", "about four"), and whether it claims the minute ("12:07", "4:52 exactly")."""
    statement = statement_of(text, clockwork)
    if statement is None:
        return None, False, False
    hedged = statement.approximate
    exact_minute = (
        statement.time is not None
        and all(hi - lo == 1 for lo, hi in statement.time)
        and not _quarter(statement.time)
    )
    precise = not hedged and (said_precisely(text) or exact_minute)
    return statement, hedged, precise


# -- the belief relation (pure) ------------------------------------------------------------------


def _verdict(kind: VerdictKind, belief: TimeBelief, rule: Rule) -> Verdict:
    return Verdict(kind, evidence=(Evidence(rule, owner_ref=belief.ref),))


def _consistent(belief: TimeBelief, affirmed: bool, holds: bool) -> Verdict:
    if holds == affirmed:
        return _verdict(VerdictKind.SUPPORTED, belief, Rule.COVERAGE)
    return _verdict(VerdictKind.CONTRADICTED, belief, Rule.FUNCTIONAL_KEY)


def verdict(claim: Proposition, belief: TimeBelief) -> Verdict:
    """The belief relation of a clock check ``claim`` against ``belief`` (see the module
    docstring): ``supported`` (consistent), ``contradicted`` (inconsistent) or ``uncertain``
    (unknown). Pure."""
    affirmed = claim.polarity is Polarity.AFFIRMED
    cw = belief.clockwork
    day = cw.day_minutes
    literal = None if claim.literal is None else claim.literal.value
    if claim.predicate_id in (TIME_IS, TIME_OF_DAY_BAND):
        if not isinstance(literal, str):
            return uncertain(VerdictReason.UNRESOLVED_TIME)
        if claim.predicate_id == TIME_IS:
            statement, hedged, precise = said_time(literal, cw)
        else:
            statement, hedged, precise = band_statement(literal, cw), False, False
        if statement is None or statement.time is None:
            return uncertain(VerdictReason.UNRESOLVED_TIME)
        if belief.times is None:
            # No estimate: a guess is a low-confidence guess; a flat time claims knowledge.
            return _consistent(belief, affirmed, hedged)
        if precise and not belief.precise:
            return _verdict(VerdictKind.CONTRADICTED, belief, Rule.FUNCTIONAL_KEY)
        stated = statement.time
        if claim.predicate_id == TIME_IS:
            stated = widened(stated, belief.tolerance, day)
        return _consistent(belief, affirmed, _intersects(stated, belief.times))
    if claim.predicate_id == ELAPSED_SINCE:
        since = claim.qualifiers.get("since")
        if not isinstance(since, int) or isinstance(literal, bool):
            return uncertain(VerdictReason.UNRESOLVED_TIME)
        if not isinstance(literal, int | float) or since > belief.now:
            return uncertain(VerdictReason.UNRESOLVED_TIME)
        felt = belief.perceived(int(since))
        factor = max(1.0, belief.factor)
        holds = literal / factor <= felt <= literal * factor
        return _consistent(belief, affirmed, holds)
    if claim.predicate_id == SCHEDULED_STATUS:
        held = next(
            (s for s in belief.schedules if claim.subject_id in (s.entity_id, s.event_id)), None
        )
        if held is None or belief.times is None or not isinstance(literal, str):
            return uncertain(VerdictReason.UNRESOLVED_TIME)
        spread = APPROXIMATE_SPREAD if belief.awareness == "approximate" else 0
        left = held.planned - belief.believed_offset
        statuses = {
            _status(left + d, held.grace, belief.tolerance) for d in range(-spread, spread + 1)
        }
        if literal == POSSIBLY_DUE:
            return _consistent(belief, affirmed, DUE in statuses or len(statuses) > 1)
        if literal not in (DUE, NOT_DUE, PAST):
            return uncertain(VerdictReason.UNRESOLVED_TIME)
        return _consistent(belief, affirmed, literal in statuses)
    return uncertain(VerdictReason.UNRESOLVED_TIME)


def _status(left: int, grace: int, tolerance: int) -> str:
    """A schedule's status ``left`` minutes before it (negative: after it)."""
    if left > tolerance:
        return NOT_DUE
    if left < -max(grace, tolerance):
        return PAST
    return DUE


# -- the reads -----------------------------------------------------------------------------------


def holds(event: Any, character_id: str) -> bool:
    """Whether ``character_id`` holds a canon scheduled event (#196): its owner, a shared owner
    or a participant."""
    return (
        event.owner == character_id
        or character_id in event.shared_owners
        or character_id in event.participants
    )


async def held_event_ids(
    conn: AsyncConnection, story_id: str, character_id: str, through: Position | None = None
) -> frozenset[str]:
    """The canon scheduled events (their proposition ids) ``character_id`` holds."""
    from epistrel.core.world_clock import stops

    found = await stops.events_at(conn, story_id, through)
    return frozenset(e.event_id for e in found if holds(e, character_id))


async def hidden_canon(
    conn: AsyncConnection, story_id: str, character_id: str, facts: Sequence[Any]
) -> set[str]:
    """The ids of ``facts`` (canon facts) a character's context and ``belief(A)`` record set
    leave out (ADR-167; see the module docstring): every fact on a clock predicate and every
    ``scheduled`` fact the character does not hold. With the clock off, none."""
    from epistrel.core.world_clock.verify import clock_on

    clocked = [f for f in facts if f.proposition.predicate_id in CLOCK_PREDICATES]
    planned = [f for f in facts if f.proposition.predicate_id == SCHEDULED]
    if not (clocked or planned) or not await clock_on(conn, story_id):
        return set()
    hidden = {str(f.id) for f in clocked}
    if planned:
        held = await held_event_ids(conn, story_id, character_id)
        hidden |= {str(f.id) for f in planned if str(f.proposition_id) not in held}
    return hidden


async def _override(
    conn: AsyncConnection, story_id: str, character_id: str, position: Position
) -> Any:
    """The character's latest open ``current_time`` belief (or accepted ``time_is`` testimony)
    held at ``position``."""
    from epistrel.core.epistemic import partitions

    through = None if position.turn >= 2**31 - 1 else position
    found = []
    for predicate in (CURRENT_TIME, TIME_IS):
        found += await partitions.partition(
            conn,
            story_id,
            character_id,
            through=through,
            predicate=predicate,
            levels=partitions.HELD_LEVELS,
        )
    held = [
        b
        for b in found
        if b.held_to is None
        and b.tx_to is None
        and b.proposition.polarity is Polarity.AFFIRMED
        and b.proposition.literal is not None
        and isinstance(b.proposition.literal.value, str)
    ]
    return max(held, key=lambda b: (_valid_from(b), b.held_from.key(), b.id), default=None)


def _valid_from(belief: Any) -> int:
    lo, _ = belief.proposition.valid_time.possible()
    if lo is not None:
        return int(lo)
    return int(belief.valid_from) if belief.valid_from is not None else 0


async def _schedules(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    position: Position,
    minute_zero: int,
) -> tuple[HeldSchedule, ...]:
    """The schedules the character holds, at the times it holds them: the open canon scheduled
    events it holds (its own ``scheduled`` belief about one moving it to the time it believes),
    then its belief-only schedules (a ``scheduled`` belief about no event it holds; FR-CLOCK-9:
    never an event, it shapes only the character's context and its belief relation)."""
    from epistrel.core.epistemic import partitions
    from epistrel.core.tables import entities, propositions
    from epistrel.core.world_clock import stops

    through = None if position.turn >= 2**31 - 1 else position
    events = [
        e
        for e in await stops.events_at(conn, story_id, through)
        if e.is_open and holds(e, character_id)
    ]
    rows = (
        {
            str(r.id): r
            for r in await conn.execute(
                select(propositions.c.id, propositions.c.object_id).where(
                    propositions.c.id.in_([e.event_id for e in events])
                )
            )
        }
        if events
        else {}
    )
    believed: dict[str, tuple[str, int]] = {}
    for b in await partitions.partition(
        conn,
        story_id,
        character_id,
        through=through,
        predicate=SCHEDULED,
        levels=partitions.HELD_LEVELS,
    ):
        lo, _ = b.proposition.valid_time.possible()
        if b.held_to is None and lo is not None and b.proposition.object_id is not None:
            believed[str(b.proposition.object_id)] = (b.proposition_id, int(lo) - minute_zero)
    objects = {str(r.object_id) for r in rows.values() if r.object_id is not None}
    names = {
        str(r.id): str(r.canonical_name)
        for r in await conn.execute(
            select(entities.c.id, entities.c.canonical_name).where(
                entities.c.id.in_(sorted(objects | set(believed)))
            )
        )
    }
    found = []
    for e in events:
        row = rows.get(e.event_id)
        entity = None if row is None or row.object_id is None else str(row.object_id)
        held = believed.get(entity or "")
        planned = e.planned if held is None else held[1]
        found.append(
            HeldSchedule(e.event_id, entity, names.get(entity or "", "plan"), planned, e.grace)
        )
    for entity, (belief_proposition, planned) in sorted(believed.items(), key=lambda i: i[1][1]):
        if entity not in objects:
            found.append(
                HeldSchedule(belief_proposition, entity, names.get(entity, "plan"), planned)
            )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Reading:
    """An override as ``derive`` takes it: the time as read or told, the story-minute it was
    read at, whether it was told (testimony) and the belief's id."""

    said: str
    seen: int
    told: bool = False
    belief_id: str | None = None


def derive(
    character_id: str,
    *,
    awareness: str,
    anchor: Anchor,
    now: int,
    minute_zero: int = 0,
    gaps: Sequence[Interval] = (),
    asleep_from: int | None = None,
    reading: Reading | None = None,
    arrived: int | None = None,
    schedules: Sequence[HeldSchedule] = (),
    tolerance: int = 15,
    factor: float = 2.0,
) -> TimeBelief:
    """The time belief the inputs make (see the module docstring; ``time_belief`` reads them):
    ``anchor`` the clock's at the position, ``now`` its story-minute, ``gaps`` the spans the
    character perceived nothing through ``now``, ``asleep_from`` the start of its latest
    unconscious span (a gap's basis), ``reading`` its latest override and ``arrived`` the
    story-minute its scene began. Pure."""
    cw = anchor.clockwork
    day = cw.day_minutes
    offset = now - minute_zero

    def derived(at_offset: int) -> tuple[tuple[Interval, ...] | None, bool]:
        if awareness == "none":
            return None, False
        times = _anchor_times(anchor, at_offset)
        if awareness == "approximate":
            return widened(times, APPROXIMATE_SPREAD, day), False
        return times, not anchor.is_unanchored

    kind: Basis = AWARENESS
    times, precise = derived(offset)
    skew = 0
    belief_id: str | None = None
    said, hedged, _ = (None, False, False) if reading is None else said_time(reading.said, cw)
    stated = None if said is None else said.time
    if (
        reading is not None
        and stated is not None
        and (asleep_from is None or reading.seen >= asleep_from)
    ):
        kind = TESTIMONY if reading.told else OBSERVATION
        # "Ten past three" in the afternoon is 15:10: the reading nearest the time it was read.
        stated = _nearest(stated, anchor, reading.seen - minute_zero, day)
        if hedged:
            stated = widened(stated, tolerance, day)
        felt = perceived_between(reading.seen, now, gaps)
        times = shifted(stated, felt, day)
        precise = kind == OBSERVATION and all(hi - lo == 1 for lo, hi in stated)
        seen_offset = reading.seen - minute_zero
        skew = felt - (now - reading.seen) + _offset_of(stated, anchor, seen_offset, day)
        belief_id = reading.belief_id
    elif asleep_from is not None and awareness != "none":
        kind = GAP
        felt = perceived_between(asleep_from, now, gaps)
        then, _ = derived(asleep_from - minute_zero)
        times = None if then is None else shifted(then, felt, day)
        precise = False
        skew = felt - (now - asleep_from)
    return TimeBelief(
        character_id=character_id,
        awareness=awareness,
        basis=kind,
        offset=offset,
        now=now,
        minute_zero=minute_zero,
        times=times,
        skew=skew,
        precise=precise,
        perceived_elapsed=perceived_between(minute_zero if arrived is None else arrived, now, gaps),
        gaps=tuple(gaps),
        schedules=tuple(schedules),
        clockwork=cw,
        tolerance=tolerance,
        factor=factor,
        belief_id=belief_id,
    )


async def time_belief(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    position: Position | None = None,
) -> TimeBelief | None:
    """``character_id``'s time belief at ``position`` (as the story stands by default; see the
    module docstring): its reads, then ``derive``; ``None`` when the story does not run the
    clock."""
    from epistrel.core import world_clock
    from epistrel.core.epistemic import scene
    from epistrel.core.world_clock.verify import clock_on

    if not await clock_on(conn, story_id):
        return None
    at = world_clock.HEAD if position is None else position
    found = await world_clock.settings(conn, story_id)
    basis = await world_clock.time_basis(conn, story_id)
    zero = basis.minute_zero
    now = basis.at(at)
    asleep = [
        lo for lo, _, _ in await scene.unconscious_spans(conn, story_id, character_id) if lo < now
    ]
    override = await _override(conn, story_id, character_id, at)
    reading = (
        None
        if override is None
        else Reading(
            str(override.proposition.literal.value),
            _valid_from(override),
            await _told(conn, story_id, override),
            override.id,
        )
    )
    anchor = await world_clock.anchor_at(conn, story_id, at)
    held = await _schedules(conn, story_id, character_id, at, zero)
    return derive(
        character_id,
        awareness=found.time_awareness.value,
        anchor=anchor,
        now=now,
        minute_zero=zero,
        gaps=await scene.perception_gaps(conn, story_id, character_id, zero, now),
        asleep_from=max(asleep, default=None),
        reading=reading,
        arrived=await scene.arrived_at(conn, story_id, character_id, now),
        schedules=tuple(replace(s, at=_minute_text(anchor, s.planned)) for s in held),
        tolerance=found.time_statement_tolerance_minutes,
        factor=float(found.elapsed_tolerance_factor),
    )


def _minute_text(anchor: Anchor, offset: int) -> str | None:
    """The time of day at clock offset ``offset`` ("17:00"), when the anchor fixes the minute
    there; else ``None``."""
    found = _anchor_times(anchor, offset)
    if len(found) != 1 or found[0][1] - found[0][0] != 1:
        return None
    return anchor.clockwork.clock(found[0][0])


def _nearest(
    stated: Sequence[Interval], anchor: Anchor, at_offset: int, day: int
) -> tuple[Interval, ...]:
    """Of a reading's admissible times (both halves of the day, "three"), the one nearest the
    clock where it was read, when the anchor fixes the minute there; else all of them."""
    actual = _anchor_times(anchor, at_offset)
    if len(stated) < 2 or len(actual) != 1 or actual[0][1] - actual[0][0] != 1:
        return tuple(stated)
    real = actual[0][0]

    def distance(i: Interval) -> int:
        middle = (i[0] + i[1] - 1) // 2
        return abs((middle - real + day // 2) % day - day // 2)

    return (min(stated, key=distance),)


def _offset_of(stated: Sequence[Interval], anchor: Anchor, at_offset: int, day: int) -> int:
    """How far a time read at clock offset ``at_offset`` ran from the clock then (minutes; the
    nearest reading), when the anchor fixes the minute there; else 0."""
    actual = _anchor_times(anchor, at_offset)
    if len(actual) != 1 or actual[0][1] - actual[0][0] != 1:
        return 0
    real = actual[0][0]
    best: int | None = None
    for lo, hi in stated:
        middle = (lo + hi - 1) // 2
        delta = (middle - real + day // 2) % day - day // 2
        if best is None or abs(delta) < abs(best):
            best = delta
    return 0 if best is None else best


async def _told(conn: AsyncConnection, story_id: str, belief: Any) -> bool:
    """Whether the transition that produced ``belief`` was testimony (#162)."""
    from epistrel.core.epistemic import transitions

    for t in await transitions.transitions_of(conn, story_id, belief_ids=[belief.id]):
        if t.id == belief.provenance:
            return t.kind is transitions.Kind.TESTIMONY
    return False


__all__ = [
    "APPROXIMATE_SPREAD",
    "AWARENESS",
    "GAP",
    "OBSERVATION",
    "REF_PREFIX",
    "TESTIMONY",
    "Basis",
    "HeldSchedule",
    "Reading",
    "TimeBelief",
    "derive",
    "duration",
    "held_event_ids",
    "hidden_canon",
    "holds",
    "perceived_between",
    "said_time",
    "shifted",
    "time_belief",
    "verdict",
    "widened",
]
