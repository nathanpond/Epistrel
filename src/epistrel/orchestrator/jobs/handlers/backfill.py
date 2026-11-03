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

from epistrel.core import author_facts, pause
from epistrel.core import backfill as core_backfill
from epistrel.core.backfill import conflicts as backfill_conflicts
from epistrel.core.backfill import coverage, reconcile
from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.genesis import deferred
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.membership import flip_membership
from epistrel.core.profiles import effective, schema
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
from epistrel.core.verify.normalize import ClaimSketch, normalize
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


_gate: dict[str, float] = {"concurrency": 1, "wait_s": 5.0}


def configure(*, concurrency: int, wait_s: float) -> None:
    """#229: at most ``concurrency`` backfills run at once across the deployment
    (``backfill_concurrency``); a queued one waits ``wait_s`` (``worker_poll_interval``) before
    it tries to claim again."""
    _gate["concurrency"] = concurrency
    _gate["wait_s"] = wait_s


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


async def _places(conn: AsyncConnection, story_id: str) -> dict[str, list[str]]:
    """#228: each place entity's name and aliases (the words a turn names a place by)."""
    found: dict[str, list[str]] = {}
    rows = await conn.execute(
        select(entities.c.id, entities.c.canonical_name).where(
            entities.c.story_id == story_id,
            entities.c.kind == "place",
            entities.c.merged_into.is_(None),
        )
    )
    for row in rows:
        found.setdefault(str(row.id), []).append(str(row.canonical_name))
    aliases = await conn.execute(
        select(entity_aliases.c.entity_id, entity_aliases.c.alias)
        .join(entities, entities.c.id == entity_aliases.c.entity_id)
        .where(entities.c.story_id == story_id, entities.c.kind == "place")
    )
    for row in aliases:
        if str(row.entity_id) in found:
            found[str(row.entity_id)].append(str(row.alias))
    return found


def _place(texts: list[str], places: dict[str, list[str]]) -> str | None:
    """#228: the place the turn's texts name last (where the turn ends), or ``None``."""
    joined = "\n".join(texts)
    best: tuple[int, str] | None = None
    for place, words in places.items():
        for word in words:
            if not word:
                continue
            for hit in re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", joined, re.IGNORECASE):
                if best is None or hit.end() > best[0]:
                    best = (hit.end(), place)
    return None if best is None else best[1]


def _placements(
    turn: reconcile.ReadTurn,
    place: str | None,
    present: list[str],
    *,
    registry: Any,
    context: ResolutionContext,
) -> None:
    """#228: the turn's inferred placements (``ReadTurn.placements``), normalized as genesis
    normalizes a placement (``core.genesis.structured``)."""
    if place is None:
        return
    for character in present:
        found = normalize(
            ClaimSketch(relation_text="located_at", subject_id=character, object_id=place),
            NARRATOR,
            (turn.turn, 0),
            registry=registry,
            context=context,
            basis=clock_off_basis(),
        )
        if isinstance(found, Proposition):
            turn.placements.append(
                reconcile.ReadProposition(proposition=found.model_dump(mode="json"))
            )


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


def _offsets(text: str, words: str | None, near: tuple[int, int] | None) -> tuple[int, int] | None:
    """#228: where ``words`` sit in ``text`` (case-insensitive; inside ``near`` first), or
    ``None``."""
    if not words or not words.strip():
        return None
    pattern = re.compile(re.escape(words.strip()), re.IGNORECASE)
    hits = list(pattern.finditer(text))
    if not hits:
        return None
    if near is not None:
        inside = [h for h in hits if h.start() >= near[0] and h.end() <= near[1]]
        if inside:
            return inside[0].start(), inside[0].end()
    return hits[0].start(), hits[0].end()


def _reference(
    text: str,
    *,
    relation: str,
    subject: str | None,
    object_: str | None,
    time: str | None,
    span: tuple[int, int] | None,
    negated: bool,
    source: str,
    registry: Any,
) -> reconcile.ReadReference | None:
    """#228: a salient reference as the checkpoint keeps it (``reconcile.ReadReference``): the
    resolved predicate and its words' offsets; ``None`` when the predicate is not registered or
    a word the sketch names is not in the message."""
    from epistrel.core.verify.normalize import predicate_for

    predicate = predicate_for(registry, relation)
    if predicate is None:
        return None
    found: dict[str, tuple[int, int] | None] = {}
    for name, words in (("subject", subject), ("object", object_), ("time", time)):
        at = _offsets(text, words, span)
        if words and words.strip() and at is None:
            return None
        found[name] = at
    return reconcile.ReadReference(
        predicate_id=predicate.id,
        span=span,
        subject=found["subject"],
        object=found["object"],
        time=found["time"],
        negated=negated,
        source=source,
    )


def _opens(sketch: ClaimSketch) -> bool:
    """The rules mode's test (``orchestrator.commit.salience.rules_salient``): the claim leaves
    an argument open."""
    from epistrel.core.commit.sketch import open_phrase

    return (sketch.subject_id is None and open_phrase(sketch.subject_text)) or (
        sketch.object_id is None and sketch.literal is None and open_phrase(sketch.object_text)
    )


async def _salient_references(
    models: ModelAccessLayer | None,
    messages: list[reconcile.ReadMessage],
    *,
    settings: ProfileSettings,
    present: list[str],
    names: dict[str, list[str]],
    registry: Any,
    budget: TurnBudget,
) -> None:
    """#228 (#146): under ``rules_and_model`` salience, the turn's reply read by the
    ``salience_detection`` purpose (the live entry point, ``detect_salience``), each salient
    span kept as a reference on its message (in place). The player's input is never read."""
    from epistrel.orchestrator.commit import salience
    from epistrel.orchestrator.models.registry import SceneContext

    if salience.salience_mode(settings) != "rules_and_model":
        return
    read = [(n, m) for n, m in enumerate(messages) if m.kind in salience.RULE_KINDS]
    if not read:
        return
    beats = [salience.Beat(index=n, kind=m.kind, text=m.text) for n, m in read]
    scene = SceneContext(
        present_entities=[names[c][0] for c in present if names.get(c)], last_beats=[]
    )
    verdict = await salience.detect_salience(
        beats,
        scene,
        mode="rules_and_model",
        models=models,
        budget=budget,
        bias=salience.salience_bias(settings),
    )
    for item in verdict.salient:
        if not 0 <= item.span.beat < len(messages):
            continue
        message = messages[item.span.beat]
        sketch = item.proposition_sketch
        found = _reference(
            message.text,
            relation=sketch.relation,
            subject=sketch.subject,
            object_=sketch.object,
            time=sketch.time,
            span=(item.span.start, item.span.end),
            negated=False,
            source="model",
            registry=registry,
        )
        if found is not None:
            message.references.append(found)


async def _extract_turn(
    models: ModelAccessLayer | None,
    messages: list[reconcile.ReadMessage],
    *,
    turn: int,
    registry: Any,
    context: ResolutionContext,
    budget: TurnBudget,
    references: bool = False,
) -> None:
    """The turn's claims, normalized onto ``messages`` (in place); with ``references`` (#228:
    commitment gained), a narration claim that leaves an argument open is kept as a salient
    reference (the rules mode's)."""
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
        full = extraction_sketch(claim, span)
        sketch = replace(full, time_text=None)
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
                    proposition=result.model_dump(mode="json"),
                    changed=bool(claim.time),
                    # #709: placed at reconciliation, against the reconciled clock.
                    time_text=full.time_text,
                )
            )
        elif references and message.kind in reconcile.REFERENCE_KINDS and _opens(full):
            found = _reference(
                message.text,
                relation=full.relation_text,
                subject=full.subject_text,
                object_=full.object_text,
                time=full.time_text,
                span=span,
                negated=claim.polarity == "negated",
                source="rules",
                registry=registry,
            )
            if found is not None:
                message.references.append(found)


async def _extract(ctx: JobContext, row: Any, chunk: Any, settings: ProfileSettings) -> None:
    story_id = str(row.story_id)
    async with ctx.db.connect() as conn:
        found = await _turns(conn, story_id, int(chunk.from_turn), int(chunk.to_turn))
        names = await _names(conn, story_id)
        places = await _places(conn, story_id)
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
        if set(row.capabilities) & reconcile.PLACEMENT_CAPABILITIES:
            _placements(
                turn,
                _place([m.text for m in turn.messages], places),
                present,
                registry=registry,
                context=context,
            )
        committing = bool(set(row.capabilities) & reconcile.COMMITMENT_CAPABILITIES)
        if wanted or committing:
            await _extract_turn(
                bound(),
                turn.messages,
                turn=turn.turn,
                registry=registry,
                context=context,
                budget=budget,
                references=committing,
            )
        if committing:
            await _salient_references(
                bound(),
                turn.messages,
                settings=settings,
                present=present,
                names=names,
                registry=registry,
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
        found = (
            await conn.execute(
                select(stories.c.timeline_revision, stories.c.settings).where(
                    stories.c.id == story_id
                )
            )
        ).one()
        revision = found.timeline_revision
        # #230: the conflict policy is read when the backfill starts; a change while it runs
        # affects the next one.
        policy = dict(row.report or {}).get("policy") or _policy(found.settings)
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
        # #231: the deferred genesis content the target enables, at position 0 under the
        # backfill's operation (dormant until completion): reconciliation meets the seeds as
        # its earliest statements.
        materialization: dict[str, Any] | None = None
        settings = await _target_settings(conn, row)
        if await deferred.pending(conn, story_id, settings, deferred.BACKFILL_START):
            materialization = await deferred.materialize_deferred(
                conn,
                story_id,
                settings,
                operation_id,
                principal=str(row.requested_by),
                only=deferred.BACKFILL_START,
            )
            await deferred.record_report(conn, operation_id, materialization)  # #698
            await flip_membership(conn, [operation_id], Membership.DORMANT, story_id=story_id)
        report: dict[str, Any] = {
            **dict(row.report or {}),
            "timeline_revision": int(revision or 0),
            "policy": policy,
        }
        if materialization is not None:
            report["materialization"] = materialization
        await conn.execute(
            update(story_backfills)
            .where(story_backfills.c.id == row.id)
            .values(
                status="extracting",
                operation_id=operation_id,
                started_at=datetime.now(UTC),
                report=report,
            )
        )
    return None


def _policy(settings: Any) -> str:
    """The story's ``backfill_conflict_policy`` (a story created before #230 may hold it under
    ``settings.inactive``; without it, ``last_mention``)."""
    found = dict(settings or {})
    value = found.get("backfill_conflict_policy")
    if value is None:
        inactive = dict(found.get("inactive") or {}).get("backfill_conflict_policy")
        value = inactive.get("value") if isinstance(inactive, dict) else None
    return str(value) if value in reconcile.POLICIES else reconcile.LAST_MENTION


async def complete_now(conn: AsyncConnection, row: Any) -> None:
    """#230: the completion transaction a resolve or resume runs in its request."""
    from epistrel.core.profiles.story import apply_held

    await core_backfill.complete(conn, row, apply_target=apply_held, after_complete=_after_complete)


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
            await deferred.undo(conn, str(row.story_id), str(row.operation_id))  # #231
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


async def _paced_turns(conn: AsyncConnection, story_id: str, op: str, turns: list[int]) -> set[int]:
    """#709 (owner decision 2): the turns among ``turns`` whose clock advances are already
    recorded (a Lite era at ``prompt_only``): they keep them, and nothing more is derived."""
    from epistrel.core.tables import state_changes, state_definitions

    c, d = state_changes.c, state_definitions.c
    clock_id = select(d.id).where(d.story_id == story_id, d.name == "world_clock")
    found = await conn.execute(
        select(c.narrative_turn).where(
            c.story_id == story_id,
            c.state_id.in_(clock_id.scalar_subquery()),
            c.narrative_turn.in_(turns),
            c.operation_id != op,
            c.membership == Membership.ACTIVE.value,
        )
    )
    return {int(r.narrative_turn) for r in found}


async def _clock_steps(
    ctx: JobContext,
    row: Any,
    turns: list[reconcile.ReadTurn],
    settings: ProfileSettings,
) -> None:
    """#709 (FR-PROF-9, FR-CLOCK-6,7): each turn's clock advance when the backfill gains the
    clock, planned before the reconciliation's transaction and in narrative order: #194's pacing
    over the Lite turn's combined render (#203), or a narrated skip in its place (its stated
    minutes, else one ``time_estimation`` call given the reconciled clock's time of day, #201),
    authorized as #199 authorizes one (a refused skip is reported, the turn paces). A turn that
    already holds recorded advances keeps them (``_paced_turns``)."""
    from epistrel.core.enums import ClockMode
    from epistrel.core.world_clock import skips
    from epistrel.core.world_clock.pacing import pacing_increment
    from epistrel.core.world_clock.timing import minute_of_day
    from epistrel.orchestrator.structured import clocking
    from epistrel.orchestrator.structured import time_estimation as estimation

    if "clock" not in set(row.capabilities) or not turns:
        return
    story_id, op = str(row.story_id), str(row.operation_id)
    async with ctx.db.connect() as conn:
        tx = await conn.begin()
        try:
            # The clock as the reconciliation left it: the operation's dormant rows read as
            # active, then rolled back.
            with effective.deriving(story_id, settings), pause.holding(str(row.id)):
                await flip_membership(conn, [op], Membership.ACTIVE, story_id=story_id)
                clock = await clocking.read_clock(conn, story_id)
                kept = await _paced_turns(conn, story_id, op, [t.turn for t in turns])
        finally:
            await tx.rollback()
        ids = [m.event_id for t in turns for m in t.messages]
        texts = {
            str(r.message_id): str(r.text)
            for r in await conn.execute(
                select(transcript_messages.c.message_id, transcript_messages.c.text).where(
                    transcript_messages.c.story_id == story_id,
                    transcript_messages.c.message_id.in_(ids),
                )
            )
        }
    if clock is None or clock.external or clock.mode is not ClockMode.PACED:
        return
    running = int(clock.start)
    window: list[str] = []
    models = bound()
    for turn in sorted(turns, key=lambda t: t.turn):
        say = " ".join(texts.get(m.event_id, "") for m in turn.messages if m.kind == "say")
        reply = " ".join(texts.get(m.event_id, "") for m in turn.messages if m.kind != "say")
        if turn.turn in kept:
            window += [say, reply]
            continue
        lines = [clocking._Line("say", say), clocking._Line("narration", reply)]
        step = reconcile.ClockStep(
            minutes=pacing_increment(lines, clock.table, mode=clock.mode), derived="pacing"
        )
        if skips.narrates_elapsing(reply):
            minutes = skips.stated_minutes(reply)
            derived, meta = "declared_skip", None
            if minutes is None and models is not None:
                mine = skips.activities(reply)
                found = await estimation.estimate(
                    models,
                    narrated_text=reply,
                    activity_context=" ".join(t for t in window if skips.activities(t) & mine),
                    clock_minute=minute_of_day(clock.anchor, running),
                    day_minutes=clock.clockwork.day_minutes,
                    budget=TurnBudget(),
                )
                if found is not None:
                    minutes, derived = found.minutes, "estimate"
                    meta = {"confidence": getattr(found, "confidence", None)}
            if minutes is not None and minutes > 0:
                classified = skips.classify_skip(skips.Skip(text=reply), say, ())
                verdict = skips.authorize(
                    classified,
                    minutes=minutes,
                    cap=clock.max_unprompted,
                    established=skips.established(window),
                )
                if isinstance(verdict, skips.Refused):
                    step = step.model_copy(update={"refused": verdict.reason})
                else:
                    step = reconcile.ClockStep(
                        minutes=minutes,
                        derived=derived,
                        reason=f"backfill: a narrated skip ({derived})",
                        meta=meta,
                    )
        turn.clock = step
        running += step.minutes
        window += [say, reply]


async def _reconcile(ctx: JobContext, row: Any, chunk: Any) -> None:
    story_id = str(row.story_id)
    extracted = reconcile.Extracted.model_validate(chunk.extraction or {})
    async with ctx.db.connect() as conn:
        settings = await _target_settings(conn, row)
    await _clock_steps(ctx, row, extracted.turns, settings)
    async with ctx.db.begin() as conn:
        registry = await load_registry(conn, story_id)
        op = str(row.operation_id)
        # #709: the live writers read the target configuration (the clock's gate among them).
        with pause.holding(str(row.id)), effective.deriving(story_id, settings):
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


AUTHOR_FACT_PARSES = "author_fact_parses"
"""#699: the report key holding each Lite lasting author fact's ``directive_parse`` changes
(``None``: kept as text), parsed after extraction (model calls outside any transaction)."""
NOTE_KEY = author_facts.NOTE_KEY
"""#700: a Lite world note's key among the parsed author facts (``note:<part id>``)."""
NOTE_PREFIX = "Author correction: "
"""#700: the prefix a Lite world note's text carries, dropped before it is parsed."""
_converted_before = author_facts.converted_before
"""#699: the Lite fact keys earlier completed backfills converted (``core.author_facts``)."""


async def _parse_author_facts(ctx: JobContext, row: Any) -> dict[str, Any]:
    """#699 (FR-LITE-8): on an upgrade, each active Lite lasting author fact's text parsed by
    ``directive_parse`` with ``durable`` forced and present scope (#226), with the target
    configuration's models; ``None`` for one that yields no structured change (it stays a
    lasting author fact, as text)."""
    from epistrel.orchestrator import directives
    from epistrel.orchestrator.rewrite import parse as parsing

    story_id = str(row.story_id)
    if str(row.kind) != "upgrade":
        return {}
    async with ctx.db.connect() as conn:
        facts = await author_facts.lite_facts(conn, story_id)
        # #700: each active Lite world note ("Author correction: ...") converts the same way,
        # at the story's start, keyed by its part.
        facts += [
            (f"{NOTE_KEY}{part_id}", 0, 0, text.removeprefix(NOTE_PREFIX))
            for part_id, text in await author_facts.lite_note_parts(conn, story_id)
        ]
        done = await _converted_before(conn, story_id)
        if not facts:
            return {}
        story = await parsing.load_story(
            conn, story_id, await load_registry(conn, story_id), margin=0.15
        )
    models = _models[0]
    parsed: dict[str, Any] = {}
    for event_id, _turn, _slot, text in facts:
        if event_id in done:
            continue
        found = None
        if models is not None:
            try:
                answer = await directives._parse(models, story, text)
            except Exception:  # an unparseable fact stays text; it never fails the backfill
                answer = None
            if answer is not None:
                changes = [
                    c.model_dump(mode="json", by_alias=True)
                    for c in answer.changes
                    if c.kind == "proposition"
                ]
                found = changes or None
        parsed[event_id] = found
    return parsed


async def _convert_author_facts(conn: AsyncConnection, row: Any) -> dict[str, Any] | None:
    """#699: the parsed Lite facts written as author facts at their slots, under the
    backfill's operation (dormant until completion, so a cancel discards them): #226's
    durable-now fact writer (``provenance = author``, ``authority = author``). Answers the
    report's ``author_facts {converted, kept_as_text, ids}`` (``None``: nothing to convert)."""
    from types import SimpleNamespace

    from epistrel.core.canon import facts as canon
    from epistrel.core.enums import FactAuthority, RecordProvenance
    from epistrel.orchestrator import directives
    from epistrel.orchestrator.models.registry import DirectiveChange
    from epistrel.orchestrator.rewrite import parse as parsing

    parsed = dict(dict(row.report or {}).get(AUTHOR_FACT_PARSES) or {})
    if not parsed:
        return None
    story_id = str(row.story_id)
    story = await parsing.load_story(
        conn, story_id, await load_registry(conn, story_id), margin=0.15
    )
    positions = {
        event_id: (turn, slot)
        for event_id, turn, slot, _ in await author_facts.lite_facts(conn, story_id)
    }
    positions.update(
        (f"{NOTE_KEY}{part_id}", (0, 0))
        for part_id, _ in await author_facts.lite_note_parts(conn, story_id)
    )
    converted: list[str] = []
    kept = 0
    for event_id, changes in parsed.items():
        if event_id not in positions:
            continue  # rewound since the parse
        if not changes:
            kept += 1
            continue
        normalized = await directives._normalize(
            conn, story, [DirectiveChange.model_validate(c) for c in changes]
        )
        if not normalized.facts or normalized.ambiguous:
            kept += 1
            continue
        turn, slot = positions[event_id]
        op = SimpleNamespace(
            operation_id=str(row.operation_id),
            story_id=story_id,
            principal=str(row.requested_by),
            narrative_turn=turn,
            turn_variant=0,
            slot_k=slot,
            base_revision=None,
        )
        for proposition in normalized.facts:
            await canon.commit_fact(
                conn,
                story_id,
                proposition,
                provenance=RecordProvenance.AUTHOR,
                authority=FactAuthority.AUTHOR,
                operation=op,
                valid_time=proposition.valid_time,
            )
        converted.append(event_id)
    return {"converted": len(converted), "kept_as_text": kept, "ids": converted}


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
        if await backfill_conflicts.open_count(conn, str(locked.id)):
            # #230 (``ask``): the pass is done; the story stays paused for the author.
            await conn.execute(
                update(story_backfills)
                .where(story_backfills.c.id == locked.id)
                .values(status=backfill_conflicts.AWAITING)
            )
            log.info("backfill awaits its conflicts", backfill_id=str(locked.id))
            return
        story_id = str(locked.story_id)
        if locked.operation_id is not None:
            # #699: the converted author facts, under the operation, before it turns active.
            op = str(locked.operation_id)
            with pause.holding(str(locked.id)):
                await flip_membership(conn, [op], Membership.ACTIVE, story_id=story_id)
                summary = await _convert_author_facts(conn, locked)
                await flip_membership(conn, [op], Membership.DORMANT, story_id=story_id)
            if summary is not None:
                await conn.execute(
                    update(story_backfills)
                    .where(story_backfills.c.id == locked.id)
                    .values(report={**dict(locked.report or {}), "author_facts": summary})
                )
        await core_backfill.complete(
            conn, locked, apply_target=apply_held, after_complete=_after_complete
        )


async def _replan(conn: AsyncConnection, row: Any) -> tuple[list[str], tuple[int, int] | None]:
    """#229: a queued backfill's capabilities and range at claim: what the target (its latest
    version) adds over the configuration the story runs on now, over the turns that lack it."""
    story_id = str(row.story_id)
    current = await effective.effective_configuration(conn, story_id, validate=False)
    target = await _target_settings(conn, row)
    levels = coverage.capabilities_of(target)
    gained = {
        m: levels.get(m) for m in schema.data_increases(current.settings, target) if m in levels
    }
    missing = await coverage.gaps_of(conn, story_id, gained)
    return sorted(missing), core_backfill.span_of(missing)


async def _claim(ctx: JobContext, row: Any) -> tuple[dict[str, str], float | None] | None:
    """Claim a queued profile-edit backfill (``core.backfill.claim``): ``None`` once claimed
    (the run goes on), else the step's result and when to try again."""
    from epistrel.core.profiles.story import apply_held

    async with ctx.db.begin() as conn:
        outcome = await core_backfill.claim(
            conn,
            str(row.id),
            concurrency=int(_gate["concurrency"]),
            replan=_replan,
            apply_target=apply_held,
        )
    if outcome == "claimed":
        return None
    if outcome == "wait":
        return {"step": "wait"}, float(_gate["wait_s"])
    return {"step": "ended"}, None


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
        if row.status == "queued" and str(row.origin) == core_backfill.PROFILE_EDIT:
            claimed = await _claim(ctx, row)
            if claimed is not None:
                return JobOutput(result={**result, **claimed[0]}, requeue_in=claimed[1])
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
            parsed = await _parse_author_facts(ctx, row)
            async with ctx.db.begin() as conn:
                await conn.execute(
                    update(story_backfills)
                    .where(story_backfills.c.id == row.id, story_backfills.c.status == "extracting")
                    .values(
                        status="reconciling",
                        report={**dict(row.report or {}), AUTHOR_FACT_PARSES: parsed},
                    )
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

__all__ = [
    "BACKFILL_HANDLER",
    "BackfillPayload",
    "bind",
    "bound",
    "complete_now",
    "configure",
    "start_job",
]
