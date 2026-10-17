"""The ``## Open threads`` context source (FR-COMMIT-7, FR-RET-7; #146, through #141's
``core.retrieval.pipeline.CONTEXT_SOURCES``).

A retrieval's tier-1 items include one ``ledger`` item per salient open thunk introduced at or
before the query's turn: ``- <reference> (<open slot> unknown; raised turn N)``, so a render
keeps in mind what the story drew attention to and has not settled. Not salient thunks (no
obligation) are left out. Omniscient-independent in M4 (shared knowledge: every viewpoint sees
the same open threads; M5's partitions narrow it). Inert unless commitment is on, and
retrieval itself runs only with episodic memory on, so a Lite prompt never sees it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.commit.lifecycle import (
    commitment_on,
    open_clause,
    predicate_names,
    thunk_columns,
    thunk_of,
)
from epistrel.core.retrieval.assemble import ContextItem, Tier
from epistrel.core.retrieval.pipeline import SourceRequest, register_context_source
from epistrel.core.tables import entities
from epistrel.core.tables.commitments import thunks

OPEN_THREADS_HEADING = "## Open threads"
"""The heading #150's prompt puts over these items."""
MAX_THREADS = 20
"""The most open threads one retrieval carries (the oldest first)."""


async def open_threads(conn: AsyncConnection, request: SourceRequest) -> Sequence[ContextItem]:
    """The story's salient open thunks as tier-1 ``ledger`` items (see the module docstring)."""
    if not await commitment_on(conn, request.story_id):
        return []
    turn = request.position[0]
    t = thunks.alias("t")
    rows = await conn.execute(
        select(*thunk_columns(t))
        .where(
            t.c.story_id == request.story_id,
            t.c.salient.is_(True),
            open_clause(t),
            or_(t.c.narrative_turn.is_(None), t.c.narrative_turn <= turn),
        )
        .order_by(t.c.id)
        .limit(MAX_THREADS)
    )
    found = [thunk_of(r) for r in rows]
    if not found:
        return []
    ids = sorted({i for f in found for i in (f.sketch.subject_id, f.sketch.object_id) if i})
    names = {
        str(r.id): str(r.canonical_name)
        for r in await conn.execute(
            select(entities.c.id, entities.c.canonical_name).where(
                entities.c.story_id == request.story_id, entities.c.id.in_(ids)
            )
        )
    }
    predicates = await predicate_names(conn, [f.sketch.predicate_id for f in found])
    return [
        ContextItem(
            Tier.LEDGER_AND_PINS,
            "ledger",
            f.id,
            f"- {f.sketch.describe(names, predicates.get(f.sketch.predicate_id))} "
            f"({f.open_slot.value} unknown; raised turn {f.narrative_turn or 0})",
        )
        for f in found
    ]


register_context_source(open_threads)

__all__ = ["MAX_THREADS", "OPEN_THREADS_HEADING", "open_threads"]
