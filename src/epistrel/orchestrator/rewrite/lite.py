"""A Lite story's history rewrite (FR-LITE-8; #245, #700): reach is model-searched.

A Lite story has no canon and no dependency edges. An instruction is first read by one
``lite_rewrite_parse`` call (``parse``): the correction as one plain change statement, whether
it changes the story's world (then the amendment gets a ``lite_note`` part, "Author
correction: ..."), and what it could not decide (each a clarification question with a
recommended option; an answer reads the instruction again). Then the **reach pass**
(``reach``) searches with the change statement:

- the rolling-summary segments first, in one call: a hit flags the segment's turn range;
- then the active transcript in chunks (``lite_reach_chunk_messages`` lines, closed early at
  ``lite_reach_chunk_tokens``), flagged ranges first and the rest newest first, up to
  ``lite_reach_parallelism`` chunks at once, within ``lite_reach_max_calls`` calls and
  ``lite_reach_deadline_seconds``;
- then one re-check call over the fixes: a fix the model still finds conflicting is no clean
  fix.

Each hit with a clean fix becomes a **record change** (#221's record part: an ``overlay``
``replace`` on the line's event), normalized and previewed by #223's planner exactly as an
instruction's own record changes are, so the originals stay in the as-originally-recorded view
and undo and redo work unchanged. A hit with no clean fix is listed, never changed. What the
caps leave out is reported ``truncated`` (the session then previews first). ``after_seq``: a reach
over only the lines committed after an event position: an undo's over the lines after the undone
amendment (#238), and the apply's revalidation over the turns played since the preview (#237).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import RewriteUnparseable
from epistrel.core.membership import active_operation
from epistrel.core.settings_schema import RewriteSettings
from epistrel.core.summary import estimate_tokens
from epistrel.core.tables import characters, events, operations, transcript_messages
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import (
    LITE_PARSE_FACTS_MAX,
    LITE_PARSE_RECENT_MAX,
    LITE_REWRITE_PARSE,
    LITE_REWRITE_REACH,
    LiteReachHit,
    LiteReachLine,
    LiteRewriteParseInputs,
    LiteRewriteParseResult,
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
PARSE_CALLS = 2
PARSE_DEADLINE_S = 60.0
NO_CANON = "lite_no_canon"


CHUNK_LINES = RewriteSettings().lite_reach_chunk_messages
"""#700: a reach call reads at most this many lines by default (``lite_reach_chunk_messages``)."""
MAX_CALLS = RewriteSettings().lite_reach_max_calls
"""#700: a reach pass makes at most this many calls by default, the summary and re-check calls
included (``lite_reach_max_calls``; about 1,400 lines)."""
LINE_CHARS_MAX = 4_000
"""A line's text as a reach call reads it (``LiteReachLine.text``'s bound)."""


RECHECK_PREFIX = "Re-check (these lines were fixed for this correction): "
"""#700: the re-check call's correction, marked so it reads (and scripts) as the re-check."""
UNDO_PREFIX = "Undo: "
UNDO_SUFFIX = " (the original text is restored)"
"""#700: an undo's reach searches with this template statement, with no parse call (#245's
Discretion: "Undo: " + the change statement + " (the original text is restored)")."""


PLAYER_ROLES = frozenset({"player"})
"""The transcript ``role`` of a line the player wrote (``core.projections.transcript``): never
rewritten silently (#223's rule)."""


@dataclass(frozen=True, slots=True)
class _Line:
    turn: int
    speaker: str
    text: str


async def lines(db: AsyncEngine, story_id: str) -> list[LiteReachLine]:
    """The active transcript's lines, newest first (the reach pass's input)."""
    return [
        LiteReachLine(turn=line.turn, speaker=line.speaker, text=line.text)
        for line in await _lines(db, story_id, limit=CHUNK_LINES * MAX_CALLS)
    ]


async def _lines(
    db: AsyncEngine, story_id: str, *, limit: int, after_seq: int | None = None
) -> list[_Line]:
    tm, e = transcript_messages.c, events.c
    query = (
        select(tm.text, tm.actor_character_id, tm.role, e.narrative_turn)
        .select_from(
            transcript_messages.join(events, events.c.id == tm.message_id).join(
                operations, operations.c.operation_id == e.operation_id
            )
        )
        .where(e.story_id == story_id, active_operation(operations))
        .order_by(e.seq.desc())
        .limit(limit)
    )
    if after_seq is not None:
        query = query.where(e.seq > after_seq)
    async with db.connect() as conn:
        names = {
            str(r.id): str(r.name)
            for r in await conn.execute(
                select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
            )
        }
        found = list(await conn.execute(query))
    return [
        _Line(
            turn=max(1, int(r.narrative_turn or 1)),
            speaker=names.get(str(r.actor_character_id or ""), "Narrator")
            if r.role not in PLAYER_ROLES
            else names.get(str(r.actor_character_id or ""), "Player"),
            text=str(r.text)[:LINE_CHARS_MAX],
        )
        for r in found
    ]


async def _segments(db: AsyncEngine, story_id: str, limit: int) -> list[Any]:
    """The active rolling-summary segments, newest first (the first reach call's input)."""
    from epistrel.core.tables import lite_summary_segments

    s = lite_summary_segments.c
    async with db.connect() as conn:
        return list(
            await conn.execute(
                select(s.covers_from, s.covers_to, s.text)
                .where(s.story_id == story_id, s.membership == "active")
                .order_by(s.covers_to.desc())
                .limit(limit)
            )
        )


# -- the parse (#700) ----------------------------------------------------------------------------


async def parse(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    instruction: str,
    answers: Sequence[str] = (),
) -> LiteRewriteParseResult:
    """The one ``lite_rewrite_parse`` call (temperature 0) over the instruction, the answers so
    far, the latest lines and the story's lasting author facts; 422 ``rewrite_unparseable``
    when it yields nothing usable."""
    from epistrel.core import author_facts

    recent = await _lines(db, story_id, limit=LITE_PARSE_RECENT_MAX)
    async with db.connect() as conn:
        facts = [text for *_, text in await author_facts.lite_facts(conn, story_id)]
    found = await models.structured_call(
        LITE_REWRITE_PARSE.name,
        LiteRewriteParseResult,
        LiteRewriteParseInputs(
            instruction=instruction,
            answers=list(answers)[:20],
            recent=[LiteReachLine(turn=c.turn, speaker=c.speaker, text=c.text) for c in recent],
            author_facts=[f[:LINE_CHARS_MAX] for f in facts[-LITE_PARSE_FACTS_MAX:]],
        ),
        params=ModelParams(temperature=0.0),
        budget=TurnBudget(max_calls=PARSE_CALLS, deadline_s=PARSE_DEADLINE_S),
    )
    if isinstance(found, Uncertain):
        raise RewriteUnparseable(
            "the instruction could not be read as a correction",
            {"reason": str(found.reason), "attempts": found.attempts},
        )
    return found


def statement_of(parsed: LiteRewriteParseResult, instruction: str) -> str:
    """The change statement the reach pass searches with (the instruction when the parse gave
    none)."""
    return parsed.change_statement.strip() or instruction


def undo_statement(statement: str) -> str:
    """#700: an undo's reach statement (no parse call)."""
    return f"{UNDO_PREFIX}{statement}{UNDO_SUFFIX}"


# -- the reach pass --------------------------------------------------------------------------------


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


async def _guarded(
    models: ModelAccessLayer, instruction: str, chunk: Sequence[LiteReachLine]
) -> LiteRewriteReachResult | None:
    """A chunk's call; a provider failure after #53's retries leaves the chunk unsearched."""
    from epistrel.orchestrator.models.errors import ModelError

    try:
        return await _call(models, instruction, chunk)
    except ModelError:
        return None


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


def chunked(ordered: Sequence[_Line], size: int, tokens: int) -> tuple[list[list[_Line]], int]:
    """The search order cut into chunks of at most ``size`` lines, each closed early when its
    next line would pass ``tokens`` (#53's estimate); and how many single lines over ``tokens``
    were left unsearched."""
    chunks: list[list[_Line]] = []
    current: list[_Line] = []
    used = skipped = 0
    for line in ordered:
        cost = estimate_tokens(line.text)
        if cost > tokens:
            skipped += 1
            continue
        if current and (len(current) >= size or used + cost > tokens):
            chunks.append(current)
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        chunks.append(current)
    return chunks, skipped


async def reach(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    instruction: str,
    settings: RewriteSettings | None = None,
    *,
    after_seq: int | None = None,
) -> Reach:
    """The reach pass (see the module docstring) with ``instruction``, the change statement.
    ``after_seq``: only the lines committed after that event position, and no summary call."""
    settings = settings or RewriteSettings()
    size, cap = settings.lite_reach_chunk_messages, settings.lite_reach_max_calls
    deadline = time.monotonic() + settings.lite_reach_deadline_seconds
    found = Reach()
    budget = cap - 1  # the re-check's call
    flagged: list[tuple[int, int]] = []
    summary = await _segments(db, story_id, size) if after_seq is None else []
    if summary:
        result = await _guarded(
            models,
            instruction,
            [
                LiteReachLine(
                    turn=max(1, int(g.covers_to)),
                    speaker="Summary",
                    text=str(g.text)[:LINE_CHARS_MAX],
                )
                for g in summary
            ],
        )
        found.calls += 1
        budget -= 1
        if result is not None:
            ranges = {int(g.covers_to): (int(g.covers_from), int(g.covers_to)) for g in summary}
            flagged = [ranges[h.turn] for h in result.hits if h.turn in ranges]
    ordered = _ordered(await _lines(db, story_id, limit=size * cap, after_seq=after_seq), flagged)
    chunks, skipped = chunked(ordered, size, settings.lite_reach_chunk_tokens)
    if skipped:
        found.truncated = True
    searched: list[_Line] = []
    succeeded = 0
    hits: dict[tuple[int, str], LiteReachHit] = {}
    width = max(1, settings.lite_reach_parallelism)
    position = 0
    while position < len(chunks):
        if budget <= 0 or time.monotonic() >= deadline:
            found.truncated = True
            break
        wave = chunks[position : position + min(width, budget)]
        position += len(wave)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            async with asyncio.timeout(remaining):
                results = await asyncio.gather(
                    *(
                        _guarded(
                            models,
                            instruction,
                            [
                                LiteReachLine(turn=c.turn, speaker=c.speaker, text=c.text)
                                for c in chunk
                            ],
                        )
                        for chunk in wave
                    )
                )
        except TimeoutError:
            results = [None] * len(wave)
        found.calls += len(wave)
        budget -= len(wave)
        # The wave's answers merge in chunk order, so the earliest chunk's hit is kept.
        for chunk, result in zip(wave, results, strict=True):
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
    if position < len(chunks):
        found.truncated = True
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
                    "quote": hit.quote,
                    "relation": hit.relation,
                    "why": hit.reason,
                }
            )
    return found


# -- conflicts (#700) ------------------------------------------------------------------------------

FIX_REFUSED = "fix_refused"
FIX_REFUSED_WHY = "the proposed fix did not pass the content checks"
PLAYER_AUTHORED = "player_authored"
AFTER_UNDO = "after_undo"
"""#700: a line committed after an undone amendment that still states its change (the undo's
reach): offered a fix, as a conflict whose ``rewrite`` makes it agree with the restored text."""
CONFLICT_REASONS = frozenset(
    {"no_clean_fix", "recheck_failed", FIX_REFUSED, PLAYER_AUTHORED, AFTER_UNDO}
)


async def _resolve(db: AsyncEngine, story_id: str, turn: int, quote: str) -> Any:
    """The active message of ``turn`` a hit quotes (the first of the turn when no line holds
    the quote; ``None`` when the turn has none)."""
    tm = transcript_messages.c
    async with db.connect() as conn:
        found = list(
            await conn.execute(
                select(tm.message_id, tm.text, tm.role)
                .where(
                    tm.story_id == story_id,
                    tm.narrative_turn == turn,
                    tm.membership == "active",
                )
                .order_by(tm.seq)
            )
        )
    wanted = _normal(quote)
    return next((r for r in found if wanted and wanted in _normal(str(r.text))), None) or (
        found[0] if found else None
    )


def _conflict_key(statement: str) -> str:
    import hashlib

    return "lite:" + hashlib.sha256(statement.encode("utf-8")).hexdigest()[:16]


async def head_seq(db: AsyncEngine, story_id: str) -> int:
    """The story's last event position (what a reach has seen, #237's Lite read set)."""
    from sqlalchemy import func

    async with db.connect() as conn:
        found = await conn.scalar(
            select(func.max(events.c.seq)).where(events.c.story_id == story_id)
        )
    return int(found or 0)


async def operation_seq(db: AsyncEngine, operation_id: str) -> int | None:
    """The last event position of an operation (an amendment's: an undo's reach starts after
    it); ``None`` when it wrote none."""
    from sqlalchemy import func

    async with db.connect() as conn:
        found = await conn.scalar(
            select(func.max(events.c.seq)).where(events.c.operation_id == operation_id)
        )
    return None if found is None else int(found)


def undo_hits(reached: Reach) -> list[dict[str, Any]]:
    """#700: an undo's reach hits as conflicts (``after_undo``): every hit that conflicts with
    the restored text, or that a fix would change; a mention with no fix is left alone."""
    hits: list[dict[str, Any]] = []
    for change in reached.changes:
        ref = change.ref
        if ref is not None and ref.turn is not None:
            hits.append(
                {
                    "reason": AFTER_UNDO,
                    "turn": ref.turn,
                    "quote": ref.quote,
                    "relation": "conflicts",
                    "why": "the line still states the undone correction",
                }
            )
    for hit in reached.unfixed:
        if hit.get("relation") == "conflicts" and hit.get("turn") is not None:
            hits.append({**hit, "reason": AFTER_UNDO})
    return hits


async def player_lines(
    db: AsyncEngine, story_id: str, changes: Sequence[RewriteChange]
) -> tuple[list[RewriteChange], list[dict[str, Any]]]:
    """#700: the reach's fixes split by who wrote the line: a fix on a line the player or a
    consumer wrote is no silent change but a conflict (``player_authored``; ``best_effort``
    may still rewrite it, and the report lists it)."""
    kept: list[RewriteChange] = []
    hits: list[dict[str, Any]] = []
    for change in changes:
        ref = change.ref
        if ref is None or ref.turn is None or ref.quote is None:
            kept.append(change)
            continue
        line = await _resolve(db, story_id, ref.turn, ref.quote)
        if line is not None and str(line.role) in PLAYER_ROLES:
            hits.append(
                {
                    "reason": PLAYER_AUTHORED,
                    "turn": ref.turn,
                    "quote": ref.quote,
                    "relation": "conflicts",
                    "why": "the player wrote this line",
                }
            )
        else:
            kept.append(change)
    return kept, hits


async def conflicts(
    db: AsyncEngine, story_id: str, statement: str, hits: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """#700 (FR-REWRITE-3): the Lite hits that become irreducible conflicts on their lines, in
    the planner's entry shape: a ``conflicts`` hit with no clean fix, a fix the re-check or a
    content check refused, and a line the player wrote that needs a fix. Each is ``high`` (every
    Lite line is player-visible), with options ``rewrite|drop|keep``; ``rewrite`` asks
    ``rewrite_record`` for the line with the change statement. A ``mentions`` hit with no fix
    contradicts nothing and stays a listed hit."""
    from epistrel.orchestrator.rewrite.plan import CONFLICT_OPTIONS, EXCERPT_MAX, _conflict_id

    key = _conflict_key(statement)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        reason = str(hit.get("reason") or "")
        if reason not in CONFLICT_REASONS:
            continue
        if reason == "no_clean_fix" and hit.get("relation") != "conflicts":
            continue
        line = await _resolve(db, story_id, int(hit.get("turn") or 0), str(hit.get("quote") or ""))
        if line is None or str(line.message_id) in seen:
            continue
        seen.add(str(line.message_id))
        excerpt = str(line.text)[:EXCERPT_MAX]
        found.append(
            {
                "record": {"table": "events", "id": str(line.message_id)},
                "edge_type": None,
                "edges": [],
                "change_key": key,
                "target": None,
                "action": "fix",
                "conflict_id": _conflict_id(key, "events", str(line.message_id)),
                "reason": reason,
                "severity": "high",
                "options": list(CONFLICT_OPTIONS),
                "statement": statement,
                "why": FIX_REFUSED_WHY if reason == FIX_REFUSED else str(hit.get("why") or ""),
                "excerpt": excerpt,
                "before_excerpt": excerpt,
            }
        )
    return found


CONFLICTING = frozenset({"conflicts"})
"""A reach hit's relation that contradicts the correction (``mentions`` only names it)."""


def _conflicting(hit: LiteReachHit) -> bool:
    return hit.relation in CONFLICTING


def _better(hit: LiteReachHit, kept: LiteReachHit) -> bool:
    """#700's merge of duplicate hits: ``conflicts`` beats ``mentions``, then a fix beats
    none; otherwise the earliest chunk's (``kept``) stays."""
    if _conflicting(hit) != _conflicting(kept):
        return _conflicting(hit)
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
        if not _conflicting(found):
            continue
        for fix in fixes:
            # A hit on a fixed line quotes the fixed text (a quote of the original names a
            # line the re-check never read).
            if fix.turn == found.turn and _normal(found.quote) in _normal(str(fix.fix_text)):
                still.add((fix.turn, fix.quote))
    return still


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


def _speaker_kind(role: str, actor: str | None) -> str:
    """A transcript line's speaker kind from its projected ``role`` (``player``,
    ``narration`` or ``character``, ``core.projections.transcript``)."""
    if role in PLAYER_ROLES:
        return "player"
    if role == "consumer":
        return "consumer"
    return "character" if actor and role != "narration" else "narrator"


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
    "chunked",
    "conflicts",
    "head_seq",
    "line_edit_change",
    "lines",
    "operation_seq",
    "parse",
    "player_lines",
    "reach",
    "statement_of",
    "undo_hits",
    "undo_statement",
]
