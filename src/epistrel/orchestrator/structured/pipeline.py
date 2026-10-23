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
earlier one established (the same-turn overlay). One ``Introductions`` per turn (#479,
``core.verify.introduce``; shared by the follow-up's and the repairs' re-checks and the composed
message's detection) mints an id for an untracked place or object a claim names. A ``say``'s
labels follow
(``core.turns.beats``): ``objective_relation`` from the verdicts, ``belief_relation``
``unknown``, ``intent`` as declared. Salience (#146) reads every render's observable beats.

**Stage and commit** (one transaction): the player's input at beat 1 (with its labels,
propositions and mentions), the rejected drafts as ``guardrail_output_rejected`` (#145's payload
v2), each beat as its ``beat_<kind>`` event (#150's registered types; narrator beats first, then
the character's), a ``character_defined`` per introduction, the composed message (a
``message_rendered`` event with ``composed`` after the beats) and, when the polish was rejected,
its ``composition_rejected`` audit record. The commit steps write, in order: the introduced
characters and their canon ``age`` facts; the permitted canon
(``core.canon.canonize.canonize_all``, each request with the minted entities its claim
names, ``canon_requests``: only a canonized claim creates one) and every ``latent`` beat's
propositions with its declared visibility (FR-TURN-4); the utterances; the episodic
memories of the observable beats (``write_episodic``, with each beat's occasion from
``beat_occasion``; under per-character partitions with perception on, #161's routing instead:
``route_committed`` routes every beat at the level current at commit and the memory sink writes
one memory per perceiver, the qualifiers having been validated before staging,
``validate_perception``); the salient references (``record_salience``), the dependences
(``apply_dependences`` → ``lifecycle.depend``), the incidental details (FR-COMMIT-9:
``detail_claims`` → ``boundary.depend_on_detail``, a novel ``think``/``do`` claim about an
attribute a ``consequential`` fact leaves open, never canonized itself) and the turn's close
(``close_turn``, its ``follow_up`` seam); and ``operations.turn_report`` (#69's report plus
``render``, ``detection``, ``verify``, ``uncertain_claims``, ``commit`` and ``composition``).

**Compose** (#152, ``orchestrator.compose``, after the repair ladder and before the commit
transaction: an ordinary turn commits its beats and its response together, docs/02 §7): the
post-repair beats as the resolved viewpoint receives them are polished by one ``generation``
call (``composition.j2``) and the polished text passes #68's output gate and the final check
(#148's detection on the composed text, every claim traced to a beat, attribution, no private
thought for a character viewpoint); otherwise, or when the polish is off, skipped under budget
pressure or the turn has one beat, the message is the deterministic assembly of the beats. The
composed message's detection call (C) is reserved at planning (#156) and released when the
assembly is used. A composition failure changes no beat: the events are
the same either way.

**The result** is M3's ``RespondResult``: the composed message, the turn's beats for the
resolved viewpoint (a character viewpoint never gets ``think`` or ``latent``; an omniscient one
gets them, labelled), and the degradation of every stage with its cause (the output gate's,
retrieval's, detection's, verification's, salience's and the ledger's).

A render that stays malformed after #53's retries fails the turn: 503 ``model_unavailable``,
``details.reason = invalid_structure`` (``age_unrepaired`` for the age repair) with
``details.structural_errors``; the operation ends ``failed`` and nothing is recorded.

**Repair** (#151, ``repair_turn``, after the follow-up render and before the commit): when the
profile's ``repair`` module is on, every rendered beat with a claim that ``needs_repair``
(contradicted, undecided under the ``repair`` disposition, or an ``unpermitted`` novel claim;
not a ``say`` declared deceptive or joking) goes to ``orchestrator.repair.ladder.RepairLadder``;
the correction it accepts (re-detected and re-verified) replaces the beat at its position with
``repaired_from {rung, class}``, the rejected beat's canon requests are dropped, and the commit
stages each repaired beat's ``verify_verdict`` and every rejected text as ``render_rejected``
(both ``audit``), with ``turn_report.repairs`` and ``epistrel_repairs_total``. With the module
off a contradicted beat stands, labelled.

**Regenerate** (#154, ``regenerate``): the head turn's recorded input is played again as a new
variant. Its reads leave the replaced variant's records out (``core.membership.excluding``), so
the render, retrieval, verification and the thunk reads see the story as it stood before the
turn; the commit retires the old variant (``dormant``) and ``core.commit.boundary`` reverts what
it committed. ``turn_report.regenerated_variant`` names the variant replaced (FR-FB-3) and
``turn_report.commit`` counts ``reverted_facts`` and ``reopened_thunks``. The turn's check runs
#154's resolution screen (``orchestrator.commit.resolution``): a binding of an open thunk that
conflicts with recorded history is a contradiction the repair ladder steers away from
(``reconciliations``, ``rejected_bindings``); the renders carry the deferral bias while an
earlier turn's thunk is open (``prompt.RESOLUTION_STEERING``).

**Budget** (#156, ``orchestrator.structured.planner``): the turn is planned before its first
model call (``plan_structured``: one primary, B one extraction per planned render, C the
composed message's detection, G the model-assisted guardrail calls; a cap below ``1 + B + C +
G`` is 422 ``budget_below_safety_minimum`` with nothing recorded). B and C are owner-only
(``TurnBudget.named``); the player's input is detected by layers (a) and (b) only; unused B is
released once the renders are detected, C by the deterministic assembly. A triggered narrator
reserves its render and extraction or is skipped (``narrator_skipped``); the age repair
displaces optional work (503 ``age_unrepaired`` at the exact minimum); salience, entailment,
the follow-up render, a model repair and the polish are optional work (``try_optional``), given
up in NFR-PERF-7's order (``skip_polish``, ``repair_deterministic``,
``entailment_safe_disposition``; ``salience_skipped`` leaves every beat texture); a render
whose reserved extraction cannot start before the deadline is replaced by the safe fallback
(``beat_replaced_safe_fallback``). No beat commits without a detection record
(``undetected_beats``). ``turn_report.budget`` adds ``minimum``, ``reservation``, ``released``,
``used_by_kind`` and ``plan``.

Logs carry ids and counts only (invariant 3).
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
from epistrel.core.commit import boundary, lifecycle, matching
from epistrel.core.commit import epistemic as knowledge_flags
from epistrel.core.commit.sketch import ThunkSketch, fills, open_sketch
from epistrel.core.concurrency import check_expected_revision, run_with_revalidation
from epistrel.core.config_changes import read_section
from epistrel.core.context import RequestContext
from epistrel.core.enums import (
    OperationKind,
    OperationStatus,
    Provenance,
    RecordProvenance,
    StoryStatus,
)
from epistrel.core.epistemic import exposure as exposure_records
from epistrel.core.epistemic import modeling as char_modeling
from epistrel.core.epistemic import observation as _observation  # noqa: F401 - registers the sink
from epistrel.core.epistemic import perception, replay
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.epistemic import transitions as belief_transitions
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import operation_record
from epistrel.core.errors import InvalidRequest, RevisionConflict, StoryArchived, TimelineChanged
from epistrel.core.events import Operation
from epistrel.core.events.types import (
    BeatLabels,
    CompositionRejected,
    MessageRendered,
    PlayerInput,
    RenderRejected,
    VerifyVerdict,
)
from epistrel.core.genesis.structured import deferred_items
from epistrel.core.history import turn_beats
from epistrel.core.membership import active_operation, beat_steps, excluding
from epistrel.core.memory import records
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Target
from epistrel.core.profiles.capabilities import level_at_least, module_on
from epistrel.core.profiles.effective import ProfileVersionRef, StoryProfile
from epistrel.core.profiles.fingerprint import Stamp
from epistrel.core.profiles.reservation import extraction_on
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.registry import Registry, canonical, consider_proposals
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.context import load_context
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
    CHARACTER_VIEWPOINT_PREFIX,
    OMNISCIENT,
    ResolvedGeneration,
    VerifySettings,
    resolve_generation,
)
from epistrel.core.tables import entity_aliases, events, facts, operations, stories
from epistrel.core.tables.characters import TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.tables.commitments import thunks
from epistrel.core.tables.operations import REGENERATED_VARIANT
from epistrel.core.tables.propositions import entities
from epistrel.core.turn_gate import Concurrency
from epistrel.core.turns import (
    CastEntry,
    RecordedTurn,
    StoryCast,
    TurnInputs,
    joined_input,
    read_regenerable,
    read_story_cast,
    read_turn_inputs,
)
from epistrel.core.turns.assembly import PRIVATE_KINDS
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
    write_character_facts,
    write_introductions,
    write_utterances,
)
from epistrel.core.verify import attribution
from epistrel.core.verify.detect import DeclaredClaim, DetectBeat, DetectedClaim
from epistrel.core.verify.introduce import Introductions
from epistrel.core.verify.normalize import ClaimSketch, Unnormalizable, normalize_declared
from epistrel.core.verify.rules import CANON, Record, VerdictKind, check
from epistrel.core.viewpoint import (
    character_viewpoint,
    is_omniscient,
    resolve_viewpoint,
    try_resolve,
)
from epistrel.observability import stages
from epistrel.observability.logging import get_logger
from epistrel.observability.metrics import (
    CHARACTERS_MODELED,
    count_perception_repair,
    count_repair,
)
from epistrel.orchestrator.budget import (
    EXTRACTION,
    FOLLOW_UP,
    GUARDRAIL_OUTPUT,
    SALIENCE,
    TurnBudget,
    counted_embedder,
)
from epistrel.orchestrator.commit import resolution, salience
from epistrel.orchestrator.commit.salience import SALIENCE_SKIPPED
from epistrel.orchestrator.compose import compose as composing
from epistrel.orchestrator.epistemic import adjudicate, speech
from epistrel.orchestrator.lite import pipeline as lite
from epistrel.orchestrator.lite.cast import ResolvedCast, resolve_cast
from epistrel.orchestrator.lite.prompt import prompt_hash
from epistrel.orchestrator.lite.schemas import (
    BudgetReport,
    DegradationStep,
    FinishedMessage,
    RegenerateRequest,
    RegenerateResult,
    RespondRequest,
    RespondResult,
)
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import routes_of
from epistrel.orchestrator.models.errors import ModelError, ModelUnavailable
from epistrel.orchestrator.models.facts import runtime_facts
from epistrel.orchestrator.models.metrics import CallLog, recording
from epistrel.orchestrator.models.registry import GenerationInputs, SceneContext
from epistrel.orchestrator.models.registry import PlayerInput as PromptPlayer
from epistrel.orchestrator.policies.check import refuse_withheld
from epistrel.orchestrator.policies.implication import Implicator, draft_context, threshold_of
from epistrel.orchestrator.policies.output_gate import (
    Draft,
    FallbackLines,
    Gated,
    gate_output,
    output_policies,
    raised_temperature,
    regeneration_cost,
    rejected_event,
)
from epistrel.orchestrator.policies.rebase import output_passes, output_recheck
from epistrel.orchestrator.policies.store import guard_context
from epistrel.orchestrator.repair.ladder import Recheck, RepairLadder, RepairResult
from epistrel.orchestrator.structured import planner
from epistrel.orchestrator.structured import prompt as prompts
from epistrel.orchestrator.structured import steps as render_steps
from epistrel.orchestrator.structured.eval_hooks import StageClock, eval_hooks
from epistrel.orchestrator.structured.render import (
    AGE_UNREPAIRED,
    STRUCTURED,
    BeatDecl,
    IntroductionDecl,
    PropositionSketch,
    RenderedBeats,
    StepDecl,
    hold_length,
    length_chars,
    length_line,
    render_beats,
    render_params,
    render_schema,
    repair_ages,
)
from epistrel.orchestrator.structured.render import (
    NARRATOR as NARRATOR_VARIANT,
)
from epistrel.orchestrator.verify import extraction, yields
from epistrel.orchestrator.verify.extraction import (
    CheckedClaim,
    ClaimDetector,
    DetectionConfig,
    DetectionOutcome,
)
from epistrel.orchestrator.verify.verifier import (
    ENTAILMENT_BUDGET,
    ClaimSource,
    TimeFrame,
    Verifier,
    story_verifier,
)

log = get_logger(__name__)

PIPELINE = "pipeline"
REPAIR_MODULE = "repair"
STRUCTURED_LEVEL = "structured"
QUERY_BEATS = 3
"""The retrieval query: the player's input and the rendering character's last beats."""
SCENE_CHANGED = "scene_changed"
UNADDRESSED = "unaddressed"
OPEN_REFERENCE = "open_reference"
CONTRADICTED_LATENT = "latent_contradicted"
INTRODUCTION_DENIED = "introduction_denied"
SAY, DO, LATENT, NARRATION, THINK = "say", "do", "latent", "narration", "think"


def is_structured(configuration: StoryProfile) -> bool:
    """Whether the story's effective configuration runs the structured pipeline (#129)."""
    return level_at_least(configuration.settings, PIPELINE, STRUCTURED_LEVEL)


# -- the cast -----------------------------------------------------------------------------------


def resolve_structured_cast(story: StoryCast, request: RespondRequest) -> ResolvedCast:
    """The actor and the one character rendered (M4's one primary, #156): the request's, or
    the first non-player, non-``minor`` character after the actor by ``ordinal`` (#163; else the
    first non-player one, else the first other one, else the actor). Two or more named are 422
    ``validation_error``."""
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
    # #163: a minor (lazily modeled) character is never the default render character.
    chosen = next(
        (c for c in others if not c.is_player and not c.minor),
        next((c for c in others if not c.is_player), others[0] if others else resolved.actor),
    )
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
    plan, budget = await planner.plan_structured(
        db,
        story_id,
        resolved_cast,
        request,
        lite._captured(concurrency),
        story_max_calls=concurrency.max_model_calls_per_turn,
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
                    plan=plan,
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
    plan, budget = await planner.plan_structured(
        db,
        story_id,
        resolved_cast,
        request,
        lite._captured(concurrency),
        story_max_calls=planned.get("max_calls", concurrency.max_model_calls_per_turn),
        turn_deadline=planned.get("deadline_s") or concurrency.turn_deadline,
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
            plan=plan,
        )


async def regenerate(
    db: AsyncEngine,
    models: ModelAccessLayer,
    ctx: RequestContext,
    story_id: str,
    request: RegenerateRequest,
    *,
    principal: str,
    gate: lite.Gate,
    lexical: LexicalSearch | None = None,
) -> RegenerateResult:
    """Regenerate a structured story's head turn (FR-COMMIT-8, FR-STORE-12; #66, #154): the
    recorded input played again as a new variant, reading the story as it stood before the turn
    (``core.membership.excluding``); the commit retires the old variant to ``dormant``, and
    ``core.commit.boundary`` reverts what it committed (its facts ``revised``, the thunks it
    resolved open again) in the same transaction. The prechecks are Lite's
    (``lite.regenerate``)."""
    started = ctx.received_at if ctx.received_at is not None else time.monotonic()
    story = await read_story_cast(db, story_id)
    if story.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )
    await pause.refuse(db, story_id, story.status)
    recorded = await read_regenerable(db, story_id, request.turn)
    replay = lite._replayed_request(recorded, request)
    resolved_cast = resolve_structured_cast(story, replay)
    await check_expected_revision(db, story_id, ctx.expected_revision)
    viewpoint = resolve_viewpoint(
        ctx.viewpoint or recorded.viewpoint,
        actor_character_id=resolved_cast.actor.id,
        story_default=story.settings.default_output_viewpoint,
        cast_ids=story.ids,
    )
    concurrency = await turn_gate.read_concurrency(db, story_id)
    plan, budget = await planner.plan_structured(
        db,
        story_id,
        resolved_cast,
        replay,
        lite._captured(concurrency),
        story_max_calls=concurrency.max_model_calls_per_turn,
        turn_deadline=concurrency.turn_deadline,
        override=request.budget,
        started=started,
    )
    timing = lite.TurnTiming(front=started)
    with recording() as calls, lite.reporting_costs(calls, budget), lite._routed(concurrency):
        gating = time.perf_counter()
        await gate(
            budget,
            joined_input(recorded.say, recorded.do),
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
            turn=recorded.turn,
            idempotency_key=ctx.idempotency_key,
            viewpoint=viewpoint,
        )
        held = await turn_gate.acquire(db, story_id, op.operation_id, lease_s=concurrency.lease_s)
        if held is not None:
            raise held.busy(story_id)
        op.holds_turn_slot = True
        try:
            async with turn_gate.renewing(db, story_id, op.operation_id, concurrency.lease_s):
                played = await play(
                    db,
                    models,
                    story_id,
                    replay,
                    resolved_cast,
                    viewpoint,
                    op,
                    budget,
                    calls,
                    story=story,
                    configuration=lite._captured(concurrency),
                    timing=timing,
                    lexical=lexical,
                    regenerating=recorded,
                    plan=plan,
                )
        finally:
            if op.status is None:  # took the slot but never began
                await turn_gate.release_now(db, story_id, op.operation_id)
    assert isinstance(played, RegenerateResult)  # noqa: S101 - play built it for a regenerate
    return played


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
    deferred: bool = False
    """#154 (FR-COMMIT-11): an open thunk an earlier turn introduced is in the context, so the
    renders carry the deferral bias (``prompts.RESOLUTION_STEERING``)."""
    flagged: bool = False
    """#165 (FR-COMMIT-11): the previous turn's resolution raised a could-have-known soft flag,
    so this turn's renders (only) carry ``prompts.KNOWLEDGE_STEERING``."""


async def _deferred(conn: AsyncConnection, story_id: str, turn: int) -> bool:
    """Whether the story has an open thunk introduced before ``turn``."""
    t = thunks.alias("t")
    found = await conn.scalar(
        select(t.c.id)
        .where(
            t.c.story_id == story_id,
            lifecycle.open_clause(t),
            t.c.narrative_turn.is_not(None),
            t.c.narrative_turn < turn,
        )
        .limit(1)
    )
    return found is not None


async def _flagged(conn: AsyncConnection, story_id: str, turn: int) -> bool:
    """Whether the active turn before ``turn`` reported a could-have-known soft flag (#165)."""
    o = operations.alias("flagged_turn")
    flags = o.c.turn_report["commit"]["could_have_known_flags"]
    found = await conn.scalar(
        select(flags)
        .where(
            o.c.story_id == story_id,
            o.c.narrative_turn == turn - 1,
            active_operation(o),
            flags.is_not(None),
        )
        .limit(1)
    )
    return bool(found)


def _steering(read: TurnRead) -> str:
    """The renders' ``resolution_steering`` line(s): the deferral bias (#154) and the knowledge
    steer after a could-have-known flag (#165)."""
    lines = [
        prompts.RESOLUTION_STEERING if read.deferred else "",
        prompts.KNOWLEDGE_STEERING if read.flagged else "",
    ]
    return " ".join(x for x in lines if x)


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
    regenerating: int | None = None,
) -> TurnRead:
    """The structured turn's inputs, read in its snapshot (see the module docstring); for a
    regenerate of turn ``regenerating`` (#154), what is positioned before it (its variant's own
    records are left out of every derived read by ``core.membership.excluding``)."""
    settings = configuration.settings
    inputs = await read_turn_inputs(
        conn, story_id, before_turn=regenerating, configuration=configuration
    )
    resolved = resolve_generation(inputs.generation, request.prompt_params)
    head = int(
        await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id)) or 1
    )
    if regenerating is not None:
        head = regenerating
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
    # By name: the scene lists them by id, which is random across runs (#154, NFR-DETERM-1).
    present = tuple(sorted(e.name for e in scene.present)) if scene is not None else ()
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
        deferred=lifecycle.commitment_enabled(settings) and await _deferred(conn, story_id, turn),
        flagged=lifecycle.commitment_enabled(settings) and await _flagged(conn, story_id, turn),
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
    character_inputs: GenerationInputs | None = None
    """The rendering character's ``generation`` inputs (a repair's regeneration edits in place
    from them, #151)."""
    narrator_inputs: GenerationInputs | None = None
    """The narrator render's ``generation`` inputs, when the narrator rendered."""
    steps: list[StepDecl] = field(default_factory=list)
    """The rendering character's declared causal steps (#168, FR-TURN-12)."""
    steps_beat: int = 0
    """The character render's first beat: where its steps sit (``steps.position_of``)."""


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
                qualifiers=_qualifiers_of(decl),
                cites=tuple(decl.cites) if decl.kind == THINK else (),
                step=decl.step or 0,
            )
        )
        sketches[number] = list(decl.propositions)
    return beats, sketches


def _qualifiers_of(decl: BeatDecl) -> perception.Qualifiers:
    """A rendered beat's perception qualifiers (#161), as declared (validated at routing)."""
    channel = decl.remote_channel
    return perception.Qualifiers(
        addressee=decl.addressee,
        manner=(decl.manner or "").strip() or None,
        whisper_to=tuple(decl.whisper_to),
        aside=decl.aside,
        eavesdrop_by=tuple(decl.eavesdrop_by),
        remote_channel=None
        if channel is None
        else perception.RemoteChannel(channel.channel_id, tuple(channel.participants)),
        language=decl.language,
        partial_text=(decl.partial_text or "").strip() or None,
        location=decl.location,
    )


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
        # #156: the age repair displaces optional work, never a reserved call; at the exact
        # minimum an ageless introduction cannot be repaired (#150's 503).
        if budget.expired() or not budget.can_afford_unreserved(1):
            error = ModelUnavailable("generation", AGE_UNREPAIRED, attempts=0)
            error.details["structural_errors"] = ["budget_exhausted"]
            raise error
        budget.hold(1)
        clues = [(d.name.strip(), _clue(d, bs)) for d, _, bs in wanted if d.age is None]
        with budget.charging("age_repair"):
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


# -- character modeling (#163) ------------------------------------------------------------------

MODELING = "modeling"
"""The budget kind of the modeling step's age repair (optional work: unfunded, the entities wait
for a later interaction, ``modeling_deferred``)."""


def _modeling_views(
    checked: Checked, read: TurnRead, *, say: str | None, do: str | None
) -> list[char_modeling.BeatView]:
    """The turn's beats as modeling reads them: the player's ``say``/``do`` (beat 1, read by
    name: its mentions are not split between the two texts) and every rendered observable beat
    with its resolved mentions."""
    views = [
        char_modeling.BeatView(kind, 1, text, read.actor.id)
        for kind, text in ((SAY, say), (DO, do))
        if text
    ]
    views += [
        char_modeling.BeatView(
            b.kind,
            b.beat,
            b.text,
            None if b.kind == NARRATION else b.character_id,
            tuple(checked.mentions.get(b.beat, ())),
        )
        for b in checked.beats
        if b.kind in (SAY, DO, NARRATION)
    ]
    return views


async def plan_modeling(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    read: TurnRead,
    checked: Checked,
    *,
    say: str | None,
    do: str | None,
    settings: ProfileSettings,
    budget: TurnBudget,
    sections: prompts.RenderSections,
) -> char_modeling.ModelingPlan | None:
    """The entities the turn models (FR-EPI-9; ``core.epistemic.modeling.candidates``), each
    with its true age from one batched age-repair call (the triggering beat's text and the
    descriptor as its clue): optional work, so an unaffordable or failed (implausible) repair
    leaves them unmodeled this turn (``modeling_deferred``). ``None`` without per-character
    partitions or while the story refuses new arrivals."""
    if not char_modeling.modeling_on(settings) or not read.allow_new_arrivals:
        return None
    async with db.connect() as conn:
        known = await char_modeling.load_entities(conn, story_id)
    found = char_modeling.candidates(
        _modeling_views(checked, read, say=say, do=do),
        known,
        salient=checked.salient_characters,
    )
    plan = char_modeling.ModelingPlan()
    if not found:
        return plan
    held = budget.held
    if not budget.try_optional(MODELING, 1):
        plan.deferred = found
        checked.degradation.append((char_modeling.MODELING_DEFERRED, "budget"))
        return plan
    bounds = {c.name: (TRUE_AGE_MIN, TRUE_AGE_MAX) for c in found}
    try:
        with budget.charging("age_repair"):
            ages = await repair_ages(
                models,
                prompts.age_inputs(sections, read.actor, [(c.name, c.clue) for c in found]),
                bounds,
                params=ModelParams(temperature=0.0),
                budget=budget,
            )
    except ModelError:
        plan.deferred = found
        checked.degradation.append((char_modeling.MODELING_DEFERRED, "modeling"))
        return plan
    finally:
        budget.release_to(min(budget.held, held))
    for candidate in found:
        age = ages.get(candidate.name.casefold())
        if age is None:
            plan.deferred.append(candidate)
        else:
            plan.ready.append(char_modeling.ModelRequest(candidate, age))
    if plan.deferred:
        checked.degradation.append((char_modeling.MODELING_DEFERRED, "modeling"))
    return plan


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

    async def time_frame(self) -> TimeFrame:
        return await self.inner.time_frame()

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
    salient_characters: list[str] = field(default_factory=list)
    """The figures #146's salience step marked as characters to model (#163)."""
    degradation: list[tuple[str, str]] = field(default_factory=list)
    staged: list[Record] = field(default_factory=list)
    """The overlay's staged claims after this check (a follow-up's check starts from them)."""
    follow_up: dict[str, Any] = field(default_factory=dict)
    """``turn_report.commit.follow_up``: whether the follow-up render ran, why not, and how
    many owed references it settled and left open."""
    follow_up_beats: frozenset[int] = frozenset()
    """The follow-up render's beat numbers."""
    follow_up_verifier: Verifier | None = None
    repairs: list[RepairResult] = field(default_factory=list)
    """What the repair ladder made of each beat it corrected (#151), in beat order."""
    rejected_bindings: list[resolution.RejectedBinding] = field(default_factory=list)
    """The bindings of open thunks reconciliation rejected (#154), in claim order."""
    repair_verifiers: list[tuple[int, Verifier]] = field(default_factory=list)
    """The verifier of each repair re-check (#151) with the rung whose candidate it checked:
    #155 records their escalations as the pass ``repair_<rung>``."""
    introductions: Introductions = field(default_factory=Introductions)
    """The turn's minted places and objects (#479): the commit creates those a canonized claim
    names (``canon_requests``)."""
    perception_repairs: list[perception.Repair] = field(default_factory=list)
    """The perception qualifiers dropped before staging (#161; FR-TURN-13), in beat order."""
    qualifier_rejected: list[tuple[TurnBeat, tuple[perception.Repair, ...]]] = field(
        default_factory=list
    )
    """Each beat as rendered whose qualifiers were repaired, with its repairs (#161): staged as
    ``render_rejected`` (rung 1)."""
    adjudications: adjudicate.Adjudications | None = None
    """The salient witnesses' testimony adjudications (#162), by ``(beat, witness)``: the commit
    applies them to the default it recomputes."""
    modeling: char_modeling.ModelingPlan | None = None
    """The entities the turn models (#163, ``plan_modeling``): their ages found, or deferred."""
    steps: list[render_steps.Step] = field(default_factory=list)
    """The rendering character's declared steps, normalized (#168)."""
    step_verdicts: dict[int, render_steps.StepVerdict] = field(default_factory=dict)
    """Each step's verdict (#168; empty with verification off)."""
    steps_beat: int = 0
    """The character render's first beat (where its steps sit)."""


LOCATED_PREDICATES = frozenset({CORE_BY_NAME["located_at"].id, CORE_BY_NAME["present_at"].id})
"""A located fact (#168): observing it needs a perception path to its place (#161)."""


def _stepper(
    conn: AsyncConnection,
    story_id: str,
    read: TurnRead,
    verifier: Verifier,
    steps: Sequence[render_steps.Step],
    *,
    first_beat: int,
    operation_id: str,
    read_set: StructuredReadSet,
) -> render_steps.StepVerifier | None:
    """The render's step verifier (#168; ``None``: no steps, or verification off): an
    ``observe`` is checked deterministically against the character's canon view through the
    turn's overlay, at the render's first beat, with a perception path for a located fact; a
    verified step joins the speaker's belief record set and the replay's staged paths."""
    if not steps or not verifier.config.on:
        return None
    character = read.character.id
    viewpoint = f"{CHARACTER_VIEWPOINT_PREFIX}{character}"
    scenes: list[perception.SceneState] = []

    async def located(claim: Proposition) -> bool:
        if claim.predicate_id not in LOCATED_PREDICATES or claim.object_id is None:
            return True
        if not scenes:
            scenes.append(
                await scene_loader.load_scene_state(conn, story_id, read.turn, read_set=read_set)
            )
        scene = scenes[0]
        here = scene.at(character)
        if here is None:
            return False
        return claim.object_id in scene.ancestry(here) or here in scene.ancestry(claim.object_id)

    async def observe(step: render_steps.Step) -> render_steps.Observed:
        claim = step.proposition
        if claim is None:  # pragma: no cover - checked by the verifier
            return render_steps.Observed(False, render_steps.UNNORMALIZABLE)
        found = await verifier.source.deterministic(
            claim, holder=CANON, viewpoint=viewpoint, position=(read.turn, first_beat)
        )
        if found is None or found[0].kind is not VerdictKind.SUPPORTED:
            return render_steps.Observed(False, render_steps.UNVERIFIED_OBJECT)
        if not await located(claim):
            return render_steps.Observed(False, render_steps.NO_PERCEPTION_PATH)
        verdict, _ = found
        beats_of = {f"staged:{r.claim_id}": r.beat for r in verifier.canon_requests() if r.claim_id}
        for evidence in verdict.evidence:
            if evidence.fact_id is not None:
                event = await conn.scalar(
                    select(facts.c.source_event_ids[1]).where(
                        facts.c.story_id == story_id, facts.c.id == evidence.fact_id
                    )
                )
                return render_steps.Observed(
                    True,
                    event_id=None if event is None else str(event),
                    fact_id=evidence.fact_id,
                )
            if evidence.proposition_id in beats_of:
                return render_steps.Observed(True, beat=beats_of[evidence.proposition_id])
        return render_steps.Observed(True)

    async def probe(record_id: str) -> Any:
        found = await render_steps.probe_any(conn, story_id, record_id, operation_id=operation_id)
        if found is not None and found.table == "beliefs" and found.holder_id != character:
            return None  # a belief antecedent is the holder's own
        return found

    beliefs = verifier.beliefs
    variant = 0 if beliefs is None else beliefs.variant

    def verified(step: render_steps.Step, verdict: render_steps.StepVerdict) -> None:
        del verdict
        if step.proposition is None or step.kind == render_steps.ACT:
            return
        at = render_steps.position_of(read.turn, variant, first_beat, step.index)
        if beliefs is not None:
            beliefs.stage(character, step.proposition, at, str(step.index))
        replay.stage_path(character, step.proposition, at, f"step:{step.index}")

    return render_steps.StepVerifier(steps, observe, probe, on_verified=verified)


def _self_attested(
    checked: Sequence[CheckedClaim],
    steps: Sequence[render_steps.Step],
    verdicts: Mapping[int, render_steps.StepVerdict],
    beats: Sequence[TurnBeat],
    registry: Registry,
) -> list[CheckedClaim]:
    """#168 (FR-TURN-12, FR-VERIFY-10): a rejected ``believe`` step's proposition, asserted in
    the beats of the first ``act`` step citing it, is self-attested: a hallucination #151
    repairs, never written."""
    marked: list[tuple[set[int], Proposition]] = []
    for step in steps:
        verdict = verdicts.get(step.index)
        if step.kind != render_steps.BELIEVE or verdict is None or verdict.verified:
            continue
        if step.proposition is None:
            continue
        act = render_steps.first_citing_act(steps, step.index)
        if act is None:
            continue
        numbers = {b.beat for b in beats if not b.narrator and b.step == act}
        marked.append((numbers, canonical(step.proposition, registry)))
    if not marked:
        return list(checked)
    found: list[CheckedClaim] = []
    for c in checked:
        claim = c.claim.proposition
        if c.result is not None and claim is not None:
            stored = canonical(claim, registry)
            if any(
                c.claim.beat in numbers
                and stored.normalized_key() == rejected.normalized_key()
                and stored.polarity is rejected.polarity
                for numbers, rejected in marked
            ):
                c = replace(c, result=replace(c.result, self_attested=True))
        found.append(c)
    return found


def _verdict_of(result: CheckedClaim) -> str | None:
    return result.verdict


def _asserted(c: CheckedClaim) -> AssertedClaim:
    claim = c.claim
    speech = None if c.result is None else c.result.speech
    return AssertedClaim(
        proposition=claim.proposition,
        verdict=_verdict_of(c),
        claim_id=c.claim_id,
        declared=claim.layer == "declared",
        span=(claim.span[1], claim.span[2]),
        category=claim.category,
        belief_relation=None if speech is None else speech.decision.belief_relation,
        classification=None if speech is None else speech.classification,
        intent_corrected=speech is not None and speech.decision.intent_corrected,
        attribution=claim.attribution,
        attribution_objective=(
            None
            if c.result is None or c.result.attribution is None
            else c.result.attribution.objective
        ),
    )


async def _cued(
    conn: AsyncConnection,
    story_id: str,
    claim: DetectedClaim,
    beats: Mapping[int, TurnBeat],
    env: Any,
) -> DetectedClaim:
    """#167: a detected (not declared) claim of a character's ``say`` under an attribution cue
    of the attribution lexicon ("Bobby thinks …") is an attribution claim, its holder resolved
    in the speaker's view."""
    if claim.layer == "declared" or claim.attribution is not None or claim.channel != SAY:
        return claim
    beat = beats.get(claim.beat or -1)
    if beat is None or beat.character_id is None:
        return claim
    found = attribution.cue(beat.text, claim.span[1])
    if found is None:
        return claim
    resolved = await attribution.resolve_holder(
        conn, story_id, found, claim.speaker, env.context_for(claim.speaker)
    )
    return replace(claim, attribution=resolved)


def _sketch_text(sketch: PropositionSketch | None) -> str:
    """A step's proposition as words (its observation memory's line, #168)."""
    if sketch is None:
        return ""
    words = [sketch.subject, *(["not"] if sketch.negated else []), sketch.relation]
    return " ".join([*words, *([sketch.object] if sketch.object else [])])


def _sketch(sketch: PropositionSketch) -> ClaimSketch:
    return ClaimSketch(
        subject_text=sketch.subject,
        relation_text=sketch.relation,
        object_text=sketch.object,
        time_text=sketch.time,
        polarity=Polarity.NEGATED if sketch.negated else Polarity.AFFIRMED,
        qualifiers=dict(sketch.qualifiers),
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
    **kwargs: Any,
) -> Checked:
    """``_check_turn`` with the render's beats at their declared steps (#168: ``step_of``) and
    its verified steps staged for the could-have-known replay (``replay.staged_paths``)."""
    owners = render_steps.beat_steps([b for b in rendered.beats if not b.narrator])
    with beat_steps(str(kwargs["operation_id"]), owners), replay.staged_paths():
        return await _check_turn(db, models, story_id, read, rendered, **kwargs)


async def _check_turn(
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
    staged_before: Sequence[Record] = (),
    first_claim: int = 0,
    with_salience: bool = True,
    backstop: bool = True,
    answers_from: Sequence[Verifier | None] = (),
    clock: StageClock | None = None,
    introductions: Introductions | None = None,
) -> Checked:
    """Detect and verify the player's input and every render's beats (see the module
    docstring). A follow-up render's check (``first_claim``: where its claim ids start) sees
    the claims the turn's earlier beats staged (``staged_before``) and reads no salience. A
    repair's re-check (#151) runs without the model backstop when ``backstop`` is off (layers
    a and b only) and reuses the entailment answers of ``answers_from``, the turn's verifiers.
    ``clock`` (#155) takes the time of its normalize, detection, verify and entailment
    stages. ``introductions`` are the turn's minted places and objects (#479; a follow-up's and
    a repair's re-check share the turn's, so one name is one id)."""
    clock = clock or StageClock()
    detecting = time.perf_counter()
    settings = configuration.settings
    routes = routes_of(settings)
    turn = read.turn
    input_speaker = PLAYER if read.actor.id == read.player_id else read.actor.id
    async with db.connect() as conn:
        config = DetectionConfig.of(settings, await read_section(conn, VerifySettings))
        if not backstop:
            config = replace(config, level=DECLARED_LEVEL)
        speakers = [input_speaker, read.character.id]
        env = await extraction.load_env(
            conn, story_id, read_set, position=(turn, 0), speakers=speakers, config=config
        )
    introductions = Introductions() if introductions is None else introductions
    env = replace(env, introductions=introductions)
    clock.add("detection", time.perf_counter() - detecting)
    # The declared propositions, normalized (an unregistered relation registers, in a short
    # transaction of its own).
    declared: dict[int, tuple[DeclaredClaim, ...]] = {}
    normalizing = time.perf_counter()
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
                    introduce=introductions,
                )
                env = replace(env, registry=registry)
                claim_id = f"d{beat.beat}.{n}"
                attributed = None
                if sketch.attribution is not None:
                    # #167: a claim about someone's belief; its holder named in the speaker's view.
                    attributed = await attribution.resolve_holder(
                        conn,
                        story_id,
                        attribution.ClaimAttribution(
                            sketch.attribution.holder.strip()
                            if sketch.attribution.holder.strip().casefold() != "i"
                            else attribution.SELF,
                            sketch.attribution.mode,
                            sketch.attribution.factive,
                        ),
                        speaker,
                        env.context_for(speaker),
                    )
                if isinstance(normalized, Unnormalizable):
                    found.append(
                        DeclaredClaim(claim_id, unnormalized=normalized, attribution=attributed)
                    )
                else:
                    found.append(
                        DeclaredClaim(claim_id, proposition=normalized, attribution=attributed)
                    )
            declared[beat.beat] = tuple(found)
        # #168: the character render's declared steps, normalized in the speaker's view at the
        # render's first beat.
        steps: list[render_steps.Step] = []
        for decl in rendered.steps:
            proposition: Proposition | None = None
            if decl.proposition is not None:
                normalized, registry = await normalize_declared(
                    conn,
                    story_id,
                    _sketch(decl.proposition),
                    read.character.id,
                    (turn, rendered.steps_beat),
                    registry=env.registry,
                    context=env.context_for(read.character.id),
                    basis=env.basis,
                    introduce=introductions,
                )
                env = replace(env, registry=registry)
                if not isinstance(normalized, Unnormalizable):
                    proposition = normalized
            steps.append(
                render_steps.Step(
                    index=decl.index,
                    kind=decl.kind,
                    step_cites=tuple(decl.step_cites()),
                    record_cites=tuple(decl.record_cites()),
                    proposition=proposition,
                    confidence=decl.confidence,
                    unnormalizable=decl.proposition is not None and proposition is None,
                    text=_sketch_text(decl.proposition),
                )
            )
    clock.add("normalize", time.perf_counter() - normalizing)
    detecting = time.perf_counter()
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
        # #156: the player's input is detected by layers (a) and (b) only; only a render's
        # extraction is reserved (B).
        is_input = bool(input_beats) and group is groups[0]
        async with db.begin() as conn:
            detector = ClaimDetector(
                replace(config, level=DECLARED_LEVEL) if is_input else config,
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
    # Every planned render was detected: what B did not spend goes back (#156).
    budget.release_named(EXTRACTION)
    mentions: dict[int, tuple[Mention, ...]] = {}
    for outcome in outcomes:
        for detected in outcome.detection.beats:
            if detected.beat is not None:
                mentions[detected.beat] = (*mentions.get(detected.beat, ()), *detected.mentions)
    clock.add("detection", time.perf_counter() - detecting)
    checked: list[CheckedClaim] = []
    verifier: Verifier | None = None
    requests: list[CanonRequest] = []
    verifying = time.perf_counter()
    async with db.connect() as conn:
        verifier = await story_verifier(conn, read_set, story_id, models=models, budget=budget)
        verifier.introductions = introductions
        for earlier in answers_from:
            if earlier is not None:
                verifier.adopt_answers(earlier)
        staged = StagedSource(verifier.source)
        staged.staged.extend(staged_before)
        verifier.source = staged
        by_beat: dict[int | None, list[DetectedClaim]] = {}
        scopes = {b.beat: b for b in rendered.beats}
        for outcome in outcomes:
            for claim in outcome.claims:
                claim = await _cued(conn, story_id, claim, scopes, env)
                by_beat.setdefault(claim.beat, []).append(claim)

        def scope_of(claim: DetectedClaim) -> tuple[str, ...] | None:
            beat = scopes.get(claim.beat or -1)
            if beat is None or beat.kind != LATENT or beat.visibility.scope != "characters":
                return None
            return tuple(beat.visibility.characters or ())

        def intent_of(claim: DetectedClaim) -> str | None:
            beat = scopes.get(claim.beat or -1)
            return None if beat is None or beat.kind != SAY else beat.intent

        def cites_of(claim: DetectedClaim) -> tuple[str, ...]:
            # #166: the citations of the speaker's think beats in the render (FR-TURN-8).
            return tuple(
                c
                for b in rendered.beats
                if b.kind == THINK and b.character_id == claim.speaker
                for c in b.cites
            )

        stepper = _stepper(
            conn,
            story_id,
            read,
            verifier,
            steps,
            first_beat=rendered.steps_beat,
            operation_id=operation_id,
            read_set=read_set,
        )
        owners = {b.beat: b.step for b in rendered.beats if not b.narrator and b.step}
        for number in sorted(k for k in by_beat if k is not None):
            if stepper is not None and owners.get(number):
                # #168: the steps before this beat's act step, verified before it.
                await stepper.run_before(owners[number])
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
                start=first_claim + len(checked),
                intent_of=intent_of,
                cites_of=cites_of,
            )
            # The same-turn overlay: what this beat may establish, later beats see.
            for request in verifier.canon_requests()[before:]:
                staged.stage(request.claim, request.claim_id or f"{number}")
        step_verdicts: dict[int, render_steps.StepVerdict] = {}
        if stepper is not None:
            step_verdicts = await stepper.run_all()
            checked = _self_attested(checked, steps, step_verdicts, rendered.beats, env.registry)
        requests = verifier.canon_requests()
        rejected_bindings: list[resolution.RejectedBinding] = []
        if lifecycle.commitment_enabled(settings):
            # #154: a binding of an open thunk that conflicts with recorded history is a
            # contradiction the repair ladder steers away from (never committed).
            checked, rejected_bindings = await resolution.screen(
                conn, story_id, checked, read_set=read_set, registry=env.registry
            )
            # A claim that resolves an open thunk commits through its dependence (#146's
            # ``depend``, reconciled), never as a second, unreconciled canon write; a rejected
            # binding commits nowhere.
            refused = {r.claim_id for r in rejected_bindings} | {
                c.claim_id for c in resolution.candidates(checked)
            }
            requests = [r for r in requests if r.claim_id is None or r.claim_id not in refused]
    entailment_s = 0.0 if verifier is None else verifier.entailment_s
    clock.add("verify", time.perf_counter() - verifying - entailment_s)
    clock.add("entailment", entailment_s)
    detecting = time.perf_counter()
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
        found_steps = verifier.degradation()
        degradation += [(d, "verification") for d in found_steps]
        if ENTAILMENT_BUDGET in found_steps:
            # NFR-PERF-7 step 4: an entailment question the budget could not pay for took the
            # safe disposition (#156).
            degradation.append((planner.ENTAILMENT_SAFE_DISPOSITION, "budget"))
    if rejected_bindings:
        degradation.append((resolution.BINDING_REJECTED, "commitment"))
    for b in beats:
        if b.kind == LATENT and not any(c.proposition is not None for c in b.claims):
            degradation.append((LATENT_UNNORMALIZED, "latent"))
        if b.kind == LATENT and any(
            c.verdict == "contradicted" and c.proposition is not None for c in b.claims
        ):
            degradation.append((CONTRADICTED_LATENT, "latent"))
    # Salience (#146): every render's observable beats.
    salient: list[tuple[salience.Beat, Any, tuple[int, int] | None]] = []
    salient_characters: list[str] = []
    mode = salience.salience_mode(settings) if with_salience else None
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
            if SALIENCE_SKIPPED in verdict.degradation:
                # #156: salience is optional work; unfunded, every beat is texture.
                continue
            index = {b.index: b for b in sal_beats}
            salient_characters.extend(verdict.characters)
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
    budget.settle_optional(SALIENCE)
    input_checked = [c for c in checked if c.claim.beat == 1]
    input_mentions = [m.as_payload() for m in mentions.get(1, ())]
    clock.add("detection", time.perf_counter() - detecting)
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
        salient_characters=list(dict.fromkeys(salient_characters)),
        degradation=degradation,
        staged=list(staged.staged),
        rejected_bindings=rejected_bindings,
        introductions=introductions,
        steps=steps,
        step_verdicts=step_verdicts,
        steps_beat=rendered.steps_beat,
    )


# -- the follow-up render ------------------------------------------------------------------------


FOLLOW_UP_SKIPPED_BUDGET = "follow_up_skipped_budget"
"""Degradation: a salient reference was open and the turn's budget could not pay for the
follow-up render, so the minimal commit settles it."""
FOLLOW_UP_FAILED = "follow_up_failed"
"""Degradation: the follow-up render failed (the model, its structure or an output policy), so
the minimal commit settles what it would have."""


def _thunk_line(sketch: ThunkSketch) -> str:
    return " ".join(p for p in (sketch.subject_text, sketch.relation_text, sketch.object_text) if p)


def _settled_by(claims: Sequence[CheckedClaim], sketch: ThunkSketch) -> bool:
    """Whether a beat's surviving claim fills ``sketch``'s open slot (a dependence)."""
    return any(
        c.claim.proposition is not None
        and c.claim.channel in lifecycle.DEPENDENCE_CHANNELS
        and (c.result is None or not c.result.needs_repair)
        and fills(sketch, c.claim.proposition)
        for c in claims
    )


async def owed_references(
    db: AsyncEngine,
    story_id: str,
    read: TurnRead,
    checked: Checked,
    *,
    read_set: StructuredReadSet,
) -> list[ThunkSketch]:
    """The salient references this turn's renders made that would owe a ledger entry at the
    commit (#146's ``record_salience``, without writing): an open argument canon does not
    already settle, each once."""
    if not checked.salient:
        return []
    found: list[ThunkSketch] = []
    del read_set  # advisory: the commit's record_salience reads (and keys) these again
    async with db.connect() as conn:
        registry = await load_registry(conn, story_id)
        lookup = KeyedLookup(conn, StructuredReadSet())
        for beat_, sketch, span in checked.salient:
            speaker = beat_.speaker_entity or NARRATOR
            context = await load_context(
                conn, story_id, position=(read.turn, 0), speaker=speaker, lookup=lookup
            )
            opened = open_sketch(
                salience._claim_of(sketch),
                speaker,
                registry=registry,
                context=context,
                span=None if span is None else (beat_.index, span[0], span[1]),
            )
            if not isinstance(opened, ThunkSketch) or opened in found:
                continue
            if await matching.settled(lookup, story_id, opened):
                continue
            found.append(opened)
    return found


async def follow_up(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    read: TurnRead,
    rendered: Rendered,
    checked: Checked,
    *,
    operation_id: str,
    read_set: StructuredReadSet,
    configuration: StoryProfile,
    budget: TurnBudget,
    sections: prompts.RenderSections,
    context: Any,
    render_ids: Sequence[str],
    clock: StageClock | None = None,
) -> Checked:
    """FR-COMMIT-7's follow-up (#146's AC, #150's pipeline): when a salient reference this turn
    made is still open after its beats were verified, and the turn's budget can pay for one
    more render, the narrator renders once more, told to settle exactly those references; its
    beats are detected and verified like any render's (over the turn's overlay) and are the
    turn's next beats. What it settles is committed by the dependence at the commit; what is
    left (or every open one, when the budget cannot pay: ``follow_up_skipped_budget``) is
    discharged there by the minimal commit. Before any commit transaction opens."""
    owed = await owed_references(db, story_id, read, checked, read_set=read_set)
    still = [s for s in owed if not _settled_by(checked.checked, s)]
    report: dict[str, Any] = {"owed": len(owed), "open": len(still), "ran": False}
    if not still:
        checked.follow_up = report
        return checked
    # Optional work (#156): the render, its output checks and its extraction, held together.
    cost = regeneration_cost(output_policies(context, render_ids)) + (
        1 if extraction_on(configuration.settings) else 0
    )
    held = budget.held
    if not budget.try_optional(FOLLOW_UP, cost):
        checked.degradation.append((FOLLOW_UP_SKIPPED_BUDGET, "budget"))
        checked.follow_up = {**report, "skipped": "budget"}
        return checked
    try:
        return await _follow_up(
            db,
            models,
            story_id,
            read,
            checked,
            still,
            report,
            operation_id=operation_id,
            read_set=read_set,
            configuration=configuration,
            budget=budget,
            sections=sections,
            context=context,
            render_ids=render_ids,
            clock=clock,
        )
    finally:
        budget.release_to(min(budget.held, held))


async def _follow_up(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    read: TurnRead,
    checked: Checked,
    still: Sequence[ThunkSketch],
    report: dict[str, Any],
    *,
    operation_id: str,
    read_set: StructuredReadSet,
    configuration: StoryProfile,
    budget: TurnBudget,
    sections: prompts.RenderSections,
    context: Any,
    render_ids: Sequence[str],
    clock: StageClock | None,
) -> Checked:
    """The follow-up render itself, its budget held (``follow_up``)."""
    start = max((b.beat for b in checked.beats), default=1) + 1
    settings = configuration.settings
    params = render_params(
        ModelParams(temperature=read.resolved.temperature, top_p=read.resolved.top_p),
        read.resolved.max_tokens,
    )
    try:
        with budget.charging(FOLLOW_UP):
            narr, _ = await render_beats(
                models,
                prompts.narrator_inputs(
                    sections,
                    read.actor,
                    say=None,
                    do=None,
                    budget=settings.render.prompt_token_budget,
                    open_references=[_thunk_line(s) for s in still],
                ),
                render_schema(
                    actor_id=None,
                    player_id=read.player_id,
                    cast_ids=list(read.names),
                    max_beats=settings.render.max_beats,
                ),
                variant=NARRATOR_VARIANT,
                params=params,
                budget=budget,
            )
        made, sketches = _beats_of(narr, start=start, actor=None, narrator=True)
        passed = await output_passes(
            models, ["\n".join(b.text for b in made)], context, budget=budget, render_ids=render_ids
        )
    except ModelError:
        passed, made, sketches = False, [], {}
    if not passed:
        checked.degradation.append((FOLLOW_UP_FAILED, "follow_up"))
        checked.follow_up = {**report, "ran": True, "failed": True}
        return checked
    extra = await check_turn(
        db,
        models,
        story_id,
        read,
        Rendered(beats=made, sketches=sketches, introductions=[], retrievals=[]),
        say=None,
        do=None,
        operation_id=operation_id,
        read_set=read_set,
        configuration=configuration,
        budget=budget,
        staged_before=checked.staged,
        first_claim=len(checked.checked),
        with_salience=False,
        clock=clock,
        introductions=checked.introductions,
    )
    left = [s for s in still if not _settled_by(extra.checked, s)]
    checked.beats += extra.beats
    checked.checked += extra.checked
    checked.outcomes += extra.outcomes
    checked.requests += extra.requests
    checked.latent += extra.latent
    checked.mentions.update(extra.mentions)
    checked.degradation += extra.degradation
    checked.staged = extra.staged
    checked.follow_up_beats = frozenset(b.beat for b in extra.beats)
    checked.follow_up_verifier = extra.verifier
    checked.follow_up = {
        **report,
        "ran": True,
        "settled": len(still) - len(left),
        "open": len(left),
        "beats": sorted(checked.follow_up_beats),
    }
    log.debug("follow-up render: %d settled, %d left", len(still) - len(left), len(left))
    return checked


async def settle_follow_up(
    conn: AsyncConnection,
    op: Operation,
    checked: Checked,
    *,
    read_set: StructuredReadSet,
    registry: Registry,
) -> int:
    """At the commit, after the turn's salient references are recorded: each surviving claim of
    the follow-up render that fills one of their (now introduced) thunks is the dependence
    that settles it (``lifecycle.depend``); answers how many."""
    if not checked.follow_up_beats:
        return 0
    lookup = KeyedLookup(conn, read_set)
    settled = 0
    for c in checked.checked:
        proposition = c.claim.proposition
        if (
            c.claim.beat not in checked.follow_up_beats
            or proposition is None
            or c.claim.channel not in lifecycle.DEPENDENCE_CHANNELS
            or (c.result is not None and c.result.needs_repair)
        ):
            continue
        target = await matching.dependence_target(lookup, str(op.story_id), proposition)
        if target is None:
            continue
        await lifecycle.depend(
            conn,
            op,
            target.id,
            proposition,
            beat=c.claim.beat,
            channel=cast(lifecycle.Channel, c.claim.channel),
            provenance=RecordProvenance.DIRECTOR,
            registry=registry,
        )
        settled += 1
    return settled


async def detail_claims(
    conn: AsyncConnection, story_id: str, claims: Sequence[CheckedClaim]
) -> list[tuple[CheckedClaim, str]]:
    """The turn's claims that are a consequential fact's incidental detail (FR-COMMIT-9,
    ``core.commit.boundary.detail_parent``), each with that fact's id: a novel, permitted
    ``think``/``do`` claim no open thunk matches and no repair takes. A ``say`` mentions; a claim
    the verifier contradicted stays its contradiction."""
    found: list[tuple[CheckedClaim, str]] = []
    for c in claims:
        proposition, result = c.claim.proposition, c.result
        if (
            proposition is None
            or result is None
            or c.claim.matched_thunk_id is not None
            or result.needs_repair
            or result.kind is not VerdictKind.NOVEL
            or result.denied is not None
        ):
            continue
        parent = await boundary.detail_parent(conn, story_id, proposition, channel=c.claim.channel)
        if parent is not None:
            found.append((c, parent))
    return found


async def resolve_details(
    conn: AsyncConnection,
    op: Operation,
    details: Sequence[tuple[CheckedClaim, str]],
    *,
    registry: Registry,
) -> list[lifecycle.Dependence]:
    """At the commit, each detail claim (``detail_claims``) resolves its detail once, through
    ``boundary.depend_on_detail`` (reconciled, at ``incidental``); answers the dependences."""
    found: list[lifecycle.Dependence] = []
    for c, parent in details:
        if c.claim.proposition is None:  # pragma: no cover - detail_claims keeps normalized
            continue
        detail = await boundary.depend_on_detail(
            conn,
            op,
            parent,
            c.claim.proposition,
            beat=c.claim.beat,
            channel=c.claim.channel,
            registry=registry,
        )
        if isinstance(detail.dependence, lifecycle.Dependence):
            found.append(detail.dependence)
    if found:
        log.debug("details: %d resolved in story %s", len(found), op.story_id)
    return found


def _close_path(follow: Mapping[str, Any], minimal: int) -> str:
    """Which way the turn's salient references were settled: ``none`` owed, ``follow_up``,
    ``follow_up_and_minimal``, or ``minimal`` (budget exhaustion or a failed follow-up)."""
    if not follow.get("owed") and not minimal:
        return "none"
    if follow.get("ran") and not follow.get("failed"):
        return "follow_up_and_minimal" if minimal else "follow_up"
    return "minimal" if minimal else "none"


# -- the repair ladder (#151) --------------------------------------------------------------------


DECLARED_LEVEL = "declared"
"""The verification level a repair's deterministic re-check detects at (no model backstop)."""
UNREPAIRED_INTENTS = frozenset({"deceptive", "joking"})
"""A ``say`` declared deceptive or joking is not meant as true: its claims are labelled, never
repaired."""


def repair_on(configuration: StoryProfile) -> bool:
    """Whether the story's profile runs the repair ladder: its ``repair`` module is on
    (``repair.max_attempts`` of 1 or more; ``Structured (M4)`` sets 1, the schema's default 0
    leaves a contradicted beat standing, labelled, as #150 did)."""
    return module_on(configuration.settings, REPAIR_MODULE)


def _hallucinated(checked: Checked, beat: TurnBeat) -> bool:
    """#166: a claim of ``beat`` has no legitimate basis, whatever the beat declared (a
    declared lie with no underlying belief is repaired, FR-VERIFY-10)."""
    return any(
        c.claim.beat == beat.beat and c.result is not None and c.result.hallucinated
        for c in checked.checked
    )


def _violations(checked: Checked, beat: TurnBeat) -> list[CheckedClaim]:
    return [
        c
        for c in checked.checked
        if c.claim.beat == beat.beat and c.result is not None and c.result.needs_repair
    ]


async def repair_turn(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    read: TurnRead,
    rendered: Rendered,
    checked: Checked,
    *,
    operation_id: str,
    read_set: StructuredReadSet,
    configuration: StoryProfile,
    budget: TurnBudget,
    context: Any,
    render_ids: Sequence[str],
    fallback: FallbackLines,
    narrator_inputs: GenerationInputs,
) -> Checked:
    """FR-REPAIR-1..4 and FR-VERIFY-12 over the turn's verified beats (#151): every rendered
    beat with a claim that ``needs_repair`` goes to ``orchestrator.repair.ladder``, in beat
    order, and what it accepts replaces the beat at its position (a dropped ``think`` or
    ``latent`` leaves the turn). The corrected beat's claims, canon requests, latent canon and
    mentions replace the rejected beat's (nothing from a rejected render canonizes); its
    salient spans are not read again. After the follow-up render, before any commit
    transaction; a story whose profile has the repair module off (``repair_on``), or a turn
    released as the safe fallback, is left as it is."""
    if not repair_on(configuration) or rendered.finish_reason == "fallback":
        return checked
    wanted = [
        (b, found)
        for b in checked.beats
        if b.beat > 1
        and not (b.kind == SAY and b.intent in UNREPAIRED_INTENTS and not _hallucinated(checked, b))
        and (found := _violations(checked, b))
    ]
    if not wanted:
        return checked
    settings = configuration.settings
    async with db.connect() as conn:
        detection = DetectionConfig.of(settings, await read_section(conn, VerifySettings))
    sketches = dict(rendered.sketches)
    claims_made = [len(checked.checked)]

    async def recheck(
        beat: TurnBeat, chosen: Sequence[PropositionSketch], backstop: bool, rung: int
    ) -> Any:
        own = {
            f"staged:{r.claim_id}" for r in checked.requests if r.beat == beat.beat and r.claim_id
        }
        found = await check_turn(
            db,
            models,
            story_id,
            read,
            Rendered(
                beats=[beat], sketches={beat.beat: list(chosen)}, introductions=[], retrievals=[]
            ),
            say=None,
            do=None,
            operation_id=operation_id,
            read_set=read_set,
            configuration=configuration,
            budget=budget,
            staged_before=[r for r in checked.staged if r.fact_id not in own],
            first_claim=claims_made[0],
            with_salience=False,
            backstop=backstop,
            answers_from=(checked.verifier, checked.follow_up_verifier),
            introductions=checked.introductions,
        )
        claims_made[0] += len(found.checked)
        if found.verifier is not None:
            checked.repair_verifiers.append((rung, found.verifier))
        (made,) = found.beats
        return Recheck(
            beat=made,
            claims=found.checked,
            degradation=[step for step, _ in found.degradation],
            value=found,
        )

    def inputs_of(beat: TurnBeat) -> GenerationInputs | None:
        if beat.narrator or beat.kind == NARRATION:
            return rendered.narrator_inputs or narrator_inputs
        return rendered.character_inputs

    async def output_ok(text: str) -> bool:
        return await output_passes(models, [text], context, budget=budget, render_ids=render_ids)

    policies = output_policies(context, render_ids)
    ladder = RepairLadder(
        db,
        models,
        story_id,
        turn=read.turn,
        read_set=read_set,
        max_attempts=settings.repair.max_attempts,
        recheck=recheck,
        output_ok=output_ok,
        fallback=fallback,
        inputs_of=inputs_of,
        sketches_of=sketches,
        params=render_params(
            ModelParams(temperature=read.resolved.temperature, top_p=read.resolved.top_p),
            read.resolved.max_tokens,
        ),
        extraction_calls=1 if detection.extraction_on else 0,
        check_calls=regeneration_cost(policies) - 1,
    )
    for beat, violations in wanted:
        result = await ladder.run(beat, violations, budget)
        _merge_repair(checked, result)
        checked.degradation += result.degradation
    return checked


def canon_requests(checked: Checked, requests: Sequence[CanonRequest]) -> list[CanonRequest]:
    """The turn's canon requests, each with the minted places and objects its claim names
    (#479: ``introduces``; ``core.canon.canonize`` creates each at its first canonized
    mention). A minted id no request names is never created."""
    return [
        replace(r, introduces=checked.introductions.introduces(r.claim)) if not r.introduces else r
        for r in requests
    ]


def _merge_repair(checked: Checked, result: RepairResult) -> None:
    """Lay ``result`` over ``checked``: the accepted beat at the original's position with its
    claims, canon requests, latent canon and mentions; the rejected beat's gone."""
    number = result.original.beat
    found: Checked | None = None if result.recheck is None else result.recheck.value
    accepted = result.beat
    if accepted is not None and found is not None:
        (made,) = found.beats
        accepted = replace(made, repaired_from=accepted.repaired_from)
    beats: list[TurnBeat] = []
    for b in checked.beats:
        if b.beat != number:
            beats.append(b)
        elif accepted is not None:
            beats.append(accepted)
    checked.beats = beats
    checked.checked = [c for c in checked.checked if c.claim.beat != number]
    checked.requests = [r for r in checked.requests if r.beat != number]
    checked.latent = [(b, c) for b, c in checked.latent if b.beat != number]
    checked.mentions.pop(number, None)
    checked.salient = [s for s in checked.salient if s[0].index != number]
    if found is not None and accepted is not None:
        checked.checked += found.checked
        checked.requests += [r for r in found.requests if not result.restates(r.claim)]
        checked.latent += [(accepted, c) for _, c in found.latent]
        if number in found.mentions:
            checked.mentions[number] = found.mentions[number]
        checked.degradation += [d for d in found.degradation if d not in checked.degradation]
    checked.repairs.append(result)


def _repair_events(checked: Checked) -> list[tuple[str, Any]]:
    """The audit records of the turn's repairs (FR-REPAIR-3, FR-STORE-25): for each repaired
    beat, its ``verify_verdict`` (the verdicts that sent it to the ladder) and a
    ``render_rejected`` per rejected text, in beat order."""
    found: list[tuple[str, Any]] = []
    for result in checked.repairs:
        found.append(
            (
                "verify_verdict",
                VerifyVerdict(
                    beat=result.original.beat,
                    verdicts=tuple(
                        r.as_payload(c.claim_id) for c in result.violations if (r := c.result)
                    ),
                ),
            )
        )
        found += [("render_rejected", r.event()) for r in result.rejected]
    return found


def repairs_report(checked: Checked) -> list[dict[str, Any]]:
    """``turn_report.repairs``: one entry per repaired claim (ids, codes and counts)."""
    return [entry for result in checked.repairs for entry in result.report()]


# -- perception routing (#161) ------------------------------------------------------------------

QUALIFIER_REPAIR = "perception_qualifier"
"""The ``render_rejected`` constraint kind of a dropped perception qualifier (#161)."""


async def _place_ids(conn: AsyncConnection, story_id: str) -> dict[str, str]:
    """The story's places by case-folded name (a narration's ``location`` may name one)."""
    found = await conn.execute(
        select(entities.c.id, entities.c.canonical_name).where(
            entities.c.story_id == story_id, entities.c.kind == "place"
        )
    )
    return {str(r.canonical_name).casefold(): str(r.id) for r in found}


def _route_beat(
    beat: TurnBeat, qualifiers: perception.Qualifiers | None = None
) -> perception.RouteBeat:
    return perception.RouteBeat(
        kind=beat.kind,
        beat=beat.beat,
        actor=None if beat.kind == NARRATION else beat.character_id,
        qualifiers=beat.qualifiers if qualifiers is None else qualifiers,
        # #164: what a ``do`` or a narration states, observed by its content perceivers.
        claims=tuple(c.proposition for c in beat.claims if c.proposition is not None)
        if beat.kind in (DO, NARRATION)
        else (),
    )


async def validate_perception(
    db: AsyncEngine,
    story_id: str,
    read: TurnRead,
    checked: Checked,
    settings: ProfileSettings,
) -> Checked:
    """The rendered beats' perception qualifiers validated before staging (FR-TURN-13; #161):
    at perception ``full``, each beat's qualifiers against the scene at the turn's start
    (``core.epistemic.perception.validate_qualifiers``; a narration's ``location`` named by a
    place's name is resolved to its id). A qualifier that cannot be honored is dropped (#151's
    rung 1: the beat stands with ``repaired_from`` ``(1, cosmetic)``, its text unchanged, and
    the beat as rendered is staged as ``render_rejected``); below ``full`` the qualifiers are
    ignored and kept as declared."""
    if scene_loader.level_of(settings) != perception.FULL:
        return checked
    if all(b.qualifiers.empty for b in checked.beats):
        return checked
    async with db.connect() as conn:
        state = await scene_loader.load_scene_state(conn, story_id, read.turn)
        places = await _place_ids(conn, story_id)
    beats: list[TurnBeat] = []
    for beat in sorted(checked.beats, key=lambda b: b.beat):
        q = beat.qualifiers
        if q.empty:
            beats.append(beat)
        else:
            if q.location is not None and q.location not in state.places:
                q = replace(q, location=places.get(q.location.casefold(), q.location))
            valid, repairs = perception.validate_qualifiers(_route_beat(beat, q), state)
            if repairs:
                checked.qualifier_rejected.append((beat, repairs))
                checked.perception_repairs.extend(repairs)
                beats.append(
                    replace(
                        beat, qualifiers=valid, repaired_from=beat.repaired_from or (1, "cosmetic")
                    )
                )
            else:
                beats.append(replace(beat, qualifiers=q))
        state = _declared_topology(state, beat)
    order = {b.beat: n for n, b in enumerate(checked.beats)}
    checked.beats = sorted(beats, key=lambda b: order[b.beat])
    return checked


def _declared_topology(state: perception.SceneState, beat: TurnBeat) -> perception.SceneState:
    """``state`` with the channels and barriers ``beat`` declares, its uncontradicted claims
    only (``core.epistemic.perception.with_declared``; #161)."""
    return perception.with_declared(
        state, [c.proposition for c in beat.declared if c.verdict != "contradicted"]
    )


def _qualifier_events(checked: Checked) -> list[tuple[str, Any]]:
    """The ``render_rejected`` audit record of each beat whose qualifiers were repaired (#161):
    the beat as rendered (attempt 0, rung 1), with one constraint per dropped qualifier."""
    return [
        (
            "render_rejected",
            RenderRejected(
                beat=beat.beat,
                attempt=0,
                rung=1,
                text=beat.text,
                constraints=tuple(
                    {
                        **{k: v for k, v in repair.report().items() if k != "kind"},
                        "kind": QUALIFIER_REPAIR,
                        "qualifier": repair.kind,
                    }
                    for repair in repairs
                ),
            ),
        )
        for beat, repairs in checked.qualifier_rejected
    ]


def _routed_beats(
    checked: Checked,
    read: TurnRead,
    *,
    say: str | None,
    do: str | None,
    ids: Mapping[int, str],
    moves: scene_loader.Moves,
    participants: Mapping[int, tuple[str, ...]] | None = None,
) -> list[perception.RouteBeat]:
    """The turn's beats as routing takes them (#161): the player's ``say`` and ``do`` (beat 1)
    and every rendered beat, each with the episodic writer's beat as its ``source`` (a ``latent``
    has none: it goes to canon) and its sound (an audible ``do``); each beat number's committed
    moves ride on its last beat."""
    found: list[perception.RouteBeat] = []
    input_id = ids.get(1)
    mentions = checked.mentions.get(1, ())
    extra = participants or {}
    for kind, text in ((SAY, say), (DO, do)):
        if text and input_id is not None:
            event_type, found_parts = beat_occasion(text, mentions)
            found.append(
                perception.RouteBeat(
                    kind=cast(perception.RouteKind, kind),
                    beat=1,
                    actor=read.actor.id,
                    sound=perception.sound_of(text) if kind == DO else None,
                    source=records.Beat(
                        cast(records.BeatKind, kind),
                        text,
                        input_id,
                        beat=1,
                        character_id=read.actor.id,
                        provenance=RecordProvenance.PLAYER,
                        event_type=event_type,
                        participants=_participants(
                            (*found_parts, *extra.get(1, ())), read.actor.id
                        ),
                    ),
                )
            )
    for beat in checked.beats:
        event_id = ids.get(beat.beat)
        if event_id is None:
            continue
        routed = _route_beat(beat)
        if beat.kind == LATENT:
            found.append(replace(routed, actor=None if beat.narrator else beat.character_id))
            continue
        event_type, found_parts = beat_occasion(beat.text, checked.mentions.get(beat.beat, ()))
        found_parts = (*found_parts, *extra.get(beat.beat, ()))
        actor = routed.actor
        found.append(
            replace(
                routed,
                sound=perception.sound_of(beat.text) if beat.kind == DO else None,
                source=records.Beat(
                    beat.kind,
                    beat.text,
                    event_id,
                    beat=beat.beat,
                    character_id=actor,
                    provenance=RecordProvenance.DIRECTOR
                    if beat.kind == NARRATION
                    else RecordProvenance.RENDER,
                    event_type=event_type,
                    participants=_participants(found_parts, beat.character_id),
                ),
            )
        )
    for index, routed in enumerate(found):
        last = index == len(found) - 1 or found[index + 1].beat != routed.beat
        if last and moves.get(routed.beat):
            found[index] = replace(routed, moves=moves[routed.beat])
    return found


async def model_committed(
    conn: AsyncConnection,
    op: Operation,
    read: TurnRead,
    rendered: Rendered,
    checked: Checked,
    *,
    level: perception.Level,
    settings: ProfileSettings,
    fixed: dict[str, Any],
) -> tuple[list[tuple[str, str]], dict[int, tuple[str, ...]]]:
    """The turn's modeling in its commit step (#163; ``core.epistemic.modeling``), before
    routing: every planned entity modeled (``model_all``: its row, entity, events) with its age
    and its place in the scene (``write_character_facts``), and every character the renders
    introduced recorded as modeled (salience). Answers the characters joining the routed scene
    (with its location) and, by beat, the new characters each triggering beat involves; sets
    ``fixed["modeling"]`` (``turn_report.modeling``) and counts
    ``epistrel_characters_modeled_total{trigger}``."""
    plan = checked.modeling
    if plan is None or not char_modeling.modeling_on(settings):
        return [], {}
    introduced = await char_modeling.record_introduced(
        conn, op, [(i.character_id, i.beat) for i in rendered.introductions]
    )
    made = await char_modeling.model_all(
        conn,
        op,
        plan.ready,
        allow_new_arrivals=read.allow_new_arrivals,
        base_revision=op.base_revision,
    )
    everyone = [*introduced, *made]
    location: str | None = None
    if everyone and level != perception.NONE and op.story_id is not None:
        state, _ = await scene_loader.load_scene(conn, op.story_id, read.turn)
        location = state.location
    await write_character_facts(
        conn,
        op,
        [
            *((m.character_id, None, m.beat) for m in introduced),
            *((m.character_id, m.age, m.beat) for m in made),
        ],
        location=location,
    )
    for m in everyone:
        CHARACTERS_MODELED.labels(trigger=m.trigger).inc()
    fixed["modeling"] = plan.report(everyone)
    joined = [(m.character_id, location) for m in everyone] if location is not None else []
    participants: dict[int, tuple[str, ...]] = {}
    for m in everyone:
        participants[m.beat] = (*participants.get(m.beat, ()), m.character_id)
    return joined, participants


async def route_committed(
    conn: AsyncConnection,
    op: Operation,
    checked: Checked,
    read: TurnRead,
    *,
    say: str | None,
    do: str | None,
    ids: Mapping[int, str],
    level: perception.Level,
    settings: ProfileSettings,
    routed: list[perception.PerceptionResult] | None = None,
    joined: Sequence[tuple[str, str]] = (),
    participants: Mapping[int, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Route the committed turn (#161), in its commit step after canon (so the moves the turn
    committed are in #137's placement projection): every beat through
    ``core.epistemic.perception.route_turn`` against the scene at the turn's start under
    ``level``, the results handed to ``PERCEPTION_SINKS`` (the memory sink writes one memory
    per perceiver). A ``latent`` that declared nothing normalizable keeps its shared
    ``objective`` memory. Answers ``turn_report.perception``: the level, the beats routed and
    every qualifier repaired (before staging, and any the commit-time scene no longer
    supports), each counted in ``epistrel_perception_repairs_total``."""
    story_id = op.story_id
    assert story_id is not None  # noqa: S101 - a turn belongs to a story
    state, moves = await scene_loader.load_scene(conn, story_id, read.turn)
    if joined:
        # #163: a character the turn modeled joins the scene from the turn's start.
        state = replace(state.moved(joined), characters=state.characters | {c for c, _ in joined})
    results = perception.route_turn(
        _routed_beats(
            checked, read, say=say, do=do, ids=ids, moves=moves, participants=participants or {}
        ),
        state,
        level,
    )
    await perception.deliver(conn, op, results)
    if routed is not None:
        routed.extend(results)
    leftovers = [
        b
        for b in _episodic(checked, read, say=None, do=None, ids=ids)
        if b.kind == NARRATION and b.character_id is None and _is_latent(checked, b.beat)
    ]
    if leftovers:
        await records.write_episodic(conn, op, leftovers, settings=settings)
    repairs = [*checked.perception_repairs, *(r for res in results for r in res.repairs)]
    for repair in repairs:
        count_perception_repair(repair.reason)
    return {
        "level": level,
        "routed": len(results),
        "qualifiers_repaired": [r.report() for r in repairs],
    }


def _is_latent(checked: Checked, number: int | None) -> bool:
    return any(b.beat == number and b.kind == LATENT for b in checked.beats)


def _verify_passes(checked: Checked) -> list[yields.VerifyPass]:
    """The turn's verifiers as #155's verification passes: the renders' (the follow-up's
    included) and each repair re-check's."""
    found = [
        yields.VerifyPass(v, yields.RENDER)
        for v in (checked.verifier, checked.follow_up_verifier)
        if v is not None
    ]
    found += [
        yields.VerifyPass(v, yields.repair_pass(rung)) for rung, v in checked.repair_verifiers
    ]
    return found


async def _raw_render_ids(
    conn: AsyncConnection, operation_id: str, ids: Mapping[int, str], checked: Checked
) -> list[str]:
    """``eval_hooks.raw_render_event_ids`` (#155): each rendered beat's event, and for a
    repaired beat its original text's ``render_rejected`` event (attempt 0)."""
    repaired = {r.original.beat for r in checked.repairs}
    found = [ids[b] for b in sorted(ids) if b > 1 and b not in repaired]
    if repaired:
        rejected = await conn.execute(
            select(events.c.id, events.c.payload).where(
                events.c.operation_id == operation_id, events.c.type == "render_rejected"
            )
        )
        found += sorted(
            str(r.id)
            for r in rejected
            if isinstance(r.payload, dict)
            and r.payload.get("attempt") == 0
            and r.payload.get("beat") in repaired
        )
    return found


# -- composition (#152) -----------------------------------------------------------------------


COMPOSITION_CAUSE = "composition"
POLISH_BUDGET_CAUSE = "budget"


def composition_detection_calls(configuration: StoryProfile, config: DetectionConfig) -> int:
    """C: the composed message's detection calls, one when the polish pass is on and the
    story's verification runs the model layer (a deterministic assembly adds no new text, so
    the call is only ever spent on a polished message)."""
    polish = configuration.settings.repair.polish.enabled
    return 1 if polish and config.extraction_on else 0


def _composition_speaker(viewpoint: str, player_id: str | None) -> str:
    """Who the composed message is detected as: the viewpoint's character (``player`` for the
    player's own), ``narrator`` for ``omniscient``."""
    if is_omniscient(viewpoint):
        return NARRATOR
    character = viewpoint.split(":", 1)[1] if ":" in viewpoint else viewpoint
    return PLAYER if character == player_id else character


async def compose_turn(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    read: TurnRead,
    checked: Checked,
    *,
    viewpoint: str,
    operation_id: str,
    configuration: StoryProfile,
    budget: TurnBudget,
    context: Any,
    render_ids: Sequence[str],
    fallback: FallbackLines,
    reserved: bool,
) -> composing.Composition:
    """The turn's message for ``viewpoint`` (``orchestrator.compose.compose``) from its
    post-repair beats: the polish under #68's output gate and the final check, or the
    deterministic assembly (see the module docstring)."""
    settings = configuration.settings
    routes = routes_of(settings)
    policies = output_policies(context, render_ids)
    plain = [p for p in policies if not isinstance(p.rule, ModelAssistedRule)]
    assisted = len(policies) - len(plain)
    speaker = _composition_speaker(viewpoint, read.player_id)
    async with db.connect() as conn:
        base_config = DetectionConfig.of(settings, await read_section(conn, VerifySettings))
    scene_context = SceneContext(
        present_entities=list(read.present)[:100], last_beats=list(read.last_beats)
    )

    async def detect(text: str, with_model: bool) -> tuple[DetectionOutcome, Registry]:
        # Under ``gated`` extraction the composed message still spends its reserved call; the
        # deterministic assembly is checked by layers (a) and (b) only.
        config = (
            replace(base_config, extraction_mode="always")
            if with_model
            else replace(base_config, level=DECLARED_LEVEL)
        )
        async with db.begin() as conn:
            env = await extraction.load_env(
                conn,
                story_id,
                StructuredReadSet(),
                position=(read.turn, 0),
                speakers=[speaker],
                config=config,
            )
            # The message names the turn's places and objects by the beats' minted ids (#479).
            env = replace(env, introductions=checked.introductions)
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
            outcome = await detector.message(
                text, (read.turn, 0), operation_id=operation_id, speaker=speaker
            )
            return outcome, detector.env.registry

    async def output_ok(text: str) -> bool:
        return await output_passes(models, [text], context, budget=budget, render_ids=render_ids)

    def deterministic_ok(text: str) -> bool:
        return not evaluate(text, plain, Target.TEXT_OUTPUT)

    safe = next(
        (t for _, t in fallback.candidates() if deterministic_ok(t)), fallback.candidates()[-1][1]
    )
    shown = [b for b in checked.beats if b.beat > 1]
    hidden = [] if is_omniscient(viewpoint) else [b for b in shown if b.kind in PRIVATE_KINDS]
    composer = composing.Composer(
        models=models,
        inputs=GenerationInputs(
            rating_guidance=read.inputs.guidance,
            custom_instructions=read.inputs.instructions,
            player_input=PromptPlayer(name=read.actor.name),
        ),
        params=ModelParams(temperature=read.resolved.temperature, top_p=read.resolved.top_p),
        names=read.names,
        detect=detect,
        output_ok=output_ok,
        deterministic_ok=deterministic_ok,
        fallback_text=safe,
        polish_on=settings.repair.polish.enabled,
        assisted_checks=assisted,
        detection_calls=composition_detection_calls(configuration, base_config),
        hidden=hidden,
        max_chars=length_chars(read.resolved.max_tokens),
    )
    return await composing.compose(shown, viewpoint, budget, composer=composer, reserved=reserved)


def _composition_degradation(composition: composing.Composition) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for step in composition.degradation:
        if step == composing.POLISH_SKIPPED_BUDGET:
            # NFR-PERF-7 step 1 (#156), beside #152's own label.
            found.append((planner.SKIP_POLISH, POLISH_BUDGET_CAUSE))
            found.append((step, POLISH_BUDGET_CAUSE))
        else:
            found.append((step, COMPOSITION_CAUSE))
    return found


def undetected_beats(checked: Checked) -> list[int]:
    """The turn's beats (the player's input included) with no detection record: none, ever
    (#156: a gated-off or level-off record counts; the pipeline refuses to commit otherwise)."""
    recorded = {
        b.beat
        for outcome in checked.outcomes
        for b in outcome.detection.beats
        if b.beat is not None
    }
    return sorted(b.beat for b in checked.beats if b.beat > 1 and b.beat not in recorded)


def _replaced_render(read: TurnRead, fallback: FallbackLines, policies: Sequence[Any]) -> Rendered:
    """The render(s) a deadline left without their reserved extraction, replaced by one
    ``say`` of the rendering character: the first safe-fallback line the deterministic output
    policies pass (#68's lines; ``think``/``latent`` beats and introductions are dropped)."""
    plain = [p for p in policies if not isinstance(p.rule, ModelAssistedRule)]
    candidates = fallback.candidates()
    line = next(
        (t for _, t in candidates if not evaluate(t, plain, Target.TEXT_OUTPUT)), candidates[-1][1]
    )
    return Rendered(
        beats=[TurnBeat(kind="say", beat=2, text=line, character_id=read.character.id)],
        sketches={},
        introductions=[],
        retrievals=[],
        finish_reason="fallback",
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
    regenerating: RecordedTurn | None = None,
    plan: planner.TurnPlan | None = None,
) -> RespondResult:
    """The structured turn itself (see the module docstring), under ``configuration``.
    ``regenerating``: the head turn this one replaces (#154; ``regenerate``). ``plan``: the
    turn's plan (#156; ``planner.plan_structured``), whose reservations ``budget`` holds."""
    timing = timing if timing is not None else lite.TurnTiming(front=budget.started)
    say, do = request.player_input.say_text, request.player_input.do_text
    settings = configuration.settings
    captured = configuration.configuration()
    read_set = StructuredReadSet()
    stamped: list[Stamp] = []
    gated: list[Gated] = []
    released: list[Rendered] = []
    found: list[Checked] = []
    composed: list[composing.Composition] = []
    fixed: dict[str, Any] = {}
    rounds: list[int] = []
    clock = StageClock()
    narrating: list[bool] = []
    """Whether the narrator renders (#156: its render and extraction reserved), once decided."""
    planned: list[tuple[str, str]] = []
    """The plan's own degradation (``narrator_skipped``, ``beat_replaced_safe_fallback``)."""

    async def begin(conn: AsyncConnection) -> TurnRead:
        clock.reset()
        reading = time.perf_counter()
        read = await read_turn(
            conn,
            story_id,
            cast_,
            request,
            configuration=configuration,
            read_set=read_set,
            story=story,
            regenerating=None if regenerating is None else regenerating.turn,
        )
        timing.assemble_s += time.perf_counter() - reading
        clock.add("plan", time.perf_counter() - reading)
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
            found = await retrieve(
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
        # NFR-PERF-1's span: the pipeline's own total, the query embedding left out (#153).
        stages.observe("retrieve", found.timings.get("total", 0.0) / 1000)
        return found

    async def generate(op: Operation, read: TurnRead) -> None:
        context = await guard_context(db, story_id)
        policies = output_policies(context, cast_.render_ids)
        rounds.append(1)
        planned.clear()
        if not narrating:
            narrating.append(
                bool(read.triggers) and (plan is None or planner.reserve_narrator(budget, plan))
            )
        if read.triggers and not narrating[0]:
            planned.append((planner.NARRATOR_SKIPPED, "budget"))
        narrate = narrating[0] and bool(read.triggers)
        if len(rounds) > 1:
            # A rebase re-renders: the renders and their extractions again (#156).
            renders = 1 + (1 if narrate else 0)
            extractions = renders if plan is not None and plan.reservation.extraction else 0
            cost = regeneration_cost(policies) + (renders - 1) + extractions
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
            budget.hold(cost - extractions)
            budget.reserve(EXTRACTION, extractions)
        by_draft: dict[int, Rendered] = {}
        joined = joined_input(say, do)
        base = prompts.RenderSections(
            guidance=read.inputs.guidance,
            instructions=read.inputs.instructions,
            scenario_text=read.inputs.scenario_text,
            pins=read.inputs.pins,
            segments=read.inputs.segments,
            scene=read.scene,
            resolution_steering=_steering(read),
            length_target=length_line(read.resolved.max_tokens),
            # #168: the step contract, for a character that keeps its own beliefs.
            belief_steps=module_on(settings, belief_transitions.PARTITIONS_MODULE),
        )
        budget_tokens = settings.render.prompt_token_budget
        max_beats = settings.render.max_beats
        cast_ids = list(read.names)
        # #464 (FR-RESP-3): the renders are held to the requested length, counted over the
        # beats the output viewpoint receives.
        max_chars = length_chars(read.resolved.max_tokens)
        hidden = frozenset() if is_omniscient(viewpoint) else PRIVATE_KINDS

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
            narrator_inputs: GenerationInputs | None = None
            next_beat = 2
            used = 0
            if narrate:
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
                narrator_inputs = prompts.narrator_inputs(
                    sections, read.actor, say=say, do=do, budget=budget_tokens
                )
                narr, n = await render_beats(
                    models,
                    narrator_inputs,
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
                narr, used, _ = hold_length(narr, max_chars, hidden=hidden)
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
            char_render, _, _ = hold_length(char_render, max_chars, hidden=hidden, used=used)
            char_start = next_beat
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
                narrator=narrate,
                model=calls.calls[-1].model if calls.calls else None,
                prompt_hash=prompt_hash(messages),
                finish_reason="stop",
                character_inputs=inputs,
                narrator_inputs=narrator_inputs,
                steps=list(char_render.steps),
                steps_beat=char_start,
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
            withheld=partial(refuse_withheld, db),
        )
        timing.policy_check_s += time.perf_counter() - gating
        clock.add("render", time.perf_counter() - gating)
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
        replaced = (
            plan is not None
            and plan.reservation.extraction > 0
            and rendered.finish_reason != "fallback"
            and not budget.can_start()
        )
        if replaced:
            # NFR-PERF-7 step 7 (#156): the renders' reserved extraction cannot start before
            # the deadline, so nothing they said is released: the safe fallback replaces them
            # before detection (its own record is layers a and b).
            rendered = _replaced_render(read, lite._fallback_lines(read.inputs, cast_), policies)
            planned.append((planner.BEAT_REPLACED, "deadline"))
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
            backstop=not replaced,
            clock=clock,
        )
        checked.degradation[:0] = planned
        # The composed message's detection call (C): held before any optional work (#152).
        async with db.connect() as conn:
            detection_config = DetectionConfig.of(
                settings, await read_section(conn, VerifySettings)
            )
        reserved = composing.reserve_detection(
            budget, composition_detection_calls(configuration, detection_config)
        )
        following = time.perf_counter()
        checked_s = clock.total(("normalize", "detection", "verify", "entailment"))
        checked = await follow_up(
            db,
            models,
            story_id,
            read,
            rendered,
            checked,
            operation_id=op.operation_id,
            read_set=read_set,
            configuration=configuration,
            budget=budget,
            sections=replace(
                base, director_notes=read.director, working_memory=read.narrator_memory
            ),
            context=context,
            render_ids=cast_.render_ids,
            clock=clock,
        )
        # The follow-up render's own time; its check's is in its stages already.
        checked_s = clock.total(("normalize", "detection", "verify", "entailment")) - checked_s
        clock.add("render", time.perf_counter() - following - checked_s)
        repairing = time.perf_counter()
        checked = await repair_turn(
            db,
            models,
            story_id,
            read,
            rendered,
            checked,
            operation_id=op.operation_id,
            read_set=read_set,
            configuration=configuration,
            budget=budget,
            context=context,
            render_ids=cast_.render_ids,
            fallback=lite._fallback_lines(read.inputs, cast_),
            narrator_inputs=prompts.narrator_inputs(
                replace(base, director_notes=read.director, working_memory=read.narrator_memory),
                read.actor,
                say=say,
                do=do,
                budget=budget_tokens,
            ),
        )
        clock.add("repair", time.perf_counter() - repairing)
        # #163: who the turn models (after verify/repair, before routing), their ages found.
        checked.modeling = await plan_modeling(
            db,
            models,
            story_id,
            read,
            checked,
            say=say,
            do=do,
            settings=settings,
            budget=budget,
            sections=base,
        )
        scene_loader.remember_level(read_set, scene_loader.level_of(settings))
        checked = await validate_perception(db, story_id, read, checked, settings)
        # #162: salient witnesses' testimony, adjudicated as optional work before the polish.
        checked.adjudications = await adjudicate.adjudicate_turn(
            db,
            models,
            story_id,
            turn=read.turn,
            actor=read.actor.id,
            primary=read.character.id,
            cast=list(read.names),
            names=read.names,
            checked=checked,
            say=say,
            settings=settings,
            budget=budget,
            read_set=read_set,
        )
        checked.degradation += checked.adjudications.degradation
        found[:] = [checked]
        composing_at = time.perf_counter()
        composition = await compose_turn(
            db,
            models,
            story_id,
            read,
            checked,
            viewpoint=viewpoint,
            operation_id=op.operation_id,
            configuration=configuration,
            budget=budget,
            context=context,
            render_ids=cast_.render_ids,
            fallback=lite._fallback_lines(read.inputs, cast_),
            reserved=reserved,
        )
        clock.add("composition", time.perf_counter() - composing_at)
        composed[:] = [composition]
        checked.degradation += _composition_degradation(composition)
        missing = undetected_beats(checked)
        if missing:  # pragma: no cover - every path detects what it releases
            raise RuntimeError(f"structured turn would commit beats without detection: {missing}")
        stage(op, read, rendered, checked, outcome, composition)

    def stage(
        op: Operation,
        read: TurnRead,
        rendered: Rendered,
        checked: Checked,
        outcome: Gated,
        composition: composing.Composition,
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
        # The repair ladder's audit records (#151), the repaired perception qualifiers' (#161)
        # included: never read by a story view.
        for event_type, payload in [*_repair_events(checked), *_qualifier_events(checked)]:
            op.append(
                event_type, payload, provenance=Provenance.ENGINE, character_id=None, beat=last + 1
            )
        # A rejected polish (#152): audit only, its text never released.
        if composition.rejected_text is not None:
            op.append(
                "composition_rejected",
                CompositionRejected(
                    text=composition.rejected_text,
                    failures=tuple(
                        f.payload()
                        for f in (composition.rejected.failures if composition.rejected else ())
                    ),
                    cause=composition.cause,
                ),
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
                text=composition.text,
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
        if regenerating is not None:
            op.add_commit_step(fail_queue)

    failed_queue: list[str] = []

    async def fail_queue(conn: AsyncConnection) -> None:
        # A regenerate is a control action: the story's queued turns fail with it (#66).
        failed_queue[:] = await turn_gate.fail_queued(
            conn,
            story_id,
            TimelineChanged(
                "a regenerate changed the timeline before the queued turn started",
                {
                    "current_revision": int(op.base_revision or 0) + 1,
                    "control_operation_id": op.operation_id,
                },
            ),
        )

    async def write_turn(
        conn: AsyncConnection, *, read: TurnRead, rendered: Rendered, checked: Checked
    ) -> None:
        # #168 (FR-STORE-31): every record a beat derives takes the beat's declared step.
        owners = render_steps.beat_steps([b for b in checked.beats if not b.narrator])
        with beat_steps(op.operation_id, owners):
            await write_turn_body(conn, read=read, rendered=rendered, checked=checked)

    async def write_turn_body(
        conn: AsyncConnection, *, read: TurnRead, rendered: Rendered, checked: Checked
    ) -> None:
        committing = time.perf_counter()
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
        # FR-COMMIT-9: a claim about a consequential fact's open detail resolves it through
        # its dependence (reconciled, ``incidental``), never as a plain canon write.
        details = (
            await detail_claims(conn, story_id, checked.checked)
            if lifecycle.commitment_enabled(settings)
            else []
        )
        routed = {c.claim_id for c, _ in details if c.claim_id is not None}
        kept = [r for r in checked.requests if r.claim_id is None or r.claim_id not in routed]
        canonized = await canonize_all(
            conn,
            story_id,
            canon_requests(checked, [*kept, *latent_requests]),
            operation=op,
            registry=registry,
        )
        utterance_rows = []
        # #166: each say's stored proposition per claim id (a lie's divergence names it).
        said: list[tuple[int, str, dict[str, str]]] = []
        for beat in checked.beats:
            if beat.kind != SAY or beat.character_id is None:
                continue
            asserted = [c for c in beat.claims if c.proposition is not None]
            props = [
                await store.write_proposition(
                    conn, story_id, c.proposition, registry, base_revision=op.base_revision
                )
                for c in asserted
                if c.proposition is not None
            ]
            utterance_rows.append(utterance_of(beat, props))
            said.append(
                (
                    beat.beat,
                    beat.character_id,
                    {c.claim_id: p for c, p in zip(asserted, props, strict=True) if c.claim_id},
                )
            )
        if say:
            asserted = [c for c in checked.input_claims if c.proposition is not None]
            props = [
                await store.write_proposition(
                    conn, story_id, c.proposition, registry, base_revision=op.base_revision
                )
                for c in asserted
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
            said.append(
                (
                    1,
                    read.actor.id,
                    {c.claim_id: p for c, p in zip(asserted, props, strict=True) if c.claim_id},
                )
            )
        utterance_ids = await write_utterances(conn, op, utterance_rows, event_ids=ids)
        # #161: under per-character partitions, routing writes one memory per perceiver (the
        # level read now, under the story lock: a mid-turn change routes under the new one).
        level = await scene_loader.current_level(conn, story_id)
        # #163: the turn's modeled characters, before routing (the triggering beat lands in the
        # new partition: they join the routed scene).
        joined, participants = await model_committed(
            conn, op, read, rendered, checked, level=level, settings=settings, fixed=fixed
        )
        told: adjudicate.Testified | None = None
        spoken: speech.Spoken | None = None
        stepped: render_steps.Written | None = None
        with belief_transitions.tallying() as tally:
            if level == perception.NONE:
                await records.write_episodic(
                    conn, op, _episodic(checked, read, say=say, do=do, ids=ids), settings=settings
                )
            else:
                results: list[perception.PerceptionResult] = []
                # #164: the observation sink writes each perceiver's observations here.
                fixed["perception"] = await route_committed(
                    conn,
                    op,
                    checked,
                    read,
                    say=say,
                    do=do,
                    ids=ids,
                    level=level,
                    settings=settings,
                    routed=results,
                    joined=joined,
                    participants=participants,
                )
                # #162: every witness's speech act, and the testimony its salience calls for;
                # #164: the other witnesses' exposures, and the lazy resolutions it needs.
                told = await adjudicate.testify_committed(
                    conn,
                    op,
                    results,
                    checked=checked,
                    actor=read.actor.id,
                    say=say,
                    cast=list(read.names),
                    ids=ids,
                    adjudications=checked.adjudications,
                    read_set=read_set,
                    utterances={
                        beat: utterance_ids[n]
                        for n, (beat, _, _) in enumerate(said)
                        if n < len(utterance_ids)
                    },
                )
            if checked.verifier is not None and checked.verifier.beliefs is not None:
                # #166: what the first-person claims license: the speaker's exposures on the
                # claims' keys (the need site), licensed beliefs and the lies' divergences.
                used = 0 if told is None else sum(told.exposures["resolutions"].values())
                spoken = await speech.speech_committed(
                    conn,
                    op,
                    _spoken(checked, said, utterance_ids, ids),
                    settings=settings,
                    registry=registry,
                    resolutions_left=exposure_records.max_lazy_resolutions(settings) - used,
                )
            if (
                checked.step_verdicts
                and checked.verifier is not None
                and checked.verifier.beliefs is not None
            ):
                # #168: the render's verified steps: observations (with their memories) and
                # beliefs citing them (``antecedent = render_step``).
                stepped = await render_steps.steps_committed(
                    conn,
                    op,
                    holder=read.character.id,
                    steps=checked.steps,
                    verdicts=checked.step_verdicts,
                    first_beat=checked.steps_beat,
                    beat_owner=owners_of(checked),
                    settings=settings,
                    registry=registry,
                    remember=partial(
                        _remember_observation,
                        op=op,
                        read=read,
                        settings=settings,
                        registry=registry,
                    ),
                )
        if told is not None or spoken is not None or stepped is not None:
            fixed["epistemic"] = {
                **({} if told is None else {"testimony": told.testimony, **told.exposures}),
                "transitions": tally.report(),
                **({} if spoken is None else {"speech": spoken.report()}),
                **(
                    {}
                    if stepped is None
                    else {
                        "steps": {
                            "transitions": stepped.transitions,
                            "memories": stepped.memories,
                            "refused": stepped.refused,
                        }
                    }
                ),
            }
            if (told is not None and told.capped) or (spoken is not None and spoken.capped):
                checked.degradation.append(
                    (exposure_records.LAZY_RESOLUTION_CAPPED, "lazy_resolution")
                )
        if spoken is not None:
            fixed["divergences_written"] = len(spoken.divergence_ids)
        commit_report: dict[str, Any] = {}
        if lifecycle.commitment_enabled(settings):
            with replay.checking() as replayed:
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
                depended = await extraction.apply_dependences(
                    conn, op, checked.checked, read_set=read_set, registry=registry
                )
                depended += await resolve_details(conn, op, details, registry=registry)
                await settle_follow_up(conn, op, checked, read_set=read_set, registry=registry)
                # What the follow-up render did not settle is discharged by the minimal commit.
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
                    "follow_up": {
                        **checked.follow_up,
                        "path": _close_path(checked.follow_up, closed.minimal_commits),
                    },
                    **(await _boundary_report(conn, story_id, regenerating, depended, checked)),
                }
            if "epistemic" in fixed:
                # #165: the turn's could-have-known checks (its resolutions' soft flags).
                fixed["epistemic"]["could_have_known"] = replayed.report()
            for step in closed.degradation:
                checked.degradation.append((step, "commitment"))
            if commit_report["could_have_known_flags"]:
                checked.degradation.append((knowledge_flags.FLAG_DEGRADATION, "commitment"))
        else:
            commit_report = {"canonized": len(canonized)}
        fixed["commit"] = commit_report
        # FR-PROP-4: the auto predicates this turn's propositions used may now earn a
        # strengthening proposal (inert until the author approves it; #131, #491).
        await consider_proposals(
            conn,
            story_id,
            [
                *(r.claim.predicate_id for r in checked.requests),
                *(r.claim.predicate_id for r in latent_requests),
                *(
                    c.proposition.predicate_id
                    for c in (*checked.input_claims, *(c for b in checked.beats for c in b.claims))
                    if c.proposition is not None
                ),
            ],
            registry,
        )
        # #155: the turn's escalation records, in its commit.
        await yields.write(
            conn,
            story_id=story_id,
            operation_id=op.operation_id,
            position=(op.narrative_turn, op.turn_variant),
            passes=_verify_passes(checked),
            repaired={
                v.claim_id: result.accepted
                for result in checked.repairs
                for v in result.violations
                if v.claim_id is not None
            },
        )
        fixed["raw_render_event_ids"] = await _raw_render_ids(conn, op.operation_id, ids, checked)
        clock.add("commit", time.perf_counter() - committing)
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
        report["budget"] = planner.budget_report(plan, budget)
        steps = _degradation(gated[0] if gated else None, rendered, checked)
        report["degradation"] = [s.step for s in steps]
        report["degradation_details"] = [{"step": s.step, "cause": s.cause} for s in steps]
        if regenerating is not None:
            # The variant this regenerate replaced (#126, #154): the feedback aggregates count a
            # regeneration of that variant's message from it.
            report[REGENERATED_VARIANT] = regenerating.variant
        report["render"] = {
            "attempts": rendered.attempts,
            "structure_retries": rendered.structure_retries,
            "introductions": len(rendered.introductions),
            "introductions_denied": rendered.denied_introductions,
            "age_repairs": rendered.age_repairs,
            "narrator": rendered.narrator,
            # #168: the render's declared causal steps.
            "steps": render_steps.steps_report(len(checked.steps), checked.step_verdicts),
        }
        composition = composed[0] if composed else None
        message_outcomes = (
            [composition.detection]
            if composition is not None and composition.detection is not None
            else []
        )
        report["detection"] = extraction.detection_report(
            [*checked.outcomes, *message_outcomes], checked.checked
        )
        if composition is not None:
            report["composition"] = composition.report()
        if checked.verifier is not None:
            report["verify"] = checked.verifier.report()
            if "speech" in report["verify"]:
                # #166: the lies' divergence records, written in the commit.
                report["verify"]["divergences_written"] = fixed.get("divergences_written", 0)
            report["uncertain_claims"] = checked.verifier.uncertain_claims()
        if checked.follow_up_verifier is not None:
            report["verify_follow_up"] = checked.follow_up_verifier.report()
        report["commit"] = fixed.get("commit", {})
        if "perception" in fixed:
            report["perception"] = fixed["perception"]
        if "epistemic" in fixed:
            report["epistemic"] = fixed["epistemic"]
        if "modeling" in fixed:
            report["modeling"] = fixed["modeling"]
        if checked.repairs:
            report["repairs"] = repairs_report(checked)
        report["eval_hooks"] = eval_hooks(
            report,
            raw_render_event_ids=fixed.get("raw_render_event_ids", []),
            repair_rungs=[
                {"beat": r.original.beat, "rung": r.rung, "accepted": r.accepted}
                for r in checked.repairs
            ],
            clock=clock,
        )
        extraction.record_metrics([*checked.outcomes, *message_outcomes], checked.checked)
        for repaired in checked.repairs:
            count_repair(repaired.rung, repaired.repair_class.value)
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
            "message": FinishedMessage(
                message_id=reply.message_id,
                text=reply.text,
                mode=composed[0].mode if composed else None,
            ),
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
        if regenerating is not None:
            return RegenerateResult(**fields, previous_variant=regenerating.variant)
        return RespondResult(**fields)

    call = idempotency.current_call()
    if call is not None:
        call.result_in_commit = result
    retiring = () if regenerating is None else await _retiring(db, story_id, regenerating.turn)
    # A regenerate reads the story as it stood before the variant it replaces (#154): that
    # variant's records are left out of every derived read; its own commit retires them.
    with excluding(retiring):
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


async def _retiring(db: AsyncEngine, story_id: str, turn: int) -> tuple[str, ...]:
    """The active operations a regenerate of ``turn`` retires (``Operation._retire_variant``'s
    set, read before the turn so its reads can leave them out)."""
    async with db.connect() as conn:
        found = await conn.scalars(
            select(operations.c.operation_id)
            .where(
                operations.c.story_id == story_id,
                operations.c.narrative_turn >= turn,
                operations.c.status == OperationStatus.COMMITTED.value,
                active_operation(operations),
            )
            .order_by(operations.c.operation_id)
        )
        return tuple(str(i) for i in found)


async def _boundary_report(
    conn: AsyncConnection,
    story_id: str,
    regenerating: RecordedTurn | None,
    depended: Sequence[lifecycle.Dependence],
    checked: Checked,
) -> dict[str, Any]:
    """``turn_report.commit``'s commit-boundary counts (#154): what the regenerate reverted and
    reopened, the reconciliations its dependences ran and the bindings it rejected."""
    reverted = reopened = 0
    if regenerating is not None:
        found = await boundary.summary(conn, story_id, regenerating.turn, regenerating.variant)
        reverted, reopened = found.reverted_facts, found.reopened_thunks
    rejected = [r.as_payload() for r in checked.rejected_bindings]
    rejected += [
        {
            "claim_id": None,
            "beat": None,
            "thunk_id": d.thunk_id,
            "bound_entity": getattr(d.reconciliation, "bound_entity", None),
            "conflicting_records": list(getattr(d.reconciliation, "conflicting_records", ())),
            "conflicting_lines": list(getattr(d.reconciliation, "conflicting_lines", ())),
        }
        for d in depended
        if d.kind == "rejected"
    ]
    return {
        "reverted_facts": reverted,
        "reopened_thunks": reopened,
        "reconciliations": len(checked.rejected_bindings)
        + sum(1 for d in depended if d.reconciliation is not None),
        "rejected_bindings": rejected,
        # #165 (FR-COMMIT-11): a warning, never a rejection.
        "could_have_known_flags": [
            d.flag.as_payload() for d in depended if d.kind == "committed" and d.flag is not None
        ],
    }


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


def _spoken(
    checked: Checked,
    said: Sequence[tuple[int, str, Mapping[str, str]]],
    utterance_ids: Sequence[str],
    ids: Mapping[int, str],
) -> list[speech.SpokenBeat]:
    """The turn's says as #166's commit step reads them: each beat's speaker, utterance id
    (``write_utterances`` answers them in row order), classified claims and stored propositions."""
    by_beat: dict[int, list[CheckedClaim]] = {}
    for c in checked.checked:
        if c.claim.beat is not None and c.claim.channel == SAY:
            by_beat.setdefault(c.claim.beat, []).append(c)
    return [
        speech.SpokenBeat(
            beat=beat,
            speaker=speaker,
            utterance_id=utterance_ids[n] if n < len(utterance_ids) else None,
            claims=tuple(by_beat.get(beat, ())),
            proposition_ids=props,
            event_id=ids.get(beat),
        )
        for n, (beat, speaker, props) in enumerate(said)
    ]


def _relation(claims: Sequence[AssertedClaim], kind: str) -> str:
    from epistrel.core.turns.beats import objective_relation_of

    del kind
    return objective_relation_of(claims)


def _degradation(
    outcome: Gated | None, rendered: Rendered | None, checked: Checked | None
) -> list[DegradationStep]:
    """Every stage's degradation with its cause: the output gate's (``guardrail_fields``),
    retrieval's, detection's, verification's, the latent routing's, salience's and the
    ledger's, each once; NFR-PERF-7's steps first, in its order (``planner.ladder_rank``)."""
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
    # NFR-PERF-7's steps first, in its order; the rest as taken (#156).
    return sorted(steps, key=lambda s: planner.ladder_rank(s.step))


def owners_of(checked: Checked) -> dict[int, int]:
    """Each character beat's owning ``act`` step (#168)."""
    return render_steps.beat_steps([b for b in checked.beats if not b.narrator])


async def _remember_observation(
    conn: AsyncConnection,
    step: render_steps.Step,
    seen: render_steps.Observed,
    position: Any,
    *,
    op: Operation,
    read: TurnRead,
    settings: ProfileSettings,
    registry: Registry,
) -> bool:
    """A verified ``observe`` step's episodic memory (#168): the character's own, at the step's
    position, sourced from the observed fact's event when there is one."""
    if step.proposition is None:  # pragma: no cover - a verified observe names one
        return False
    stored = await store.write_proposition(
        conn, str(op.story_id), step.proposition, registry, base_revision=op.base_revision
    )
    found = await records.write_observed(
        conn,
        op,
        holder=read.character.id,
        proposition_id=stored,
        text=step.text,
        source_event_ids=[seen.event_id] if seen.event_id is not None else [],
        beat=position.beat,
        step=position.step,
        settings=settings,
    )
    return found is not None


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
    "FOLLOW_UP_FAILED",
    "FOLLOW_UP_SKIPPED_BUDGET",
    "INTRODUCTION_DENIED",
    "OPEN_REFERENCE",
    "SCENE_CHANGED",
    "UNADDRESSED",
    "Checked",
    "Rendered",
    "StagedSource",
    "TurnRead",
    "compose_turn",
    "follow_up",
    "is_structured",
    "owed_references",
    "play",
    "read_turn",
    "regenerate",
    "resolve_structured_cast",
    "respond",
    "run_queued",
]
