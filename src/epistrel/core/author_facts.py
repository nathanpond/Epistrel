"""A Lite story's author facts as an upgrade sees them (FR-LITE-8; #699, #700, #717).

A Lite story holds two kinds of author text that an upgrade to a structured configuration
parses with ``directive_parse``, one model call each:

- its active lasting author facts (``lite_facts``: the ``directive_applied`` events a Lite
  durable directive recorded, #244);
- its active ``lite_note`` amendment parts (``lite_note_parts``: a Lite rewrite's world notes,
  #700).

A completed backfill records the facts it converted (``converted_before``), so a re-upgrade
never parses one twice. ``pending_parses`` is what an upgrade's estimate counts (#244's
**Upgrade** AC: "the estimate counts one parse call per active fact"). The queries live in Core
so the impact report can count them; the Orchestrator's backfill job reads the same functions.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.membership import active_operation
from epistrel.core.tables import amendment_parts, amendments, events, operations, story_backfills

NOTE_KEY = "note:"
"""A Lite world note's key among the parsed author facts (``note:<part id>``, #700)."""


async def lite_facts(conn: AsyncConnection, story_id: str) -> list[tuple[str, int, int, str]]:
    """#699: the active Lite lasting author facts in narrative order: ``(event id, turn,
    slot, text)``."""
    o, e = operations.c, events.c
    rows = await conn.execute(
        select(e.id, o.narrative_turn, o.slot_k, e.payload)
        .join(operations, o.operation_id == e.operation_id)
        .where(
            e.story_id == story_id,
            e.type == "directive_applied",
            e.payload["lite"].as_boolean().is_(True),
            active_operation(operations),
        )
        .order_by(o.narrative_turn, o.slot_k, e.seq)
    )
    return [
        (str(r.id), int(r.narrative_turn or 0), int(r.slot_k or 0), str(r.payload.get("text")))
        for r in rows
        if str(dict(r.payload or {}).get("text", "")).strip()
    ]


async def lite_note_parts(conn: AsyncConnection, story_id: str) -> list[tuple[str, str]]:
    """#700: the story's active ``lite_note`` parts, oldest first: ``(part_id, text)`` (an
    upgrade converts each like a lasting author fact)."""
    a, p = amendments.c, amendment_parts.c
    found = await conn.execute(
        select(p.id, p.patch_after)
        .select_from(amendment_parts.join(amendments, a.id == p.amendment_id))
        .where(
            p.story_id == story_id,
            p.kind == "lite_note",
            p.status == "active",
            a.status == "active",
        )
        .order_by(p.tx_from, p.id)
    )
    return [(str(i), str((after or {}).get("text") or "")) for i, after in found]


async def converted_before(conn: AsyncConnection, story_id: str) -> set[str]:
    """#699: the Lite fact keys earlier completed backfills of the story converted (a
    re-upgrade never converts one twice)."""
    rows = await conn.execute(
        select(story_backfills.c.report).where(
            story_backfills.c.story_id == story_id, story_backfills.c.status == "completed"
        )
    )
    found: set[str] = set()
    for (report,) in rows:
        found.update(str(i) for i in dict(report or {}).get("author_facts", {}).get("ids", []))
    return found


async def pending_keys(conn: AsyncConnection, story_id: str) -> list[str]:
    """The keys of the facts and notes an upgrade of the story would parse now: each active
    Lite lasting fact's event id and each active note's ``note:<part id>``, less the ones an
    earlier backfill converted."""
    keys = [event_id for event_id, *_ in await lite_facts(conn, story_id)]
    keys += [f"{NOTE_KEY}{part_id}" for part_id, _ in await lite_note_parts(conn, story_id)]
    if not keys:
        return []
    done = await converted_before(conn, story_id)
    return [k for k in keys if k not in done]


async def pending_parses(conn: AsyncConnection, story_ids: Collection[str]) -> dict[str, int]:
    """#717: per story, the ``directive_parse`` calls an upgrade's backfill would make (one per
    pending fact or note); a story with none is left out."""
    found: dict[str, int] = {}
    for story_id in sorted(set(story_ids)):
        count = len(await pending_keys(conn, story_id))
        if count:
            found[story_id] = count
    return found


__all__ = [
    "NOTE_KEY",
    "converted_before",
    "lite_facts",
    "lite_note_parts",
    "pending_keys",
    "pending_parses",
]
