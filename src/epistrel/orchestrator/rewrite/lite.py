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
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.membership import active_operation
from epistrel.core.tables import characters, events, operations, transcript_messages
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import (
    LITE_REWRITE_REACH,
    LiteReachHit,
    LiteReachLine,
    LiteRewriteReachInputs,
    LiteRewriteReachResult,
    RewriteChange,
    RewriteParseResult,
    RewriteRef,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.rewrite.api import LineEdit

REACH_CALLS = 2
REACH_DEADLINE_S = 60.0
NO_CANON = "lite_no_canon"


CHUNK_LINES = 60
"""#700: a reach call reads at most this many lines (``lite_reach_chunk_messages``)."""
MAX_CALLS = 24
"""#700: a reach pass makes at most this many calls, the summary and re-check calls included
(``lite_reach_max_calls``; about 1,400 lines)."""


RECHECK_PREFIX = "Re-check (these lines were fixed for this correction): "
"""#700: the re-check call's correction, marked so it reads (and scripts) as the re-check."""


@dataclass(frozen=True, slots=True)
class _Line:
    turn: int
    speaker: str
    text: str


async def lines(db: AsyncEngine, story_id: str) -> list[LiteReachLine]:
    """The active transcript's lines, newest first (the reach pass's input)."""
    return [
        LiteReachLine(turn=line.turn, speaker=line.speaker, text=line.text)
        for line in await _lines(db, story_id)
    ]


async def _lines(db: AsyncEngine, story_id: str) -> list[_Line]:
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
            .limit(CHUNK_LINES * MAX_CALLS)
        )
        found = list(rows)
    return [
        _Line(
            turn=max(1, int(r.narrative_turn or 1)),
            speaker=names.get(str(r.actor_character_id or ""), "Narrator")
            if r.role != "user"
            else names.get(str(r.actor_character_id or ""), "Player"),
            text=str(r.text)[:4_000],
        )
        for r in found
    ]


async def _segments(db: AsyncEngine, story_id: str) -> list[Any]:
    """The active rolling-summary segments, newest first (the first reach call's input)."""
    from epistrel.core.tables import lite_summary_segments

    s = lite_summary_segments.c
    async with db.connect() as conn:
        return list(
            await conn.execute(
                select(s.covers_from, s.covers_to, s.text)
                .where(s.story_id == story_id, s.membership == "active")
                .order_by(s.covers_to.desc())
                .limit(CHUNK_LINES)
            )
        )


@dataclass(slots=True)
class Reach:
    """A reach pass: the record changes its fixes make, the hits it could not fix, and what it
    searched (``preview.reach``)."""

    changes: list[RewriteChange] = field(default_factory=list)
    unfixed: list[dict[str, Any]] = field(default_factory=list)
    searched: dict[str, int] | None = None
    truncated: bool = False
    calls: int = 0

    def meta(self) -> dict[str, Any]:
        return {"method": "model_searched", "searched": self.searched, "truncated": self.truncated}


async def _call(
    models: ModelAccessLayer, instruction: str, chunk: Sequence[LiteReachLine]
) -> LiteRewriteReachResult | None:
    found = await models.structured_call(
        LITE_REWRITE_REACH.name,
        LiteRewriteReachResult,
        LiteRewriteReachInputs(instruction=instruction, lines=list(chunk)),
        params=ModelParams(temperature=0.0),
        budget=TurnBudget(max_calls=REACH_CALLS, deadline_s=REACH_DEADLINE_S),
    )
    return None if isinstance(found, Uncertain) else found


def _ordered(found: Sequence[_Line], flagged: Sequence[tuple[int, int]]) -> list[_Line]:
    """The lines in search order: those in a flagged range (a summary segment that got a hit,
    newest segment first) first, then the rest newest first."""
    first: list[_Line] = []
    seen: set[int] = set()
    for lo, hi in flagged:
        for n, line in enumerate(found):
            if n not in seen and lo <= line.turn <= hi:
                first.append(line)
                seen.add(n)
    return [*first, *(line for n, line in enumerate(found) if n not in seen)]


async def reach(
    db: AsyncEngine, models: ModelAccessLayer, story_id: str, instruction: str
) -> Reach:
    """The reach pass (#700): the summary segments first (their hits flag turn ranges), then the
    transcript in chunks of ``CHUNK_LINES``, flagged ranges first and the rest newest first,
    within ``MAX_CALLS`` (one kept for the re-check); then one re-check call over the fixes
    (a fix still conflicting is no clean fix). What the cap leaves out is reported
    ``truncated``."""
    found = Reach()
    budget = MAX_CALLS - 1  # the re-check's call
    flagged: list[tuple[int, int]] = []
    segments = await _segments(db, story_id)
    if segments:
        result = await _call(
            models,
            instruction,
            [
                LiteReachLine(turn=max(1, int(g.covers_to)), speaker="Summary", text=str(g.text))
                for g in segments
            ],
        )
        found.calls += 1
        budget -= 1
        if result is not None:
            ranges = {int(g.covers_to): (int(g.covers_from), int(g.covers_to)) for g in segments}
            flagged = [ranges[h.turn] for h in result.hits if h.turn in ranges]
    ordered = _ordered(await _lines(db, story_id), flagged)
    chunks = [ordered[n : n + CHUNK_LINES] for n in range(0, len(ordered), CHUNK_LINES)]
    searched: list[_Line] = []
    succeeded = 0
    hits: dict[tuple[int, str], LiteReachHit] = {}
    for chunk in chunks:
        if budget <= 0:
            found.truncated = True
            break
        result = await _call(
            models,
            instruction,
            [LiteReachLine(turn=c.turn, speaker=c.speaker, text=c.text) for c in chunk],
        )
        found.calls += 1
        budget -= 1
        if result is None:
            found.truncated = True
            continue
        succeeded += 1
        searched.extend(chunk)
        for hit in result.hits:
            key = (hit.turn, hit.quote)
            kept = hits.get(key)
            if kept is None or _better(hit, kept):
                hits[key] = hit
    if chunks and not succeeded:
        found.unfixed.append({"reason": "reach_unavailable"})
        return found
    if searched:
        turns = [line.turn for line in searched]
        found.searched = {"from_turn": min(turns), "to_turn": max(turns)}
    fixes = [h for h in hits.values() if h.fix_text and h.fix_text.strip()]
    still = await _recheck(models, instruction, fixes) if fixes else set()
    if fixes:
        found.calls += 1
    for hit in sorted(hits.values(), key=lambda h: (h.turn, h.quote)):
        fix = (hit.fix_text or "").strip()
        if fix and (hit.turn, hit.quote) not in still:
            found.changes.append(
                RewriteChange(
                    kind="record",
                    ref=RewriteRef(turn=hit.turn, quote=hit.quote),
                    action="replace",
                    new_text=fix,
                )
            )
        else:
            found.unfixed.append(
                {
                    "reason": "no_clean_fix" if not fix else "recheck_failed",
                    "turn": hit.turn,
                    "relation": hit.relation,
                    "why": hit.reason,
                }
            )
    return found


def _better(hit: LiteReachHit, kept: LiteReachHit) -> bool:
    """#700's merge of duplicate hits: ``conflicts`` beats ``mentions``, then a fix beats
    none; otherwise the earliest chunk's (``kept``) stays."""
    if (hit.relation == "conflicts") != (kept.relation == "conflicts"):
        return hit.relation == "conflicts"
    return bool(hit.fix_text) and not kept.fix_text


async def _recheck(
    models: ModelAccessLayer, instruction: str, fixes: Sequence[LiteReachHit]
) -> set[tuple[int, str]]:
    """One re-check call over the fixed lines: a fix the model still finds conflicting with the
    correction (a ``conflicts`` hit on its line) is no clean fix. A failed call keeps none."""
    result = await _call(
        models,
        RECHECK_PREFIX + instruction,
        [LiteReachLine(turn=h.turn, speaker="Fixed", text=str(h.fix_text)) for h in fixes[:60]],
    )
    if result is None:
        return {(h.turn, h.quote) for h in fixes}
    still: set[tuple[int, str]] = set()
    for found in result.hits:
        if found.relation != "conflicts":
            continue
        for fix in fixes:
            # A hit on a fixed line quotes the fixed text (a quote of the original names a
            # line the re-check never read).
            if fix.turn == found.turn and _normal(found.quote) in _normal(str(fix.fix_text)):
                still.add((fix.turn, fix.quote))
    return still


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


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


def _speaker_kind(role: str, actor: str | None) -> str:
    if role == "user":
        return "player"
    if role == "consumer":
        return "consumer"
    return "character" if actor else "narrator"


async def line_edit_change(db: AsyncEngine, story_id: str, edit: LineEdit) -> RewriteParseResult:
    """#700: a line edit as the one record change it plans (no model call): the message named
    by ``event_id``, or by ``turn`` (and ``beat``) on the active variant. 422
    ``line_not_editable`` for an event that is no message of the transcript; 422
    ``beat_required`` for a turn of several messages named without a beat; 409
    ``record_inactive`` for a message off the active timeline (``dormant_variant`` when it is
    on a dormant one); 422 ``rewrite_noop`` for the line's own text."""
    from epistrel.core.errors import (
        AmendmentTargetInactive,
        BeatRequired,
        LineNotEditable,
        RewriteNoop,
    )

    tm, e, o = transcript_messages.c, events.c, operations.c
    event_kind: str | None = None
    async with db.connect() as conn:
        if edit.event_id is not None:
            event = (
                await conn.execute(
                    select(e.id, e.type).where(e.story_id == story_id, e.id == edit.event_id)
                )
            ).one_or_none()
            if event is None:
                raise LineNotEditable(
                    "the line edit names no event of the story", {"event_kind": None}
                )
            event_kind = str(event.type)
            where = tm.message_id == edit.event_id
        else:
            where = tm.narrative_turn == edit.turn
        found = list(
            await conn.execute(
                select(
                    tm.message_id,
                    tm.text,
                    tm.role,
                    tm.actor_character_id,
                    tm.beat,
                    tm.narrative_turn,
                    tm.turn_variant,
                    o.membership,
                    active_operation(operations).label("active"),
                )
                .select_from(
                    transcript_messages.join(operations, o.operation_id == tm.operation_id)
                )
                .where(tm.story_id == story_id, where)
                .order_by(tm.seq)
            )
        )
        if edit.event_id is not None and not found:
            raise LineNotEditable(
                "the line edit names an event that is not a message of the transcript",
                {"event_kind": event_kind},
            )
        active = [r for r in found if r.active]
        if edit.turn is not None:
            if edit.beat is not None:
                active = [r for r in active if r.beat == edit.beat]
            if not active:
                raise AmendmentTargetInactive(
                    "the line edit names no active message of that turn",
                    {"record_table": "events", "record_id": None, "membership": None},
                )
            if len(active) > 1:
                raise BeatRequired(
                    "the turn has several messages: name the beat",
                    {
                        "beats": [
                            {
                                "beat": r.beat,
                                "event_id": str(r.message_id),
                                "speaker_kind": _speaker_kind(str(r.role), r.actor_character_id),
                            }
                            for r in active
                        ]
                    },
                )
        elif not active:
            row = found[0]
            details: dict[str, Any] = {
                "record_table": "events",
                "record_id": str(row.message_id),
                "membership": str(getattr(row.membership, "value", row.membership)),
            }
            if details["membership"] == "dormant":
                details["dormant_variant"] = {
                    "turn": row.narrative_turn,
                    "variant": row.turn_variant,
                }
            raise AmendmentTargetInactive("the line is not on the active timeline", details)
        (line,) = active
    if str(line.text) == edit.new_text:
        raise RewriteNoop("the line already reads so", {"already_true": 1})
    return RewriteParseResult(
        changes=[
            RewriteChange(
                kind="record",
                ref=RewriteRef(event_id=str(line.message_id)),
                action="replace",
                new_text=edit.new_text,
            )
        ]
    )


__all__ = [
    "CHUNK_LINES",
    "MAX_CALLS",
    "NO_CANON",
    "Reach",
    "line_edit_change",
    "lines",
    "lite_result",
    "reach",
]
