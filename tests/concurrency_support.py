"""Helpers for the concurrency tests (#47): a stub model held on latches, turns driven through
``run_with_revalidation``, and the test-only truth-changing operations.

The timeline controls (#65/#66) are not built yet, so their operations are written here
through ``Operation`` with the flags the real ones will declare (``change_setting`` stands in
for a settings write, which #64 records as a ``control`` operation); ``set_pins`` and
``set_instructions`` write #60's events raw (no caps or gate). #61/#63/#64 reuse these.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.concurrency import (
    ConfigRecheckFn,
    LiteReadSet,
    RevalidationResult,
    run_with_revalidation,
)
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from tests.event_support import play_turn, rows, scalar

TURN_PRINCIPAL = "p-player"
EDIT_PRINCIPAL = "p-author"

OnCall = Callable[[Operation], Awaitable[None]]


@dataclass
class HeldModel:
    """A stub model: every call waits until the test releases it, then stages a reply.

    ``on_call`` runs when a call arrives, before it waits (the lock probe uses it).
    """

    said: str
    on_call: OnCall | None = None
    calls: int = 0
    _gate: asyncio.Event | None = None
    _arrived: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, op: Operation, inputs: Any) -> None:
        self.calls += 1
        gate = asyncio.Event()
        self._gate = gate
        if self.on_call is not None:
            await self.on_call(op)
        self._arrived.set()
        await gate.wait()
        op.append("player_input", {"text": self.said}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": f"reply {self.calls} to {self.said}"})

    async def arrived(self) -> None:
        await self._arrived.wait()

    def release(self) -> None:
        assert self._gate is not None
        self._arrived.clear()
        self._gate.set()


@dataclass
class HeldTurn:
    """A turn whose generation is held: start it, act on the story, then release it."""

    op: Operation
    model: HeldModel
    task: asyncio.Task[RevalidationResult]

    async def release(self) -> None:
        """Let the current generation finish; return when the turn ended or generates again."""
        self.model.release()
        waiting = asyncio.ensure_future(self.model.arrived())
        await asyncio.wait({self.task, waiting}, return_when=asyncio.FIRST_COMPLETED)
        waiting.cancel()

    @property
    def done(self) -> bool:
        return self.task.done()


async def _no_inputs(conn: AsyncConnection) -> None:
    return None


async def start_turn(
    engine: AsyncEngine | AsyncConnection,
    story_id: str,
    said: str,
    *,
    budget: int | None = None,
    on_call: OnCall | None = None,
    turn: int | None = None,
    model: HeldModel | None = None,
    config_recheck: ConfigRecheckFn | None = None,
) -> HeldTurn:
    """Begin a turn under FR-CONC-4 and hold it inside its first model call. ``model``
    replaces the default ``HeldModel``; ``config_recheck`` is #64's re-check (FR-CONC-16)."""
    model = model or HeldModel(said, on_call=on_call)
    op = Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=TURN_PRINCIPAL, turn=turn
    )
    task = asyncio.ensure_future(
        run_with_revalidation(
            op,
            begin=_no_inputs,
            generate=model,
            budget=budget,
            read_set=LiteReadSet(),
            config_recheck=config_recheck,
        )
    )
    arrived = asyncio.ensure_future(model.arrived())
    await asyncio.wait({task, arrived}, return_when=asyncio.FIRST_COMPLETED)
    arrived.cancel()
    if task.done():
        task.result()  # surface a failure to begin
    return HeldTurn(op=op, model=model, task=task)


async def edit(
    engine: AsyncEngine,
    story_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    bumps_revision: bool,
    kind: OperationKind = OperationKind.CONTROL,
    turn: int | None = None,
) -> Operation:
    """A single-event operation that commits against current state."""
    async with Operation(
        engine,
        story_id=story_id,
        kind=kind,
        principal=EDIT_PRINCIPAL,
        turn=turn,
        bumps_revision=bumps_revision,
    ) as op:
        op.append(event_type, payload, provenance=Provenance.CONSUMER)
        await op.commit()
    return op


async def set_pins(engine: AsyncEngine, story_id: str, *memories: str) -> Operation:
    """A raw pinned-memory event: truth-changing (FR-CONC-1) and a read-set member. Bypasses
    #60's caps, gate and id rules (``PUT /stories/{id}/pinned-memories`` is the real path), so
    a test can pin more than any cap allows; ``previous`` is not recorded."""
    items = [{"id": new_ulid(), "text": memory} for memory in memories]
    return await edit(
        engine, story_id, "pinned_memories_set", {"items": items}, bumps_revision=True
    )


async def set_instructions(engine: AsyncEngine, story_id: str, text: str) -> Operation:
    """A raw custom-instructions event (see ``set_pins``)."""
    return await edit(
        engine, story_id, "custom_instructions_set", {"text": text}, bumps_revision=True
    )


async def change_setting(engine: AsyncEngine, story_id: str) -> Operation:
    """A story setting change: not a truth change (FR-CONC-1), no bump."""
    return await edit(
        engine, story_id, "config_changed", {"setting": "temperature"}, bumps_revision=False
    )


async def truth_outside_read_set(engine: AsyncEngine, story_id: str) -> Operation:
    """A committed truth change no Lite prompt input reads (e.g. a later milestone's API-made
    change): bumps the revision without touching the read set, so turns rebase."""
    return await edit(
        engine, story_id, "config_changed", {"setting": "outside-read-set"}, bumps_revision=True
    )


async def summarise(engine: AsyncEngine, story_id: str, through: int) -> Operation:
    """Derived background work: a summary segment. Never bumps the revision."""
    return await edit(
        engine,
        story_id,
        "lite_summary_segment",
        {"text": f"summary through {through}", "through_turn": through},
        bumps_revision=False,
        kind=OperationKind.CONSOLIDATION,
        turn=through,
    )


async def reject_render(engine: AsyncEngine, story_id: str, at: int) -> Operation:
    """Audit-only work (a rejected render) recorded at a turn. Never bumps the revision."""
    return await edit(
        engine,
        story_id,
        "guardrail_output_rejected",
        {"text": "no", "reason": "test"},
        bumps_revision=False,
        kind=OperationKind.MAINTENANCE,
        turn=at,
    )


async def regenerate(engine: AsyncEngine, story_id: str) -> Operation:
    """Regenerate the head turn: a new variant; the old one goes dormant (a control action)."""
    head = int(
        await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id)
    )
    return await play_turn(engine, story_id, "again", turn=head - 1)


async def next_turn_index(engine: AsyncEngine, story_id: str) -> int:
    return int(
        await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id)
    )


async def revision(engine: AsyncEngine, story_id: str) -> int:
    return int(
        await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story_id)
    )


async def operation_row(engine: AsyncEngine, operation_id: str) -> Any:
    (row,) = await rows(
        engine,
        "SELECT status, base_revision, read_set, attempts, narrative_turn, turn_variant "
        "FROM operations WHERE operation_id = :o",
        o=operation_id,
    )
    return row


async def event_count(engine: AsyncEngine, operation_id: str) -> int:
    return int(
        await scalar(engine, "SELECT count(*) FROM events WHERE operation_id = :o", o=operation_id)
    )


async def chain(engine: AsyncEngine, story_id: str) -> list[tuple[int, int]]:
    """The active committed turn variants, by turn: ``[(turn, variant), ...]``."""
    found = await rows(
        engine,
        "SELECT narrative_turn, turn_variant FROM operations WHERE story_id = :s "
        "AND kind = 'turn' AND status = 'committed' AND membership = 'active' "
        "ORDER BY narrative_turn, turn_variant",
        s=story_id,
    )
    return [(int(t), int(v)) for t, v in found]


async def assert_single_chain(engine: AsyncEngine, story_id: str) -> None:
    """Exactly one active variant per turn index, turns 1..k with no gap, next index k+1."""
    active = await chain(engine, story_id)
    turns = [t for t, _ in active]
    assert turns == list(range(1, len(turns) + 1)), active
    head = int(
        await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id)
    )
    assert head == len(turns) + 1, (head, active)
