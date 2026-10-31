"""External facts: a committed reference to an external value (FR-EXT-4; U-08; #205).

When a turn's character or narration states an externally owned State's value and the claim is
``supported`` by the turn's pinned read (``PinnedReads``), the commit writes a fact through
#133 (``core.canon.commit_fact``) that **references the ``external_read`` event** instead of
re-capturing the value:

- ``provenance = external`` and ``authority = external``;
- ``source_event_ids = [<the external_read event>]``;
- ``valid_from`` = the read's story-time (``core.world_clock.story_time_at`` of its position);
- ``visibility = {scope: engine}``: omniscient only, like true ages (a shared scope would reach
  every character's canon and context under ADR-167, FR-STATE-8; owner decision 2026-10-28);
- the proposition's literal is read from the event's payload at write time, never from the
  claim or the provider, so later live drift changes neither the event nor the fact.

One fact per value the turn referenced (the first claim's beat). Identifiers only in logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import world_clock
from epistrel.core.canon.facts import commit_fact
from epistrel.core.enums import FactAuthority, RecordProvenance, StateType
from epistrel.core.events.types import EXTERNAL_READ
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval
from epistrel.core.propositions.vocabulary import STATE_VALUE
from epistrel.core.states import verify as state_verify
from epistrel.core.states.model import from_json
from epistrel.core.tables import events
from epistrel.core.verify.rules import OwnedValue, VerdictKind
from epistrel.core.visibility import Visibility
from epistrel.orchestrator.providers.pinned import scope_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from epistrel.core.events.operation import Operation
    from epistrel.orchestrator.providers.pinned import PinnedReads, ValueRef

ENGINE_ONLY = Visibility(scope="engine")
"""An external fact's visibility: omniscient only."""


@dataclass(frozen=True, slots=True)
class Reference:
    """A value the turn committed a reference to: its State and scope key, the claim's
    subject and beat, and the State's type (to read the event's literal)."""

    ref: ValueRef
    subject: str
    beat: int
    type: StateType


def references(pins: PinnedReads, checked: Sequence[Any]) -> list[Reference]:
    """The pinned external values ``checked``'s supported ``state_value`` claims refer to, one
    per value (the first claim's), whose claimed value is the pinned one."""
    defined = pins.known()
    found: dict[ValueRef, Reference] = {}
    for c in checked:
        result = getattr(c, "result", None)
        proposition = c.claim.proposition
        if result is None or result.kind is not VerdictKind.SUPPORTED or proposition is None:
            continue
        if proposition.predicate_id != STATE_VALUE or proposition.literal is None:
            continue
        definition = defined.get(state_verify.state_of(proposition) or "")
        if definition is None:
            continue

        ref = (str(definition.id), scope_of(definition, proposition.subject_id))
        pinned = pins.pinned(ref[0], ref[1], None)
        if pinned is None or pinned.value is None or ref in found:
            continue
        owned = OwnedValue(
            proposition=state_verify.proposition(
                ref[0], proposition.subject_id, state_verify.value_literal(pinned.value)
            ),
            ref=f"{state_verify.REF_PREFIX}{ref[0]}",
            kind=definition.type.value,
            tolerance=float(definition.constraints.tolerance),
        )
        if state_verify.matches(owned, proposition.literal) is not True:
            continue
        found[ref] = Reference(ref, proposition.subject_id, int(c.claim.beat or 0), definition.type)
    return list(found.values())


async def commit(conn: AsyncConnection, *, op: Operation, references: Sequence[Reference]) -> None:
    """Write one external fact per reference (see the module docstring), in the turn's commit
    after its events are written."""
    assert op.story_id is not None  # noqa: S101 - a turn is a story's
    assert op.narrative_turn is not None  # noqa: S101 - positioned by the commit
    e = events.c
    read = await conn.execute(
        select(e.id, e.payload, e.beat, e.step)
        .where(e.operation_id == op.operation_id, e.type == EXTERNAL_READ)
        .order_by(e.seq)
    )
    by_ref: dict[tuple[str, str | None], Any] = {}
    for found in read:
        by_ref[(str(found.payload["state_id"]), found.payload.get("scope_key"))] = found
    written: list[str] = []
    for reference in references:
        hit = by_ref.get(reference.ref)
        if hit is None or hit.payload.get("value") is None:
            continue
        row = hit
        value = from_json(reference.type, row.payload["value"])
        assert value is not None  # noqa: S101 - checked above
        position = Position(
            op.narrative_turn,
            int(op.turn_variant or 0),
            int(op.slot_k or 0),
            int(row.beat or 0),
            int(row.step or 0),
        )
        minute = await world_clock.story_time_at(conn, op.story_id, position)
        fact = await commit_fact(
            conn,
            op.story_id,
            state_verify.proposition(
                reference.ref[0], reference.subject, state_verify.value_literal(value)
            ),
            provenance=RecordProvenance.EXTERNAL,
            authority=FactAuthority.EXTERNAL,
            visibility=ENGINE_ONLY,
            operation=op,
            valid_time=Interval.of(minute, None),
            source_event_ids=[str(row.id)],
            beat=reference.beat,
        )
        written.append(fact.id)
    op.stage("external_facts", f"written:{len(written)}")


__all__ = ["ENGINE_ONLY", "Reference", "commit", "references"]
