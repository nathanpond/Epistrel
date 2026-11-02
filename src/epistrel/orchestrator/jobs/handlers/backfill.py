"""``backfill``: one story's backfill, a step per run (FR-PROF-9; #228; ``core.backfill``).

Each run does one step, commits it with its checkpoint, and requeues itself at once
(``JobOutput.requeue_in``, #117), so a crash costs the step in progress and nothing before it:

1. **start** (``queued``): wait for a turn in flight to commit; open the ``backfill`` operation
   (committed, on the story's active timeline) that every written row names; ``extracting``.
2. **extract** a chunk (``extracting``): read its turns' active transcript (each message, its
   speaker and the characters present, by name or alias in the text), ask the
   ``extraction_backstop`` purpose for each turn's claims with the held target configuration's
   models, normalize them against the story's entities and registry (#140), and store the
   result as the chunk's checkpoint (``backfill_chunks.extraction``). A chunk already extracted
   is never extracted again. After the last chunk: ``reconciling``.
3. **reconcile** the next chunk's turns in narrative order (``core.backfill.reconcile``) in one
   transaction: the operation's earlier rows are made active for the span, the turns written,
   and every row of the operation made ``dormant`` again with the cursor moved
   (``reconcile_cursor``), so no read sees a partial backfill.
4. **complete** after the last turn (``core.backfill.complete``): rows active, coverage
   recorded, the held configuration applied, the story resumed, consolidation, importance
   scoring and embedding enqueued.

The job declares the story's timeline revision when it starts; a truncation or an erasure since
(only operator and erasure paths can move a paused story) discards the operation's rows and
restarts from extraction. A cancelled or failed backfill stops the job; a step that keeps failing
ends the backfill ``failed`` (discarded, the story resumed on its previous configuration). No
generation, regeneration or repair purpose is called; the run's model calls are counted under
the job, never a turn's budget. The result holds ids and counts only (invariant 3).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import backfill as core_backfill
from epistrel.core import pause
from epistrel.core.backfill import reconcile
from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.membership import flip_membership
from epistrel.core.profiles import effective
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.resolution.context import load_index
from epistrel.core.resolution.entities import NARRATOR, ResolutionContext
from epistrel.core.tables import (
    backfill_chunks,
    characters,
    entities,
    entity_aliases,
    operations,
    stories,
    story_backfills,
    transcript_messages,
)
from epistrel.core.verify.normalize import normalize
from epistrel.core.world_clock import clock_off_basis
from epistrel.observability.logging import get_logger
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.registry import ExtractionBeat, ExtractionInputs
from epistrel.orchestrator.verify.extraction import call_extraction, extraction_sketch, locate_quote

log = get_logger(__name__)

MAX_ATTEMPTS = 5
WAIT_S = 1.0
_models: list[ModelAccessLayer | None] = [None]


def bind(models: ModelAccessLayer | None) -> None:
    """The Model Access Layer extraction calls go through (the Engine binds it at start-up)."""
    _models[0] = models


def bound() -> ModelAccessLayer | None:
    return _models[0]


class BackfillPayload(BaseModel):
    """The backfill a ``backfill`` job runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backfill_id: str = Field(pattern=ULID_PATTERN)


async def start_job(conn: AsyncConnection, backfill_id: str, story_id: str) -> str:
    """Enqueue ``backfill_id``'s job in ``conn``'s transaction (``core.backfill.StartJob``),
    as a job of its story (an erasure block holds it, #125)."""
    return await queue.enqueue(
        conn,
        core_backfill.BACKFILL_KIND,
        BackfillPayload(backfill_id=backfill_id),
        story_id=story_id,
    )


# -- reading the transcript -----------------------------------------------------------------------


async def _names(conn: AsyncConnection, story_id: str) -> dict[str, list[str]]:
    """Each character's name and aliases (the deterministic presence detector's words)."""
    found: dict[str, list[str]] = {}
    for row in await conn.execute(
        select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
    ):
        found.setdefault(str(row.id), []).append(str(row.name))
    rows = await conn.execute(
        select(entity_aliases.c.entity_id, entity_aliases.c.alias)
        .join(entities, entities.c.id == entity_aliases.c.entity_id)
        .where(entities.c.story_id == story_id)
    )
    for row in rows:
        if str(row.entity_id) in found:
            found[str(row.entity_id)].append(str(row.alias))
    return found


def _present(texts: list[str], names: dict[str, list[str]], speakers: set[str]) -> list[str]:
    """The characters named in the turn's texts, and its speakers."""
    joined = "\n".join(texts)
    found = set(speakers)
    for character, words in names.items():
        for word in words:
            if word and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", joined, re.IGNORECASE):
                found.add(character)
                break
    return sorted(found)


async def _turns(conn: AsyncConnection, story_id: str, low: int, high: int) -> list[Any]:
    t = transcript_messages.c
    return list(
        await conn.execute(
            select(
                t.message_id,
                t.narrative_turn,
                t.turn_variant,
                t.kind,
                t.role,
                t.actor_character_id,
                t.text,
            )
            .where(
                t.story_id == story_id,
                t.membership == Membership.ACTIVE.value,
                t.narrative_turn.between(low, high),
            )
            .order_by(t.narrative_turn, t.seq)
        )
    )


def _kind(row: Any) -> tuple[str, str | None]:
    """A message's beat kind and speaker: the player's input is a ``say`` of its actor, every
    other message narration (a Lite reply narrates the whole scene)."""
    if row.role == "player" and row.actor_character_id is not None:
        return "say", str(row.actor_character_id)
    return "narration", None


async def _extract_turn(
    models: ModelAccessLayer | None,
    messages: list[reconcile.ReadMessage],
    *,
    turn: int,
    registry: Any,
    context: ResolutionContext,
    budget: TurnBudget,
) -> None:
    """The turn's claims, normalized onto ``messages`` (in place)."""
    if models is None or not messages:
        return
    beats = [
        ExtractionBeat(index=n, kind=cast(Any, m.kind), text=m.text[:8_000])
        for n, m in enumerate(messages)
    ]
    answer = await call_extraction(models, ExtractionInputs(beats=beats), budget=budget)
    if answer is None:
        return
    for claim in answer.claims:
        if claim.beat >= len(messages):
            continue
        message = messages[claim.beat]
        span = locate_quote(message.text, claim.span)
        if span is None:
            continue
        # A narrated change ("turned thirty-five on his birthday") is read as one (``changed``);
        # its time expression is the clock's to place (#199), never a reason to drop the claim.
        sketch = replace(extraction_sketch(claim, span), time_text=None)
        result = normalize(
            sketch,
            message.speaker or NARRATOR,
            (turn, claim.beat + 1),
            registry=registry,
            context=context,
            basis=clock_off_basis(),
        )
        if isinstance(result, Proposition):
            message.propositions.append(
                reconcile.ReadProposition(
                    proposition=result.model_dump(mode="json"), changed=bool(claim.time)
                )
            )


async def _extract(ctx: JobContext, row: Any, chunk: Any, settings: ProfileSettings) -> None:
    story_id = str(row.story_id)
    async with ctx.db.connect() as conn:
        found = await _turns(conn, story_id, int(chunk.from_turn), int(chunk.to_turn))
        names = await _names(conn, story_id)
        registry = await load_registry(conn, story_id)
        index = await load_index(conn, story_id)
    context = ResolutionContext(index=index, beats=())
    wanted = bool(set(row.capabilities) & reconcile.FACT_CAPABILITIES)
    budget = TurnBudget()
    turns: dict[int, reconcile.ReadTurn] = {}
    for message in found:
        turn = turns.setdefault(
            int(message.narrative_turn),
            reconcile.ReadTurn(
                turn=int(message.narrative_turn), variant=int(message.turn_variant or 0)
            ),
        )
        kind, speaker = _kind(message)
        turn.messages.append(
            reconcile.ReadMessage(
                event_id=str(message.message_id), kind=kind, speaker=speaker, text=message.text
            )
        )
    for turn in turns.values():
        present = _present(
            [m.text for m in turn.messages], names, {m.speaker for m in turn.messages if m.speaker}
        )
        for message in turn.messages:
            message.present = present
        if wanted:
            await _extract_turn(
                bound(),
                turn.messages,
                turn=turn.turn,
                registry=registry,
                context=context,
                budget=budget,
            )
    for turn in turns.values():
        for message in turn.messages:
            message.text = ""  # the checkpoint keeps ids and propositions, never the text
    extracted = reconcile.Extracted(turns=[turns[t] for t in sorted(turns)])
    async with ctx.db.begin() as conn:
        current = await conn.scalar(
            select(backfill_chunks.c.status).where(
                backfill_chunks.c.backfill_id == row.id,
                backfill_chunks.c.chunk_index == chunk.chunk_index,
            )
        )
        if current != "pending":
            return
        await conn.execute(
            update(backfill_chunks)
            .where(
                backfill_chunks.c.backfill_id == row.id,
                backfill_chunks.c.chunk_index == chunk.chunk_index,
            )
            .values(
                status="extracted",
                extraction=extracted.model_dump(mode="json"),
                extracted_at=datetime.now(UTC),
            )
        )
        await conn.execute(
            update(story_backfills)
            .where(story_backfills.c.id == row.id)
            .values(chunks_done=story_backfills.c.chunks_done + 1)
        )


# -- the steps ---------------------------------------------------------------------------------


async def _target_settings(conn: AsyncConnection, row: Any) -> ProfileSettings:
    """The configuration the backfill derives for: the held target's (an upgrade), else the
    story's own (a gap fill)."""
    current = await effective.effective_configuration(conn, str(row.story_id), validate=False)
    target = dict(row.target or {})
    document = current.document
    if target.get("profile_id"):
        held = await effective.assignable(
            conn, str(target["profile_id"]), version=target.get("pin")
        )
        document = held.document
    overrides = target.get("overrides", current.overrides)
    return effective.merged_settings(current.defaults, document, overrides)


async def _in_flight(conn: AsyncConnection, story_id: str) -> bool:
    o = operations.c
    found = await conn.scalar(
        select(func.count())
        .select_from(operations)
        .where(o.story_id == story_id, o.status == OperationStatus.IN_FLIGHT.value)
    )
    return bool(found)


async def _begin(ctx: JobContext, row: Any) -> JobOutput | None:
    story_id = str(row.story_id)
    async with ctx.db.begin() as conn:
        if await _in_flight(conn, story_id):
            return JobOutput(
                result={"backfill_id": str(row.id), "waiting": "turn"}, requeue_in=WAIT_S
            )
        revision = await conn.scalar(
            select(stories.c.timeline_revision).where(stories.c.id == story_id)
        )
        operation_id = new_ulid()
        now = func.transaction_timestamp()
        await conn.execute(
            insert(operations).values(
                operation_id=operation_id,
                story_id=story_id,
                kind=OperationKind.BACKFILL.value,
                principal=str(row.requested_by),
                status=OperationStatus.COMMITTED.value,
                membership_tx=now,
                committed_at=now,
            )
        )
        await conn.execute(
            update(story_backfills)
            .where(story_backfills.c.id == row.id)
            .values(
                status="extracting",
                operation_id=operation_id,
                started_at=datetime.now(UTC),
                report={**dict(row.report or {}), "timeline_revision": int(revision or 0)},
            )
        )
    return None


async def _restart_if_moved(ctx: JobContext, row: Any) -> bool:
    """When the story's timeline moved since the run began, discard and restart (see the module
    docstring); whether it did."""
    async with ctx.db.begin() as conn:
        revision = await conn.scalar(
            select(stories.c.timeline_revision).where(stories.c.id == row.story_id)
        )
        started = (row.report or {}).get("timeline_revision")
        if started is None or revision == started:
            return False
        if row.operation_id is not None:
            await flip_membership(
                conn, [str(row.operation_id)], Membership.DISCARDED, story_id=str(row.story_id)
            )
        await conn.execute(
            update(backfill_chunks)
            .where(backfill_chunks.c.backfill_id == row.id)
            .values(status="pending", extraction=None, extracted_at=None)
        )
        await conn.execute(
            update(story_backfills)
            .where(story_backfills.c.id == row.id)
            .values(
                status="queued",
                chunks_done=0,
                reconcile_cursor=None,
                operation_id=None,
                report={
                    k: v for k, v in dict(row.report or {}).items() if k != "timeline_revision"
                },
            )
        )
    log.info("backfill restarted: the timeline moved", backfill_id=str(row.id))
    return True


async def _reconcile(ctx: JobContext, row: Any, chunk: Any) -> None:
    story_id = str(row.story_id)
    extracted = reconcile.Extracted.model_validate(chunk.extraction or {})
    async with ctx.db.begin() as conn:
        settings = await _target_settings(conn, row)
        registry = await load_registry(conn, story_id)
        op = str(row.operation_id)
        with pause.holding(str(row.id)):
            await flip_membership(conn, [op], Membership.ACTIVE, story_id=story_id)
            tally = await reconcile.reconcile_turns(
                conn,
                row,
                extracted.turns,
                capabilities=list(row.capabilities),
                registry=registry,
                settings=settings,
            )
            await flip_membership(conn, [op], Membership.DORMANT, story_id=story_id)
        await conn.execute(
            update(story_backfills)
            .where(story_backfills.c.id == row.id)
            .values(
                reconcile_cursor=int(chunk.to_turn),
                report=reconcile.merge_report(row.report or {}, tally),
            )
        )


async def _after_complete(conn: AsyncConnection, story_id: str) -> None:
    """Consolidation, importance scoring and embedding of the newly active memories."""
    from epistrel.orchestrator.jobs.handlers import consolidation, importance, reembed

    await consolidation.schedule_in(conn, story_id)
    await importance.schedule_in(conn, story_id)
    await reembed.schedule(conn, reembed.EMBED, story_id)


async def _complete(ctx: JobContext, row: Any) -> None:
    from epistrel.core.profiles.story import apply_held

    async with ctx.db.begin() as conn:
        locked = (
            await conn.execute(
                select(story_backfills).where(story_backfills.c.id == row.id).with_for_update()
            )
        ).one_or_none()
        if locked is None or locked.status != "reconciling":
            return
        await core_backfill.complete(
            conn, locked, apply_target=apply_held, after_complete=_after_complete
        )


async def _run(ctx: JobContext) -> JobOutput:
    payload = cast(BackfillPayload, ctx.payload)
    result: dict[str, Any] = {"backfill_id": payload.backfill_id}
    async with ctx.db.connect() as conn:
        row = (
            await conn.execute(
                select(story_backfills).where(story_backfills.c.id == payload.backfill_id)
            )
        ).one_or_none()
    if row is None or row.status in core_backfill.TERMINAL:
        return JobOutput(result={**result, "skipped": "ended"})
    try:
        if row.status == "queued":
            waiting = await _begin(ctx, row)
            return waiting or JobOutput(result={**result, "step": "begin"}, requeue_in=0)
        if await _restart_if_moved(ctx, row):
            return JobOutput(result={**result, "step": "restart"}, requeue_in=0)
        async with ctx.db.connect() as conn:
            chunks = list(
                await conn.execute(
                    select(backfill_chunks)
                    .where(backfill_chunks.c.backfill_id == row.id)
                    .order_by(backfill_chunks.c.chunk_index)
                )
            )
            settings = await _target_settings(conn, row)
        if row.status == "extracting":
            pending = [c for c in chunks if c.status == "pending"]
            if pending:
                await _extract(ctx, row, pending[0], settings)
                return JobOutput(result={**result, "step": "extract"}, requeue_in=0)
            async with ctx.db.begin() as conn:
                await conn.execute(
                    update(story_backfills)
                    .where(story_backfills.c.id == row.id, story_backfills.c.status == "extracting")
                    .values(status="reconciling")
                )
            return JobOutput(result={**result, "step": "extracted"}, requeue_in=0)
        if row.status == "reconciling":
            cursor = row.reconcile_cursor
            remaining = [c for c in chunks if cursor is None or int(c.from_turn) > int(cursor)]
            if remaining:
                await _reconcile(ctx, row, remaining[0])
                return JobOutput(result={**result, "step": "reconcile"}, requeue_in=0)
            await _complete(ctx, row)
            return JobOutput(result={**result, "step": "complete"})
        return JobOutput(result={**result, "skipped": str(row.status)})
    except Exception as exc:
        attempt = int(ctx.job.attempt or 1)
        if attempt >= MAX_ATTEMPTS:
            async with ctx.db.begin() as conn:
                locked = (
                    await conn.execute(
                        select(story_backfills)
                        .where(story_backfills.c.id == payload.backfill_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if locked is not None and locked.status not in core_backfill.TERMINAL:
                    await core_backfill.discard(
                        conn, locked, status="failed", error=type(exc).__name__
                    )
            log.warning(
                "backfill failed", backfill_id=payload.backfill_id, error=type(exc).__name__
            )
            return JobOutput(result={**result, "status": "failed"})
        raise


BACKFILL_HANDLER = JobHandler(
    kind=core_backfill.BACKFILL_KIND,
    payload=BackfillPayload,
    run=_run,
    max_attempts=MAX_ATTEMPTS,
    records_operation=False,
)

__all__ = ["BACKFILL_HANDLER", "BackfillPayload", "bind", "bound", "start_job"]
