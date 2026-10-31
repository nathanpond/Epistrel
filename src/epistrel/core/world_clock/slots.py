"""The shared inter-turn slot opener (FR-STORE-31's ``N⁺k``; #193).

A write between turns (a consumer's clock advance, #186's State set, definition and definition
update, #196's scheduled events) is an operation of its own, positioned in an inter-turn slot of
the last completed turn: ``open_inter_turn(db, story_id, *, source, principal)`` answers an
``inter_turn`` ``Operation`` whose commit positions it, under the story lock, at
``Position(turn=N, variant=N's active variant, slot_k=k, beat=0, step=0)``, where ``N`` is the
last completed turn (0 before the first) and ``k`` the next slot no operation at ``N`` has
taken (two slots on one turn are ``k`` 1 and 2; a number is never reused, like a variant). A
truncation to ``N`` or earlier discards the slot (``core.timeline``), and the slot's writes
with it. ``slot_position(op)`` is the slot's position inside a commit step.

A slot's operation bumps the timeline revision, and should record a non-audit event so that a
fork copies it (``core.fork`` copies the operations that recorded one; #185's finding).
"""

from __future__ import annotations

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events.operation import Database, Operation
from epistrel.core.membership import Position


def open_inter_turn(
    db: Database,
    story_id: str,
    *,
    source: Provenance,
    principal: str | None,
    idempotency_key: str | None = None,
) -> Operation:
    """An inter-turn slot operation of ``story_id`` (see the module docstring); ``source`` is
    who originated its records."""
    return Operation(
        db,
        story_id=story_id,
        kind=OperationKind.INTER_TURN,
        principal=principal,
        provenance=Provenance(source),
        bumps_revision=True,
        idempotency_key=idempotency_key,
        slot=True,
    )


def slot_position(op: Operation) -> Position:
    """The position of slot operation ``op``, once its commit positioned it (in a commit
    step)."""
    if not op.slot or op.narrative_turn is None or op.slot_k is None:
        raise RuntimeError("the slot is positioned by its commit")
    return Position(op.narrative_turn, op.turn_variant or 0, op.slot_k, 0, 0)


__all__ = ["open_inter_turn", "slot_position"]
