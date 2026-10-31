"""A staged turn copied into a fork, closed there (FR-EXT-19; docs/02 §4.8; #212).

A fork taken while a staged turn of the source is incomplete (its intent committed, its
completion not: ``operations.turn_stage`` ``intent_committed``, ``awaiting_outcome`` or
``outcome_recorded``) copies the turn's committed stages, and the effect only as a
non-executable record (#211's fork rule; listed in the fork's ``effects_pending_in_source``).
Nothing will ever answer for it in the fork, so ``close_in_fork`` closes the copy there, in the
fork's own transaction, with a deterministic completion step:

- one narrator ``narration`` beat, ``FORK_CLOSURE`` ("outcome not known in this story"), after
  every event the copy holds, and the turn's message (the deterministic assembly of the turn's
  beats for the viewpoint it was played for), both under the copied operation;
- the projections maintained for both events, and the narration routed like any committed
  beat (``core.epistemic.perception``: the scene at the turn's start and the turn's moves;
  episodic memories under perception ``none``);
- ``turn_stage = completed`` and a ``completion`` stage logged at the fork's revision (the
  closure is part of the fork's creation: no revision of its own).

The orchestrator's staged turns (``orchestrator.structured.staged``) close a turn of the story
itself; this is the fork's deterministic counterpart, written without a model.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock, projections
from epistrel.core.enums import (
    UNFINISHED_TURN_STAGES,
    Authority,
    Membership,
    OperationKind,
    Provenance,
    RecordProvenance,
    StagedTurnStage,
)
from epistrel.core.epistemic import perception
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.events.registry import lookup, validate
from epistrel.core.events.types import BEAT_TYPES, MessageRendered
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.memory import records
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import characters, events, operations
from epistrel.core.turns.assembly import AssemblyBeat, deterministic_assembly
from epistrel.core.turns.beats import TurnBeat, beat_payload, event_type_of

FORK_CLOSURE = "The outcome is not known in this story."
"""The fork's deterministic completion step for a staged turn it copied incomplete."""

_BEAT_EVENTS = tuple(event_type_of(k) for k in BEAT_TYPES)


async def close_in_fork(conn: AsyncConnection, fork_id: str, principal: str) -> list[str]:
    """Close every unfinished staged turn of ``fork_id`` (see the module docstring), in the
    fork's transaction; answers the closed operations' ids."""
    rows = list(
        await conn.execute(
            select(
                operations.c.operation_id,
                operations.c.narrative_turn,
                operations.c.turn_variant,
                operations.c.base_revision,
                operations.c.viewpoint,
            ).where(
                operations.c.story_id == fork_id,
                operations.c.turn_stage.in_(sorted(s.value for s in UNFINISHED_TURN_STAGES)),
            )
        )
    )
    closed: list[str] = []
    for row in rows:
        await _close(conn, fork_id, principal, row)
        closed.append(str(row.operation_id))
    return closed


async def _close(conn: AsyncConnection, fork_id: str, principal: str, row: Any) -> None:
    operation_id = str(row.operation_id)
    turn, variant = int(row.narrative_turn), int(row.turn_variant or 0)
    found = list(
        await conn.execute(
            select(events.c.type, events.c.beat, events.c.character_id, events.c.payload)
            .where(events.c.operation_id == operation_id)
            .order_by(events.c.seq)
        )
    )
    beat = max((int(e.beat) for e in found if e.beat is not None), default=1) + 1
    closure = TurnBeat(
        kind="narration", beat=beat, text=FORK_CLOSURE, character_id=None, narrator=True
    )
    names = {
        str(r.id): str(r.name)
        for r in await conn.execute(
            select(characters.c.id, characters.c.name).where(characters.c.story_id == fork_id)
        )
    }
    assembled = [
        AssemblyBeat(
            str(e.type).removeprefix("beat_"),
            str((e.payload or {}).get("text", "")),
            None if e.character_id is None else str(e.character_id),
            int(e.beat),
        )
        for e in found
        if e.type in _BEAT_EVENTS and e.beat is not None
    ]
    assembled.append(AssemblyBeat("narration", FORK_CLOSURE, None, beat))
    message = MessageRendered(
        text=deterministic_assembly(assembled, names, str(row.viewpoint or OMNISCIENT)),
        finish_reason="deterministic",
        composed=True,
    )
    staged = [
        ("beat_narration", validate("beat_narration", beat_payload(closure)), beat, None),
        ("message_rendered", validate("message_rendered", message), beat + 1, None),
    ]
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == fork_id)
    )
    from epistrel.core import world_clock  # here: the clock reads States, which use this

    basis = await world_clock.time_basis(conn, fork_id)
    ids: list[str] = []
    log: list[projections.LogEvent] = []
    for n, (kind, payload, at, character) in enumerate(staged):
        event_id = new_ulid()
        ids.append(event_id)
        seq = int(last or 0) + 1 + n
        category = lookup(kind).category
        provenance = Provenance.SYSTEM if kind.startswith("beat_") else Provenance.ENGINE
        await conn.execute(
            insert(events).values(
                id=event_id,
                story_id=fork_id,
                seq=seq,
                operation_id=operation_id,
                category=category.value,
                narrative_turn=turn,
                turn_variant=variant,
                beat=at,
                step=0,
                valid_time_minutes=basis.at(Position(turn, variant, 0, at, 0)),
                character_id=character,
                principal=principal,
                type=kind,
                payload=payload.model_dump(mode="json"),
                provenance=provenance.value,
                authority=Authority.AUTHORITATIVE.value,
            )
        )
        log.append(
            projections.LogEvent(
                id=event_id,
                story_id=fork_id,
                seq=seq,
                operation_id=operation_id,
                kind=OperationKind.TURN,
                membership=Membership.ACTIVE,
                category=category,
                narrative_turn=turn,
                turn_variant=variant,
                beat=at,
                character_id=character,
                provenance=provenance,
                type=kind,
                payload=payload,
            )
        )
    await projections.apply_events(conn, fork_id, log, kind=OperationKind.TURN.value)
    writer = SimpleNamespace(
        operation_id=operation_id,
        story_id=fork_id,
        narrative_turn=turn,
        turn_variant=variant,
        base_revision=row.base_revision,
    )
    source = records.Beat(
        "narration",
        FORK_CLOSURE,
        ids[0],
        beat=beat,
        provenance=RecordProvenance.DIRECTOR,
    )
    level = await scene_loader.current_level(conn, fork_id)
    if level == perception.NONE:
        await records.write_episodic(conn, writer, [source])
    else:
        state, moves = await scene_loader.load_scene(conn, fork_id, turn)
        for _, step in sorted(moves.items()):
            state = state.moved(step)
        routed = perception.route_turn(
            [perception.RouteBeat("narration", beat, None, perception.Qualifiers(), source=source)],
            state,
            level,
        )
        await perception.deliver(conn, writer, routed)
    entry = {"stage": "completion", "status": "done", "at": clock.now().isoformat(), "revision": 1}
    await conn.execute(
        update(operations)
        .where(operations.c.operation_id == operation_id)
        .values(
            turn_stage=StagedTurnStage.COMPLETED.value,
            stage_log=func.coalesce(operations.c.stage_log, text("'[]'::jsonb"))
            + literal([entry], JSONB),
        )
    )


__all__ = ["FORK_CLOSURE", "close_in_fork"]
