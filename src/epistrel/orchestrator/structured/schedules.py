"""What a turn's schedule statements commit (FR-CLOCK-19,10, FR-EPI-14; docs/02 §7.1; #198).

The verifier classified each ``scheduled`` claim of the turn's beats
(``core.world_clock.schedule_statements``; ``VerifyResult.schedule``). ``committed`` writes,
in the turn's commit after its canon (``core.canon.canonize.canonize_all``), what they call
for:

- **A create** by a character: its canon ``scheduled`` fact was canonized like any permitted
  novel claim (``core.world_clock.stops.on_canon_fact`` made it an event owned by the speaker).
  The speaker holds it: a ``discovery`` belief through ``core.epistemic.transitions.transition``
  citing that canon fact as its ``record`` antecedent, at the beat's step + 1 (FR-EPI-14: a
  belief citing the asserting beat itself would be ``self_attestation``; ADR-168). The world's
  create (a director's or narration's beat) gives no one a belief.
- **A change**: the event is rescheduled at the beat's step (``stops.record``: ``rescheduled``,
  back to ``pending`` at the new time, its one stop cleared), and the speaker holds the new
  time: an ``observation`` citing the reschedule's ``scheduled_event`` event at step + 1.
- **A report** writes nothing here: it is an ordinary first-person claim (#166's
  ``orchestrator.epistemic.speech``).

Beliefs are written only under per-character partitions. A guard refusal is counted, never
raised. Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import world_clock
from epistrel.core.canon import canonize
from epistrel.core.canon.facts import Fact
from epistrel.core.enums import BeliefConfidenceLevel, ScheduledStatus
from epistrel.core.epistemic import transitions
from epistrel.core.epistemic.errors import (
    AntecedentInvalid,
    ConfidenceInconsistent,
    SelfAttestation,
)
from epistrel.core.epistemic.scene import PARTITIONS_MODULE
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import POSITION_MAX, Position, step_of
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.verify import speech_acts
from epistrel.core.world_clock import schedule_statements as schedules
from epistrel.core.world_clock import stops
from epistrel.orchestrator.verify.extraction import CheckedClaim

log = logging.getLogger(__name__)

SPEAKERS = frozenset({"character", "player"})
"""The asserters whose schedule statement is a character's own (it holds what it states)."""


@dataclass(slots=True)
class Written:
    """What ``committed`` wrote: the creates' beliefs, the changes and the changes' beliefs,
    and the transitions the guard refused."""

    created: int = 0
    changed: int = 0
    believed: int = 0
    refused: int = 0


def _speaker(c: CheckedClaim) -> str | None:
    asserter = None if c.result is None else c.result.asserter
    if asserter is None or asserter.kind not in SPEAKERS:
        return None
    return asserter.character_id


def _fact_of(claim: Proposition, canonized: Sequence[Fact]) -> Fact | None:
    """The canon fact the turn canonized for a create's claim."""
    for fact in canonized:
        p = fact.proposition
        if (
            fact.predicate_id == SCHEDULED
            and p.subject_id == claim.subject_id
            and p.object_id == claim.object_id
        ):
            return fact
    return None


async def committed(
    conn: AsyncConnection,
    op: Any,
    checked: Sequence[CheckedClaim],
    canonized: Sequence[Fact],
    *,
    settings: Any,
    registry: Registry,
    introductions: Any = None,
) -> Written:
    """Write what the turn's schedule statements call for (see the module docstring);
    ``introductions``: the turn's minted entities (#641: a belief-only plan creates its own)."""
    found = Written()
    story_id = str(op.story_id)
    turn, variant = int(op.narrative_turn or 0), int(op.turn_variant or 0)
    partitioned = module_on(settings, PARTITIONS_MODULE)
    for c in checked:
        result = c.result
        claim = c.claim.proposition
        beat = c.claim.beat
        if result is None or result.schedule is None or claim is None or beat is None:
            continue
        statement = result.schedule
        speaker = _speaker(c)
        at = Position(turn, variant, 0, beat, step_of(beat, str(op.operation_id)))
        after = replace(at, step=min(POSITION_MAX, at.step + 1))
        if isinstance(statement, schedules.Create):
            fact = _fact_of(claim, canonized)
            if fact is not None:
                await _hold_time_of_day(conn, op, story_id, c, fact, at, after)
            if fact is None or speaker is None:
                continue
            found.created += 1
            if partitioned:
                await _believe(
                    conn,
                    op,
                    found,
                    holder=speaker,
                    kind=transitions.Kind.DISCOVERY,
                    proposition=fact.proposition,
                    antecedent=transitions.Antecedent.record("facts", fact.id),
                    position=after,
                    settings=settings,
                    registry=registry,
                )
        elif isinstance(statement, schedules.Report) and statement.event is None:
            # #641: a belief-only plan said sincerely: the speaker holds it (never canon, never
            # an event, so it stops no clock); its hearers hold it through #162's testimony.
            speech = result.speech
            if (
                not partitioned
                or speaker is None
                or speech is None
                or speech.classification != speech_acts.SINCERE
            ):
                continue
            said = await _say_event(conn, op, beat)
            if said is None:
                continue
            if introductions is not None:
                # The plan's minted event (never canonized) exists, so a later mention of it
                # resolves to it and the belief names a real entity.
                await canonize.introduce(
                    conn,
                    story_id,
                    claim,
                    introductions.introduces(claim),
                    operation=op,
                    registry=registry,
                    skip_existing=True,
                )
            await _believe(
                conn,
                op,
                found,
                holder=speaker,
                kind=transitions.Kind.OBSERVATION,
                proposition=claim,
                antecedent=transitions.Antecedent.record("events", said),
                position=after,
                settings=settings,
                registry=registry,
            )
        elif isinstance(statement, schedules.Change):
            event = await stops.event_at(conn, story_id, statement.event.event_id)
            if event is None or event.status is ScheduledStatus.CANCELLED:
                continue
            zero = (await world_clock.time_basis(conn, story_id)).minute_zero
            done = await stops.record(
                conn,
                op,
                story_id,
                event,
                ScheduledStatus.PENDING,
                position=at,
                cause=stops.RESCHEDULED,
                planned=max(0, statement.planned_at - int(zero)),
            )
            found.changed += 1
            if partitioned and speaker is not None:
                await _believe(
                    conn,
                    op,
                    found,
                    holder=speaker,
                    kind=transitions.Kind.OBSERVATION,
                    proposition=claim,
                    antecedent=transitions.Antecedent.record("events", done.record_id),
                    position=after,
                    settings=settings,
                    registry=registry,
                )
    log.debug(
        "schedules: %d created, %d changed, %d believed, %d refused",
        found.created,
        found.changed,
        found.believed,
        found.refused,
    )
    return found


async def _say_event(conn: AsyncConnection, op: Any, beat: int) -> str | None:
    """The ``say`` beat's event the turn wrote at ``beat`` (#641's belief cites it)."""
    from sqlalchemy import select

    from epistrel.core.tables.events import events

    found = await conn.scalar(
        select(events.c.id).where(
            events.c.operation_id == str(op.operation_id),
            events.c.beat == beat,
            events.c.type == "beat_say",
        )
    )
    return None if found is None else str(found)


async def _hold_time_of_day(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    c: CheckedClaim,
    fact: Fact,
    at: Position,
    after: Position,
) -> None:
    """#645: a create whose time of day the clock's anchor holds only as a range is held at its
    frame minute (``stops.place_event``, as ``POST …/scheduled-events`` holds one), so #197's
    pause applies to it; ``possibly_due`` at once when the range already holds it."""
    sketch = c.claim.sketch
    said = c.claim.time_text or (None if sketch is None else sketch.time_text)
    if not said:
        return
    basis = await world_clock.time_basis(conn, story_id)
    placed = schedules.range_placement(said, basis, at)
    if placed is None or basis.anchor is None or schedules.place_time_exact(said, basis, at):
        return
    event = await stops.event_at(conn, story_id, fact.proposition_id)
    if event is None or event.planned_anchor is not None:
        return
    current = basis.at(at) - basis.minute_zero
    await stops.place_event(
        conn,
        op,
        story_id,
        event,
        at=placed[1],
        anchor=basis.anchor,
        offset=current,
        position=after,
    )


async def _believe(
    conn: AsyncConnection,
    op: Any,
    found: Written,
    *,
    holder: str,
    kind: transitions.Kind,
    proposition: Proposition,
    antecedent: transitions.Antecedent,
    position: Position,
    settings: Any,
    registry: Registry,
) -> None:
    try:
        await transitions.transition(
            conn,
            op,
            holder=holder,
            kind=kind,
            proposition=proposition,
            antecedent=antecedent,
            position=position,
            confidence=transitions.Confidence(BeliefConfidenceLevel.KNOWN),
            settings=settings,
            registry=registry,
        )
    except SelfAttestation, AntecedentInvalid, ConfidenceInconsistent:
        found.refused += 1  # counted by transition() itself
        return
    except InvalidRequest:
        transitions.refused()
        found.refused += 1
        return
    found.believed += 1


__all__ = ["Written", "committed"]
