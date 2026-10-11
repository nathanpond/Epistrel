"""Helpers for the projection tests: synthetic timeline control through ``Operation``, and
canonical snapshots of the projection rows.

Truncate, untruncate and swap call ``core.timeline`` (#65, #66).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline
from epistrel.core.errors import NothingToUndo, UnknownChain
from epistrel.core.tables import stories
from epistrel.core.timeline import Swapped, Truncation, Untruncation
from epistrel.core.variants import ChainRef, SwapRequest

CONTROL_PRINCIPAL = "p-control"


@dataclass(frozen=True)
class Snapshot:
    """Every projection row of a story as canonical JSON text, in primary-key order."""

    transcript: tuple[str, ...]
    heads: tuple[str, ...]
    texts: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()


async def snapshot(engine: AsyncEngine, story_id: str) -> Snapshot:
    async with engine.connect() as conn:
        transcript = await conn.scalars(
            text(
                "SELECT to_jsonb(t)::text FROM transcript_messages t "
                "WHERE story_id = :s ORDER BY message_id"
            ),
            {"s": story_id},
        )
        transcript_rows = tuple(transcript)
        heads = await conn.scalars(
            text(
                "SELECT to_jsonb(h)::text FROM story_heads h WHERE story_id = :s ORDER BY story_id"
            ),
            {"s": story_id},
        )
        heads_rows = tuple(heads)
        texts = tuple(
            await conn.scalars(
                text("SELECT to_jsonb(x)::text FROM story_texts x WHERE story_id = :s"),
                {"s": story_id},
            )
        )
        segments = await conn.scalars(
            text(
                "SELECT to_jsonb(g)::text FROM lite_summary_segments g "
                "WHERE story_id = :s ORDER BY segment_id"
            ),
            {"s": story_id},
        )
        return Snapshot(
            transcript=transcript_rows, heads=heads_rows, texts=texts, segments=tuple(segments)
        )


async def truncate(engine: AsyncEngine, story_id: str, to_turn: int) -> Truncation | None:
    """Truncate through #65's ``core.timeline.truncate``; ``None`` (nothing written) when
    ``to_turn`` is at or past the head, which the real call refuses with 422."""
    async with engine.connect() as conn:
        found = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    head = int(found or 0)
    if to_turn >= head - 1:
        return None
    return await timeline.truncate(
        engine, story_id, timeline.TruncateRequest(to_turn=to_turn), principal=CONTROL_PRINCIPAL
    )


async def untruncate(engine: AsyncEngine, story_id: str) -> Untruncation | None:
    """Untruncate through #65's ``core.timeline.untruncate`` (a swap when newer turns were
    written over the tail, #66); ``None`` when there is nothing to undo."""
    try:
        return await timeline.untruncate(engine, story_id, principal=CONTROL_PRINCIPAL)
    except NothingToUndo:
        return None


async def swap(engine: AsyncEngine, story_id: str) -> Swapped | None:
    """Swap to a dormant continuation through #66's ``core.timeline.swap``: the tail of the
    latest undoable truncation when there is one, else the latest dormant chain that resolves
    on its own; ``None`` when there is none."""
    state = await timeline.undo_state(engine, story_id)
    ordered = sorted(
        state.dormant_chains,
        key=lambda c: (c.truncate_operation_id or "", c.from_turn, c.variant),
        reverse=True,
    )
    for chain in ordered:
        try:
            return await timeline.swap(
                engine,
                story_id,
                SwapRequest(to_variant_chain=ChainRef(turn=chain.from_turn, variant=chain.variant)),
                principal=CONTROL_PRINCIPAL,
            )
        except UnknownChain:
            continue
    return None
