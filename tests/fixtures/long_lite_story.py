"""A long Lite story for the backfill tests (#228): N messages (N/2 turns, each the player's
input and a reply), committed one turn per transaction, with planted statements at known turns.

The planted phrasings are module constants, and ``extraction_answer`` is what the stub's
``extraction_backstop`` answers for each (the stub is scripted per planted line): Bobby is
thirty at ``EARLY`` and thirty-five at ``LATE`` (a contradiction), and, in the ``changed``
variant, turns thirty-five at ``CHANGE`` (a narrated change between them).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.orchestrator.models.registry import EXTRACTION_BACKSTOP
from epistrel.orchestrator.models.stub import StubAdapter

EARLY = 100
LATE = 400
CHANGE = 250
THIRTY = "Bobby is thirty years old."
THIRTY_FIVE = "Bobby is thirty-five years old."
TURNED = "Bobby turned thirty-five on his birthday."
PLANTED_REPLY = "Cal nods at that."


def say_at(turn: int, *, changed: bool) -> str | None:
    """The player's planted line at ``turn``, if any."""
    if turn == EARLY:
        return THIRTY
    if turn == LATE:
        return THIRTY_FIVE
    if changed and turn == CHANGE:
        return TURNED
    return None


async def long_lite_story(
    engine: AsyncEngine, story_id: str, player: str, messages: int, *, changed: bool = False
) -> dict[int, str]:
    """Play ``messages`` messages into ``story_id`` (``messages // 2`` turns): each turn the
    player's input (``player`` speaking) and a reply; answers each turn's input event id."""
    inputs: dict[int, str] = {}
    for n in range(1, messages // 2 + 1):
        line = say_at(n, changed=changed) or f"Line {n}."
        async with Operation(
            engine, story_id=story_id, kind=OperationKind.TURN, principal="p-test"
        ) as op:
            op.append(
                "player_input",
                {"text": line, "say": line},
                provenance=Provenance.PLAYER,
                character_id=player,
            )
            op.append(
                "message_rendered",
                {"text": PLANTED_REPLY if say_at(n, changed=changed) else f"Reply {n}."},
            )
            await op.commit()
        inputs[int(op.narrative_turn or n)] = op.operation_id
    return inputs


def _claim(span: str, value: str, *, time: str | None = None) -> dict[str, Any]:
    return {
        "beat": 0,
        "span": span,
        "subject": "Bobby",
        "predicate": "age",
        "object": value,
        "time": time,
        "polarity": "affirmed",
        "confidence": 0.95,
    }


def script(stub: StubAdapter) -> None:
    """Script the stub's extraction for each planted line."""
    for line, claim in (
        (THIRTY, _claim("Bobby is thirty years old", "30")),
        (THIRTY_FIVE, _claim("Bobby is thirty-five years old", "35")),
        (TURNED, _claim("Bobby turned thirty-five", "35", time="on his birthday")),
    ):
        stub.when(line, json.dumps({"claims": [claim]}), purpose=EXTRACTION_BACKSTOP.name)
