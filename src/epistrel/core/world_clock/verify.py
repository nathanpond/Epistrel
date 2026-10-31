"""The clock checks: deterministic propositions verified against the clock's anchor (FR-CLOCK-3,10;
#195), through #191's owner-lookup hook (``core.propositions.registry.register_owner_lookup``).

**The predicates.** ``time_of_day_band`` (a band's name), ``elapsed_since`` (minutes since a
story-minute, its ``since`` qualifier) and ``scheduled_status`` (an event's status) are the clock's
alone. M4's ``time_is`` (the time as said, which #148's ``time`` detector writes for a clock-time
expression) is the clock's **while the story runs it** (a conditional lookup, ``active`` =
``clock_level``): this answers the planner's question of how ``time_is`` claims reach
``anchor.evaluate`` (no separate ``current_time`` predicate). With the clock off, ``time_is`` is
the ordinary fact-backed predicate it was in M4, and the three checks are
``uncertain(no_clock)``.

**The read** (``read``): one ``OwnedValue`` for the clock, a ``clock_anchor`` proposition whose
literal holds the anchor at the claim's position, the clock's offset there and the tolerances
(``time_statement_tolerance_minutes``, ``elapsed_tolerance_factor``), so #140's read set hashes
them and a changed anchor fails the rebase re-check. Its ``ref`` is the clock State's
(``state:<id>``).

**The verdict** (``verdict``, pure): the claim becomes an ``anchor`` check (``statement_of`` the
time as said, or the band's ``band_statement``; ``Elapsed``; ``Scheduled``), a stated exact time
widened by the tolerance, and ``Anchor.evaluate`` decides: ``supported`` (coverage, citing the
clock), ``contradicted`` (``functional_key``, citing the clock: "it's 13:00" under "Saturday
morning", repaired as any contradicted claim is), or, undecided, ``novel`` when the statement can
narrow the anchor as said (the narrator's "it's 9:15" under "Saturday morning": #195's anchoring
statements, which ``Verifier.canon_requests`` turns into ``clock_anchor`` commitments) and
``uncertain(approximate_bounds)`` otherwise; a time this module cannot read is
``uncertain(unresolved_time)``. No verdict ever picks a value inside a range.

**Within a timed turn** (#194, FR-CLOCK-17): while a structured turn's render is verified or its
commit runs, the story's time basis carries the turn's in-turn times
(``core.world_clock.turn_scope``), and the read marks its value ``at_claim``: the check is made
at the claim's own time (its valid-time's exact start: its step's start) rather than at the
read's position, so "at 15:40 she starts driving" at 15:05 is checked at 15:40, the time its
step starts.

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.membership import Position
from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    LiteralKind,
    Polarity,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.registry import register_owner_lookup
from epistrel.core.propositions.vocabulary import (
    CLOCK_ANCHOR,
    ELAPSED_SINCE,
    SCHEDULED_STATUS,
    TIME_IS,
    TIME_OF_DAY_BAND,
)
from epistrel.core.verify.rules import (
    Evidence,
    OwnedValue,
    Rule,
    Verdict,
    VerdictKind,
    VerdictReason,
    uncertain,
)
from epistrel.core.world_clock.anchor import (
    Anchor,
    Check,
    Clockwork,
    Elapsed,
    Outcome,
    Scheduled,
    Statement,
    band_statement,
    statement_of,
)

REF_PREFIX = "state:"
KIND = "clock"
REFINES: frozenset[str] = frozenset({TIME_IS, TIME_OF_DAY_BAND})
"""The checks whose statement, undecided, may narrow the anchor (anchoring statements)."""


async def clock_on(conn: AsyncConnection, story_id: str) -> bool:
    """The story runs the clock (``time_is``'s ``active`` test)."""
    from epistrel.core.world_clock import clock_level

    return await clock_level(conn, story_id) is not None


async def read(
    conn: AsyncConnection,
    story_id: str,
    subjects: Sequence[str] | None,
    through: Any,
    viewpoint: str,
) -> list[OwnedValue]:
    """The clock as a claim at ``through`` reads it (see the module docstring); nothing when
    the story does not run the clock."""
    from epistrel.core import world_clock

    if not await clock_on(conn, story_id):
        return []
    found = await world_clock.settings(conn, story_id)
    clock = await world_clock.clock_definition(conn, story_id)
    assert clock.id is not None  # noqa: S101 - a stored declaration has its id
    basis = await world_clock.time_basis(conn, story_id)
    at = world_clock.HEAD if through is None else through
    minute = basis.at(at)
    anchor = await world_clock.anchor_at(conn, story_id, at)
    return [
        owned_value(
            clock.id,
            anchor,
            offset=minute - basis.minute_zero,
            minute_zero=basis.minute_zero,
            tolerance=found.time_statement_tolerance_minutes,
            factor=float(found.elapsed_tolerance_factor),
            at_claim=basis.in_turn is not None,
        )
    ]


def owned_value(
    clock_id: str,
    anchor: Anchor,
    *,
    offset: int,
    minute_zero: int = 0,
    tolerance: int = 15,
    factor: float = 2.0,
    at_claim: bool = False,
) -> OwnedValue:
    """The clock as the lookup hands it to ``verdict``: ``anchor`` at clock offset ``offset``
    with the tolerances (see the module docstring); ``at_claim`` (#194): checked at the claim's
    own time, within a timed turn."""
    clockwork = anchor.clockwork
    payload: dict[str, Any] = {
        "anchor": anchor.document(),
        "calendar": clockwork.calendar.document(),
        "bands": [list(b) for b in clockwork.bands],
        "offset": offset,
        "minute_zero": minute_zero,
        "tolerance": tolerance,
        "factor": factor,
    }
    if at_claim:
        payload["at_claim"] = True
    return OwnedValue(
        proposition=Proposition(
            subject_id=clock_id,
            predicate_id=CLOCK_ANCHOR,
            literal=TypedLiteral(
                kind=LiteralKind.STR,
                value=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
            valid_time=Instant.of(minute_zero + offset, Granularity.MINUTE),
        ),
        ref=f"{REF_PREFIX}{clock_id}",
        kind=KIND,
    )


def _clock(owned: Sequence[OwnedValue]) -> tuple[OwnedValue, dict[str, Any]] | None:
    for value in owned:
        literal = value.proposition.literal
        if value.kind != KIND or literal is None or not isinstance(literal.value, str):
            continue
        try:
            return value, json.loads(literal.value)
        except ValueError:
            continue
    return None


def _anchor(payload: dict[str, Any]) -> Anchor:
    clockwork = Clockwork(_calendar(payload.get("calendar")), _bands(payload))
    document = payload.get("anchor")
    if not document:
        return Anchor.unanchored(clockwork)
    return Anchor.from_document(document, clockwork)


def _calendar(raw: Any) -> Any:
    from epistrel.core.states.model import Calendar

    return Calendar.of(raw)


def _bands(payload: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    from epistrel.core.states.model import DEFAULT_BANDS

    raw = payload.get("bands")
    if not raw:
        return DEFAULT_BANDS
    return tuple((str(b[0]), str(b[1]), str(b[2])) for b in raw)


def check_of(claim: Proposition, anchor: Anchor, payload: dict[str, Any]) -> Check | None:
    """The anchor check ``claim`` amounts to (``None``: it states nothing this module reads)."""
    literal = claim.literal
    clockwork = anchor.clockwork
    if claim.predicate_id == TIME_IS:
        if literal is None or not isinstance(literal.value, str):
            return None
        return statement_of(literal.value, clockwork)
    if claim.predicate_id == TIME_OF_DAY_BAND:
        if literal is None or not isinstance(literal.value, str):
            return None
        return band_statement(literal.value, clockwork)
    if claim.predicate_id == ELAPSED_SINCE:
        since = claim.qualifiers.get("since")
        if literal is None or isinstance(literal.value, bool) or not isinstance(since, int):
            return None
        if not isinstance(literal.value, int | float):
            return None
        return Elapsed(
            since=int(since) - int(payload.get("minute_zero", 0)),
            minutes=int(literal.value),
            factor=float(payload.get("factor", 2.0)),
        )
    if claim.predicate_id == SCHEDULED_STATUS:
        status = literal.value if literal is not None else ""
        return Scheduled(claim.subject_id, str(status))
    return None


def verdict(claim: Proposition, owned: Sequence[OwnedValue]) -> Verdict:
    """A canon holder's verdict on a clock check over the clock ``owned`` (see the module
    docstring). Pure."""
    found = _clock(owned)
    if found is None:
        return uncertain(VerdictReason.NO_CLOCK)
    value, payload = found
    anchor = _anchor(payload)
    offset = int(payload.get("offset", 0))
    own = _claim_minute(claim) if payload.get("at_claim") else None
    if own is not None:
        offset = own - int(payload.get("minute_zero", 0))
    check = check_of(claim, anchor, payload)
    if check is None:
        return uncertain(VerdictReason.UNRESOLVED_TIME)
    tolerance = int(payload.get("tolerance", 0))
    widened = check
    if isinstance(check, Statement) and claim.predicate_id == TIME_IS:
        widened = check.widened(tolerance, anchor.clockwork.day_minutes)
    outcome = anchor.evaluate(widened, offset)
    affirmed = claim.polarity is Polarity.AFFIRMED
    if outcome is Outcome.SUPPORTED:
        if affirmed:
            return Verdict(
                VerdictKind.SUPPORTED, evidence=(Evidence(Rule.COVERAGE, owner_ref=value.ref),)
            )
        return Verdict(
            VerdictKind.CONTRADICTED, evidence=(Evidence(Rule.POLARITY, owner_ref=value.ref),)
        )
    if outcome is Outcome.CONTRADICTED:
        if affirmed:
            return Verdict(
                VerdictKind.CONTRADICTED,
                evidence=(Evidence(Rule.FUNCTIONAL_KEY, owner_ref=value.ref),),
            )
        return Verdict(
            VerdictKind.SUPPORTED, evidence=(Evidence(Rule.COVERAGE, owner_ref=value.ref),)
        )
    if isinstance(check, Statement) and affirmed and claim.predicate_id in REFINES:
        stated = refinement(check, tolerance, anchor.clockwork.day_minutes)
        if isinstance(anchor.refine(stated, offset), Anchor):
            return Verdict(VerdictKind.NOVEL)
    if isinstance(check, Scheduled | Elapsed):
        return uncertain(VerdictReason.UNRESOLVED_TIME)
    return uncertain(VerdictReason.APPROXIMATE_BOUNDS)


def _claim_minute(claim: Proposition) -> int | None:
    """The exact story-minute a claim's valid-time starts at (``None``: not exact)."""
    found = claim.valid_time
    if isinstance(found, Instant):
        return found.at if found.granularity is Granularity.MINUTE else None
    start = found.start
    return None if start is None or start.inner != start.outer else int(start.inner)


def refinement(statement: Statement, tolerance: int, day_minutes: int = 1440) -> Statement:
    """What an anchoring statement narrows the anchor by: as said, or widened by the
    tolerance when said approximately ("about nine")."""
    if statement.approximate:
        return statement.widened(tolerance, day_minutes)
    return statement


def statement_for(claim: Proposition, clockwork: Clockwork, tolerance: int) -> Statement | None:
    """The statement an anchoring claim (``time_is``, ``time_of_day_band``) commits."""
    literal = claim.literal
    if literal is None or not isinstance(literal.value, str):
        return None
    if claim.predicate_id == TIME_IS:
        found = statement_of(literal.value, clockwork)
    elif claim.predicate_id == TIME_OF_DAY_BAND:
        found = band_statement(literal.value, clockwork)
    else:
        return None
    return None if found is None else refinement(found, tolerance, clockwork.day_minutes)


class _ClockLookup:
    """The clock checks' owner lookup (``core.propositions.registry.OwnerLookup``)."""

    async def read(
        self,
        conn: AsyncConnection,
        story_id: str,
        subjects: Sequence[str] | None,
        through: Position | None,
        viewpoint: str,
    ) -> list[OwnedValue]:
        return await read(conn, story_id, subjects, through, viewpoint)

    def verdict(self, claim: Proposition, owned: Sequence[OwnedValue]) -> Verdict:
        return verdict(claim, owned)

    def implied(
        self, consequence: Proposition, cause: Proposition, owned: Sequence[OwnedValue]
    ) -> Proposition | None:
        return None  # no declared rule concludes a clock check


CLOCK_LOOKUP = _ClockLookup()
register_owner_lookup(TIME_IS, CLOCK_LOOKUP, active=clock_on)
for _predicate in (TIME_OF_DAY_BAND, ELAPSED_SINCE, SCHEDULED_STATUS):
    register_owner_lookup(_predicate, CLOCK_LOOKUP)

__all__ = [
    "CLOCK_LOOKUP",
    "REFINES",
    "check_of",
    "clock_on",
    "owned_value",
    "read",
    "refinement",
    "statement_for",
    "verdict",
]
