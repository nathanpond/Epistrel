"""The structured turn: plan → render → detect → verify → (repair) → compose → commit
(FR-TURN-1,4,5,6, FR-CAT-12, FR-RESP-1,8,12, FR-GUARD-5, FR-COMMIT-7, FR-MEM-1,5; docs/02 §4.4,
§4.9, §5; #150).

``Engine.respond`` plays a turn here when the story's effective configuration runs the
``structured`` pipeline (#129's ``level_at_least(settings, "pipeline", "structured")``, from
#113's ``effective.py``; never a request flag). Everything the request may do and every
precheck is the Lite turn's (``orchestrator.lite.pipeline.respond``): the cast check, the
archived and paused checks, the idempotency claim, the expected revision, the viewpoint, the
budget (#69), the input gate (#56) and the turn gate (#63). Two differences: one character is
rendered (an explicit ``render_character_ids`` of two or more is 422 ``validation_error``;
unnamed, the first non-player character after the actor by ``ordinal``), and the turn is one
``turn`` operation under #47's revision protocol with a ``StructuredReadSet`` (#136): every
keyed lookup its verdicts rely on is recorded, and a change to one regenerates the turn.

**Read** (the operation's snapshot, ``begin``): #57's turn inputs (rating guidance, custom
instructions, text pins, the rolling summary when the profile keeps one, the cast, a scenario
story's scenario text and director notes), the fingerprint stamp (#109), the rendering
character's working memory (``core.memory.records.working_memory``: the latest
``render.working_memory_beats`` beats as it perceives them, FR-MEM-5) and the narrator's, the
scene (#137, through ``KeyedLookup``), the director guidance (#122's notes and #149's
unmaterialized ``director`` items, ``core.genesis.structured.deferred_items``), and the
narrator's triggers. The narrator renders first, and only, when the turn needs narration
(deterministic): the scene changed since the last turn, the player's input addresses no
character (it says nothing and its action names no cast member), or a salient reference is
still open in #146's ledger.

**Render** (``produce``, inside #68's output gate): each render retrieves afresh (#141's
``core.retrieval.pipeline.retrieve`` for the rendering character's viewpoint, the turn's read
set, embedders counted with ``counted_embedder``; the narrator's for ``omniscient``), assembles
its prompt (``orchestrator.structured.prompt``) and calls ``generation`` with a beat
``response_schema`` (``orchestrator.structured.render``). Introduced characters without an age
are repaired by one batched age-repair call. The draft the gate checks is every beat's text
(``think`` and ``latent`` are stored content too), carrying the records and pins its prompt
held (``policies.implication.draft_context``) so a rejected render implicates them and an
``Implicator`` can quarantine a repeat offender (FR-GUARD-5, #145); the regeneration retrieves
again without it. A render every attempt of which broke a policy ends as the safe fallback, one
``say`` of the rendering character.

**Detect and verify** (after the gate): each beat's declared propositions are normalized
(#140's ``normalize_declared``: an unregistered relation registers), #148's ``ClaimDetector``
runs over the player's input and every render (with a connection, so an extraction claim's
relation registers too), and every detected claim goes to #142's verifier
(``story_verifier``, one per turn) beat by beat with its asserter (#144's permission). A
``StagedSource`` lays the turn's earlier permitted claims over canon, so a later beat sees what an
earlier one established (the same-turn overlay). A ``say``'s labels follow
(``core.turns.beats``): ``objective_relation`` from the verdicts, ``belief_relation``
``unknown``, ``intent`` as declared. Salience (#146) reads every render's observable beats.

**Stage and commit** (one transaction): the player's input at beat 1 (with its labels,
propositions and mentions), the rejected drafts as ``guardrail_output_rejected`` (#145's payload
v2), each beat as its ``beat_<kind>`` event (#150's registered types; narrator beats first, then
the character's), a ``character_defined`` per introduction and the composed message (#152 owns
composition: in M4 the observable beats joined, a ``message_rendered`` event with ``composed``
after the beats). The commit steps write, in order: the introduced characters and their canon
``age`` facts; the permitted canon (``core.canon.canonize.canonize_all``) and every ``latent``
beat's propositions with its declared visibility (FR-TURN-4); the utterances; the episodic
memories of the observable beats (``write_episodic``, with each beat's occasion from
``beat_occasion``); the salient references (``record_salience``), the dependences
(``apply_dependences`` → ``lifecycle.depend``) and the turn's close (``close_turn``, its
``follow_up`` seam); and ``operations.turn_report`` (#69's report plus ``render``,
``detection``, ``verify``, ``uncertain_claims`` and ``commit``).

**The result** is M3's ``RespondResult``: the composed message, the turn's beats for the
resolved viewpoint (a character viewpoint never gets ``think`` or ``latent``; an omniscient one
gets them, labelled), and the degradation of every stage with its cause (the output gate's,
retrieval's, detection's, verification's, salience's and the ledger's).

A render that stays malformed after #53's retries fails the turn: 503 ``model_unavailable``,
``details.reason = invalid_structure`` (``age_unrepaired`` for the age repair) with
``details.structural_errors``; the operation ends ``failed`` and nothing is recorded.

Not here (seams, #151/#152/#154/#156): the repair ladder (a claim that ``needs_repair`` is
reported, its beat stands), model composition, regeneration of a structured turn, budget
planning. Logs carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import idempotency, pause, turn_gate
from epistrel.core.canon.canonize import CanonRequest, canonize_all
from epistrel.core.commit import lifecycle
from epistrel.core.concurrency import check_expected_revision, run_with_revalidation
from epistrel.core.config_changes import read_section
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, Provenance, RecordProvenance, StoryStatus
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import operation_record
from epistrel.core.errors import InvalidRequest, RevisionConflict, StoryArchived
from epistrel.core.events import Operation
from epistrel.core.events.types import BeatLabels, MessageRendered, PlayerInput
from epistrel.core.genesis.structured import deferred_items
from epistrel.core.history import turn_beats
from epistrel.core.memory import records
from epistrel.core.profiles.capabilities import level_at_least
from epistrel.core.profiles.effective import ProfileVersionRef, StoryProfile
from epistrel.core.profiles.fingerprint import Stamp
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.entities import NARRATOR, PLAYER
from epistrel.core.resolution.types import Mention
from epistrel.core.retrieval.lexical import LexicalSearch
from epistrel.core.retrieval.pipeline import (
    RetrievalQuery,
    RetrievalResult,
    beat_occasion,
    retrieve,
)
from epistrel.core.settings_schema import (
    OMNISCIENT,
    ResolvedGeneration,
    VerifySettings,
    resolve_generation,
)
from epistrel.core.tables import entity_aliases, operations, stories
from epistrel.core.tables.propositions import entities
from epistrel.core.turn_gate import Concurrency
from epistrel.core.turns import (
    CastEntry,
    StoryCast,
    TurnInputs,
    joined_input,
    read_story_cast,
    read_turn_inputs,
)
from epistrel.core.turns.beats import (
    AGE,
    LATENT_UNNORMALIZED,
    OBSERVABLE_KINDS,
    AssertedClaim,
    Introduction,
    TurnBeat,
    beat_event_ids,
    stage_beats,
    stage_introductions,
    utterance_of,
    visibility_of,
    write_introductions,
    write_utterances,
)
from epistrel.core.verify.detect import DeclaredClaim, DetectBeat, DetectedClaim
from epistrel.core.verify.normalize import ClaimSketch, Unnormalizable, normalize_declared
from epistrel.core.verify.rules import Record, VerdictKind, check
from epistrel.core.viewpoint import character_viewpoint, resolve_viewpoint, try_resolve
from epistrel.observability.logging import get_logger
from epistrel.orchestrator.budget import GUARDRAIL_OUTPUT, TurnBudget, counted_embedder
from epistrel.orchestrator.commit import salience
from epistrel.orchestrator.lite import pipeline as lite
from epistrel.orchestrator.lite.cast import ResolvedCast, resolve_cast
from epistrel.orchestrator.lite.prompt import prompt_hash
from epistrel.orchestrator.lite.schemas import (
    BudgetReport,
    DegradationStep,
    FinishedMessage,
    RespondRequest,
    RespondResult,
)
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import routes_of
from epistrel.orchestrator.models.facts import runtime_facts
from epistrel.orchestrator.models.metrics import CallLog, recording
from epistrel.orchestrator.models.registry import GenerationInputs, SceneContext
from epistrel.orchestrator.policies.implication import Implicator, draft_context, threshold_of
from epistrel.orchestrator.policies.output_gate import (
    Draft,
    Gated,
    gate_output,
    output_policies,
    raised_temperature,
    regeneration_cost,
    rejected_event,
)
from epistrel.orchestrator.policies.rebase import output_recheck
from epistrel.orchestrator.policies.store import guard_context
from epistrel.orchestrator.structured import prompt as prompts
from epistrel.orchestrator.structured.render import (
    NARRATOR as NARRATOR_VARIANT,
)
from epistrel.orchestrator.structured.render import (
    STRUCTURED,
    BeatDecl,
    IntroductionDecl,
    PropositionSketch,
    RenderedBeats,
    render_beats,
    render_params,
    render_schema,
    repair_ages,
)
from epistrel.orchestrator.verify import extraction
from epistrel.orchestrator.verify.extraction import (
    CheckedClaim,
    ClaimDetector,
    DetectionConfig,
    DetectionOutcome,
)
from epistrel.orchestrator.verify.verifier import ClaimSource, Verifier, story_verifier

log = get_logger(__name__)

PIPELINE = "pipeline"
STRUCTURED_LEVEL = "structured"
QUERY_BEATS = 3
"""The retrieval query: the player's input and the rendering character's last beats."""
SCENE_CHANGED = "scene_changed"
UNADDRESSED = "unaddressed"
OPEN_REFERENCE = "open_reference"
CONTRADICTED_LATENT = "latent_contradicted"
INTRODUCTION_DENIED = "introduction_denied"
SAY, DO, LATENT, NARRATION = "say", "do", "latent", "narration"


def is_structured(configuration: StoryProfile) -> bool:
    """Whether the story's effective configuration runs the structured pipeline (#129)."""
    return level_at_least(configuration.settings, PIPELINE, STRUCTURED_LEVEL)


# -- the cast -----------------------------------------------------------------------------------


def resolve_structured_cast(story: StoryCast, request: RespondRequest) -> ResolvedCast:
    """The actor and the one character rendered (M4's one primary, #156): the request's, or
    the first non-player character after the actor by ``ordinal`` (else the first other one,
    else the actor). Two or more named are 422 ``validation_error``."""
    resolved = resolve_cast(story, request)
    if request.render_character_ids is not None:
        named = list(dict.fromkeys(request.render_character_ids))
        if len(named) > 1:
            raise InvalidRequest(
                "a structured turn renders one character",
                {
                    "field": "body.render_character_ids",
                    "reason": "one_render_character",
                    "errors": [
                        {
                            "field": "body.render_character_ids",
                            "message": "a structured turn renders one character",
                            "type": "value_error",
                        }
                    ],
                },
            )
        return resolved
    others = [c for c in story.cast if c.id != resolved.actor.id]
    chosen = next((c for c in others if not c.is_player), others[0] if others else resolved.actor)
    return ResolvedCast(actor=resolved.actor, render=(chosen,))


# -- respond ------------------------------------------------------------------------------------


async def respond(
    db: AsyncEngine,
    models: ModelAccessLayer,
    ctx: RequestContext,
    story_id: str,
    request: RespondRequest,
    *,
    principal: str,
    gate: lite.Gate,
    queue_turn: lite.QueueTurn,
    lexical: LexicalSearch | None = None,
) -> RespondResult:
    """Play one structured turn; see the module docstring (the prechecks are Lite's)."""
    started = ctx.received_at if ctx.received_at is not None else time.monotonic()
    story = await read_story_cast(db, story_id)
    resolved_cast = resolve_structured_cast(story, request)
    if story.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )
    await pause.refuse(db, story_id, story.status)
    resolution = partial(
        resolve_viewpoint,
        ctx.viewpoint,
        actor_character_id=resolved_cast.actor.id,
        story_default=story.settings.default_output_viewpoint,
        cast_ids=story.ids,
    )
    call = idempotency.current_call()
    if call is not None:
        await call.claim(
            {
                "story_id": story_id,
                "body": request.model_dump(mode="json", exclude={"output_viewpoint"})
                | {"render_character_ids": resolved_cast.render_ids},
                "actor_character_id": resolved_cast.actor.id,
                "viewpoint": try_resolve(resolution),
                "expected_revision": ctx.expected_revision,
            },
            sent={
                "story_id": story_id,
                "body": request.model_dump(mode="json"),
                "viewpoint": ctx.viewpoint,
                "expected_revision": ctx.expected_revision,
            },
        )
    await check_expected_revision(db, story_id, ctx.expected_revision)
    viewpoint = resolution()
    concurrency = await turn_gate.read_concurrency(db, story_id)
    budget = await lite.plan_budget(
        db,
        story_id,
        resolved_cast,
        story_max_calls=concurrency.max_model_calls_per_turn,
        deployment_max_calls=None,
        turn_deadline=concurrency.turn_deadline,
        override=request.budget,
        started=started,
    )
    timing = lite.TurnTiming(front=started)
    with recording() as calls, lite.reporting_costs(calls, budget), lite._routed(concurrency):
        gating = time.perf_counter()
        await gate(
            budget,
            joined_input(request.player_input.say_text, request.player_input.do_text),
            resolved_cast.actor.id,
            resolved_cast.render_ids,
        )
        timing.policy_check_s += time.perf_counter() - gating
        budget.release_to(1 + budget.reserved.get(GUARDRAIL_OUTPUT, 0))
        op = Operation(
            db,
            story_id=story_id,
            kind=OperationKind.TURN,
            principal=principal,
            idempotency_key=ctx.idempotency_key,
            viewpoint=viewpoint,
        )
        held = await turn_gate.acquire(db, story_id, op.operation_id, lease_s=concurrency.lease_s)
        if held is not None:
            if concurrency.mode == "reject":
                raise held.busy(story_id)
            queued = {
                "body": request.model_dump(mode="json"),
                "turn_budget": {"max_calls": budget.max_calls, "deadline_s": budget.deadline_s},
            }
            return await queue_turn(op.operation_id, queued, viewpoint, concurrency)
        op.holds_turn_slot = True
        try:
            async with turn_gate.renewing(db, story_id, op.operation_id, concurrency.lease_s):
                return await play(
                    db,
                    models,
                    story_id,
                    request,
                    resolved_cast,
                    viewpoint,
                    op,
                    budget,
                    calls,
                    story=story,
                    configuration=lite._captured(concurrency),
                    timing=timing,
                    lexical=lexical,
                )
        finally:
            if op.status is None:  # took the slot but never began
                await turn_gate.release_now(db, story_id, op.operation_id)


async def run_queued(
    db: AsyncEngine,
    models: ModelAccessLayer,
    ctx: RequestContext,
    story_id: str,
    request: RespondRequest,
    *,
    operation_id: str,
    principal: str,
    concurrency: Concurrency,
    planned: dict[str, Any] | None = None,
    lexical: LexicalSearch | None = None,
) -> RespondResult:
    """Play a queued structured turn (#63's drainer took the slot for it); see Lite's."""
    story = await read_story_cast(db, story_id)
    resolved_cast = resolve_structured_cast(story, request)
    await pause.refuse(db, story_id, story.status)
    viewpoint = ctx.viewpoint
    if viewpoint is None:  # pragma: no cover - always resolved at enqueue
        raise RuntimeError(f"queued turn {operation_id} has no viewpoint")
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.TURN,
        principal=principal,
        idempotency_key=ctx.idempotency_key,
        viewpoint=viewpoint,
        operation_id=operation_id,
        queued=True,
    )
    op.holds_turn_slot = True
    planned = planned or {}
    front = time.monotonic()
    timing = lite.TurnTiming(front=front, queued_ms=await lite.queued_ms(db, operation_id))
    budget = await lite.plan_budget(
        db,
        story_id,
        resolved_cast,
        story_max_calls=planned.get("max_calls", concurrency.max_model_calls_per_turn),
        deployment_max_calls=None,
        turn_deadline=planned.get("deadline_s") or concurrency.turn_deadline,
        override=None,
        started=front,
    )
    budget.release_to(1 + budget.reserved.get(GUARDRAIL_OUTPUT, 0))
    with recording() as calls, lite.reporting_costs(calls, budget), lite._routed(concurrency):
        return await play(
            db,
            models,
            story_id,
            request,
            resolved_cast,
            viewpoint,
            op,
            budget,
            calls,
            story=story,
            configuration=lite._captured(concurrency),
            timing=timing,
            lexical=lexical,
        )


# -- what the turn reads -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnRead:
    """What ``begin`` read in the turn's snapshot."""

    inputs: TurnInputs
    resolved: ResolvedGeneration
    turn: int
    character: CastEntry
    actor: CastEntry
    names: Mapping[str, str]
    player_id: str | None
    character_memory: tuple[str, ...]
    narrator_memory: tuple[str, ...]
    last_beats: tuple[str, ...]
    scene: str
    present: tuple[str, ...]
    director: str
    triggers: tuple[str, ...]
    allow_new_arrivals: bool


async def _aliases(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> list[str]:
    """The names and aliases of the cast members ``ids`` (their entities', #139)."""
    found = await conn.execute(
        select(entity_aliases.c.alias)
        .join(entities, entities.c.id == entity_aliases.c.entity_id)
        .where(entities.c.story_id == story_id, entities.c.id.in_(list(ids)))
    )
    return [str(r.alias) for r in found]


def _addresses(text: str, names: Sequence[str]) -> bool:
    folded = f" {' '.join(text.casefold().split())} "
    return any(f" {n.casefold()} " in folded or f" {n.casefold()}," in folded for n in names)


async def _scene(lookup: KeyedLookup, story_id: str, as_of: int) -> tuple[str | None, Any]:
    if as_of < 0:
        return None, None
    try:
        scene = await lookup.scene(story_id, as_of=as_of, viewpoint=OMNISCIENT)
    except InvalidRequest:
        return None, None
    location = None if scene.location is None else scene.location.entity_id
    return location, scene


async def read_turn(
    conn: AsyncConnection,
    story_id: str,
    cast_: ResolvedCast,
    request: RespondRequest,
    *,
    configuration: StoryProfile,
    read_set: StructuredReadSet,
    story: StoryCast,
) -> TurnRead:
    """The structured turn's inputs, read in its snapshot (see the module docstring)."""
    settings = configuration.settings
    inputs = await read_turn_inputs(conn, story_id, configuration=configuration)
    resolved = resolve_generation(inputs.generation, request.prompt_params)
    head = int(
        await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id)) or 1
    )
    turn = head
    current = {c.id: c for c in inputs.cast}
    character = current.get(cast_.render[0].id, cast_.render[0])
    actor = current.get(cast_.actor.id, cast_.actor)
    names = {c.id: c.name for c in inputs.cast}
    player = next((c.id for c in inputs.cast if c.is_player), None)
    n = settings.render.working_memory_beats
    mine = await records.working_memory(conn, story_id, character.id, None, n)
    everyone = await records.working_memory(conn, story_id, None, None, n)
    character_memory = tuple(
        prompts.working_line(
            b.kind, names.get(b.character_id or ""), b.text, own=b.character_id == character.id
        )
        for b in mine
    )
    narrator_memory = tuple(
        prompts.working_line(b.kind, names.get(b.character_id or ""), b.text, own=False)
        for b in everyone
        if b.kind not in ("think", "latent")
    )
    last_beats = tuple(b.text for b in mine if b.character_id == character.id)[-QUERY_BEATS:]
    lookup = KeyedLookup(conn, read_set)
    location, scene = await _scene(lookup, story_id, head - 1)
    before, _ = await _scene(lookup, story_id, head - 2)
    present = tuple(e.name for e in scene.present) if scene is not None else ()
    scene_text = ""
    if scene is not None and (scene.location is not None or present):
        where = scene.location.name if scene.location is not None else "an unnamed place"
        scene_text = f"Location: {where}." + (f" Present: {', '.join(present)}." if present else "")
    triggers: list[str] = []
    if location is not None and before is not None and location != before:
        triggers.append(SCENE_CHANGED)
    say, do = request.player_input.say_text, request.player_input.do_text
    others = [c.id for c in inputs.cast if c.id != actor.id]
    if not say and others:
        called = [names[i] for i in others] + await _aliases(conn, story_id, others)
        if not _addresses(do or "", called):
            triggers.append(UNADDRESSED)
    if lifecycle.commitment_enabled(settings) and await lifecycle.blockers(conn, story_id):
        triggers.append(OPEN_REFERENCE)
    director = [inputs.director_notes] if inputs.director_notes else []
    for item in await deferred_items(conn, story_id):
        if item.field_class.value == "director":
            value = item.payload.get("value")
            if isinstance(value, str) and value.strip():
                director.append(value.strip())
            elif isinstance(value, list):
                director.extend(str(v) for v in value if isinstance(v, str) and v.strip())
    return TurnRead(
        inputs=inputs,
        resolved=resolved,
        turn=turn,
        character=character,
        actor=actor,
        names=names,
        player_id=player,
        character_memory=character_memory,
        narrator_memory=narrator_memory,
        last_beats=last_beats,
        scene=scene_text,
        present=present,
        director="\n\n".join(director),
        triggers=tuple(triggers),
        allow_new_arrivals=story.settings.allow_new_arrivals,
    )


# -- rendering -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Rendered:
    """One attempt's renders: the beats in turn order (numbered from 2; claims not yet
    checked), their declared sketches, the introductions, the retrievals and the counts."""

    beats: list[TurnBeat]
    sketches: dict[int, list[PropositionSketch]]
    introductions: list[Introduction]
    retrievals: list[RetrievalResult]
    attempts: int = 0
    structure_retries: int = 0
    age_repairs: int = 0
    denied_introductions: int = 0
    narrator: bool = False
    model: str | None = None
    prompt_hash: str | None = None
    finish_reason: str | None = None


def _canon_lines(result: RetrievalResult, own: str | None) -> tuple[list[str], list[str]]:
    """The retrieved context's lines: everything else, and the canon (never the rendering
    character's own age, M4)."""
    hidden = {
        f.id
        for f in result.canon
        if own is not None and f.proposition.subject_id == own and f.proposition.predicate_id == AGE
    }
    retrieved: list[str] = []
    canon: list[str] = []
    for item in result.context.items:
        if item.kind == "canon":
            if item.item_id not in hidden:
                canon.append(item.text)
        else:
            retrieved.append(item.text)
    return retrieved, canon


def _beats_of(
    render: RenderedBeats,
    *,
    start: int,
    actor: str | None,
    narrator: bool,
) -> tuple[list[TurnBeat], dict[int, list[PropositionSketch]]]:
    beats: list[TurnBeat] = []
    sketches: dict[int, list[PropositionSketch]] = {}
    for offset, decl in enumerate(render.beats):
        number = start + offset
        scope = decl.visibility.scope if decl.visibility is not None else "objective"
        chars = tuple(decl.visibility.characters) if decl.visibility is not None else ()
        beats.append(
            TurnBeat(
                kind=decl.kind,
                beat=number,
                text=decl.text.strip(),
                character_id=None if decl.kind == NARRATION else actor,
                intent=(decl.intent or "unspecified") if decl.kind == SAY else "unspecified",
                visibility=visibility_of(scope, chars)
                if decl.kind == LATENT
                else visibility_of(""),
                visibility_declared=scope,
                narrator=narrator,
            )
        )
        sketches[number] = list(decl.propositions)
    return beats, sketches


def _clue(decl: IntroductionDecl, beats: Sequence[BeatDecl]) -> str:
    return decl.clue or next(
        (b.text for b in beats if decl.name.casefold() in b.text.casefold()), ""
    )


async def _introductions(
    models: ModelAccessLayer,
    read: TurnRead,
    renders: Sequence[tuple[RenderedBeats, list[TurnBeat]]],
    taken: Mapping[str, str],
    sections: prompts.RenderSections,
    *,
    params: ModelParams,
    budget: TurnBudget,
) -> tuple[list[Introduction], int, int]:
    """The turn's new characters: each ``introduces`` item whose name the cast does not have,
    while the story allows new arrivals; an age the render did not give comes from one batched
    age-repair call. Answers them, the repairs made and the introductions denied."""
    wanted: list[tuple[IntroductionDecl, TurnBeat, Sequence[BeatDecl]]] = []
    seen: set[str] = set()
    for render, beats in renders:
        for decl in render.introduces:
            key = decl.name.strip().casefold()
            if key in taken or key in seen:
                continue  # a mention of a character the story already has
            seen.add(key)
            where = next((b for b in beats if key in b.text.casefold()), beats[0])
            wanted.append((decl, where, render.beats))
    if not wanted:
        return [], 0, 0
    if not read.allow_new_arrivals:
        return [], 0, len(wanted)
    missing = {d.name.strip(): d.bounds() for d, _, _ in wanted if d.age is None}
    ages: dict[str, int] = {}
    if missing:
        clues = [(d.name.strip(), _clue(d, bs)) for d, _, bs in wanted if d.age is None]
        ages = await repair_ages(
            models,
            prompts.age_inputs(sections, read.actor, clues),
            missing,
            params=params,
            budget=budget,
        )
    intros = [
        Introduction(
            name=d.name.strip(),
            age=d.age if d.age is not None else ages[d.name.strip().casefold()],
            source="declared" if d.age is not None else "repaired",
            beat=where.beat,
            definition=where.text,
        )
        for d, where, _ in wanted
    ]
    return intros, (1 if missing else 0), 0


def compose(beats: Sequence[TurnBeat], names: Mapping[str, str]) -> str:
    """The M4 composed message (#152 composes with a model): the observable beats joined, a
    ``say`` as ``Name: "…"``, a ``do`` as ``*…*``, narration verbatim. ``think`` and ``latent``
    never appear in it (FR-TURN-3)."""
    lines: list[str] = []
    for beat in beats:
        name = names.get(beat.character_id or "", "")
        if beat.kind == SAY:
            lines.append(f'{name}: "{beat.text}"' if name else f'"{beat.text}"')
        elif beat.kind == DO:
            lines.append(f"*{beat.text}*")
        elif beat.kind == NARRATION:
            lines.append(beat.text)
    return "\n\n".join(lines)


# -- the same-turn overlay -----------------------------------------------------------------------


class StagedSource:
    """A ``ClaimSource`` over canon with the turn's earlier permitted claims laid over it (the
    same-turn overlay, #144's carried note): a claim canon leaves ``novel`` or ``uncertain`` is
    checked against the staged claims too, and a conclusive verdict there wins."""

    def __init__(self, inner: ClaimSource) -> None:
        self.inner = inner
        self.staged: list[Record] = []

    def stage(self, claim: Proposition, claim_id: str) -> None:
        self.staged.append(Record(claim, f"staged:{claim_id}"))

    async def registry(self) -> Registry:
        return await self.inner.registry()

    async def deterministic(self, claim: Proposition, **kwargs: Any) -> Any:
        found = await self.inner.deterministic(claim, **kwargs)
        if found is None or not self.staged:
            return found
        verdict, fetched = found
        if verdict.kind not in (VerdictKind.NOVEL, VerdictKind.UNCERTAIN):
            return found
        staged = check(
            claim,
            self.staged,
            registry=await self.inner.registry(),
            holder=kwargs.get("holder", verdict.holder),
            position=kwargs.get("position"),
        )
        if staged.kind in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED):
            return staged, [*fetched, *self.staged]
        return found

    async def neighbors(self, claim: Proposition, **kwargs: Any) -> Any:
        return await self.inner.neighbors(claim, **kwargs)

    async def names(self, ids: Sequence[str]) -> Mapping[str, str]:
        return await self.inner.names(ids)

    async def permission(self, claim: Proposition, asserter: Any, **kwargs: Any) -> Any:
        return await self.inner.permission(claim, asserter, **kwargs)

    async def anchor(self, claim: Proposition, **kwargs: Any) -> Any:
        return await self.inner.anchor(claim, **kwargs)


# -- detection and verification ------------------------------------------------------------------


@dataclass(slots=True)
class Checked:
    """What detection and verification made of the released renders."""

    beats: list[TurnBeat]
    input_claims: list[AssertedClaim]
    input_mentions: list[dict[str, Any]]
    outcomes: list[DetectionOutcome]
    checked: list[CheckedClaim]
    requests: list[CanonRequest]
    latent: list[tuple[TurnBeat, AssertedClaim]]
    mentions: dict[int, tuple[Mention, ...]]
    verifier: Verifier | None
    salient: list[tuple[salience.Beat, Any, tuple[int, int] | None]]
    degradation: list[tuple[str, str]] = field(default_factory=list)


def _verdict_of(result: CheckedClaim) -> str | None:
    return result.verdict


def _asserted(c: CheckedClaim) -> AssertedClaim:
    claim = c.claim
    return AssertedClaim(
        proposition=claim.proposition,
        verdict=_verdict_of(c),
        claim_id=c.claim_id,
        declared=claim.layer == "declared",
        span=(claim.span[1], claim.span[2]),
        category=claim.category,
    )


def _sketch(sketch: PropositionSketch) -> ClaimSketch:
    return ClaimSketch(
        subject_text=sketch.subject,
        relation_text=sketch.relation,
        object_text=sketch.object,
        time_text=sketch.time,
        polarity=Polarity.NEGATED if sketch.negated else Polarity.AFFIRMED,
        declared=True,
    )


def _speaker(beat: TurnBeat) -> str:
    if beat.kind == NARRATION or beat.narrator:
        return NARRATOR
    return beat.character_id or NARRATOR


async def check_turn(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    read: TurnRead,
    rendered: Rendered,
    *,
    say: str | None,
    do: str | None,
    operation_id: str,
    read_set: StructuredReadSet,
    configuration: StoryProfile,
    budget: TurnBudget,
) -> Checked:
    """Detect and verify the player's input and every render's beats (see the module
    docstring)."""
    settings = configuration.settings
    routes = routes_of(settings)
    turn = read.turn
    input_speaker = PLAYER if read.actor.id == read.player_id else read.actor.id
    async with db.connect() as conn:
        config = DetectionConfig.of(settings, await read_section(conn, VerifySettings))
        speakers = [input_speaker, read.character.id]
        env = await extraction.load_env(
            conn, story_id, read_set, position=(turn, 0), speakers=speakers, config=config
        )
    # The declared propositions, normalized (an unregistered relation registers, in a short
    # transaction of its own).
    declared: dict[int, tuple[DeclaredClaim, ...]] = {}
    async with db.begin() as conn:
        for beat in rendered.beats:
            found: list[DeclaredClaim] = []
            speaker = _speaker(beat)
            for n, sketch in enumerate(rendered.sketches.get(beat.beat, ())):
                normalized, registry = await normalize_declared(
                    conn,
                    story_id,
                    _sketch(sketch),
                    speaker,
                    (turn, beat.beat),
                    registry=env.registry,
                    context=env.context_for(speaker),
                    basis=env.basis,
                )
                env = replace(env, registry=registry)
                claim_id = f"d{beat.beat}.{n}"
                if isinstance(normalized, Unnormalizable):
                    found.append(DeclaredClaim(claim_id, unnormalized=normalized))
                else:
                    found.append(DeclaredClaim(claim_id, proposition=normalized))
            declared[beat.beat] = tuple(found)
    scene_context = SceneContext(
        present_entities=list(read.present)[:100], last_beats=list(read.last_beats)
    )
    outcomes: list[DetectionOutcome] = []
    input_beats = [
        DetectBeat(1, channel, text, input_speaker)
        for channel, text in ((SAY, say), (DO, do))
        if text
    ]
    groups: list[list[DetectBeat]] = []
    if input_beats:
        groups.append(input_beats)
    narrator_beats = [b for b in rendered.beats if b.narrator]
    character_beats = [b for b in rendered.beats if not b.narrator]
    for render_group in (narrator_beats, character_beats):
        if render_group:
            groups.append(
                [
                    DetectBeat(b.beat, b.kind, b.text, _speaker(b), declared.get(b.beat, ()))
                    for b in render_group
                ]
            )
    for group in groups:
        async with db.begin() as conn:
            detector = ClaimDetector(
                config,
                env,
                models=models,
                budget=budget,
                routes=routes,
                scene_context=scene_context,
                conn=conn,
                story_id=story_id,
            )
            outcomes.append(await detector.render(group, (turn, 0), operation_id=operation_id))
            env = detector.env
    mentions: dict[int, tuple[Mention, ...]] = {}
    for outcome in outcomes:
        for detected in outcome.detection.beats:
            if detected.beat is not None:
                mentions[detected.beat] = (*mentions.get(detected.beat, ()), *detected.mentions)
    checked: list[CheckedClaim] = []
    verifier: Verifier | None = None
    requests: list[CanonRequest] = []
    async with db.connect() as conn:
        verifier = await story_verifier(conn, read_set, story_id, models=models, budget=budget)
        staged = StagedSource(verifier.source)
        verifier.source = staged
        by_beat: dict[int | None, list[DetectedClaim]] = {}
        for outcome in outcomes:
            for claim in outcome.claims:
                by_beat.setdefault(claim.beat, []).append(claim)
        scopes = {b.beat: b for b in rendered.beats}

        def scope_of(claim: DetectedClaim) -> tuple[str, ...] | None:
            beat = scopes.get(claim.beat or -1)
            if beat is None or beat.kind != LATENT or beat.visibility.scope != "characters":
                return None
            return tuple(beat.visibility.characters or ())

        for number in sorted(k for k in by_beat if k is not None):
            outcome = DetectionOutcome(
                outcomes[0].detection.with_claims(by_beat[number]), speakers={}
            )
            before = len(verifier.canon_requests())
            checked += await extraction.verify_detected(
                verifier,
                outcome,
                (turn, number),
                operation_id=operation_id,
                player_id=read.player_id,
                scope_of=scope_of,
                start=len(checked),
            )
            # The same-turn overlay: what this beat may establish, later beats see.
            for request in verifier.canon_requests()[before:]:
                staged.stage(request.claim, request.claim_id or f"{number}")
        requests = verifier.canon_requests()
    by_claim: dict[int, list[CheckedClaim]] = {}
    for c in checked:
        if c.claim.beat is not None:
            by_claim.setdefault(c.claim.beat, []).append(c)
    beats = [
        replace(b, claims=tuple(_asserted(c) for c in by_claim.get(b.beat, [])))
        for b in rendered.beats
    ]
    latent_ids = {c.claim_id for b in beats if b.kind == LATENT for c in b.claims}
    requests = [r for r in requests if r.claim_id not in latent_ids]
    latent = [
        (b, c)
        for b in beats
        if b.kind == LATENT
        for c in b.claims
        if c.proposition is not None and c.verdict != "contradicted"
    ]
    degradation: list[tuple[str, str]] = []
    for outcome in outcomes:
        degradation += [(d, "extraction") for d in outcome.degradation]
    if verifier is not None:
        degradation += [(d, "verification") for d in verifier.degradation()]
    for b in beats:
        if b.kind == LATENT and not any(c.proposition is not None for c in b.claims):
            degradation.append((LATENT_UNNORMALIZED, "latent"))
        if b.kind == LATENT and any(
            c.verdict == "contradicted" and c.proposition is not None for c in b.claims
        ):
            degradation.append((CONTRADICTED_LATENT, "latent"))
    # Salience (#146): every render's observable beats.
    salient: list[tuple[salience.Beat, Any, tuple[int, int] | None]] = []
    mode = salience.salience_mode(settings)
    if mode is not None:
        for render_group in (narrator_beats, character_beats):
            sal_beats = [
                salience.Beat(
                    b.beat,
                    b.kind,
                    b.text,
                    speaker_entity=b.character_id,
                    speaker_name=read.names.get(b.character_id or ""),
                )
                for b in render_group
            ]
            if not sal_beats:
                continue
            verdict = await salience.detect_salience(
                sal_beats, scene_context, mode=mode, models=models, budget=budget, routes=routes
            )
            degradation += [(d, "salience") for d in verdict.degradation]
            index = {b.index: b for b in sal_beats}
            for span in verdict.salient:
                at = index.get(span.span.beat)
                if at is not None:
                    salient.append((at, span.proposition_sketch, (span.span.start, span.span.end)))
            claims = [
                (index[c.claim.beat], c.claim.sketch)
                for c in checked
                if c.claim.beat in index and c.claim.sketch is not None
            ]
            for at, claim_sketch in salience.rules_salient(claims):
                salient.append((at, claim_sketch, None))
    input_checked = [c for c in checked if c.claim.beat == 1]
    input_mentions = [m.as_payload() for m in mentions.get(1, ())]
    return Checked(
        beats=beats,
        input_claims=[_asserted(c) for c in input_checked],
        input_mentions=input_mentions,
        outcomes=outcomes,
        checked=checked,
        requests=requests,
        latent=latent,
        mentions=mentions,
        verifier=verifier,
        salient=salient,
        degradation=degradation,
    )


# -- the turn ------------------------------------------------------------------------------------


async def play(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    request: RespondRequest,
    cast_: ResolvedCast,
    viewpoint: str,
    op: Operation,
    budget: TurnBudget,
    calls: CallLog,
    *,
    story: StoryCast,
    configuration: StoryProfile,
    timing: lite.TurnTiming | None = None,
    lexical: LexicalSearch | None = None,
) -> RespondResult:
    """The structured turn itself (see the module docstring), under ``configuration``."""
    timing = timing if timing is not None else lite.TurnTiming(front=budget.started)
    say, do = request.player_input.say_text, request.player_input.do_text
    settings = configuration.settings
    captured = configuration.configuration()
    read_set = StructuredReadSet()
    stamped: list[Stamp] = []
    gated: list[Gated] = []
    released: list[Rendered] = []
    found: list[Checked] = []
    fixed: dict[str, Any] = {}
    rounds: list[int] = []

    async def begin(conn: AsyncConnection) -> TurnRead:
        reading = time.perf_counter()
        read = await read_turn(
            conn,
            story_id,
            cast_,
            request,
            configuration=configuration,
            read_set=read_set,
            story=story,
        )
        timing.assemble_s += time.perf_counter() - reading
        facts = await runtime_facts(conn)
        stamped[:] = [captured.stamp(facts)]
        op.config_revisions = {
            **(op.config_revisions or {}),
            "generation_settings": read.resolved.model_dump(mode="json"),
            **stamped[0].record(captured.profile_id, captured.version),
            "overrides_revision": configuration.overrides_revision,
        }
        return read

    async def retrieve_for(actor: str, viewpoint_: str, text: str) -> RetrievalResult:
        embeddings = models.embeddings
        async with db.begin() as conn:
            return await retrieve(
                conn,
                story_id,
                RetrievalQuery(text=text[:2_000] or ".", actor=actor),
                viewpoint=viewpoint_,
                embed=counted_embedder(embeddings.query_embedder(), budget),
                embed_batch=counted_embedder(embeddings.batch_embedder(), budget),
                read_set=read_set,
                lexical=lexical,
                settings=settings,
            )

    async def generate(op: Operation, read: TurnRead) -> None:
        context = await guard_context(db, story_id)
        policies = output_policies(context, cast_.render_ids)
        rounds.append(1)
        if len(rounds) > 1:
            cost = regeneration_cost(policies)
            if budget.expired() or not budget.can_afford_unreserved(cost):
                raise RevisionConflict(
                    "the story changed under the turn and its model budget cannot pay to "
                    "generate again",
                    {
                        "current_revision": int(op.base_revision or 0),
                        "attempts": len(op.attempts),
                        "reason": "budget",
                    },
                )
            budget.hold(cost)
        by_draft: dict[int, Rendered] = {}
        joined = joined_input(say, do)
        base = prompts.RenderSections(
            guidance=read.inputs.guidance,
            instructions=read.inputs.instructions,
            scenario_text=read.inputs.scenario_text,
            pins=read.inputs.pins,
            segments=read.inputs.segments,
            scene=read.scene,
        )
        budget_tokens = settings.render.prompt_token_budget
        max_beats = settings.render.max_beats
        cast_ids = list(read.names)

        async def produce(constraints: tuple[str, ...], attempt: int) -> Draft:
            assembling = time.perf_counter()
            params = render_params(
                ModelParams(
                    temperature=raised_temperature(read.resolved.temperature, attempt),
                    top_p=read.resolved.top_p,
                ),
                read.resolved.max_tokens,
            )
            retrievals: list[RetrievalResult] = []
            renders: list[tuple[RenderedBeats, list[TurnBeat]]] = []
            beats: list[TurnBeat] = []
            sketches: dict[int, list[PropositionSketch]] = {}
            retries = 0
            narration: list[str] = []
            next_beat = 2
            if read.triggers:
                narr_retrieval = await retrieve_for(NARRATOR, OMNISCIENT, joined)
                retrievals.append(narr_retrieval)
                retrieved, canon = _canon_lines(narr_retrieval, None)
                sections = replace(
                    base,
                    director_notes=read.director,
                    working_memory=read.narrator_memory,
                    retrieved=tuple(retrieved),
                    canon=tuple(canon),
                    constraints=constraints,
                )
                timing.assemble_s += time.perf_counter() - assembling
                narr, n = await render_beats(
                    models,
                    prompts.narrator_inputs(
                        sections, read.actor, say=say, do=do, budget=budget_tokens
                    ),
                    render_schema(
                        actor_id=None,
                        player_id=read.player_id,
                        cast_ids=cast_ids,
                        max_beats=max_beats,
                    ),
                    variant=NARRATOR_VARIANT,
                    params=params,
                    budget=budget,
                )
                retries += n
                made, found_sketches = _beats_of(narr, start=next_beat, actor=None, narrator=True)
                beats += made
                sketches.update(found_sketches)
                renders.append((narr, made))
                narration = [b.text for b in made if b.kind == NARRATION]
                next_beat += len(made)
            assembling = time.perf_counter()
            query = " ".join([joined, *read.last_beats])
            char_retrieval = await retrieve_for(
                read.character.id, character_viewpoint(read.character.id), query
            )
            retrievals.append(char_retrieval)
            retrieved, canon = _canon_lines(char_retrieval, read.character.id)
            player_line = prompts.working_line(SAY, read.actor.name, say or "", own=False)
            this_turn = tuple(narration) + ((player_line,) if say else ())
            if do:
                this_turn += (prompts.working_line(DO, read.actor.name, do, own=False),)
            sections = replace(
                base,
                working_memory=(*read.character_memory, *this_turn),
                retrieved=tuple(retrieved),
                canon=tuple(canon),
                constraints=constraints,
            )
            timing.assemble_s += time.perf_counter() - assembling
            inputs: GenerationInputs = prompts.character_inputs(
                sections, read.character, read.actor, say=say, do=do, budget=budget_tokens
            )
            char_render, n = await render_beats(
                models,
                inputs,
                render_schema(
                    actor_id=read.character.id,
                    player_id=read.player_id if read.player_id != read.character.id else None,
                    cast_ids=cast_ids,
                    max_beats=max_beats,
                ),
                variant=STRUCTURED,
                params=params,
                budget=budget,
            )
            retries += n
            made, found_sketches = _beats_of(
                char_render, start=next_beat, actor=read.character.id, narrator=False
            )
            beats += made
            sketches.update(found_sketches)
            renders.append((char_render, made))
            taken = {name.casefold(): cid for cid, name in read.names.items()}
            intros, repairs, denied = await _introductions(
                models, read, renders, taken, base, params=params, budget=budget
            )
            _, _, messages = models.prepare("generation", inputs, variant=STRUCTURED)
            rendered = Rendered(
                beats=beats,
                sketches=sketches,
                introductions=intros,
                retrievals=retrievals,
                attempts=attempt + 1,
                structure_retries=retries,
                age_repairs=repairs,
                denied_introductions=denied,
                narrator=bool(read.triggers),
                model=calls.calls[-1].model if calls.calls else None,
                prompt_hash=prompt_hash(messages),
                finish_reason="stop",
            )
            draft = Draft(
                text="\n".join(b.text for b in beats),
                model=rendered.model,
                prompt_hash=rendered.prompt_hash,
                finish_reason=rendered.finish_reason,
            )
            for retrieval in retrievals:
                draft = _with_context(draft, retrieval)
            by_draft[id(draft)] = rendered
            return draft

        gating = time.perf_counter()
        outcome = await gate_output(
            models,
            context,
            policies,
            produce,
            budget=budget,
            retry_budget=lite.polish_budget(settings),
            fallback=lite._fallback_lines(read.inputs, cast_),
            implicate=Implicator(
                db,
                story_id,
                turn_operation_id=op.operation_id,
                threshold=threshold_of(settings),
                policies=policies,
            ),
        )
        timing.policy_check_s += time.perf_counter() - gating
        gated[:] = [outcome]
        rendered = by_draft.get(id(outcome.draft))
        if rendered is None or outcome.fallback_used:
            # The safe fallback: one say of the rendering character, nothing else.
            rendered = Rendered(
                beats=[
                    TurnBeat(
                        kind="say", beat=2, text=outcome.draft.text, character_id=read.character.id
                    )
                ],
                sketches={},
                introductions=[],
                retrievals=[],
                finish_reason="fallback",
            )
        released[:] = [rendered]
        checked = await check_turn(
            db,
            models,
            story_id,
            read,
            rendered,
            say=say,
            do=do,
            operation_id=op.operation_id,
            read_set=read_set,
            configuration=configuration,
            budget=budget,
        )
        found[:] = [checked]
        stage(op, read, rendered, checked, outcome)

    def stage(
        op: Operation, read: TurnRead, rendered: Rendered, checked: Checked, outcome: Gated
    ) -> None:
        input_labels = (
            BeatLabels(
                objective_relation=_relation(checked.input_claims, SAY),
                intent="unspecified",
            )
            if say
            else None
        )
        op.append(
            "player_input",
            PlayerInput(
                text=joined_input(say, do),
                say=say,
                do=do,
                labels=input_labels,
                propositions=tuple(c.payload() for c in checked.input_claims if c.proposition),
                mentions=tuple(checked.input_mentions),
            ),
            provenance=Provenance.PLAYER,
            character_id=read.actor.id,
            beat=1,
        )
        last = max((b.beat for b in checked.beats), default=1)
        for record in [rejected_event(r) for r in outcome.rejected]:
            op.append(
                "guardrail_output_rejected",
                record,
                provenance=Provenance.ENGINE,
                character_id=None,
                beat=last + 1,
            )
        beats = [
            replace(b, mentions=tuple(m.as_payload() for m in checked.mentions.get(b.beat, ())))
            for b in checked.beats
        ]
        stage_beats(op, beats)
        stage_introductions(op, rendered.introductions)
        op.append(
            "message_rendered",
            MessageRendered(
                text=compose(beats, read.names) or outcome.draft.text,
                render_character_ids=(read.character.id,),
                model=rendered.model,
                prompt_hash=rendered.prompt_hash,
                finish_reason=rendered.finish_reason,
                fallback_used=outcome.fallback_used,
                composed=True,
            ),
            provenance=Provenance.ENGINE,
            character_id=None,
            beat=last + 1,
        )
        op.add_commit_step(partial(write_turn, read=read, rendered=rendered, checked=checked))

    async def write_turn(
        conn: AsyncConnection, *, read: TurnRead, rendered: Rendered, checked: Checked
    ) -> None:
        ids = await beat_event_ids(conn, op.operation_id)
        await write_introductions(conn, op, rendered.introductions, base_revision=op.base_revision)
        registry = await load_registry(conn, story_id)
        latent_requests = [
            CanonRequest(
                claim=claim.proposition,
                provenance=RecordProvenance.DIRECTOR if beat.narrator else RecordProvenance.RENDER,
                visibility=beat.visibility,
                beat=beat.beat,
                claim_id=claim.claim_id,
            )
            for beat, claim in checked.latent
            if claim.proposition is not None
        ]
        canonized = await canonize_all(
            conn, story_id, [*checked.requests, *latent_requests], operation=op, registry=registry
        )
        utterance_rows = []
        for beat in checked.beats:
            if beat.kind != SAY or beat.character_id is None:
                continue
            props = [
                await store.write_proposition(
                    conn, story_id, c.proposition, registry, base_revision=op.base_revision
                )
                for c in beat.claims
                if c.proposition is not None
            ]
            utterance_rows.append(utterance_of(beat, props))
        if say:
            props = [
                await store.write_proposition(
                    conn, story_id, c.proposition, registry, base_revision=op.base_revision
                )
                for c in checked.input_claims
                if c.proposition is not None
            ]
            utterance_rows.append(
                utterance_of(
                    TurnBeat(
                        kind="say",
                        beat=1,
                        text=say,
                        character_id=read.actor.id,
                        claims=tuple(c for c in checked.input_claims if c.span is not None),
                    ),
                    props,
                )
            )
        await write_utterances(conn, op, utterance_rows, event_ids=ids)
        await records.write_episodic(
            conn, op, _episodic(checked, read, say=say, do=do, ids=ids), settings=settings
        )
        commit_report: dict[str, Any] = {}
        if lifecycle.commitment_enabled(settings):
            position = (read.turn, 0)
            sal = await salience.record_salience(
                conn,
                op,
                checked.salient,
                read_set=read_set,
                position=position,
                registry=registry,
                on=True,
            )
            await extraction.apply_dependences(
                conn, op, checked.checked, read_set=read_set, registry=registry
            )
            closed = await lifecycle.close_turn(conn, op, follow_up=None, on=True)
            commit_report = {
                **closed.model_dump(mode="json"),
                "salience": {
                    "entries": sal.entries,
                    "recorded": [
                        {"beat": r.beat, "outcome": r.outcome, "thunk_id": r.thunk_id}
                        for r in sal.recorded
                    ],
                    "degradation": list(sal.degradation),
                },
                "canonized": len(canonized),
            }
            for step in closed.degradation:
                checked.degradation.append((step, "commitment"))
        else:
            commit_report = {"canonized": len(canonized)}
        fixed["commit"] = commit_report
        await write_report(conn, rendered, checked)

    async def write_report(conn: AsyncConnection, rendered: Rendered, checked: Checked) -> None:
        fixed["elapsed_ms"] = budget.elapsed_ms()
        report = lite.turn_report(
            calls,
            budget,
            gated[0] if gated else None,
            fixed["elapsed_ms"],
            queued_ms=timing.queued_ms,
            first_call_offset_ms=timing.first_call_offset_ms(calls),
        )
        steps = _degradation(gated[0] if gated else None, rendered, checked)
        report["degradation"] = [s.step for s in steps]
        report["degradation_details"] = [{"step": s.step, "cause": s.cause} for s in steps]
        report["render"] = {
            "attempts": rendered.attempts,
            "structure_retries": rendered.structure_retries,
            "introductions": len(rendered.introductions),
            "introductions_denied": rendered.denied_introductions,
            "age_repairs": rendered.age_repairs,
            "narrator": rendered.narrator,
        }
        report["detection"] = extraction.detection_report(checked.outcomes, checked.checked)
        if checked.verifier is not None:
            report["verify"] = checked.verifier.report()
            report["uncertain_claims"] = checked.verifier.uncertain_claims()
        report["commit"] = fixed.get("commit", {})
        extraction.record_metrics(checked.outcomes, checked.checked)
        await conn.execute(
            update(operations)
            .where(operations.c.operation_id == op.operation_id)
            .values(turn_report=report)
        )
        await index_records(
            conn,
            [
                operation_record(
                    SimpleNamespace(
                        operation_id=op.operation_id, story_id=story_id, turn_report=report
                    )
                )
            ],
        )
        if extraction.shadow_owed(checked.outcomes):
            await extraction.enqueue_shadow(conn, story_id, op.operation_id)

    async def result(conn: AsyncConnection | AsyncEngine, committed: Operation) -> RespondResult:
        beats = await turn_beats(conn, story_id, committed.operation_id, viewpoint)
        reply = next(item for item in beats if item.kind == "message_rendered")
        assert committed.narrative_turn is not None  # noqa: S101 - a committed turn's position
        assert committed.turn_variant is not None  # noqa: S101
        assert committed.timeline_revision is not None  # noqa: S101
        outcome = gated[0] if gated else None
        fields: dict[str, Any] = {
            "message": FinishedMessage(message_id=reply.message_id, text=reply.text),
            "beats": beats,
            "turn": committed.narrative_turn,
            "variant": committed.turn_variant,
            "timeline_revision": committed.timeline_revision,
            "output_viewpoint": viewpoint,
            "model_calls": list(calls.calls),
            "elapsed_ms": fixed.get("elapsed_ms", budget.elapsed_ms()),
            "call_counts": calls.by_purpose(),
            "budget": BudgetReport(**budget.report()),
            "queued_ms": timing.queued_ms,
            "first_call_offset_ms": timing.first_call_offset_ms(calls),
            **lite.guardrail_fields(outcome),
            **lite._stamp_fields(stamped[0] if stamped else None),
            "profile": ProfileVersionRef(id=captured.profile_id, version=captured.version),
            "config_revision": configuration.config_revision,
        }
        steps = _degradation(
            outcome, released[0] if released else None, found[0] if found else None
        )
        fields["degradation"] = [s.step for s in steps]
        fields["degradation_details"] = steps
        return RespondResult(**fields)

    call = idempotency.current_call()
    if call is not None:
        call.result_in_commit = result
    await run_with_revalidation(
        op,
        begin=begin,
        generate=generate,
        read_set=read_set,
        config_recheck=output_recheck(db, models, budget),
    )
    timing.observe(calls)
    if call is not None and call.stored_in_commit:
        stored: RespondResult = call.committed_value
        return stored
    return await result(db, op)


def _with_context(draft: Draft, retrieval: RetrievalResult) -> Draft:
    """``draft`` carrying, beside what it already holds, the records and pins ``retrieval``
    placed in its prompt (FR-GUARD-5)."""
    found = draft_context(retrieval, draft)
    return Draft(
        text=draft.text,
        model=draft.model,
        prompt_hash=draft.prompt_hash,
        finish_reason=draft.finish_reason,
        context=tuple(dict.fromkeys((*draft.context, *found.context))),
        pins=tuple(dict.fromkeys((*draft.pins, *found.pins))),
    )


def _relation(claims: Sequence[AssertedClaim], kind: str) -> str:
    from epistrel.core.turns.beats import objective_relation_of

    del kind
    return objective_relation_of(claims)


def _degradation(
    outcome: Gated | None, rendered: Rendered | None, checked: Checked | None
) -> list[DegradationStep]:
    """Every stage's degradation with its cause: the output gate's (``guardrail_fields``),
    retrieval's, detection's, verification's, the latent routing's, salience's and the
    ledger's, each once."""
    steps = list(lite._degradation_details(outcome))
    seen = {s.step for s in steps}
    extra: list[tuple[str, str]] = []
    if rendered is not None:
        for retrieval in rendered.retrievals:
            extra += [(d, "retrieval") for d in retrieval.degradation]
        if rendered.denied_introductions:
            extra.append((INTRODUCTION_DENIED, "allow_new_arrivals"))
    if checked is not None:
        extra += checked.degradation
    for step, cause in extra:
        if step not in seen:
            seen.add(step)
            steps.append(DegradationStep(step=step, cause=cause))
    return steps


def _episodic(
    checked: Checked,
    read: TurnRead,
    *,
    say: str | None,
    do: str | None,
    ids: Mapping[int, str],
) -> list[records.Beat]:
    """The episodic writer's beats (#135): the player's ``say`` and ``do``, and every
    observable beat, each with its occasion (``beat_occasion``); a ``latent`` that declared
    nothing normalizable is kept as an ``objective`` memory (``latent_unnormalized``)."""
    found: list[records.Beat] = []
    input_id = ids.get(1)
    mentions = checked.mentions.get(1, ())
    for kind, text in ((SAY, say), (DO, do)):
        if text and input_id is not None:
            event_type, participants = beat_occasion(text, mentions)
            found.append(
                records.Beat(
                    cast(records.BeatKind, kind),
                    text,
                    input_id,
                    beat=1,
                    character_id=read.actor.id,
                    provenance=RecordProvenance.PLAYER,
                    event_type=event_type,
                    participants=_participants(participants, read.actor.id),
                )
            )
    for beat in checked.beats:
        event_id = ids.get(beat.beat)
        if event_id is None:
            continue
        kind = beat.kind
        if kind == LATENT:
            if any(c.proposition is not None for c in beat.claims):
                continue
            kind = NARRATION  # an unnormalized latent: an objective memory
        if kind not in OBSERVABLE_KINDS:
            continue
        event_type, participants = beat_occasion(beat.text, checked.mentions.get(beat.beat, ()))
        found.append(
            records.Beat(
                cast(records.BeatKind, kind),
                beat.text,
                event_id,
                beat=beat.beat,
                character_id=None if kind == NARRATION else beat.character_id,
                provenance=RecordProvenance.DIRECTOR
                if kind == NARRATION
                else RecordProvenance.RENDER,
                event_type=event_type,
                participants=_participants(participants, beat.character_id),
            )
        )
    return found


def _participants(found: Sequence[str], actor: str | None) -> tuple[str, ...]:
    return tuple(p for p in found if p != actor)


__all__ = [
    "INTRODUCTION_DENIED",
    "OPEN_REFERENCE",
    "SCENE_CHANGED",
    "UNADDRESSED",
    "Checked",
    "Rendered",
    "StagedSource",
    "TurnRead",
    "compose",
    "is_structured",
    "play",
    "read_turn",
    "resolve_structured_cast",
    "respond",
    "run_queued",
]
