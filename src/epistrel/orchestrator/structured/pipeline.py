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

**Reconciliation and the audience filter** (#177): after #175's adjudication and the repairs,
``reconcile_checked`` reconciles the turn's concurrent commitments on one timeline
(``orchestrator.structured.reconcile``: a contradictory pair's loser rewritten as an attempt or
its ``latent`` dropped; ``turn_report.reconcile``, the response's ``reconcile``, a
``turn_reconciled`` audit event). The composition then takes the beats the output viewpoint
receives (``audience_of``, ``core.epistemic.audience``: routed over the scene at the turn's
start with the moves its canon writes stage, under the request's or the story's
``player_view``), the content it withheld held against it (``fidelity_leak``); the message
records the level and player view it was filtered under, and the response's beats come from the
same filter over the committed turn (``core.history.filtered_turn_beats``). The player's
``whisper_to`` qualifies the input's ``say`` like a render's (``target_absent`` repaired
silently); a ``think`` in the input is dropped (``turn_report.player_think_dropped``).

Logs carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import idempotency, pause, turn_gate
from epistrel.core.canon import facts as canon_facts
from epistrel.core.canon.canonize import CanonRequest, canonize_all
from epistrel.core.commit import boundary, hidden, lifecycle, matching
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
from epistrel.core.epistemic import audience as audience_rules
from epistrel.core.epistemic import context as character_context
from epistrel.core.epistemic import exposure as exposure_records
from epistrel.core.epistemic import modeling as char_modeling
from epistrel.core.epistemic import observation as _observation  # noqa: F401 - registers the sink
from epistrel.core.epistemic import perception, replay
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.epistemic import testimony as testimony_rules
from epistrel.core.epistemic import transitions as belief_transitions
from epistrel.core.epistemic.context import CharacterContext
from epistrel.core.epistemic.stage import Acceptance, StagedBeat, TurnStage
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
    TurnReconciled,
    VerifyVerdict,
)
from epistrel.core.genesis.structured import deferred_items
from epistrel.core.history import filtered_turn_beats
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
from epistrel.core.propositions.registry import (
    Registry,
    canonical,
    consider_proposals,
    file_identity_proposal,
)
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution import identity as identity_rules
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
from epistrel.core.verify.permission import Asserter
from epistrel.core.verify.protected import PrivateRecord
from epistrel.core.verify.rules import CANON, Record, VerdictKind, check
from epistrel.core.viewpoint import (
    is_omniscient,
    resolve_viewpoint,
    try_resolve,
)
from epistrel.core.visibility import Visibility
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
    TurnWarning,
    body_of,
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
from epistrel.orchestrator.structured import multi, planner
from epistrel.orchestrator.structured import player_authority as authority_rules
from epistrel.orchestrator.structured import prompt as prompts
from epistrel.orchestrator.structured import reconcile as reconcile_rules
from epistrel.orchestrator.structured import roles as roles_rt
from epistrel.orchestrator.structured import simultaneous as simultaneous_rules
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
_COMBINED = ProfileSettings()
"""The schema's defaults (``multi_character = combined``): the cast check without a profile."""


def is_structured(configuration: StoryProfile) -> bool:
    """Whether the story's effective configuration runs the structured pipeline (#129)."""
    return level_at_least(configuration.settings, PIPELINE, STRUCTURED_LEVEL)


# -- the cast -----------------------------------------------------------------------------------


def resolve_structured_cast(
    story: StoryCast,
    request: RespondRequest,
    settings: ProfileSettings | None = None,
    story_id: str = "",
) -> ResolvedCast:
    """The actor and the characters rendered: the request's (#174: several only under
    ``per_character`` renders in ``sequential`` mode, ``multi.check``; under ``combined`` two or
    more named are 422 ``validation_error``, ``one_render_character``), or one, the first
    non-player, non-``minor`` character after the actor by ``ordinal`` (#163; else the first
    non-player one, else the first other one, else the actor). ``settings``: the story's
    effective profile settings (``None``: one primary, as M4). #180: under ``player_authority =
    own_character`` a character the input asks something of renders (``player_authority``'s
    ``requested_renders``): after the named primaries under ``per_character`` renders, else as
    the default render character."""
    resolved = resolve_cast(story, request)
    if settings is None or not multi.per_character(settings):
        # #176: a stored role is inert here; a request's role override is refused.
        lite.refuse_role_overrides(request)
        promoted: tuple[str, ...] = ()
    else:
        named = [c.id for c in resolved.render] if request.render_character_ids else []
        promoted = roles_rt.promoted(story.cast, request, named, resolved.actor.id)
    if promoted:
        # #176: a ``primary`` override names the character, after the named ones.
        by_id = story.by_id()
        extra = tuple(by_id[c] for c in promoted if c in by_id)
        render = (*resolved.render, *extra) if request.render_character_ids else extra
        resolved = ResolvedCast(actor=resolved.actor, render=render)
        multi.check(settings or _COMBINED, story_id, story.settings, request, resolved.render)
        return resolved
    # #180 (FR-RESP-11): the characters an ``own_character`` input asks something of.
    asked = authority_rules.requested_renders(
        story.cast,
        request.player_input.say_text,
        request.player_input.do_text,
        story=story.settings,
        actor_id=resolved.actor.id,
        rendering=[c.id for c in resolved.render] if request.render_character_ids else [],
    )
    per_character = settings is not None and multi.per_character(settings)
    if request.render_character_ids is not None:
        if settings is None:
            multi.check(_COMBINED, story_id, story.settings, request, resolved.render)
        else:
            multi.check(settings, story_id, story.settings, request, resolved.render)
        if asked and per_character and multi.mode_of(story.settings, request) == multi.SEQUENTIAL:
            # Appended after the named primaries, in mention order.
            resolved = ResolvedCast(actor=resolved.actor, render=(*resolved.render, *asked))
        return resolved
    if not per_character:
        multi.refuse_override(story_id, request)
    if asked:
        # The requested characters render (one under ``combined``).
        return ResolvedCast(
            actor=resolved.actor, render=tuple(asked) if per_character else (asked[0],)
        )
    others = [c for c in story.cast if c.id != resolved.actor.id]
    # #163: a minor (lazily modeled) character is never the default render character.
    chosen = next(
        (c for c in others if not c.is_player and not c.minor),
        next((c for c in others if not c.is_player), others[0] if others else resolved.actor),
    )
    return ResolvedCast(actor=resolved.actor, render=(chosen,))


def _planned_cast(cast_: ResolvedCast, plan: planner.TurnPlan) -> ResolvedCast:
    """The cast the plan renders (#174: the primaries the budget deferred left out)."""
    if len(cast_.render) <= plan.primaries:
        return cast_
    return ResolvedCast(actor=cast_.actor, render=cast_.render[: plan.primaries])


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
    concurrency = await turn_gate.read_concurrency(db, story_id)
    resolved_cast = resolve_structured_cast(
        story, request, lite._captured(concurrency).settings, story_id
    )
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
                "body": body_of(request, "output_viewpoint")
                | {"render_character_ids": resolved_cast.render_ids},
                "actor_character_id": resolved_cast.actor.id,
                "viewpoint": try_resolve(resolution),
                "expected_revision": ctx.expected_revision,
            },
            sent={
                "story_id": story_id,
                "body": body_of(request),
                "viewpoint": ctx.viewpoint,
                "expected_revision": ctx.expected_revision,
            },
        )
    await check_expected_revision(db, story_id, ctx.expected_revision)
    viewpoint = resolution()
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
        director=multi.order_of(story.settings, request) == multi.DIRECTOR,
        stored=roles_rt.stored_of(story.cast, story.settings),
    )
    resolved_cast = _planned_cast(resolved_cast, plan)
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
        budget.release_to(planner.held_after_input(plan, budget))
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
                "body": body_of(request),
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
    resolved_cast = resolve_structured_cast(
        story, request, lite._captured(concurrency).settings, story_id
    )
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
        director=multi.order_of(story.settings, request) == multi.DIRECTOR,
        stored=roles_rt.stored_of(story.cast, story.settings),
    )
    resolved_cast = _planned_cast(resolved_cast, plan)
    budget.release_to(planner.held_after_input(plan, budget))
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
    concurrency = await turn_gate.read_concurrency(db, story_id)
    resolved_cast = resolve_structured_cast(
        story, replay, lite._captured(concurrency).settings, story_id
    )
    await check_expected_revision(db, story_id, ctx.expected_revision)
    viewpoint = resolve_viewpoint(
        ctx.viewpoint or recorded.viewpoint,
        actor_character_id=resolved_cast.actor.id,
        story_default=story.settings.default_output_viewpoint,
        cast_ids=story.ids,
    )
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
        director=multi.order_of(story.settings, replay) == multi.DIRECTOR,
        stored=roles_rt.stored_of(story.cast, story.settings),
    )
    resolved_cast = _planned_cast(resolved_cast, plan)
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
        budget.release_to(planner.held_after_input(plan, budget))
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
    narrator_memory: tuple[str, ...]
    last_beats: tuple[str, ...]
    scene: str
    present: tuple[str, ...]
    director: str
    triggers: tuple[str, ...]
    allow_new_arrivals: bool
    present_ids: tuple[str, ...] = ()
    """The entities authoritatively present (#170: the character's ``## Scene`` card)."""
    location_name: str | None = None
    deferred: bool = False
    """#154 (FR-COMMIT-11): an open thunk an earlier turn introduced is in the context, so the
    renders carry the deferral bias (``prompts.RESOLUTION_STEERING``)."""
    flagged: bool = False
    """#165 (FR-COMMIT-11): the previous turn's resolution raised a could-have-known soft flag,
    so this turn's renders (only) carry ``prompts.KNOWLEDGE_STEERING``."""
    characters: tuple[CastEntry, ...] = ()
    """#174: every primary in request order (``character`` is the one rendering: the first, or
    the current one inside a sequential turn's loop)."""


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
    # The orchestration context's shared view (detection's last beats); the character's own
    # prompt is #170's ``build_context``, viewpoint-filtered.
    mine = await records.working_memory(conn, story_id, character.id, None, n)
    everyone = await records.working_memory(conn, story_id, None, None, n)
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
        narrator_memory=narrator_memory,
        last_beats=last_beats,
        scene=scene_text,
        present=present,
        director="\n\n".join(director),
        triggers=tuple(triggers),
        allow_new_arrivals=story.settings.allow_new_arrivals,
        present_ids=tuple(e.entity_id for e in scene.present) if scene is not None else (),
        location_name=None if scene is None or scene.location is None else str(scene.location.name),
        deferred=lifecycle.commitment_enabled(settings) and await _deferred(conn, story_id, turn),
        flagged=lifecycle.commitment_enabled(settings) and await _flagged(conn, story_id, turn),
        characters=tuple(current.get(c.id, c) for c in cast_.render),
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
    context: CharacterContext | None = None
    """The rendering character's context (#170): what its prompt knew, and the exposures it
    needed (resolved in the commit)."""
    contexts: list[CharacterContext] = field(default_factory=list)
    """#174: every primary's context, in render order (one, for a single primary)."""
    reactions: list[Any] = field(default_factory=list)
    """#176: the reactions the character render declared (``render.ReactionDecl``)."""


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
                hidden=None
                if decl.kind != LATENT or decl.hidden is None
                else tuple(decl.hidden.noticers),
                observed="fact"
                if decl.kind != LATENT or decl.hidden is None
                else decl.hidden.observed,
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
        # #171: an entity canon holds to be a modeled character is that character.
        canon_ids = await identity_rules.identities_for(
            KeyedLookup(conn, StructuredReadSet()), story_id, identity_rules.CANON
        )
        known = await char_modeling.load_entities(
            conn,
            story_id,
            linked=identity_rules.linked_characters(
                canon_ids, [e for pair in canon_ids.pairs() for e in pair]
            ),
        )
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


@dataclass(frozen=True, slots=True)
class StepSet:
    """One character render's declared steps (#168) and their verdicts: whose, and where its
    steps sit (the render's first beat). A sequential turn has one per primary (#174)."""

    holder: str
    steps: list[render_steps.Step]
    verdicts: dict[int, render_steps.StepVerdict]
    first_beat: int


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
    step_sets: list[StepSet] = field(default_factory=list)
    """#174: every primary render's steps (the commit writes each for its holder)."""
    more_verifiers: list[Verifier] = field(default_factory=list)
    """#174: the later primaries' verifiers (the first is ``verifier``), in render order."""
    silent: frozenset[str] = frozenset()
    """#176: the turn's ``silent`` characters (never salient testimony witnesses)."""


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
        beyond=None
        if speech is None or speech.hidden not in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED)
        else speech.hidden.value,
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


def _turn_thoughts(beats: Sequence[TurnBeat]) -> list[PrivateRecord]:
    """#172: this turn's ``think`` beats as private records (the thought side index's
    turn-local part: every other speaker's beats are scanned for them)."""
    return [
        PrivateRecord.of(b.character_id, f"beat:{b.beat}", THINK, b.text)
        for b in beats
        if b.kind == THINK and b.character_id
    ]


async def check_turn(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    read: TurnRead,
    rendered: Rendered,
    **kwargs: Any,
) -> Checked:
    """``_check_turn`` with the render's beats at their declared steps (#168: ``step_of``) and
    its verified steps staged for the could-have-known replay (``replay.staged_paths``); #174:
    the rendering character's acceptances of earlier staged beats (``staged_beliefs``) staged
    there too."""
    owners = render_steps.beat_steps([b for b in rendered.beats if not b.narrator])
    with beat_steps(str(kwargs["operation_id"]), owners), replay.staged_paths():
        for accepted in kwargs.get("staged_beliefs", ()):
            replay.stage_path(
                accepted.witness, accepted.proposition, accepted.position, f"beat:{accepted.beat}"
            )
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
    release_extraction: bool = True,
    settle_salience: bool = True,
    staged_beliefs: Sequence[Acceptance] = (),
) -> Checked:
    """Detect and verify the player's input and every render's beats (see the module
    docstring). #174: a sequential turn's earlier primaries keep the unspent extraction and
    salience reservations for the later ones (``release_extraction``, ``settle_salience``), and
    a later primary's staged acceptances join its belief record set (``staged_beliefs``). A
    follow-up render's check (``first_claim``: where its claim ids start) sees the claims the
    turn's earlier beats staged (``staged_before``) and reads no salience. A
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
        # #176: a narrated reaction's reactor speaks in its own view.
        reactors = [
            str(b.character_id)
            for b in rendered.beats
            if b.narrated_by is not None and b.character_id is not None
        ]
        speakers = list(dict.fromkeys([input_speaker, read.character.id, *reactors]))
        env = await extraction.load_env(
            conn, story_id, read_set, position=(turn, 0), speakers=speakers, config=config
        )
    introductions = Introductions() if introductions is None else introductions
    env = replace(env, introductions=introductions).with_private(_turn_thoughts(rendered.beats))
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
    # #180: the ``co_author`` player's narrated beats are detected with the input, by layers
    # (a) and (b) only, each in its character's own view.
    input_beats += [
        DetectBeat(b.beat, b.kind, b.text, _speaker(b), declared.get(b.beat, ()))
        for b in rendered.beats
        if authority_rules.is_narrated(b)
    ]
    groups: list[list[DetectBeat]] = []
    if input_beats:
        groups.append(input_beats)
    narrator_beats = [b for b in rendered.beats if b.narrator]
    character_beats = [
        b for b in rendered.beats if not b.narrator and not authority_rules.is_narrated(b)
    ]
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
    if release_extraction:
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
        if verifier.beliefs is not None:
            # #174: what the character accepted from earlier staged beats (FR-EPI-14 (b)).
            for accepted in staged_beliefs:
                verifier.beliefs.stage(
                    accepted.witness,
                    accepted.proposition,
                    accepted.position,
                    f"beat:{accepted.beat}",
                )
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

        reactive_asserter = roles_rt.asserter_of(scopes)

        def asserter_of(claim: DetectedClaim) -> Any:
            beat = scopes.get(claim.beat or -1)
            if beat is not None and authority_rules.is_narrated(beat):
                # #180 (FR-RESP-11): the co_author player's narration of another character.
                return Asserter("co_author", read.actor.id)
            # #176 (FR-MULTI-9): a narrated reaction's claims are the reactive exception's.
            found = reactive_asserter(claim)
            return found or extraction.asserter_for(claim, read.player_id)

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
                asserter_of=asserter_of,
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
            # #169: a different value for a reference the world already fixed is contradicted.
            checked = await resolution.determined(conn, story_id, checked)
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
    if settle_salience:
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
        step_sets=[StepSet(read.character.id, steps, step_verdicts, rendered.steps_beat)]
        if steps
        else [],
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
    only: frozenset[int] | None = None,
) -> Checked:
    """FR-REPAIR-1..4 and FR-VERIFY-12 over the turn's verified beats (#151): every rendered
    beat with a claim that ``needs_repair`` goes to ``orchestrator.repair.ladder``, in beat
    order, and what it accepts replaces the beat at its position (a dropped ``think`` or
    ``latent`` leaves the turn). The corrected beat's claims, canon requests, latent canon and
    mentions replace the rejected beat's (nothing from a rejected render canonizes); its
    salient spans are not read again. After the follow-up render, before any commit
    transaction; a story whose profile has the repair module off (``repair_on``), or a turn
    released as the safe fallback, is left as it is. ``only`` (#174): the beats to look at (a
    sequential turn's earlier primaries were repaired before the next rendered)."""
    if not repair_on(configuration) or rendered.finish_reason == "fallback":
        return checked
    wanted = [
        (b, found)
        for b in checked.beats
        if b.beat > 1
        and (only is None or b.beat in only)
        # #180: the co_author player's narration is checked by its own rules, never repaired.
        and not authority_rules.is_narrated(b)
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
    input_qualifiers: perception.Qualifiers | None = None,
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
                    # #177: the player's whisper qualifies the input's ``say``.
                    qualifiers=input_qualifiers
                    if kind == SAY and input_qualifiers is not None
                    else perception.Qualifiers(),
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
                    narrated=beat.narrated_by is not None,
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
    mode: str = perception.SEQUENTIAL,
    input_qualifiers: perception.Qualifiers | None = None,
) -> dict[str, Any]:
    """Route the committed turn (#161), in its commit step after canon (so the moves the turn
    committed are in #137's placement projection): every beat through
    ``core.epistemic.perception.route_turn`` against the scene at the turn's start under
    ``level``, the results handed to ``PERCEPTION_SINKS`` (the memory sink writes one memory
    per perceiver). A ``latent`` that declared nothing normalizable keeps its shared
    ``objective`` memory. Answers ``turn_report.perception``: the level, the beats routed and
    every qualifier repaired (before staging, and any the commit-time scene no longer
    supports), each counted in ``epistrel_perception_repairs_total``. ``mode`` (#175): a
    ``simultaneous`` turn's beats all route against the starting placements."""
    story_id = op.story_id
    assert story_id is not None  # noqa: S101 - a turn belongs to a story
    state, moves = await scene_loader.load_scene(conn, story_id, read.turn)
    if joined:
        # #163: a character the turn modeled joins the scene from the turn's start.
        state = replace(state.moved(joined), characters=state.characters | {c for c, _ in joined})
    results = perception.route_turn(
        _routed_beats(
            checked,
            read,
            say=say,
            do=do,
            ids=ids,
            moves=moves,
            participants=participants or {},
            input_qualifiers=input_qualifiers,
        ),
        state,
        level,
        mode=mode,
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


def _hidden_latent(beat: TurnBeat) -> bool:
    """#169: a ``latent`` that commits a hidden fact: one declaring ``hidden``, or scoped to
    characters."""
    return beat.kind == LATENT and (
        beat.hidden is not None or beat.visibility.scope == "characters"
    )


async def hidden_committed(
    conn: AsyncConnection,
    op: Operation,
    checked: Checked,
    read: TurnRead,
    *,
    ids: Mapping[int, str],
    level: perception.Level,
    settings: ProfileSettings,
    registry: Any,
) -> list[hidden.HiddenCommit]:
    """The turn's hidden latents (#169, FR-COMMIT-4), in its commit under partitions: each
    normalized, uncontradicted proposition through ``core.commit.hidden.commit_hidden``, its
    noticers the declared ones (the scope's characters, else the rendering character) that
    perceive the beat (``perception.noticers`` over the scene at the turn's start)."""
    beats = {b.beat: b for b in checked.beats if _hidden_latent(b)}
    wanted = [(beats[b.beat], c) for b, c in checked.latent if b.beat in beats]
    if not wanted:
        return []
    story_id = str(op.story_id)
    state, _ = await scene_loader.load_scene(conn, story_id, read.turn)
    found: list[hidden.HiddenCommit] = []
    for beat, claim in wanted:
        if claim.proposition is None:  # pragma: no cover - checked.latent keeps normalized ones
            continue
        actor = None if beat.narrator else beat.character_id
        scoped = beat.visibility.characters or () if beat.visibility.scope == "characters" else ()
        candidates = tuple(beat.hidden or ()) or tuple(scoped) or ((actor,) if actor else ())
        routed = perception.RouteBeat(kind="latent", beat=beat.beat, actor=actor)
        found.append(
            await hidden.commit_hidden(
                conn,
                op,
                claim.proposition,
                noticers=perception.noticers(routed, state, candidates, level),
                observed="signal" if beat.observed == "signal" else "fact",
                event_id=ids.get(beat.beat),
                beat=beat.beat,
                provenance=RecordProvenance.DIRECTOR if beat.narrator else RecordProvenance.RENDER,
                recorded=beat.visibility,
                settings=settings,
                registry=registry,
            )
        )
    return found


async def identity_requests_of(
    conn: AsyncConnection,
    story_id: str,
    checked: Checked,
    kept: Sequence[CanonRequest],
    latent: Sequence[CanonRequest],
    *,
    partitioned: bool,
) -> tuple[list[CanonRequest], list[CanonRequest], list[CanonRequest], list[str]]:
    """#171 (FR-PROP-7): the turn's canon requests without its identity claims that commit
    elsewhere. A ``same_as`` between two of the story's characters is not committed (merging
    them is M7's amendment): an identity proposal is filed (``characters``) and its id
    reported. Under partitions a narration's ``same_as`` is set aside for
    ``identities_committed``. Answers the kept requests, the kept latent requests, the
    narrated identities and the refused proposals' ids."""
    pairs = {
        id(r): pair
        for r in (*kept, *latent)
        if (pair := identity_rules.pair_of(r.claim)) is not None
    }
    if not pairs:
        return list(kept), list(latent), [], []
    cast = await canon_facts.characters_among(
        conn, story_id, {e for pair in pairs.values() for e in pair}
    )
    narrated = {b.beat for b in checked.beats if b.kind == NARRATION}
    refused: list[str] = []
    narrated_requests: list[CanonRequest] = []

    def keep(requests: Sequence[CanonRequest]) -> list[CanonRequest]:
        out: list[CanonRequest] = []
        for request in requests:
            pair = pairs.get(id(request))
            if pair is not None and set(pair) <= cast:
                refused.append(pair[0] + ":" + pair[1])
                continue
            if pair is not None and partitioned and request.beat in narrated:
                narrated_requests.append(request)
                continue
            out.append(request)
        return out

    kept_out, latent_out = keep(kept), keep(latent)
    proposals: list[str] = []
    for key in dict.fromkeys(refused):
        a, b = key.split(":")
        proposals.append(await file_identity_proposal(conn, story_id, (a, b), reason="characters"))
    return kept_out, latent_out, narrated_requests, proposals


async def identities_committed(
    conn: AsyncConnection,
    op: Operation,
    checked: Checked,
    read: TurnRead,
    requests: Sequence[CanonRequest],
    *,
    ids: Mapping[int, str],
    level: perception.Level,
    settings: ProfileSettings,
    registry: Any,
) -> list[hidden.HiddenCommit]:
    """#171: each narrated ``same_as`` through ``core.commit.hidden.commit_identity``: its
    perceivers the characters where the narration happens (``perception.noticers`` over the
    scene at the turn's start: its declared ``location``, else the scene's; everyone at
    perception ``none``), its selves the characters it names."""
    if not requests:
        return []
    story_id = str(op.story_id)
    state, _ = await scene_loader.load_scene(conn, story_id, read.turn)
    beats = {b.beat: b for b in checked.beats if b.kind == NARRATION}
    found: list[hidden.HiddenCommit] = []
    for request in requests:
        beat = beats.get(request.beat or -1)
        pair = identity_rules.pair_of(request.claim)
        if beat is None or pair is None:  # pragma: no cover - identity_requests_of kept them
            continue
        routed = perception.RouteBeat(
            kind="narration", beat=beat.beat, actor=None, qualifiers=beat.qualifiers
        )
        found.append(
            await hidden.commit_identity(
                conn,
                op,
                request.claim,
                perceivers=perception.noticers(routed, state, state.characters, level),
                selves=sorted(set(pair) & set(state.characters)),
                event_id=ids.get(beat.beat),
                beat=beat.beat,
                provenance=request.provenance,
                settings=settings,
                registry=registry,
            )
        )
    return found


def _content_perceivers(results: Sequence[perception.PerceptionResult]) -> dict[int, set[str]]:
    """Each routed beat's content-fidelity perceivers (a partial eavesdrop is not content)."""
    found: dict[int, set[str]] = {}
    for result in results:
        found.setdefault(result.beat.beat, set()).update(
            p.character_id
            for p in result.perceived
            if p.fidelity == perception.CONTENT and p.partial_text is None
        )
    return found


async def reveals_committed(
    conn: AsyncConnection,
    op: Operation,
    checked: Checked,
    read: TurnRead,
    results: Sequence[perception.PerceptionResult],
    *,
    ids: Mapping[int, str],
    level: perception.Level,
    registry: Any,
) -> list[hidden.Reveal]:
    """The turn's reveals (#169): every rendered ``say`` (never a deceptive one) or ``do`` whose
    supported claims assert a hidden fact its actor reads, widened to the beat's content
    perceivers (``core.commit.hidden.reveal``; at perception ``none`` everyone in the scene)."""
    content = _content_perceivers(results)
    everyone: frozenset[str] | None = None
    found: list[hidden.Reveal] = []
    for beat in checked.beats:
        if beat.kind not in (SAY, DO) or beat.character_id is None or beat.narrator:
            continue
        claims = [
            c.proposition
            for c in beat.claims
            if c.proposition is not None and c.verdict == "supported" and not c.attributed
        ]
        if not claims:
            continue
        perceivers: Sequence[str] | frozenset[str]
        if level == perception.NONE:
            if everyone is None:
                state, _ = await scene_loader.load_scene(conn, str(op.story_id), read.turn)
                everyone = state.characters
            perceivers = everyone
        else:
            perceivers = sorted(content.get(beat.beat, ()))
        found += await hidden.reveal(
            conn,
            op,
            claims,
            speaker=beat.character_id,
            perceivers=perceivers,
            beat=beat.beat,
            event_id=ids.get(beat.beat),
            intent=beat.intent if beat.kind == SAY else "unspecified",
            registry=registry,
        )
    return found


def agent_scope(
    results: Sequence[perception.PerceptionResult],
) -> Callable[[CheckedClaim], Visibility | None]:
    """#169 (FR-COMMIT-6,13): under partitions, a modeled character's ``do`` resolving a thunk
    about itself ("I knock") commits on its side: scoped to it and the beat's content
    perceivers (``None``: any other dependence, which stays ``objective``)."""
    content = _content_perceivers(results)

    def of(c: CheckedClaim) -> Visibility | None:
        claim = c.claim
        speaker = claim.speaker
        if claim.channel != DO or claim.beat is None or claim.beat not in content:
            return None
        if speaker in (None, PLAYER, NARRATOR) or claim.proposition is None:
            return None
        if claim.proposition.subject_id != speaker:
            return None
        return Visibility(
            scope="characters", characters=tuple({str(speaker), *content[claim.beat]})
        )

    return of


def _is_latent(checked: Checked, number: int | None) -> bool:
    return any(b.beat == number and b.kind == LATENT for b in checked.beats)


def _verify_passes(checked: Checked) -> list[yields.VerifyPass]:
    """The turn's verifiers as #155's verification passes: the renders' (the follow-up's
    included) and each repair re-check's."""
    found = [
        yields.VerifyPass(v, yields.RENDER)
        for v in (checked.verifier, *checked.more_verifiers, checked.follow_up_verifier)
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
    heard: Heard | None = None,
) -> composing.Composition:
    """The turn's message for ``viewpoint`` (``orchestrator.compose.compose``) from its
    post-repair beats: the polish under #68's output gate and the final check, or the
    deterministic assembly (see the module docstring). ``heard`` (#177): the beats the
    viewpoint receives and those the audience filter withheld (``audience_of``)."""
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
            # The message names the turn's places and objects by the beats' minted ids (#479),
            # and is scanned for the turn's private thoughts too (#172).
            env = replace(env, introductions=checked.introductions).with_private(
                _turn_thoughts(checked.beats)
            )
            if speaker == NARRATOR:
                # #484 (owner's option (a)): the turn's render characters are not the narrator's
                # "other character" on the composition path.
                env = replace(env, context_for=_composing(env.context_for, render_ids))
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
    turn_beats_ = [b for b in checked.beats if b.beat > 1]
    shown = turn_beats_ if heard is None else heard.shown
    hidden = [] if is_omniscient(viewpoint) else [b for b in turn_beats_ if b.kind in PRIVATE_KINDS]
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
        withheld=[] if heard is None else heard.withheld,
    )
    return await composing.compose(shown, viewpoint, budget, composer=composer, reserved=reserved)


def _composing(
    context_for: Callable[[str], Any], render_ids: Sequence[str]
) -> Callable[[str], Any]:
    """``context_for`` with the composed renders' characters on every context (#484)."""
    composed = frozenset(render_ids)

    def found(speaker: str) -> Any:
        return replace(context_for(speaker), composing=composed)

    return found


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


def _replaced_render(
    read: TurnRead, fallback: FallbackLines, policies: Sequence[Any], *, start: int = 2
) -> Rendered:
    """The render(s) a deadline left without their reserved extraction, replaced by one
    ``say`` of the rendering character: the first safe-fallback line the deterministic output
    policies pass (#68's lines; ``think``/``latent`` beats and introductions are dropped)."""
    plain = [p for p in policies if not isinstance(p.rule, ModelAssistedRule)]
    candidates = fallback.candidates()
    line = next(
        (t for _, t in candidates if not evaluate(t, plain, Target.TEXT_OUTPUT)), candidates[-1][1]
    )
    return Rendered(
        beats=[TurnBeat(kind="say", beat=start, text=line, character_id=read.character.id)],
        sketches={},
        introductions=[],
        retrievals=[],
        finish_reason="fallback",
    )


# -- a sequential turn's renders (#174) ----------------------------------------------------------


def _restaged(before: Sequence[Record], checked: Checked) -> list[Record]:
    """The same-turn overlay after a primary's own repair: what the earlier primaries staged and
    this primary's surviving permitted claims (a repaired-away claim never reaches a later
    render's verification)."""
    return [
        *before,
        *(Record(r.claim, f"staged:{r.claim_id or r.beat}") for r in checked.requests),
    ]


def _simultaneous_of(multis: Sequence[multi.MultiReport]) -> str | None:
    """``simultaneous`` for a turn whose several primaries rendered simultaneously (#175)."""
    if multis and multis[0].mode == multi.SIMULTANEOUS and len(multis[0].order) > 1:
        return multi.SIMULTANEOUS
    return None


def _starting_overlay(rendered: Rendered, checked: Checked) -> list[Record]:
    """#175: the same-turn overlay of a ``simultaneous`` turn's starting state, from its first
    primary's check: the player's input's and the narration's permitted claims, never the
    primary's own (a later primary is verified alone); #180: the ``co_author`` player's narrated
    beats are part of it."""
    own = {b.beat for b in rendered.beats if not b.narrator and not authority_rules.is_narrated(b)}
    mine = {f"staged:{r.claim_id or r.beat}" for r in checked.requests if r.beat in own}
    return [r for r in checked.staged if r.proposition_id not in mine]


async def _entity_names(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> dict[str, str]:
    """The canonical names of the entities ``ids``."""
    if not ids:
        return {}
    found = await conn.execute(
        select(entities.c.id, entities.c.canonical_name).where(
            entities.c.story_id == story_id, entities.c.id.in_(list(ids))
        )
    )
    return {str(r.id): str(r.canonical_name) for r in found}


# -- reconciliation and the audience filter (#177) -----------------------------------------------


def _render_index(beats: Sequence[TurnBeat], firsts: Sequence[int]) -> dict[int, int]:
    """Each beat's render (its place in render order), from each render's first beat."""
    found: dict[int, int] = {}
    for b in beats:
        index = 0
        for k, first in enumerate(firsts):
            if first <= b.beat:
                index = k
        found[b.beat] = index
    return found


async def reconcile_checked(
    db: AsyncEngine,
    story_id: str,
    read: TurnRead,
    checked: Checked,
    *,
    mode: str,
    order: Sequence[str],
    firsts: Sequence[int],
    settled: Sequence[tuple[int, int]] = (),
) -> reconcile_rules.Reconciliation:
    """The turn's concurrent commitments reconciled (``orchestrator.structured.reconcile``)
    and the losers rewritten in ``checked``: a losing ``do`` as an attempt (#151's rung 2, its
    claims, canon requests and mentions gone, a ``render_rejected`` record), a losing
    ``latent``'s canon write dropped, any other loser's canon request dropped."""
    renders = _render_index(checked.beats, firsts)
    commitments = reconcile_rules.commitments_of(
        checked.requests, checked.latent, checked.beats, render_of=renders
    )
    if len(commitments) < 2:
        return reconcile_rules.Reconciliation()
    async with db.connect() as conn:
        registry = await load_registry(conn, story_id)
    saw = (
        {(a, b) for a in renders for b in renders if renders[a] < renders[b]}
        if mode == multi.SEQUENTIAL
        else set()
    )
    found = reconcile_rules.reconcile_turn(
        commitments, mode=mode, registry=registry, order=order, saw=saw, settled=settled
    )
    if not found.pairs:
        return found
    if found.attempts:
        async with db.connect() as conn:
            object_names = await _entity_names(conn, story_id, list(found.contested.values()))
        by_beat = {b.beat: b for b in checked.beats}
        for number in found.attempts:
            beat = by_beat.get(number)
            if beat is None or beat.kind != DO:
                continue
            losing = [c for c in checked.checked if c.claim.beat == number and c.claim.proposition]
            _merge_repair(
                checked,
                simultaneous_rules.attempt_repair(
                    beat,
                    losing,
                    actor=read.names.get(beat.character_id or "", "Someone"),
                    obj=object_names.get(found.contested.get(number, ""), "it"),
                ),
            )
    dropped = found.dropped_latents | found.dropped_requests
    if dropped:
        checked.latent = [(b, c) for b, c in checked.latent if (b.beat, c.claim_id) not in dropped]
        checked.requests = [r for r in checked.requests if (r.beat, r.claim_id) not in dropped]
    log.debug("reconciled turn: %d pairs", len(found.pairs))
    return found


@dataclass(frozen=True, slots=True)
class Heard:
    """What the output viewpoint receives of the turn's beats (#177; ``audience_of``): the
    beats the composition takes (an envelope as a ``do`` of its envelope text, an eavesdropper's
    part as that part), the beats it withheld, the ``audience_filter`` and the level."""

    shown: list[TurnBeat]
    withheld: list[TurnBeat]
    audience_filter: str
    level: perception.Level


async def _staged_moves(
    conn: AsyncConnection, story_id: str, checked: Checked
) -> dict[int, tuple[tuple[str, str | None], ...]]:
    """The moves the turn's canon writes stage, by beat (#177): each ``located_at`` of a
    character's entity a canon request or a ``latent`` commits (negated: an exit, unplaced), so
    the output is routed as the commit will route the memories."""
    located = CORE_BY_NAME["located_at"].id
    claims = [(r.beat, r.claim) for r in checked.requests] + [
        (b.beat, c.proposition) for b, c in checked.latent
    ]
    wanted = [(n, p) for n, p in claims if n and p is not None and p.predicate_id == located]
    if not wanted:
        return {}
    found = await conn.execute(
        select(entities.c.id, entities.c.character_id).where(
            entities.c.story_id == story_id, entities.c.character_id.is_not(None)
        )
    )
    character_of = {str(r.id): str(r.character_id) for r in found}
    moves: dict[int, list[tuple[str, str | None]]] = {}
    for number, p in wanted:
        who = character_of.get(str(p.subject_id))
        if who is None:
            continue
        affirmed = p.polarity is Polarity.AFFIRMED
        to = str(p.object_id) if affirmed and p.object_id is not None else None
        if to is None and any(w == who for w, _ in moves.get(int(number), [])):
            continue
        moves.setdefault(int(number), []).append((who, to))
    return {n: tuple(m) for n, m in moves.items()}


async def audience_of(
    db: AsyncEngine,
    story_id: str,
    read: TurnRead,
    checked: Checked,
    *,
    viewpoint: str,
    player_view: audience_rules.PlayerView,
    settings: ProfileSettings,
    say: str | None,
    do: str | None,
    input_qualifiers: perception.Qualifiers,
    mode: str = perception.SEQUENTIAL,
) -> Heard:
    """The turn's beats as ``viewpoint`` receives them, before the commit (FR-RESP-13;
    ``core.epistemic.audience.filter_for_viewpoint``): routed over the scene at the turn's
    start, the channels and barriers its beats declare and the moves its canon writes stage."""
    level = scene_loader.level_of(settings)
    beats = [b for b in checked.beats if b.beat > 1]
    stated = audience_rules.audience_filter_of(viewpoint, level, player_view)
    if stated == "none":
        return Heard(beats, [], stated, level)
    async with db.connect() as conn:
        state, _ = await scene_loader.load_scene(conn, story_id, read.turn)
        moves = await _staged_moves(conn, story_id, checked)
        names, places = await records.places_and_names(conn, story_id)
    state = perception.with_declared(
        state,
        [c.proposition for b in checked.beats for c in b.declared if c.verdict != "contradicted"],
    )
    heard: list[audience_rules.AudienceBeat] = []
    for kind, text in ((SAY, say), (DO, do)):
        if text:
            heard.append(
                audience_rules.AudienceBeat(
                    kind=kind,
                    beat=1,
                    character_id=read.actor.id,
                    text=text,
                    qualifiers=input_qualifiers if kind == SAY else perception.Qualifiers(),
                )
            )
    heard += [
        audience_rules.AudienceBeat(
            kind=b.kind,
            beat=b.beat,
            character_id=None if b.kind == NARRATION else b.character_id,
            text=b.text,
            qualifiers=b.qualifiers,
            source=b,
        )
        for b in sorted(checked.beats, key=lambda b: b.beat)
    ]
    for index, item in enumerate(heard):
        last = index == len(heard) - 1 or heard[index + 1].beat != item.beat
        if last and moves.get(item.beat):
            heard[index] = replace(item, moves=moves[item.beat])
    filtered = audience_rules.filter_for_viewpoint(
        heard,
        viewpoint,
        scene=state,
        level=level,
        names=names,
        places=places,
        player_view=player_view,
        mode=mode,
    )
    shown: list[TurnBeat] = []
    for seen in filtered.seen:
        source = seen.beat.source
        if not isinstance(source, TurnBeat):
            continue
        if seen.envelope:
            shown.append(
                replace(
                    source,
                    kind="do",
                    text=seen.text,
                    claims=(),
                    mentions=(),
                    qualifiers=perception.Qualifiers(),
                )
            )
        elif seen.partial:
            shown.append(replace(source, text=seen.text, claims=()))
        else:
            shown.append(source)
    withheld = [b.source for b in filtered.withheld if isinstance(b.source, TurnBeat)]
    return Heard(shown, withheld, filtered.audience_filter, level)


def _filtered_level(heard: Sequence[Heard]) -> str | None:
    """The level a message was audience-filtered at (``None``: perception ``none``)."""
    if not heard or heard[0].level == perception.NONE:
        return None
    return heard[0].level


def _merge_checked(into: Checked, extra: Checked) -> Checked:
    """Lay a later primary's check over the turn's so far (its beats after the earlier ones)."""
    into.beats += extra.beats
    into.checked += extra.checked
    into.outcomes += extra.outcomes
    into.requests += extra.requests
    into.latent += extra.latent
    into.mentions.update(extra.mentions)
    into.degradation += [d for d in extra.degradation if d not in into.degradation]
    into.staged = extra.staged
    into.salient += extra.salient
    into.salient_characters = list(
        dict.fromkeys([*into.salient_characters, *extra.salient_characters])
    )
    into.repairs += extra.repairs
    into.rejected_bindings += extra.rejected_bindings
    into.repair_verifiers += extra.repair_verifiers
    into.step_sets += extra.step_sets
    if extra.verifier is not None:
        into.more_verifiers.append(extra.verifier)
    into.more_verifiers += extra.more_verifiers
    return into


def _merged_rendered(renders: Sequence[Rendered]) -> Rendered:
    """A sequential turn's renders as one (the first primary's inputs and steps kept; every
    primary's context in ``contexts``)."""
    first = renders[0]
    return Rendered(
        beats=[b for r in renders for b in r.beats],
        sketches={n: v for r in renders for n, v in r.sketches.items()},
        introductions=[i for r in renders for i in r.introductions],
        retrievals=[x for r in renders for x in r.retrievals],
        attempts=sum(r.attempts for r in renders),
        structure_retries=sum(r.structure_retries for r in renders),
        age_repairs=sum(r.age_repairs for r in renders),
        denied_introductions=sum(r.denied_introductions for r in renders),
        narrator=any(r.narrator for r in renders),
        model=renders[-1].model or first.model,
        prompt_hash=renders[-1].prompt_hash or first.prompt_hash,
        finish_reason="fallback" if all(r.finish_reason == "fallback" for r in renders) else "stop",
        character_inputs=first.character_inputs,
        narrator_inputs=first.narrator_inputs,
        steps=first.steps,
        steps_beat=first.steps_beat,
        context=first.context,
        contexts=[c for r in renders for c in r.contexts],
    )


def _merged_gated(outcomes: Sequence[Gated]) -> Gated:
    """A sequential turn's output-gate outcomes as one."""
    used = [o for o in outcomes if o.fallback_used]
    return replace(
        outcomes[-1],
        violations=tuple(v for o in outcomes for v in o.violations),
        fallback_used=bool(used),
        fallback_source=used[0].fallback_source if used else None,
        rejected=tuple(r for o in outcomes for r in o.rejected),
        attempts=sum(o.attempts for o in outcomes),
        fallback_cause=next((o.fallback_cause for o in outcomes if o.fallback_cause), None),
        quarantined=tuple(q for o in outcomes for q in o.quarantined),
        pins_quarantined=tuple(q for o in outcomes for q in o.pins_quarantined),
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
    staging: list[TurnStage] = []
    """#174: the turn's staged beats (a ``sequential`` turn's earlier primaries)."""
    multis: list[multi.MultiReport] = []
    """#174: ``turn_report.multi``."""
    roles_state: list[roles_rt.TurnRoles] = []
    """#176: the turn's participation roles and reactive narration (per-character renders)."""
    view: audience_rules.PlayerView = request.player_view or story.settings.player_view
    """#177: the player view the output is produced under (the request's, else the story's)."""
    input_qualifiers: list[perception.Qualifiers] = [perception.Qualifiers()]
    """#177: the player's ``say``'s validated ``whisper_to``."""
    reconciled: list[reconcile_rules.Reconciliation] = []
    """#177: the turn's reconciliation (``turn_report.reconcile``)."""
    settled_pairs: list[tuple[int, int]] = []
    """#175's adjudicated ``(winner, loser)`` beats, which reconciliation skips."""
    heard_state: list[Heard] = []
    """#177: what the output viewpoint receives of the turn."""
    authority: list[authority_rules.Split] = []
    """#180 (FR-RESP-11): what the story's ``player_authority`` made of the input (its requests,
    narrated beats and dropped spans); ``say``/``do`` become the player's own remainder."""
    typed_say, typed_do = say, do

    async def stage_render(
        turn_stage: TurnStage,
        read_k: TurnRead,
        checked_k: Checked,
        later: Sequence[CastEntry],
    ) -> None:
        """#174 (FR-MULTI-5,11, FR-EPI-14 (b)): primary ``read_k.character``'s verified and
        repaired beats join the stage, each with the propositions its claims verified
        (``supported``, or permitted and canonized at the commit); under per-character
        partitions, each later primary that perceived a ``say``'s content forms its testimony
        acceptance now (#162's default over its records), which joins its belief record set
        for its own render's verification."""
        canonized = {r.claim_id for r in checked_k.requests if r.claim_id}
        verified: dict[int, list[Proposition]] = {}
        for c in checked_k.checked:
            proposition = c.claim.proposition
            if proposition is None or c.claim.beat is None or c.claim.beat <= 1:
                continue
            if c.verdict == "supported" or (c.claim_id is not None and c.claim_id in canonized):
                verified.setdefault(c.claim.beat, []).append(proposition)
        for b in checked_k.beats:
            if b.beat <= 1:
                continue
            turn_stage.stage(
                StagedBeat(
                    beat=b.beat,
                    kind=b.kind,
                    character_id=b.character_id,
                    text=b.text,
                    qualifiers=b.qualifiers,
                    narrator=b.narrator,
                    step=b.step,
                ),
                verified.get(b.beat, ()),
            )
        if not later or not module_on(settings, belief_transitions.PARTITIONS_MODULE):
            return
        level = scene_loader.level_of(settings)
        async with db.connect() as conn:
            scene = (
                None
                if level == perception.NONE
                else (await scene_loader.load_scene(conn, story_id, read_k.turn))[0]
            )
            names, places = await records.places_and_names(conn, story_id)
            registry = await load_registry(conn, story_id)
            lookup = KeyedLookup(conn, read_set)
            for witness in later:
                heard = {
                    b.beat
                    for b in turn_stage.perceived_for(
                        witness.id, level=level, scene=scene, names=names, places=places
                    )
                    if b.fidelity == "content"
                }
                for b in checked_k.beats:
                    props = verified.get(b.beat, [])
                    if (
                        b.kind != SAY
                        or b.character_id in (None, witness.id)
                        or b.beat not in heard
                        or not props
                    ):
                        continue
                    speaker = str(b.character_id)
                    act = testimony_rules.SpeechAct(
                        speaker=speaker, beat=b.beat, propositions=tuple(props), text=b.text
                    )
                    consistency = await testimony_rules.consistency_of(
                        lookup, story_id, witness.id, props, registry=registry
                    )
                    trust = await testimony_rules.trust(conn, story_id, witness.id, speaker)
                    for outcome in testimony_rules.testimony(
                        witness.id,
                        act,
                        testimony_rules.perceived(b.intent),
                        consistency=consistency,
                        trust=trust,
                    ):
                        if (
                            outcome.transition is belief_transitions.Kind.TESTIMONY
                            and outcome.proposition is not None
                        ):
                            turn_stage.accept(
                                witness.id,
                                b.beat,
                                outcome.proposition,
                                testimony_rules.witness_position(read_k.turn, 0, b.beat),
                            )

    async def adjudicate_conflicts(
        read: TurnRead,
        checked: Checked,
        order: Sequence[CastEntry],
        report_multi: multi.MultiReport,
        turn_stage: TurnStage,
    ) -> None:
        """#175 (FR-MULTI-8; ``orchestrator.structured.simultaneous``): the simultaneous turn's
        conflicts, detected over its verified effects, settled by the story's tie-break; each
        losing beat repaired in character as an attempt (its effect dropped) and restaged."""
        rule, warning = multi.tie_break_of(story.settings, request)
        report_multi.tie_break = rule
        report_multi.tie_break_warning = warning
        async with db.connect() as conn:
            registry = await load_registry(conn, story_id)
            conflicts = simultaneous_rules.conflicts_of(
                checked.checked,
                checked.beats,
                [p.id for p in order],
                actor_id=read.actor.id,
                registry=registry,
            )
            object_names = await _entity_names(conn, story_id, [c.object_id for c in conflicts])
        if not conflicts:
            return
        winners: dict[str, str] | None = None
        if rule == simultaneous_rules.DIRECTOR:
            judged = [c for c in conflicts if not any(e.player for e in c.effects)]
            if judged:
                winners, cause = await simultaneous_rules.director_winners(
                    models,
                    judged,
                    {b.beat: b for b in checked.beats},
                    names=read.names,
                    object_names=object_names,
                    actor=read.actor,
                    say=say,
                    do=do,
                    guidance=read.inputs.guidance,
                    instructions=read.inputs.instructions,
                    scenario_text=read.inputs.scenario_text,
                    director_notes=read.director,
                    scene=read.scene,
                    recent=read.narrator_memory,
                    budget=budget,
                    params=ModelParams(
                        temperature=read.resolved.temperature, top_p=read.resolved.top_p
                    ),
                )
                if cause is not None:
                    checked.degradation.append(
                        (simultaneous_rules.DIRECTOR_TIEBREAK_SKIPPED, cause)
                    )
        settled = simultaneous_rules.settle(conflicts, winners)
        report_multi.adjudications = [a.report() for a in settled]
        settled_pairs[:] = [(a.winner_beat, a.loser_beat) for a in settled]
        losing = simultaneous_rules.losing_claims(checked.checked, settled)
        by_beat = {b.beat: b for b in checked.beats}
        for adjudication in sorted(
            {a.loser_beat: a for a in settled}.values(), key=lambda a: a.loser_beat
        ):
            beat = by_beat.get(adjudication.loser_beat)
            if beat is None:
                continue
            result = simultaneous_rules.attempt_repair(
                beat,
                losing.get(beat.beat, []),
                actor=read.names.get(adjudication.loser, "Someone"),
                obj=object_names.get(adjudication.object_id, "it"),
            )
            _merge_repair(checked, result)
            staged = turn_stage.get(beat.beat)
            if staged is not None and result.beat is not None:
                turn_stage.replace(beat.beat, replace(staged, text=result.beat.text, verified=()))
        log.debug("simultaneous turn: %d conflicts, %d adjudicated", len(conflicts), len(settled))

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

    adjudicated: dict[str, tuple[Any, float] | None] = {}
    """#170: the needed exposures the model adjudicated this turn, by id (once per turn)."""
    context_degradation: list[tuple[str, str]] = []
    adjudicator = adjudicate.ExposureAdjudicator(models, settings, budget, context_degradation)

    async def adjudicate_needed(
        conn: AsyncConnection, item: Any, outcome: Any
    ) -> tuple[Any, float] | None:
        if item.id not in adjudicated:
            adjudicated[item.id] = await adjudicator(conn, item, outcome)
        return adjudicated[item.id]

    async def context_for(
        read: TurnRead, narration: Sequence[TurnBeat], staged: TurnStage | None = None
    ) -> CharacterContext:
        """The rendering character's context (#170's ``build_context``): this turn's input and
        narration routed like history, retrieval under its viewpoint (the turn's read set,
        embedders counted); #174: in a ``sequential`` turn, the earlier primaries' staged beats
        as it perceived them (``## Just now``)."""
        embeddings = models.embeddings
        pending = [
            *([character_context.PendingBeat("say", read.actor.id, say)] if say else []),
            *([character_context.PendingBeat("do", read.actor.id, do)] if do else []),
            # #180: the co_author player's narrated beats, as they happened.
            *(
                character_context.PendingBeat(
                    "say" if n.kind == "say" else "do", n.character_id, n.text
                )
                for n in (authority[0].narrated if authority else ())
            ),
            *(
                character_context.PendingBeat("narration", None, b.text, b.qualifiers)
                for b in narration
            ),
        ]
        state = character_context.TurnState(
            story_id=story_id,
            turn=read.turn,
            settings=settings,
            say=say,
            do=do,
            pending=tuple(pending),
            location_name=read.location_name,
            present=read.present_ids,
            scene_text=read.scene,
            read_set=read_set,
            embed=counted_embedder(embeddings.query_embedder(), budget),
            embed_batch=counted_embedder(embeddings.batch_embedder(), budget),
            lexical=lexical,
            adjudicate=adjudicate_needed if planner.adjudicates(settings) else None,
            stage=staged if staged is not None and staged.beats else None,
        )
        async with db.begin() as conn:
            found = await character_context.build_context(conn, read.character.id, state)
        if found.retrieval is not None:
            # NFR-PERF-1's span: the pipeline's own total, the query embedding left out (#153).
            stages.observe("retrieve", found.retrieval.timings.get("total", 0.0) / 1000)
        return found

    async def generate(op: Operation, read: TurnRead) -> None:
        nonlocal say, do
        # #180 (FR-RESP-11): the input's spans about other characters, per the story's
        # ``player_authority``, out of the player's own text before anything renders.
        split = authority_rules.split(
            typed_say,
            typed_do,
            mode=authority_rules.mode_of(story.settings),
            cast_=story.cast,
            actor_id=read.actor.id,
            present=authority_rules.present_of(story.cast, read.present),
            roles=roles_rt.stored_of(story.cast, story.settings)
            if multi.per_character(settings)
            else None,
        )
        authority[:] = [split]
        say, do = split.say, split.do
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
        primaries = list(read.characters or (read.character,))
        output_checks = regeneration_cost(policies) - 1
        if len(rounds) > 1:
            # A rebase re-renders: the renders and their extractions again (#156).
            renders = len(primaries) + (1 if narrate else 0)
            extractions = renders if plan is not None and plan.reservation.extraction else 0
            cost = (
                regeneration_cost(policies)
                + (renders - 1) * (1 + output_checks if len(primaries) > 1 else 1)
                + extractions
            )
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
            # #173: a partitioned story's seeds are in the partitions, so a character render
            # gets the scenario text without them; the narrator (omniscient) keeps them all.
            scenario_text=read.inputs.shared_scenario_text
            if module_on(settings, belief_transitions.PARTITIONS_MODULE)
            else read.inputs.scenario_text,
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
        narrator_sections = replace(
            base,
            scenario_text=read.inputs.scenario_text,
            director_notes=read.director,
            working_memory=read.narrator_memory,
        )
        # #174 (FR-MULTI-6): the primaries' order, the request's or the director's.
        ordered = multi.Ordered(tuple(p.id for p in primaries), multi.REQUEST)
        if len(primaries) > 1 and multi.order_of(story.settings, request) == multi.DIRECTOR:
            ordered = await multi.director_order(
                models,
                primaries,
                actor=read.actor,
                say=say,
                do=do,
                guidance=read.inputs.guidance,
                instructions=read.inputs.instructions,
                scenario_text=read.inputs.scenario_text,
                director_notes=read.director,
                scene=read.scene,
                recent=read.narrator_memory,
                budget=budget,
                params=ModelParams(
                    temperature=read.resolved.temperature, top_p=read.resolved.top_p
                ),
            )
            planned.extend(ordered.degradation)
        by_id = {p.id: p for p in primaries}
        order = [by_id[i] for i in ordered.order]
        mode = multi.mode_of(story.settings, request)
        # #175 (FR-MULTI-5,8): several primaries in ``simultaneous`` mode render against the
        # turn's starting state, verified alone, then adjudicated.
        simultaneous = len(order) > 1 and mode == multi.SIMULTANEOUS
        turn_stage = TurnStage(
            op.operation_id,
            read.turn,
            mode=multi.SIMULTANEOUS if simultaneous else multi.SEQUENTIAL,
        )
        staging[:] = [turn_stage]
        turn_narration: list[TurnBeat] = []
        """#175: the narrator's narration, part of the starting state every primary sees."""
        report_multi = multi.MultiReport(
            mode=mode,
            order=[p.id for p in order],
            order_source=ordered.source,
            deferred=list(plan.deferred) if plan is not None else [],
        )
        multis[:] = [report_multi]
        if plan is not None and plan.reduced:
            planned.append((planner.PRIMARIES_REDUCED, "budget"))
        if report_multi.deferred:
            planned.append((planner.PRIMARIES_DEFERRED, "budget"))
        if plan is not None and plan.deferred_fixed:
            planned.append((planner.PRIMARY_DEFERRED, "budget"))
        sequential = len(order) > 1
        # #176 (FR-MULTI-7): every present character's role for the turn.
        turn_roles: roles_rt.TurnRoles | None = None
        if multi.per_character(settings):
            turn_roles = roles_rt.resolve(
                story.cast,
                present_names=read.present,
                rendering=[p.id for p in order],
                actor_id=read.actor.id,
                request=request,
                story=story.settings,
                settings=settings,
                reduced=() if plan is None else plan.reduced,
                deferred=() if plan is None else plan.deferred,
                deferred_fixed=() if plan is None else plan.deferred_fixed,
            )
        roles_state[:] = [] if turn_roles is None else [turn_roles]
        cast_by_id = {c.id: c for c in story.cast}
        extraction_each = 1 if plan is not None and plan.reservation.extraction else 0

        def afford_escalation() -> bool:
            """#176 (FR-MULTI-10): one more primary render, its first output check and its
            extraction, from what no reservation holds (held now when they fit)."""
            cost = 1 + output_checks + extraction_each
            if budget.expired() or not budget.can_afford_unreserved(cost):
                return False
            budget.hold(1 + output_checks)
            budget.reserve(EXTRACTION, extraction_each)
            return True

        async def render_primary(
            k: int, read_k: TurnRead, start: int
        ) -> tuple[Rendered, Gated, bool]:
            """Primary ``k``'s render (the narrator's first, when it renders, before the first
            primary), gated by #68's output check; ``True`` when the deadline replaced it."""
            first = k == 0
            fallback_k = lite._fallback_lines(
                read.inputs, ResolvedCast(actor=read.actor, render=(read_k.character,))
            )

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
                narration: list[TurnBeat] = []
                narrator_inputs: GenerationInputs | None = None
                next_beat = start
                used = 0
                if narrate and first:
                    narr_retrieval = await retrieve_for(NARRATOR, OMNISCIENT, joined)
                    retrievals.append(narr_retrieval)
                    retrieved, canon = _canon_lines(narr_retrieval, None)
                    sections = replace(
                        narrator_sections,
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
                    made, found_sketches = _beats_of(
                        narr, start=next_beat, actor=None, narrator=True
                    )
                    beats += made
                    sketches.update(found_sketches)
                    renders.append((narr, made))
                    narration = [b for b in made if b.kind == NARRATION]
                    turn_narration[:] = narration
                    next_beat += len(made)
                assembling = time.perf_counter()
                # #170 (NFR-PRIV-4): the character's knowledge sections, built structurally;
                # #174: a later primary's with the staged beats before it; #175: in a
                # simultaneous turn every primary's from the starting state (the input and the
                # narration), with no staged beat of this turn.
                knowing = await context_for(
                    read_k,
                    turn_narration if simultaneous and not first else narration,
                    None if first or simultaneous else turn_stage,
                )
                if knowing.retrieval is not None:
                    retrievals.append(knowing.retrieval)
                sections = replace(base, constraints=constraints)
                timing.assemble_s += time.perf_counter() - assembling
                inputs: GenerationInputs = prompts.character_inputs(
                    knowing,
                    sections,
                    read_k.character,
                    read.actor,
                    say=say,
                    do=do,
                    budget=budget_tokens,
                    others_present=() if turn_roles is None else turn_roles.others_present(),
                    interjections=turn_roles is not None and turn_roles.interjections,
                    player_requests=split.requests_for(read_k.character.id),
                )
                char_render, n = await render_beats(
                    models,
                    inputs,
                    render_schema(
                        actor_id=read_k.character.id,
                        player_id=read.player_id if read.player_id != read_k.character.id else None,
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
                    char_render, start=next_beat, actor=read_k.character.id, narrator=False
                )
                beats += made
                sketches.update(found_sketches)
                renders.append((char_render, made))
                taken = {name.casefold(): cid for cid, name in read.names.items()}
                intros, repairs, denied = await _introductions(
                    models, read_k, renders, taken, base, params=params, budget=budget
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
                    narrator=narrate and first,
                    model=calls.calls[-1].model if calls.calls else None,
                    prompt_hash=prompt_hash(messages),
                    finish_reason="stop",
                    character_inputs=inputs,
                    narrator_inputs=narrator_inputs,
                    steps=list(char_render.steps),
                    steps_beat=char_start,
                    context=knowing,
                    contexts=[knowing],
                    reactions=list(char_render.reactions),
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
                fallback=fallback_k,
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
            rendered = by_draft.get(id(outcome.draft))
            if rendered is None or outcome.fallback_used:
                # The safe fallback: one say of the rendering character, nothing else.
                rendered = Rendered(
                    beats=[
                        TurnBeat(
                            kind="say",
                            beat=start,
                            text=outcome.draft.text,
                            character_id=read_k.character.id,
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
                # NFR-PERF-7 step 7 (#156): the renders' reserved extraction cannot start
                # before the deadline, so nothing they said is released: the safe fallback
                # replaces them before detection (its own record is layers a and b).
                rendered = _replaced_render(read_k, fallback_k, policies, start=start)
                planned.append((planner.BEAT_REPLACED, "deadline"))
            return rendered, outcome, replaced

        renders_done: list[Rendered] = []
        outcomes: list[Gated] = []
        merged: Checked | None = None
        starting: list[Record] = []
        """#175: the same-turn overlay a simultaneous primary is verified against: the input's
        and the narration's claims only, never another primary's."""
        # #180: the co_author player's narrated beats are beats 2..k, before every render's.
        next_beat = 2 + len(split.narrated)
        read_k = read
        try:
            for k, primary in enumerate(order):
                last = k == len(order) - 1
                read_k = (
                    read if primary.id == read.character.id else replace(read, character=primary)
                )
                if sequential and read.present and primary.name not in read.present:
                    # A named primary who is not in the scene renders with nothing perceived.
                    report_multi.not_present.append(primary.id)
                turn_stage.open_render(primary.id, next_beat)
                rendered_k, outcome_k, replaced = await render_primary(k, read_k, next_beat)
                fresh = 0
                if turn_roles is not None and rendered_k.finish_reason != "fallback":
                    # #176 (FR-MULTI-9,10): the reactions it narrates, judged; a reaction that
                    # needs a render of its own escalates its reactor (appended to the order).
                    reactions = roles_rt.collect(
                        rendered_k.beats,
                        rendered_k.reactions,
                        primary=primary.id,
                        start=next_beat,
                        roles=turn_roles,
                    )
                    if reactions:
                        rendered_k.beats, escalate = roles_rt.apply(
                            rendered_k.beats,
                            rendered_k.sketches,
                            reactions,
                            turn_roles,
                            afford=afford_escalation,
                            escalating=primary.id not in turn_roles.escalated,
                        )
                        for cid in escalate:
                            turn_roles.escalate(cid)
                            order.append(cast_by_id[cid])
                            report_multi.order.append(cid)
                            fresh += 1
                last = k == len(order) - 1
                if not last:
                    # #174: the output gate released what it held; the later primaries' renders
                    # and first output checks stay held through this one's optional work (#176:
                    # an escalation's own were held when it was afforded).
                    budget.hold((len(order) - k - 1 - fresh) * (1 + output_checks))
                if k == 0 and split.narrated:
                    # #180: the narrated beats join the first primary's, checked with it.
                    rendered_k.beats = sorted(
                        [*authority_rules.narrated_beats(split), *rendered_k.beats],
                        key=lambda b: b.beat,
                    )
                if k == 0:
                    released[:] = [rendered_k]
                checked_k = await check_turn(
                    db,
                    models,
                    story_id,
                    read_k,
                    rendered_k,
                    say=say if k == 0 else None,
                    do=do if k == 0 else None,
                    operation_id=op.operation_id,
                    read_set=read_set,
                    configuration=configuration,
                    budget=budget,
                    backstop=not replaced,
                    clock=clock,
                    staged_before=()
                    if merged is None
                    else (starting if simultaneous else merged.staged),
                    first_claim=0 if merged is None else len(merged.checked),
                    introductions=None if merged is None else merged.introductions,
                    release_extraction=last,
                    settle_salience=last,
                    staged_beliefs=turn_stage.accepted(primary.id),
                )
                if k == 0 and split.narrated:
                    # #180: each narrated beat checked against its character's viewpoint.
                    await authority_rules.verify(
                        db,
                        story_id,
                        turn=read.turn,
                        checked=checked_k,
                        found=split,
                        settings=settings,
                        actor_id=read.actor.id,
                        player_id=read.player_id,
                        read_set=read_set,
                    )
                if turn_roles is not None:
                    # #176: each reaction checked against its reactor's viewpoint.
                    await roles_rt.verify(
                        db,
                        story_id,
                        turn=read.turn,
                        checked=checked_k,
                        roles=turn_roles,
                        settings=settings,
                        actor_id=read.actor.id,
                        say=say,
                        do=do,
                        read_set=read_set,
                    )
                if not last:
                    # FR-MULTI-11: the beat that fails verification is repaired alone, before
                    # the next primary renders and sees it.
                    checked_k = await repair_turn(
                        db,
                        models,
                        story_id,
                        read_k,
                        rendered_k,
                        checked_k,
                        operation_id=op.operation_id,
                        read_set=read_set,
                        configuration=configuration,
                        budget=budget,
                        context=context,
                        render_ids=cast_.render_ids,
                        fallback=lite._fallback_lines(
                            read.inputs, ResolvedCast(actor=read.actor, render=(primary,))
                        ),
                        narrator_inputs=prompts.narrator_inputs(
                            narrator_sections, read.actor, say=say, do=do, budget=budget_tokens
                        ),
                    )
                    checked_k.staged = _restaged(() if merged is None else merged.staged, checked_k)
                    if simultaneous and k == 0:
                        starting[:] = _starting_overlay(rendered_k, checked_k)
                    # #175: a simultaneous primary's beats are staged for adjudication only; no
                    # later primary perceives them or forms a belief from them.
                    await stage_render(
                        turn_stage, read_k, checked_k, () if simultaneous else order[k + 1 :]
                    )
                merged = checked_k if merged is None else _merge_checked(merged, checked_k)
                renders_done.append(rendered_k)
                outcomes.append(outcome_k)
                next_beat = max(next_beat, *(b.beat + 1 for b in rendered_k.beats))
        except BaseException:
            turn_stage.discard()
            raise
        assert merged is not None  # noqa: S101 - every turn renders a primary
        checked = merged
        sequential = len(order) > 1  # #176: an escalation renders another primary
        if turn_roles is not None:
            checked.silent = turn_roles.silent()
        rendered = renders_done[0] if len(renders_done) == 1 else _merged_rendered(renders_done)
        last_rendered = renders_done[-1]
        outcome = outcomes[0] if len(outcomes) == 1 else _merged_gated(outcomes)
        gated[:] = [outcome]
        released[:] = [rendered]
        checked.degradation[:0] = [*planned, *dict.fromkeys(context_degradation)]
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
            read_k,
            last_rendered,
            checked,
            operation_id=op.operation_id,
            read_set=read_set,
            configuration=configuration,
            budget=budget,
            sections=narrator_sections,
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
            read_k,
            last_rendered,
            checked,
            operation_id=op.operation_id,
            read_set=read_set,
            configuration=configuration,
            budget=budget,
            context=context,
            render_ids=cast_.render_ids,
            fallback=lite._fallback_lines(
                read.inputs, ResolvedCast(actor=read.actor, render=(read_k.character,))
            ),
            narrator_inputs=prompts.narrator_inputs(
                narrator_sections, read.actor, say=say, do=do, budget=budget_tokens
            ),
            only=None
            if not sequential
            else frozenset(
                {
                    *(b.beat for b in last_rendered.beats),
                    *checked.follow_up_beats,
                }
            ),
        )
        clock.add("repair", time.perf_counter() - repairing)
        if simultaneous:
            # #175 (FR-MULTI-8): detect → tie-break → repair the losers as attempts.
            await adjudicate_conflicts(read, checked, order, report_multi, turn_stage)
        if sequential:
            report_multi.staged = len(turn_stage.beats)
            report_multi.accepted = turn_stage.report()["accepted"]
            report_multi.regenerated_beats = [
                r.original.beat for r in checked.repairs if r.original.beat > 1
            ]
        # #177 (FR-MULTI-3): the turn's concurrent commitments, reconciled on one timeline
        # after #175's adjudication (its settled pairs skipped), before the composition.
        firsts = [f for f in (turn_stage.render_of(p.id) for p in order) if f is not None]
        reconciliation = await reconcile_checked(
            db,
            story_id,
            read,
            checked,
            mode=multi.SIMULTANEOUS if simultaneous else multi.SEQUENTIAL,
            order=[p.id for p in order],
            firsts=firsts or [2],
            settled=settled_pairs,
        )
        reconciled[:] = [reconciliation]
        for number in reconciliation.attempts:
            staged = turn_stage.get(number)
            rewritten = next((b for b in checked.beats if b.beat == number), None)
            if staged is not None and rewritten is not None:
                turn_stage.replace(number, replace(staged, text=rewritten.text, verified=()))
        # #180: whether each requested character complied, from its verified beats.
        authority_rules.outcomes(split, checked.beats, list(read.names.values()))
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
        input_qualifiers[:] = [perception.Qualifiers()]
        if say and request.player_input.whisper_to:
            # #177 (from #161): the player's whisper; a target not in the room is dropped
            # silently (``qualifiers_repaired``), every target dropped leaves an aside.
            asked = perception.Qualifiers(whisper_to=tuple(request.player_input.whisper_to))
            if scene_loader.level_of(settings) == perception.FULL:
                async with db.connect() as conn:
                    state = await scene_loader.load_scene_state(conn, story_id, read.turn)
                asked, repairs = perception.validate_qualifiers(
                    perception.RouteBeat("say", 1, read.actor.id, asked), state
                )
                checked.perception_repairs[:0] = list(repairs)
            input_qualifiers[:] = [asked]
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
        # #177 (FR-RESP-13): the beats the output viewpoint receives, the rest withheld.
        heard = await audience_of(
            db,
            story_id,
            read,
            checked,
            viewpoint=viewpoint,
            player_view=view,
            settings=settings,
            say=say,
            do=do,
            input_qualifiers=input_qualifiers[0],
            mode=perception.SIMULTANEOUS if simultaneous else perception.SEQUENTIAL,
        )
        heard_state[:] = [heard]
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
            heard=heard,
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
                perception=input_qualifiers[0].payload() or None,
                requests=authority[0].payloads() if authority else (),
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
        for event_type, payload in [
            *_repair_events(checked),
            *_qualifier_events(checked),
            *roles_rt.rejected_events(roles_state[0] if roles_state else None),
            *authority_rules.rejected_events(authority[0] if authority else None),
        ]:
            op.append(
                event_type, payload, provenance=Provenance.ENGINE, character_id=None, beat=last + 1
            )
        # #177: the reconciled concurrent commitments, audit only.
        if reconciled and reconciled[0].pairs:
            op.append(
                "turn_reconciled",
                TurnReconciled(pairs=tuple(p.report() for p in reconciled[0].pairs)),
                provenance=Provenance.ENGINE,
                character_id=None,
                beat=last + 1,
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
                # #174: every primary, in the order they rendered (a regenerate replays them).
                render_character_ids=tuple(multis[0].order)
                if multis and multis[0].order
                else (read.character.id,),
                model=rendered.model,
                prompt_hash=rendered.prompt_hash,
                finish_reason=rendered.finish_reason,
                fallback_used=outcome.fallback_used,
                composed=True,
                # #175: a simultaneous turn's beats route against the starting placements.
                interaction_mode=_simultaneous_of(multis),
                # #177: the audience filtering the message was composed under.
                perception_level=_filtered_level(heard_state),
                player_view=view if _filtered_level(heard_state) is not None else None,
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
        turn_stage = staging[0] if staging and staging[0].beats else None
        if turn_stage is not None:
            # #174: the staged beats' antecedent refs name this variant.
            turn_stage.bind(int(op.turn_variant or 0))
        await write_introductions(conn, op, rendered.introductions, base_revision=op.base_revision)
        registry = await load_registry(conn, story_id)
        # #169: under partitions a hidden latent commits through ``commit_hidden`` (its scope
        # and its noticers' observations), after routing; every other latent canonizes here.
        partitioned = module_on(settings, belief_transitions.PARTITIONS_MODULE)
        latent_requests = [
            CanonRequest(
                claim=claim.proposition,
                provenance=RecordProvenance.DIRECTOR if beat.narrator else RecordProvenance.RENDER,
                visibility=beat.visibility,
                beat=beat.beat,
                claim_id=claim.claim_id,
            )
            for beat, claim in checked.latent
            if claim.proposition is not None and not (partitioned and _hidden_latent(beat))
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
        # #171 (FR-PROP-7): identity is held. A ``same_as`` between two characters is M7's
        # merge (an identity proposal instead); under partitions a narrated one is scoped to
        # its perceivers and the characters it names, after routing (``commit_identity``).
        kept, latent_requests, identity_requests, identity_refused = await identity_requests_of(
            conn, story_id, checked, kept, latent_requests, partitioned=partitioned
        )
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
        context_resolved: exposure_records.Pass | None = None
        spoken: speech.Spoken | None = None
        stepped: render_steps.Written | None = None
        results: list[perception.PerceptionResult] = []
        hidden_commits: list[hidden.HiddenCommit] = []
        with belief_transitions.tallying() as tally:
            if partitioned:
                # #169 (FR-COMMIT-4): the hidden latents, with their noticers' observations.
                hidden_commits = await hidden_committed(
                    conn,
                    op,
                    checked,
                    read,
                    ids=ids,
                    level=level,
                    settings=settings,
                    registry=registry,
                )
                # #171: the narrated identities, held by who perceived them.
                hidden_commits += await identities_committed(
                    conn,
                    op,
                    checked,
                    read,
                    identity_requests,
                    ids=ids,
                    level=level,
                    settings=settings,
                    registry=registry,
                )
            if level == perception.NONE:
                await records.write_episodic(
                    conn, op, _episodic(checked, read, say=say, do=do, ids=ids), settings=settings
                )
            else:
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
                    mode=_simultaneous_of(multis) or perception.SEQUENTIAL,
                    input_qualifiers=input_qualifiers[0],
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
                    stage=turn_stage,
                )
            used = 0 if told is None else sum(told.exposures["resolutions"].values())
            contexts = rendered.contexts or ([] if rendered.context is None else [rendered.context])
            for knowing in contexts:
                needed = knowing.needed
                if not (partitioned and needed and exposure_records.lazy_on(settings)):
                    continue
                # #170: the exposures the character's context needed (#164's need site), with
                # the model's adjudications where the turn afforded them (#174: each
                # primary's).
                needing = exposure_records.Pass(
                    cap=max(0, exposure_records.max_lazy_resolutions(settings) - used)
                )
                await exposure_records.resolve_needed(
                    conn,
                    op,
                    knowing.character_id,
                    [n.exposure.proposition for n in needed],
                    budget=needing,
                    settings=settings,
                    registry=registry,
                    decisions={
                        n.exposure.id: n.adjudicated for n in needed if n.adjudicated is not None
                    },
                )
                used += len(needing.made)
                if context_resolved is None:
                    context_resolved = needing
                else:
                    context_resolved.made += needing.made
                    context_resolved.capped = context_resolved.capped or needing.capped
            if checked.verifier is not None and checked.verifier.beliefs is not None:
                # #166: what the first-person claims license: the speaker's exposures on the
                # claims' keys (the need site), licensed beliefs and the lies' divergences.
                spoken = await speech.speech_committed(
                    conn,
                    op,
                    _spoken(checked, said, utterance_ids, ids),
                    settings=settings,
                    registry=registry,
                    resolutions_left=exposure_records.max_lazy_resolutions(settings) - used,
                )
            if checked.verifier is not None and checked.verifier.beliefs is not None:
                # #168: each render's verified steps: observations (with their memories) and
                # beliefs citing them (``antecedent = render_step``; #174: a staged beat's
                # through the turn's stage).
                for step_set in checked.step_sets:
                    if not step_set.verdicts:
                        continue
                    wrote = await render_steps.steps_committed(
                        conn,
                        op,
                        holder=step_set.holder,
                        steps=step_set.steps,
                        verdicts=step_set.verdicts,
                        first_beat=step_set.first_beat,
                        beat_owner=owners_of(checked),
                        settings=settings,
                        registry=registry,
                        remember=partial(
                            _remember_observation,
                            op=op,
                            holder=step_set.holder,
                            settings=settings,
                            registry=registry,
                        ),
                        stage=turn_stage,
                    )
                    if stepped is None:
                        stepped = wrote
                    else:
                        stepped.transitions += wrote.transitions
                        stepped.memories += wrote.memories
                        stepped.refused += wrote.refused
        reveals: list[hidden.Reveal] = []
        if partitioned:
            # #169: a sincere say or a do of a hidden fact by one who knows it widens it to the
            # beat's content perceivers (their transitions are routing's and testimony's).
            reveals = await reveals_committed(
                conn, op, checked, read, results, ids=ids, level=level, registry=registry
            )
        if (
            told is not None
            or spoken is not None
            or stepped is not None
            or context_resolved is not None
        ):
            fixed["epistemic"] = {
                **({} if told is None else {"testimony": told.testimony, **told.exposures}),
                **(
                    {}
                    if context_resolved is None
                    else {"context_resolutions": context_resolved.report()}
                ),
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
            if (
                (told is not None and told.capped)
                or (spoken is not None and spoken.capped)
                or (context_resolved is not None and context_resolved.capped)
            ):
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
                    conn,
                    op,
                    checked.checked,
                    read_set=read_set,
                    registry=registry,
                    visibility_of=agent_scope(results) if partitioned else None,
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
                    "hidden_commits": [h.report() for h in hidden_commits],
                    "reveals": [r.report() for r in reveals],
                    **({"identity_refused": identity_refused} if identity_refused else {}),
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
            commit_report = {
                "canonized": len(canonized),
                **(
                    {
                        "hidden_commits": [h.report() for h in hidden_commits],
                        "reveals": [r.report() for r in reveals],
                    }
                    if partitioned
                    else {}
                ),
                **({"identity_refused": identity_refused} if identity_refused else {}),
            }
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
            # #168: the render's declared causal steps (#174: every primary's).
            "steps": _steps_report(checked),
        }
        if roles_state:
            # #176: the turn's roles and what reactive narration made of them.
            report["roles"] = roles_state[0].report()
        if authority and authority[0].active:
            # #180: the player's input about other characters.
            report["player_authority"] = authority[0].report()
            authority[0].count()
        if multis and multi.per_character(settings):
            # #174: the sequential turn's mode, order and staging.
            report["multi"] = multis[0].report()
            if len(multis[0].order) > 1:
                multi.count_turn(multis[0].mode)
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
            report["uncertain_claims"] = [
                *checked.verifier.uncertain_claims(),
                *(u for v in checked.more_verifiers for u in v.uncertain_claims()),
            ]
            if checked.more_verifiers:
                # #174: each later primary's verification.
                report["verify_primaries"] = [v.report() for v in checked.more_verifiers]
        if checked.follow_up_verifier is not None:
            report["verify_follow_up"] = checked.follow_up_verifier.report()
        report["commit"] = fixed.get("commit", {})
        if "perception" in fixed:
            report["perception"] = fixed["perception"]
        # #177: the reconciled commitments, and what the output viewpoint received.
        report["reconcile"] = (
            reconciled[0] if reconciled else reconcile_rules.Reconciliation()
        ).report()
        if heard_state:
            report["audience"] = {
                "audience_filter": heard_state[0].audience_filter,
                "player_view": view,
                "withheld": sorted(b.beat for b in heard_state[0].withheld),
            }
        if "epistemic" in fixed:
            report["epistemic"] = fixed["epistemic"]
        if "modeling" in fixed:
            report["modeling"] = fixed["modeling"]
        if rendered.context is not None:
            # #170 (#182's source): what the character's context assembly read.
            report["context"] = dict(rendered.context.report)
        if len(rendered.contexts) > 1:
            report["contexts"] = [
                {"character_id": c.character_id, **dict(c.report)} for c in rendered.contexts
            ]
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
        # #177: the committed turn's beats through the same audience filter.
        filtered = await filtered_turn_beats(
            conn, story_id, committed.operation_id, viewpoint, player_view=view
        )
        beats = filtered.items
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
            "audience_filter": filtered.audience_filter,
            "player_view": view,
            "reconcile": [p.body() for p in reconciled[0].pairs] if reconciled else [],
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
        if multis and multi.per_character(settings):
            # #175: the mode the turn ran in, as requested (one primary included).
            fields["interaction_mode"] = multis[0].mode
        if roles_state:
            # #176 (FR-MULTI-7,10, NFR-PERF-7 (6)).
            found_roles = roles_state[0]
            fields["roles"] = {c: r.payload() for c, r in found_roles.roles.items()}
            fields["escalation_deferred"] = list(found_roles.escalation_deferred)
            fields["deferred_characters"] = list(found_roles.deferred)
            fields["reduced_characters"] = list(found_roles.reduced)
        if multis and len(multis[0].order) > 1:
            # #174: the primaries in the order they rendered; one not in the scene is named.
            fields["order"] = list(multis[0].order)
            fields["warnings"] = [
                *fields.get("warnings", []),
                *(
                    TurnWarning(kind=multi.PRIMARY_NOT_PRESENT, character_id=c)
                    for c in multis[0].not_present
                ),
                *(
                    [TurnWarning(kind=multis[0].tie_break_warning)]
                    if multis[0].tie_break_warning
                    else []
                ),
            ]
            # #175: a simultaneous turn's settled conflicts.
            fields["adjudications"] = list(multis[0].adjudications)
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


def _steps_report(checked: Checked) -> dict[str, Any]:
    """``turn_report.render.steps``: the render's (#168); a sequential turn's summed over its
    primaries, each rejection naming its holder (#174)."""
    if len(checked.step_sets) <= 1:
        return render_steps.steps_report(len(checked.steps), checked.step_verdicts)
    found: dict[str, Any] = {"declared": 0, "verified": 0, "rejected": []}
    for step_set in checked.step_sets:
        one = render_steps.steps_report(len(step_set.steps), step_set.verdicts)
        found["declared"] += one["declared"]
        found["verified"] += one["verified"]
        found["rejected"] += [{**r, "holder": step_set.holder} for r in one["rejected"]]
    return found


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
    holder: str,
    settings: ProfileSettings,
    registry: Registry,
) -> bool:
    """A verified ``observe`` step's episodic memory (#168): the character's own (``holder``),
    at the step's position, sourced from the observed fact's event when there is one."""
    if step.proposition is None:  # pragma: no cover - a verified observe names one
        return False
    stored = await store.write_proposition(
        conn, str(op.story_id), step.proposition, registry, base_revision=op.base_revision
    )
    found = await records.write_observed(
        conn,
        op,
        holder=holder,
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
