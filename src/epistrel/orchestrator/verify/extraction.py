"""Claim detection's model layer, its gating and its shadow sample; and the hand-off from
detection to checking (FR-VERIFY-13,14, FR-OBS-4, FR-PROP-12, NFR-QUAL-10; docs/02 §4.9; #148).

**One detection per render, one per composed message.** ``ClaimDetector.render(beats, …)`` runs
#148's core layers (``core.verify.detect``: declared structure and the deterministic detectors)
over every beat of a render and then, at the story's verification level and
``extraction_mode``, layer (c): the ``extraction_backstop`` purpose through the Model Access
Layer's ``structured_call`` (hot path, parse-validate-retry, ``extraction.j2``): ``{beats,
scene_context}`` → ``{claims: [{beat, span, subject, predicate, object, time, polarity,
confidence}]}``. ``ClaimDetector.message(text, …)`` does the same for the final composed message
(FR-VERIFY-14; #152), as the output viewpoint's character. #156 reserves the calls: one per
render (all its beats in one request) and one per composed message: a render's call spends
its B slot, the message's (``charging(COMPOSED)``) its C slot.

- ``verification.level`` below ``extraction`` (``off``, ``declared``): layers (a) and (b) only,
  and no call is made (FR-PROF-2's ladder). A Lite story detects nothing (no structured turn).
- ``extraction_mode = always``: one call per render, every beat.
- ``extraction_mode = gated`` (the schema's default; the owner's decision at the 2026-10-16
  replan): a call only when a beat is flagged (a detector hit of a risk category, a
  ``say``/``think`` beat naming an entity nothing covers, #478, or a ``say``/``do`` beat that
  declared nothing; ``core.verify.detect``), carrying only the flagged beats. The other beats'
  detection records ``extraction: skipped``. A share of renders (the deployment's
  ``extraction_shadow_sample_rate``, 0.1, drawn per operation and render from the operation id:
  ``shadow_sampled``) is a **shadow sample**: after the turn commits, the ``extraction_shadow``
  job runs the extraction over the skipped beats under its own budget (never the turn's) and
  records what gating missed (``core.verify.shadow``; FR-OBS-4).

An extraction claim's ``span`` is located in its beat by search (first exact, then case-folded
occurrence); one that cannot be located, or whose ``confidence`` is below
``EXTRACTION_MIN_CONFIDENCE``, is dropped. Its subject, object and time are read with
underscores as spaces (``spaced``, #479: the model writes ``the_inn``). Its sketch is
normalized like a declared claim (#140's ``normalize_declared``: an unregistered relation
auto-registers with the weakest semantics) when the detector is given the turn's connection,
else purely. Either way an untracked place or object takes an id minted by the turn's
``DetectionEnv.introductions`` when the environment has them (a structured turn does; an eval
and the shadow job do not; ``core.verify.introduce``). It merges into a
declared or detector claim of the same beat that has its shape or overlaps it by half the
longer span or more (``detect.merge``:
not verified twice). ``ModelUnavailable``, a spent budget or an ``Uncertain`` answer after the
retries leaves the render on layers (a) and (b): ``extraction: failed`` and degradation
``extraction_degraded``. The stub answers ``{"claims": []}``.

**Nothing bypasses detection** (``verify_detected``). Verification consumes the detected claims
and only them: a claim matched to a declared proposition is verified once, as the declared
claim; every other one as a new claim (``Verifier.verify``, its asserter named so a novel claim
passes #144's permission check, a protected-content hit with ``protected=True``); one that did
not normalize takes ``Verifier.unnormalizable`` (``uncertain``, never a guess, FR-PROP-12)
with the safe disposition, except an extraction finding of its own that is uncertain only
because a weak relation names something no entity holds: it is surfaced, not repaired
(``surfaces``; #479, ADR-163).
With verification off nothing is verified, and no verify call is made.

**After verification** (#150 calls these per beat): ``dependences`` lists the claims of
``think``/``do``/narration beats that fill an open thunk and survived verification, and
``apply_dependences`` hands each to ``core.commit.lifecycle.depend`` (#146: the dependence
commits the thunk's value); ``mentions_of`` is each beat's resolved mentions (#139's payload
shape) for the beat event. ``detection_report`` is ``turn_report.detection``: ``{declared,
detector_hits, extraction_claims, matched, verified_new, detected, checked, shadow,
shadow_missed, beats, message, claims, protected_index_truncated, degradation}``, ids, spans and
codes only; ``record_metrics`` counts ``epistrel_claims_detected_total{layer,category}`` and
``epistrel_claims_checked_total{verdict,category}`` at the commit: detection and checking are
separate series. Zero unchecked detected claims is the guarantee; complete detection is the
measured rate (NFR-QUAL-10, E-10).

Logs and metrics carry counts and codes only (invariant 3).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.commit import lifecycle, matching
from epistrel.core.commit.lifecycle import open_clause, thunk_columns, thunk_of
from epistrel.core.commit.sketch import ThunkSketch
from epistrel.core.config_changes import read_section
from epistrel.core.enums import OperationKind
from epistrel.core.errors import InvalidRequest
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.context import (
    SharedContext,
    load_context,
    load_index,
    load_time_basis,
)
from epistrel.core.resolution.entities import NARRATOR, PLAYER, ResolutionContext
from epistrel.core.resolution.time import Position, turn_of
from epistrel.core.settings_schema import OMNISCIENT, VerifySettings
from epistrel.core.tables import events, operations
from epistrel.core.tables.commitments import thunks
from epistrel.core.verify import shadow
from epistrel.core.verify.detect import (
    MESSAGE,
    BeatDetection,
    DeclaredClaim,
    DetectBeat,
    DetectedClaim,
    Detection,
    DetectionEnv,
    counts_of,
    detect_message,
    detect_render,
    merge,
    shape_hash,
)
from epistrel.core.verify.normalize import (
    ClaimSketch,
    Unnormalizable,
    normalize,
    normalize_declared,
)
from epistrel.core.verify.permission import Asserter
from epistrel.core.verify.protected import (
    CanonReads,
    ProtectedIndex,
    load_protected_index,
    viewpoint_of,
)
from epistrel.core.verify.rules import VerdictReason
from epistrel.observability.metrics import CLAIMS_CHECKED, CLAIMS_DETECTED
from epistrel.orchestrator.budget import COMPOSED, TurnBudget
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.models.access import ModelAccessLayer, routing
from epistrel.orchestrator.models.adapter import MAX_COMPLETION_TOKENS, ModelParams
from epistrel.orchestrator.models.config import Route, routes_of
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelError, ModelUnavailable
from epistrel.orchestrator.models.registry import (
    EXTRACTION_BACKSTOP,
    EXTRACTION_MAX_BEATS,
    EXTRACTION_MIN_CONFIDENCE,
    ExtractedClaim,
    ExtractionBeat,
    ExtractionInputs,
    ExtractionResult,
    SceneContext,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.verify.verifier import (
    ClaimRef,
    SafeDisposition,
    Verifier,
    VerifyResult,
)

log = logging.getLogger(__name__)

ExtractionMode = Literal["gated", "always"]
ExtractionState = Literal["ran", "skipped", "failed", "off"]
EXTRACTION_DEGRADED = "extraction_degraded"
EXTRACTION_LEVELS = frozenset({"extraction", "entailment"})
"""The verification levels with claim extraction (FR-PROF-2)."""
BUILT_IN_PARAMS = ModelParams(temperature=0.0, max_tokens=MAX_COMPLETION_TOKENS)
"""The extraction call's own params; the story's ``extraction_backstop`` route's params win.
``max_tokens`` is the adapter's cap (#464): the answer may list ``EXTRACTION_MAX_CLAIMS``
claims, and a smaller cap cut a long render's full list at ``length``, which is invalid
structure, so the backstop failed (``extraction_degraded``) on exactly the renders that say
the most."""
SHADOW_JOB = "extraction_shadow"
SHADOW_MAX_CALLS = 2
"""A shadow job's own budget: the render's call and the composed message's."""
SHADOW_PENDING = "pending"
_KINDS = frozenset({"say", "do", "think", "latent", "narration", "message"})
CATEGORY_OF_PREDICATE: Mapping[str, str] = {
    "holds": "possession",
    "consumed": "consumption",
    "injured": "injury",
    "located_at": "movement",
    "time_is": "time",
    "present_at": "entity_mention",
}
"""A missed claim's category, by its predicate's name (``extracted`` when none applies)."""


# -- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    """What one story's claim detection runs under: whether it is a structured story, its
    ``verification.level`` and ``extraction_mode`` and the deployment's sample rate and index
    cap."""

    structured: bool = False
    level: str = "off"
    extraction_mode: ExtractionMode = "gated"
    shadow_rate: float = VerifySettings().extraction_shadow_sample_rate
    protected_index_max: int = VerifySettings().protected_index_max

    @property
    def extraction_on(self) -> bool:
        return self.structured and self.level in EXTRACTION_LEVELS

    @classmethod
    def of(
        cls, profile: ProfileSettings, deployment: VerifySettings | None = None
    ) -> DetectionConfig:
        d = deployment or VerifySettings()
        v = profile.verification
        return cls(
            structured=profile.pipeline == "structured",
            level=v.level,
            extraction_mode=v.extraction_mode,
            shadow_rate=d.extraction_shadow_sample_rate,
            protected_index_max=d.protected_index_max,
        )

    @classmethod
    async def load(cls, conn: AsyncConnection, story_id: str) -> DetectionConfig:
        settings = (await effective_configuration(conn, story_id)).settings
        return cls.of(settings, await read_section(conn, VerifySettings))


def shadow_sampled(operation_id: str, rate: float, *, salt: str = "render") -> bool:
    """Whether the render (``salt``) of ``operation_id`` is a shadow sample at ``rate``: a draw
    seeded from the operation id, so a retry of the turn draws the same."""
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(f"{operation_id}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


# -- the environment ----------------------------------------------------------------------------


async def load_env(
    conn: AsyncConnection,
    story_id: str,
    read_set: StructuredReadSet,
    *,
    position: Position,
    speakers: Sequence[str],
    config: DetectionConfig | None = None,
    registry: Registry | None = None,
) -> DetectionEnv:
    """The detection environment of one turn, built once at plan time: the registry, the
    resolution context and protected index of each speaker (remembered for the rebase
    re-check), the scene's place and placements, and the open thunks."""
    config = config or await DetectionConfig.load(conn, story_id)
    registry = registry or await load(conn, story_id)
    basis = await load_time_basis(conn, story_id)
    index = await load_index(conn, story_id)
    lookup = KeyedLookup(conn, read_set)
    turn = turn_of(position)
    contexts: dict[str, ResolutionContext] = {}
    protected: dict[str, ProtectedIndex | None] = {}
    # The speakers share one canon read and one scene and relationships read (#452).
    reads: CanonReads = {}
    shared: SharedContext = {}
    for speaker in dict.fromkeys([*speakers, NARRATOR]):
        contexts[speaker] = await load_context(
            conn,
            story_id,
            position=position,
            speaker=speaker,
            index=index,
            lookup=lookup,
            shared=shared,
        )
        protected[speaker] = await load_protected_index(
            conn,
            story_id,
            speaker,
            index=index,
            through_position=(turn, 0),
            max_terms=config.protected_index_max,
            read_set=read_set,
            reads=reads,
        )
    scene_entity: str | None = None
    placement: dict[str, str] = {}
    try:
        scene = await lookup.scene(story_id, as_of=max(turn - 1, 0))
    except InvalidRequest:  # no scene projection yet: no scene known
        scene = None
    if scene is not None and scene.location is not None:
        scene_entity = scene.location.entity_id
        placement = {e.entity_id: scene_entity for e in scene.present}
    t = thunks.alias("t")
    rows = await conn.execute(
        select(*thunk_columns(t))
        .where(t.c.story_id == story_id, open_clause(t))
        .order_by(t.c.id)
        .limit(await lifecycle.max_open(conn))
    )
    open_thunks: list[tuple[str, ThunkSketch]] = [(x.id, x.sketch) for x in map(thunk_of, rows)]

    def context_for(speaker: str) -> ResolutionContext:
        return contexts.get(speaker) or contexts[NARRATOR]

    def protected_for(speaker: str) -> ProtectedIndex | None:
        return protected.get(speaker)

    return DetectionEnv(
        registry=registry,
        basis=basis,
        context_for=context_for,
        protected_for=protected_for,
        scene_entity=scene_entity,
        placement=placement,
        open_thunks=open_thunks,
    )


# -- layer (c) -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectionOutcome:
    """One render's (or composed message's) detection with layer (c): the merged claims, each
    beat's extraction state, whether the model was called, the degradation, and whether a
    shadow sample is owed after the commit."""

    detection: Detection
    extraction: Mapping[int | None, ExtractionState] = field(default_factory=dict)
    called: bool = False
    degradation: tuple[str, ...] = ()
    shadow: bool = False
    speakers: Mapping[int | None, str] = field(default_factory=dict)
    event_ids: Mapping[int | None, str] = field(default_factory=dict)

    @property
    def claims(self) -> tuple[DetectedClaim, ...]:
        return self.detection.claims


def locate_quote(text: str, span: str) -> tuple[int, int] | None:
    """Where a model's verbatim quote ``span`` lies in ``text`` (``[start, end)``): its first
    exact occurrence, else its first case-insensitive one, else ``None``. Shared by the
    extraction backstop and salience detection (#461): models quote, the engine locates."""
    found = text.find(span)
    if found < 0:
        found = text.casefold().find(span.casefold())
    if found < 0:
        return None
    return found, found + len(span)


def spaced(text: str | None) -> str | None:
    """A model's argument text with underscores read as spaces (#479: an extraction model
    writes ``the_inn`` for "the inn", copying the snake_case of its predicates); ``None`` for
    an empty one."""
    if not text:
        return None
    return " ".join(text.replace("_", " ").split()) or None


def extraction_sketch(claim: ExtractedClaim, span: tuple[int, int]) -> ClaimSketch:
    """An extraction claim as a #140 sketch (normalized like a declared one; its subject,
    object and time with underscores read as spaces, ``spaced``)."""
    return ClaimSketch(
        subject_text=spaced(claim.subject) or "",
        relation_text=claim.predicate,
        object_text=spaced(claim.object),
        time_text=spaced(claim.time),
        polarity=Polarity.NEGATED if claim.polarity == "negated" else Polarity.AFFIRMED,
        span=span,
        declared=True,
    )


Normalizer = Callable[[ClaimSketch, str, Position], Any]


async def to_claims(
    answer: ExtractionResult,
    beats: Mapping[int | None, DetectBeat],
    position: Position,
    *,
    normalize_one: Normalizer,
    by_index: Mapping[int, int | None] | None = None,
) -> list[DetectedClaim]:
    """The model's claims as layer (c) claims: located, confident enough, normalized."""
    found: list[DetectedClaim] = []
    turn = turn_of(position)
    unknown = 0
    for item in answer.claims:
        if item.confidence < EXTRACTION_MIN_CONFIDENCE:
            continue
        located = _resolve(item, beats, by_index)
        if located is None:
            unknown += 1
            continue
        key, beat, span = located
        if span is None:
            continue
        sketch = extraction_sketch(item, span)
        at = (turn, beat.index if key is not None else 0)
        result = await normalize_one(sketch, beat.speaker, at)
        proposition = result if isinstance(result, Proposition) else None
        found.append(
            DetectedClaim(
                span=(key, *span),
                layer="extraction",
                category="extracted",
                channel=beat.channel,
                speaker=beat.speaker,
                proposition=proposition,
                unnormalized=result if isinstance(result, Unnormalizable) else None,
                sketch=sketch,
                confidence=item.confidence,
            )
        )
    if unknown:
        # #459: never a silent drop. Counts only: the claims' text is story content.
        log.warning(
            "extraction: %d of %d claims named a beat marker the request did not show "
            "(markers %s); dropped",
            unknown,
            len(answer.claims),
            sorted(by_index) if by_index is not None else sorted(k for k in beats if k is not None),
        )
    return found


def _resolve(
    item: ExtractedClaim,
    beats: Mapping[int | None, DetectBeat],
    by_index: Mapping[int, int | None] | None,
) -> tuple[int | None, DetectBeat, tuple[int, int] | None] | None:
    """The beat a claim is on, and its span there (``None`` when the span is not found);
    ``None`` when the marker names no beat the request showed.

    The request numbers its beats ``0..n-1`` (#459), so the marker maps back through
    ``by_index``. A request with one beat accepts any marker (a model that answers ``beat: 0``,
    or the beat's turn index, still means that beat). When the marker is unknown, or its beat
    does not hold the quoted span, the claim goes to the one shown beat that holds the span,
    if exactly one does."""
    shown = (
        dict(beats)
        if by_index is None
        else {k: b for k, b in beats.items() if k in by_index.values()}
    )
    key: int | None
    if by_index is None:
        key = item.beat
    elif item.beat in by_index:
        key = by_index[item.beat]
    elif len(shown) == 1:
        key = next(iter(shown))
    else:
        key = -1  # unknown: only the span can place it
    beat = shown.get(key)
    if beat is not None:
        span = locate_quote(beat.text, item.span)
        if span is not None or len(shown) == 1:
            return key, beat, span
    hits = [
        (k, b, s) for k, b in shown.items() if (s := locate_quote(b.text, item.span)) is not None
    ]
    if len(hits) == 1:
        return hits[0]
    return None if beat is None else (key, beat, None)


def _speaker_name(env: DetectionEnv, speaker: str) -> str | None:
    if speaker in (NARRATOR, PLAYER):
        return None
    entry = env.context_for(speaker).index.get(speaker)
    return None if entry is None else entry.canonical_name


def extraction_inputs(
    beats: Sequence[DetectBeat], env: DetectionEnv, scene_context: SceneContext
) -> tuple[ExtractionInputs, dict[int, int | None]]:
    """The request: each beat with its ``[n]`` marker, numbered ``0..n-1`` in request order
    (#459: a render's beats start at turn index 2, and a model answers from 0; the composed
    message is a beat of kind ``message``), and the map from the marker back to the beat (its
    turn index, or ``None`` for the composed message)."""
    lines: list[ExtractionBeat] = []
    by_index: dict[int, int | None] = {}
    for n, beat in enumerate(beats[:EXTRACTION_MAX_BEATS]):
        kind = beat.channel if beat.channel in _KINDS else "narration"
        by_index[n] = None if beat.channel == MESSAGE else beat.index
        lines.append(
            ExtractionBeat(
                index=n,
                kind=cast(Any, kind),
                speaker=_speaker_name(env, beat.speaker),
                text=beat.text[:8_000],
            )
        )
    return ExtractionInputs(beats=lines, scene_context=scene_context), by_index


async def call_extraction(
    models: ModelAccessLayer | None,
    inputs: ExtractionInputs,
    *,
    budget: TurnBudget,
    routes: Mapping[str, Route] | None = None,
) -> ExtractionResult | None:
    """One ``extraction_backstop`` call; ``None`` when it failed (no model, a spent budget, an
    unreachable model or an answer that stayed invalid)."""
    if models is None:
        return None
    route = (routes or {}).get(EXTRACTION_BACKSTOP.name)
    params = BUILT_IN_PARAMS.merged(route.params if route is not None else None)
    try:
        with routing(routes or {}):
            answer = await models.structured_call(
                EXTRACTION_BACKSTOP.name,
                ExtractionResult,
                inputs,
                params=params,
                deterministic_first=lambda: ExtractionResult() if not inputs.beats else None,
                budget=budget,
            )
    except BudgetExhausted, ModelUnavailable:
        return None
    if isinstance(answer, Uncertain):
        return None
    return answer


class ClaimDetector:
    """One turn's claim detection (see the module docstring). ``conn``/``story_id`` let an
    extraction claim's unregistered relation auto-register (in the turn's transaction)."""

    def __init__(
        self,
        config: DetectionConfig,
        env: DetectionEnv,
        *,
        models: ModelAccessLayer | None,
        budget: TurnBudget,
        routes: Mapping[str, Route] | None = None,
        scene_context: SceneContext | None = None,
        conn: AsyncConnection | None = None,
        story_id: str | None = None,
    ) -> None:
        self.config = config
        self.env = env
        self.models = models
        self.budget = budget
        self.routes = routes
        self.scene_context = scene_context or SceneContext()
        self.conn = conn
        self.story_id = story_id
        self.outcomes: list[DetectionOutcome] = []

    async def _normalize(self, sketch: ClaimSketch, speaker: str, at: Position) -> Any:
        context = self.env.context_for(speaker)
        if self.conn is not None and self.story_id is not None:
            found, registry = await normalize_declared(
                self.conn,
                self.story_id,
                sketch,
                speaker,
                at,
                registry=self.env.registry,
                context=context,
                basis=self.env.basis,
                introduce=self.env.introductions,
            )
            if registry is not self.env.registry:
                object.__setattr__(self.env, "registry", registry)
            return found
        found_ = normalize(
            replace(sketch, declared=False),
            speaker,
            at,
            registry=self.env.registry,
            context=context,
            basis=self.env.basis,
            introduce=self.env.introductions,
        )
        return found_

    async def _layer_c(
        self,
        detection: Detection,
        beats: Sequence[DetectBeat],
        position: Position,
        *,
        operation_id: str,
        salt: str,
    ) -> DetectionOutcome:
        keys: list[int | None] = [b.beat for b in detection.beats]
        by_key = dict(zip(keys, beats, strict=True))
        speakers = {k: b.speaker for k, b in by_key.items()}
        if not self.config.extraction_on:
            return DetectionOutcome(detection, dict.fromkeys(keys, "off"), speakers=speakers)
        if self.config.extraction_mode == "always":
            chosen = list(keys)
        else:
            chosen = [b.beat for b in detection.beats if b.flagged]
        states: dict[int | None, ExtractionState] = dict.fromkeys(keys, "skipped")
        sampled = False
        if self.config.extraction_mode == "gated" and len(chosen) < len(keys):
            sampled = shadow_sampled(operation_id, self.config.shadow_rate, salt=salt)
        if not chosen:
            return DetectionOutcome(detection, states, shadow=sampled, speakers=speakers)
        inputs, by_index = extraction_inputs(
            [by_key[k] for k in chosen], self.env, self.scene_context
        )
        answer = await call_extraction(self.models, inputs, budget=self.budget, routes=self.routes)
        if answer is None:
            for k in chosen:
                states[k] = "failed"
            return DetectionOutcome(
                detection,
                states,
                called=self.models is not None,
                degradation=(EXTRACTION_DEGRADED,),
                shadow=sampled,
                speakers=speakers,
            )
        for k in chosen:
            states[k] = "ran"
        shown = {k: by_key[k] for k in chosen}  # a claim on a beat it was not shown is dropped
        extracted = await to_claims(
            answer, shown, position, normalize_one=self._normalize, by_index=by_index
        )
        log.debug("extraction: %d claims over %d beats", len(extracted), len(chosen))
        return DetectionOutcome(
            merge(detection, extracted), states, called=True, shadow=sampled, speakers=speakers
        )

    async def render(
        self,
        beats: Sequence[DetectBeat],
        position: Position,
        *,
        operation_id: str,
        event_ids: Mapping[int, str] | None = None,
    ) -> DetectionOutcome:
        """One render's detection, all three layers its level enables."""
        detection = detect_render(beats, position, self.env)
        found = await self._layer_c(
            detection, beats, position, operation_id=operation_id, salt="render"
        )
        ids: dict[int | None, str] = dict((event_ids or {}).items())
        found = replace(found, event_ids=ids)
        self.outcomes.append(found)
        return found

    async def message(
        self,
        text: str,
        position: Position,
        *,
        operation_id: str,
        speaker: str = NARRATOR,
        declared: Sequence[DeclaredClaim] = (),
        event_id: str | None = None,
    ) -> DetectionOutcome:
        """The composed message's detection (FR-VERIFY-14)."""
        detection = detect_message(text, position, self.env, speaker=speaker, declared=declared)
        beat = DetectBeat(0, MESSAGE, text, speaker, tuple(declared))
        # The composed message's call spends C, never a render's B (#156).
        with self.budget.charging(COMPOSED):
            found = await self._layer_c(
                detection, [beat], position, operation_id=operation_id, salt="message"
            )
        found = replace(found, event_ids={} if event_id is None else {None: event_id})
        self.outcomes.append(found)
        return found


# -- checking what was detected ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckedClaim:
    """One detected claim and its verdict (``None``: verification off, or the claim was
    matched to a declared one, which carries the verdict)."""

    claim: DetectedClaim
    result: VerifyResult | None
    claim_id: str | None = None

    @property
    def verdict(self) -> str | None:
        if self.result is None:
            return None
        if self.result.denied is not None:
            return "denied"
        return self.result.kind.value


SURFACE: SafeDisposition = "surface"
SAY_CHANNEL = "say"
SURFACED_LAYERS: frozenset[str] = frozenset({"declared", "extraction"})
"""The layers whose untracked weak-relation claims are surfaced; a detector's risk claims
(possession, transfer, quantity, time, protected content) always keep the safe disposition."""


def surfaces(claim: DetectedClaim) -> bool:
    """Whether an unnormalized claim is surfaced rather than repaired (#479, ADR-163, ADR-165): a
    declared or extraction claim (not one a detector also found, no protected content) that
    is uncertain only because a weak relation names something no entity holds
    (``Unnormalizable.untracked_weak``: the resolver found nothing for it, and no other
    argument is another modeled character or the player's). FR-PROP-4 (c): a weak relation is never
    ``contradicted`` by cardinality, exclusion, implication or containment, and a direct
    polarity conflict needs the identical proposition, which an untracked argument cannot
    form. So repairing the claim buys no consistency and only destroys scene texture
    (FR-VERIFY-11). Everything else keeps the safe disposition."""
    found = claim.unnormalized
    return (
        claim.layer in SURFACED_LAYERS
        and "detector" not in claim.merged
        and not claim.protected
        and found is not None
        and found.untracked_weak
    )


def asserter_for(claim: DetectedClaim, player_id: str | None = None) -> Asserter:
    """The default asserter of a claim: its beat's character, the director for narration and
    the composed message, the player for the player's input."""
    if claim.speaker == PLAYER:
        return Asserter("player", player_id)
    if claim.speaker == NARRATOR or claim.channel in ("narration", MESSAGE):
        return Asserter("director")
    return Asserter("character", claim.speaker)


async def verify_detected(
    verifier: Verifier | None,
    outcome: DetectionOutcome,
    position: tuple[int, int] | None,
    *,
    operation_id: str | None = None,
    player_id: str | None = None,
    asserter_of: Callable[[DetectedClaim], Asserter] | None = None,
    scope_of: Callable[[DetectedClaim], tuple[str, ...] | None] | None = None,
    start: int = 0,
    intent_of: Callable[[DetectedClaim], str | None] | None = None,
    cites_of: Callable[[DetectedClaim], Sequence[str]] | None = None,
) -> list[CheckedClaim]:
    """Verify every detected claim of ``outcome`` (see the module docstring); claim ids are
    ``<operation>:<n>`` from ``start``. A character's ``say`` claim (never the player's own,
    FR-EPI-12) is first-person: it names its speaker as the believer (#166), with its beat's
    declared intent (``intent_of``) and its render's ``think`` citations (``cites_of``)."""
    checked: list[CheckedClaim] = []
    on = verifier is not None and verifier.config.on
    for n, claim in enumerate(outcome.claims, start):
        claim_id = None if operation_id is None else f"{operation_id}:{n}"
        if not on or verifier is None:
            checked.append(CheckedClaim(claim, None, claim_id))
            continue
        ref = ClaimRef(
            claim_id=claim_id,
            beat=claim.beat,
            span=(claim.span[1], claim.span[2]),
            channel=claim.channel if claim.channel != MESSAGE else "narration",
            scope=None if scope_of is None else scope_of(claim),
        )
        if claim.proposition is None:
            reason = claim.unnormalized or Unnormalizable(VerdictReason.UNRESOLVED_ENTITY)
            disposition: SafeDisposition | None = SURFACE if surfaces(claim) else None
            checked.append(
                CheckedClaim(
                    claim,
                    verifier.unnormalizable(reason, ref=ref, disposition=disposition),
                    claim_id,
                )
            )
            continue
        asserter = (asserter_of or (lambda c: asserter_for(c, player_id)))(claim)
        # Narration is checked against the whole of canon (the director knows it); a character's
        # claim against what that character may read.
        viewpoint = (
            OMNISCIENT if claim.speaker == NARRATOR else viewpoint_of(claim.speaker, player_id)
        )
        believer = (
            claim.speaker
            if claim.channel == SAY_CHANNEL and claim.speaker not in (PLAYER, NARRATOR)
            else None
        )
        result = await verifier.verify(
            claim.proposition,
            viewpoint,
            position,
            ref=ref,
            asserter=asserter,
            protected=claim.protected,
            believer=believer,
            intent=None if intent_of is None or believer is None else intent_of(claim),
            cites=() if cites_of is None or believer is None else tuple(cites_of(claim)),
            # #146's resolution exemption: a claim filling an open thunk resolves it, so
            # binding the thunk to a modeled character is reconciliation's to judge (#154,
            # #165), not a permission denial.
            resolves=claim.matched_thunk_id
            if claim.channel in lifecycle.DEPENDENCE_CHANNELS
            else None,
        )
        checked.append(CheckedClaim(claim, result, claim_id))
    return checked


# -- after verification ---------------------------------------------------------------------------


def dependences(checked: Sequence[CheckedClaim]) -> list[CheckedClaim]:
    """The claims that depend on an open thunk (#146): on a ``think``, ``do`` or narration
    beat, filling the thunk's slot (``matched_thunk_id``), and not handed to repair."""
    return [
        c
        for c in checked
        if c.claim.matched_thunk_id is not None
        and c.claim.proposition is not None
        and c.claim.channel in lifecycle.DEPENDENCE_CHANNELS
        and (c.result is None or not c.result.needs_repair)
    ]


async def apply_dependences(
    conn: AsyncConnection,
    operation: Any,
    checked: Sequence[CheckedClaim],
    *,
    read_set: StructuredReadSet,
    registry: Registry | None = None,
) -> list[lifecycle.Dependence]:
    """Hand each dependence (``dependences``) to ``core.commit.lifecycle.depend`` in the turn's
    commit step, after re-finding its thunk through the keyed lookup (#146's read-set key)."""
    found: list[lifecycle.Dependence] = []
    story_id = str(operation.story_id)
    lookup = KeyedLookup(conn, read_set)
    for c in dependences(checked):
        proposition = c.claim.proposition
        if proposition is None:  # pragma: no cover - dependences() keeps normalized claims
            continue
        target = await matching.dependence_target(lookup, story_id, proposition)
        if target is None:
            continue
        found.append(
            await lifecycle.depend(
                conn,
                operation,
                target.id,
                proposition,
                beat=c.claim.beat,
                channel=cast(lifecycle.Channel, c.claim.channel),
                registry=registry,
            )
        )
    return found


def mentions_of(outcome: DetectionOutcome) -> dict[int | None, list[dict[str, Any]]]:
    """Each beat's resolved mentions, as #139's ``Mention.as_payload`` (the beat event's
    ``mentions``, persisted by #150)."""
    return {b.beat: b.mentions_payload() for b in outcome.detection.beats}


def _beat_record(outcome: DetectionOutcome, beat: BeatDetection) -> dict[str, Any]:
    return {
        "beat": beat.beat,
        "channel": beat.channel,
        "speaker": outcome.speakers.get(beat.beat),
        "event_id": outcome.event_ids.get(beat.beat),
        "layers": ["declared", "detector"]
        + (["extraction"] if outcome.extraction.get(beat.beat) == "ran" else []),
        "extraction": outcome.extraction.get(beat.beat, "off"),
        "flags": list(beat.flags),
        "hits": dict(beat.hits),
        "noise": beat.noise,
    }


def detection_report(
    outcomes: Sequence[DetectionOutcome], checked: Sequence[CheckedClaim]
) -> dict[str, Any]:
    """``turn_report.detection`` (see the module docstring)."""
    claims = [c for o in outcomes for c in o.claims]
    counts = counts_of(claims)
    verified_new = sum(1 for c in checked if c.result is not None and c.claim.layer != "declared")
    beats: list[dict[str, Any]] = []
    message: dict[str, Any] | None = None
    for outcome in outcomes:
        for beat in outcome.detection.beats:
            if beat.channel == MESSAGE:
                message = _beat_record(outcome, beat)
            else:
                beats.append(_beat_record(outcome, beat))
    return {
        **counts,
        "verified_new": verified_new,
        "detected": len(claims),
        "checked": sum(1 for c in checked if c.result is not None),
        "shadow": SHADOW_PENDING if any(o.shadow for o in outcomes) else None,
        "shadow_missed": None,
        "beats": beats,
        "message": message,
        "claims": [c.payload() for c in claims],
        "protected_index_truncated": any(o.detection.protected_index_truncated for o in outcomes),
        "degradation": list(dict.fromkeys(d for o in outcomes for d in o.degradation)),
    }


def record_metrics(outcomes: Sequence[DetectionOutcome], checked: Sequence[CheckedClaim]) -> None:
    """Count the turn's detection and checking (at its commit): two separate series."""
    for outcome in outcomes:
        for claim in outcome.claims:
            CLAIMS_DETECTED.labels(layer=claim.layer, category=claim.category).inc()
            for layer in claim.merged:
                CLAIMS_DETECTED.labels(layer=layer, category=claim.category).inc()
    for c in checked:
        if c.verdict is not None:
            CLAIMS_CHECKED.labels(verdict=c.verdict, category=c.claim.category).inc()


# -- the shadow sample ----------------------------------------------------------------------------


class ShadowPayload(BaseModel):
    """The shadow job's payload: the sampled turn's operation (ids only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str


def shadow_owed(outcomes: Sequence[DetectionOutcome]) -> bool:
    return any(o.shadow for o in outcomes)


async def enqueue_shadow(conn: AsyncConnection, story_id: str, operation_id: str) -> None:
    """Enqueue the sampled turn's shadow job in the turn's commit (#150 calls it when
    ``shadow_owed``); it runs after the commit, under its own budget."""
    # Imported here, not at the top: ``jobs.handlers`` registers this module's
    # ``SHADOW_HANDLER`` and ``jobs.queue`` imports ``jobs.handlers``, so a top-level import
    # makes importing this module first a cycle (#499).
    from epistrel.orchestrator.jobs import queue

    await queue.enqueue(
        conn,
        SHADOW_JOB,
        ShadowPayload(operation_id=operation_id),
        story_id=story_id,
        coalesce_key=f"{SHADOW_JOB}:{operation_id}",
    )


_bound: list[ModelAccessLayer] = []


def bind(models: ModelAccessLayer | None) -> None:
    """Make ``models`` the layer the shadow job calls through; ``None`` unbinds."""
    _bound.clear()
    if models is not None:
        _bound.append(models)


def bound() -> ModelAccessLayer | None:
    return _bound[0] if _bound else None


def _covered(claim: DetectedClaim, recorded: Sequence[Mapping[str, Any]]) -> bool:
    for entry in recorded:
        span = entry.get("span") or [None, 0, 0]
        if span[0] != claim.beat:
            continue
        if claim.shape is not None and entry.get("shape") == claim.shape:
            return True
        a0, a1 = int(span[1]), int(span[2])
        b0, b1 = claim.span[1], claim.span[2]
        longer = max(1, a1 - a0, b1 - b0)
        if max(0, min(a1, b1) - max(a0, b0)) / longer >= 0.5:
            return True
    return False


def missed_claims(
    extracted: Sequence[DetectedClaim],
    recorded: Sequence[Mapping[str, Any]],
    registry: Registry,
) -> list[shadow.Missed]:
    """The shadow extraction's claims no recorded claim covers (same shape, or half the longer
    span)."""
    found: list[shadow.Missed] = []
    for claim in extracted:
        if _covered(claim, recorded):
            continue
        predicate = None if claim.proposition is None else claim.proposition.predicate_id
        known = None if predicate is None else registry.get(predicate)
        name = None if known is None else known.name
        found.append(
            shadow.Missed(
                beat=claim.beat,
                start=claim.span[1],
                end=claim.span[2],
                category=CATEGORY_OF_PREDICATE.get(name or "", "extracted"),
                predicate_id=predicate,
                shape=None if claim.proposition is None else shape_hash(claim.proposition),
            )
        )
    return found


async def _beat_text(conn: AsyncConnection, operation_id: str, record: Mapping[str, Any]) -> str:
    event_id = record.get("event_id")
    if event_id:
        payload = await conn.scalar(
            select(events.c.payload).where(
                events.c.id == event_id, events.c.operation_id == operation_id
            )
        )
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            return str(payload["text"])
    from epistrel.core.memory.records import beats_of_operation

    for beat in await beats_of_operation(conn, operation_id):
        if beat.beat == record.get("beat"):
            return beat.text
    return ""


async def _run_shadow(ctx: JobContext) -> JobOutput:
    payload = cast(ShadowPayload, ctx.payload)
    story_id = ctx.job.story_id or ""
    async with ctx.snapshot() as conn:
        row = (
            await conn.execute(
                select(operations.c.turn_report, operations.c.narrative_turn).where(
                    operations.c.operation_id == payload.operation_id,
                    operations.c.story_id == story_id,
                    operations.c.kind == OperationKind.TURN.value,
                )
            )
        ).one_or_none()
        report = (row.turn_report or {}).get("detection") if row is not None else None
        if row is None or not isinstance(report, dict):
            return JobOutput(result={"sampled": 0, "reason": "no_detection"})
        await ctx.declare_input(conn, f"{operations.name}:{payload.operation_id}")
        turn = int(row.narrative_turn or 0)
        message = report.get("message")
        records = [*report.get("beats", []), *([message] if message else [])]
        skipped = [r for r in records if r.get("extraction") == "skipped"]
        if not skipped:
            return JobOutput(result={"sampled": 0, "reason": "nothing_skipped"})
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
        routes = routes_of(settings)
        speakers = [str(r.get("speaker") or NARRATOR) for r in skipped]
        env = await load_env(
            conn,
            story_id,
            StructuredReadSet(),
            position=(turn, 0),
            speakers=speakers,
            config=DetectionConfig.of(settings),
        )
        beats: dict[int | None, DetectBeat] = {}
        for r in skipped:
            text = await _beat_text(conn, payload.operation_id, r)
            key = r.get("beat")
            channel = str(r.get("channel") or "narration")
            beats[key] = DetectBeat(
                0 if key is None else int(key), channel, text, str(r.get("speaker") or NARRATOR)
            )
    models = bound()
    if models is None:
        raise ShadowUnbound("no Model Access Layer is bound for the extraction_shadow job")
    budget = TurnBudget(max_calls=SHADOW_MAX_CALLS)
    ordered = list(beats.values())
    inputs, by_index = extraction_inputs(ordered, env, SceneContext())
    status = shadow.SAMPLED
    missed: list[shadow.Missed] = []
    total = 0
    try:
        answer = await call_extraction(models, inputs, budget=budget, routes=routes)
    except ModelError:
        answer = None
    if answer is None:
        status = shadow.FAILED
    else:

        async def pure(sketch: ClaimSketch, speaker: str, at: Position) -> Any:
            return normalize(
                replace(sketch, declared=False),
                speaker,
                at,
                registry=env.registry,
                context=env.context_for(speaker),
                basis=env.basis,
            )

        keyed = {(None if b.channel == MESSAGE else b.index): b for b in ordered}
        extracted = await to_claims(answer, keyed, (turn, 0), normalize_one=pure, by_index=by_index)
        total = len(extracted)
        missed = missed_claims(extracted, report.get("claims", []), env.registry)
    written: dict[str, Any] = {}

    def stage(op: Any) -> None:
        async def step(conn: AsyncConnection) -> None:
            written["sample_id"] = await shadow.write_sample(
                conn,
                story_id,
                payload.operation_id,
                status=status,
                extraction_claims=total,
                missed=missed,
            )

        op.add_commit_step(step)

    return JobOutput(
        stage=stage,
        result={"status": status, "extraction_claims": total, "missed": len(missed)},
        result_of=lambda: {
            "status": status,
            "extraction_claims": total,
            "missed": len(missed),
            "written": written.get("sample_id") is not None,
        },
    )


class ShadowUnbound(RuntimeError):
    """No Model Access Layer is bound in this process (``bind``)."""


SHADOW_HANDLER = JobHandler(kind=SHADOW_JOB, payload=ShadowPayload, run=_run_shadow, max_attempts=1)
"""The post-commit shadow sample (FR-OBS-4): one ``maintenance`` operation per sampled turn."""


__all__ = [
    "BUILT_IN_PARAMS",
    "CATEGORY_OF_PREDICATE",
    "EXTRACTION_DEGRADED",
    "EXTRACTION_LEVELS",
    "SHADOW_HANDLER",
    "SHADOW_JOB",
    "CheckedClaim",
    "ClaimDetector",
    "DetectionConfig",
    "DetectionOutcome",
    "ShadowPayload",
    "apply_dependences",
    "asserter_for",
    "bind",
    "bound",
    "call_extraction",
    "dependences",
    "detection_report",
    "enqueue_shadow",
    "extraction_inputs",
    "extraction_sketch",
    "load_env",
    "locate_quote",
    "mentions_of",
    "missed_claims",
    "record_metrics",
    "shadow_owed",
    "shadow_sampled",
    "to_claims",
    "verify_detected",
]
