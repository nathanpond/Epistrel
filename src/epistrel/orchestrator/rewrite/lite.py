"""A Lite story's history rewrite (FR-LITE-8; #245): reach is model-searched.

A Lite story has no canon and no dependency edges, so a rewrite's reach is found by one
``lite_rewrite_reach`` pass over the active transcript (newest first, at most
``LITE_REACH_LINES_MAX`` lines): each line the correction contradicts or mentions, with the
least fix. Every hit with a fix becomes a **record change** (#221's record part: an ``overlay``
``replace`` on the line's event), normalized and previewed by #223's planner exactly as an
instruction's own record changes are, so the originals stay in the as-originally-recorded view
and undo and redo work unchanged. A hit with no clean fix is listed as a notification, never
changed. The instruction's proposition and State changes have no Lite form (Lite has no canon):
they are refused (``lite_no_canon``), and the record changes carry the correction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.membership import active_operation
from epistrel.core.tables import characters, events, operations, transcript_messages
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import (
    LITE_REACH_LINES_MAX,
    LITE_REWRITE_REACH,
    LiteReachLine,
    LiteRewriteReachInputs,
    LiteRewriteReachResult,
    RewriteChange,
    RewriteParseResult,
    RewriteRef,
)
from epistrel.orchestrator.models.structured import Uncertain

REACH_CALLS = 2
REACH_DEADLINE_S = 60.0
NO_CANON = "lite_no_canon"


async def lines(db: AsyncEngine, story_id: str) -> list[LiteReachLine]:
    """The active transcript's lines, newest first (the reach pass's input)."""
    tm, e = transcript_messages.c, events.c
    async with db.connect() as conn:
        names = {
            str(r.id): str(r.name)
            for r in await conn.execute(
                select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
            )
        }
        rows = await conn.execute(
            select(tm.text, tm.actor_character_id, tm.role, e.narrative_turn)
            .select_from(
                transcript_messages.join(events, events.c.id == tm.message_id).join(
                    operations, operations.c.operation_id == e.operation_id
                )
            )
            .where(e.story_id == story_id, active_operation(operations))
            .order_by(e.seq.desc())
            .limit(LITE_REACH_LINES_MAX)
        )
        found = list(rows)
    return [
        LiteReachLine(
            turn=max(1, int(r.narrative_turn or 1)),
            speaker=names.get(str(r.actor_character_id or ""), "Narrator")
            if r.role != "user"
            else names.get(str(r.actor_character_id or ""), "Player"),
            text=str(r.text)[:4_000],
        )
        for r in found
    ]


async def reach(
    db: AsyncEngine, models: ModelAccessLayer, story_id: str, instruction: str
) -> tuple[list[RewriteChange], list[dict[str, Any]]]:
    """The reach pass: the record changes its fixes make, and the hits it could not fix
    (notifications)."""
    found = await models.structured_call(
        LITE_REWRITE_REACH.name,
        LiteRewriteReachResult,
        LiteRewriteReachInputs(instruction=instruction, lines=await lines(db, story_id)),
        params=ModelParams(temperature=0.0),
        budget=TurnBudget(max_calls=REACH_CALLS, deadline_s=REACH_DEADLINE_S),
    )
    if isinstance(found, Uncertain):
        return [], [{"reason": "reach_unavailable", "detail": str(found.reason)}]
    changes: list[RewriteChange] = []
    unfixed: list[dict[str, Any]] = []
    for hit in found.hits:
        if hit.fix_text and hit.fix_text.strip():
            changes.append(
                RewriteChange(
                    kind="record",
                    ref=RewriteRef(turn=hit.turn, quote=hit.quote),
                    action="replace",
                    new_text=hit.fix_text.strip(),
                )
            )
        else:
            unfixed.append(
                {
                    "reason": "no_clean_fix",
                    "turn": hit.turn,
                    "relation": hit.relation,
                    "why": hit.reason,
                }
            )
    return changes, unfixed


def lite_result(
    parsed: RewriteParseResult, reached: Sequence[RewriteChange]
) -> tuple[RewriteParseResult, list[dict[str, Any]]]:
    """The parse result a Lite story plans: its record and intent changes plus the reach
    pass's; its proposition and State changes refused (``lite_no_canon``)."""
    kept = [c for c in parsed.changes if c.kind in ("record", "intent")]
    refused = [
        {"reason": NO_CANON, "change": c.model_dump(mode="json", by_alias=True)}
        for c in parsed.changes
        if c.kind not in ("record", "intent")
    ]
    seen = {(c.ref.turn, c.ref.quote) for c in kept if c.ref is not None}
    merged = [*kept, *(c for c in reached if (c.ref.turn, c.ref.quote) not in seen)]  # type: ignore[union-attr]
    return parsed.model_copy(update={"changes": merged[:20]}), refused


__all__ = ["NO_CANON", "lines", "lite_result", "reach"]
