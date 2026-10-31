"""The world clock's post-advance callbacks (FR-STATE-7; #192).

``core.world_clock.advance`` runs every registered callback after it writes an advance, in the
advance's transaction (the operation's commit), with the written ``Advance``: whatever a
callback writes commits with the advance, follows its operation's membership (a truncation, a
swap or a fork moves both together) and is rolled back with it. ``core.states.rules`` registers
its ``elapsed`` rules here (``rules.apply_elapsed``); #196/#197 may register due-event and grace
checks the same way. A callback runs in registration order and registering one twice keeps one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncConnection

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation
    from epistrel.core.world_clock import Advance

PostAdvance = Callable[[AsyncConnection, "Operation", str, "Advance"], Awaitable[None]]
"""``(conn, op, story_id, advance)``: called after ``advance`` is written in ``op``."""

_CALLBACKS: list[PostAdvance] = []


def register_post_advance(callback: PostAdvance) -> None:
    """Run ``callback`` after every committed clock advance (once, however often registered)."""
    if callback not in _CALLBACKS:
        _CALLBACKS.append(callback)


def unregister_post_advance(callback: PostAdvance) -> None:
    """Stop running ``callback`` (a test's own)."""
    if callback in _CALLBACKS:
        _CALLBACKS.remove(callback)


def registered() -> tuple[PostAdvance, ...]:
    """The callbacks, in the order they run."""
    return tuple(_CALLBACKS)


async def run_post_advance(
    conn: AsyncConnection, op: Operation, story_id: str, advance: Advance
) -> None:
    """Run every callback for ``advance`` (``core.world_clock.advance`` calls this)."""
    for callback in tuple(_CALLBACKS):
        await callback(conn, op, story_id, advance)


__all__ = [
    "PostAdvance",
    "register_post_advance",
    "registered",
    "run_post_advance",
    "unregister_post_advance",
]
