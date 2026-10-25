"""``Verifier.verify``: deterministic first, then the gate, then a validated entailment call on
the uncertain residual (FR-VERIFY-1,3,8; docs/02 §4.9; #142).

**Deterministic first.** ``verify(claim, viewpoint, position)`` asks #140's rules for the
verdict (``core.verify.rules.verify`` over canon through ``CanonSource``, which also records it
for the turn's rebase re-check; or ``check`` over records passed in through ``RecordsSource``).
``supported``, ``contradicted`` and ``novel`` are conclusive: they are returned as they are and
no model is called. Verification off (a Lite story) is inert: ``None``, nothing read.

**The residual.** An ``uncertain`` verdict is settled here:

- ``partial_coverage`` and ``granularity_refinement`` never reach the gate. Their remainders
  (#140's ``remainder_verdicts``) are settled one by one, and the claim takes the worst of them:
  ``contradicted`` over ``uncertain`` over ``novel`` (the covered part is supported and the
  added detail is novel, which the pipeline may canonize, #144). A remainder that is itself only
  partly covered or a refinement is ``novel`` (#140 does not split further).
- Every other reason goes to the gate (``gate.Gate``). A claim the gate does not escalate takes
  the safe disposition with no call, and so does every one when the effective profile's
  ``verification.level`` is below ``entailment`` (FR-VERIFY-3's last sentence).
- An escalated claim is sent to the ``entailment`` purpose through the Model Access Layer's
  ``structured_call`` (constrained decoding or parse-validate-retry; FR-MODEL-5,6,7), with only
  the claim and the holder's records: the records the deterministic check was decided over, then
  the facts of the claim subject's neighborhood (``core.verify.neighborhood``), up to the
  deployment's ``entailment_max_records``. Each is rendered ``<subject> <predicate> <object>``
  with its holder and valid-time, readable (#460; ``render_time``): calendar dates on a story
  with a calendar anchor, offsets from the story's start on one without, and an approximate
  bound as ``sometime between A and B``.

**Validated, never trusted** (FR-VERIFY-8). ``Uncertain`` (a parse or schema failure that
retries did not cure, a verdict outside the enum, a confidence below the deployment's
``uncertain_below``, the budget), a confidence below the profile's
``verification.entailment_min_confidence`` (0.6), and a model ``novel`` (the model's word never
canonizes) all take the safe disposition, and so does ``ModelUnavailable`` (degradation
``entailment_unavailable``: the turn goes on; never a 503). An ``approximate_bounds`` residual
never reaches the model (the gate's ``NEVER_ESCALATES``, #467, ADR-162). FR-PROP-10 needs every
admissible reading for a verdict, and the rules showed that the readings disagree. The model
knows no more about the bounds than the records say, so the residual takes the safe disposition
with no call. Otherwise only a validated ``supported`` or ``contradicted`` at or above the
floor is taken, with ``source = entailment``; a model ``contradicted`` cites no record (rule
``entailment``: #151 repairs it as a constraint). Because only ``uncertain`` escalates, a
model's ``supported`` can never override a deterministic ``contradicted``.

**The safe disposition** is the profile's ``verification.safe_disposition``: ``repair`` (the
default, the owner's decision on #142) hands the claim to #151's repair as if it were
contradicted (``needs_repair``; the sentence is rewritten so it no longer asserts it, and the
reason is recorded); ``surface`` lets the beat stand and lists the claim in the turn report's
``uncertain_claims`` (degradation ``uncertain_surfaced``). Either way the final kind stays
``uncertain``: an undecided claim is never a silent ``supported``. One case is surfaced
whatever the profile says (#479, ADR-163): an extraction claim that did not normalize only
because a weak relation names something untracked (``orchestrator.verify.extraction.surfaces``;
the caller passes ``unnormalizable(..., disposition="surface")``).

**Novelty is not permission** (FR-VERIFY-11,12; #144). Every claim that ends ``novel`` and
names its asserter (``verify(..., asserter=...)``) passes ``core.verify.permission`` before it
can be canonized or repaired (``ClaimSource.permission``: over canon, ``permission.check``,
its reads keyed and recorded for the rebase re-check). A ``Permitted`` claim becomes a
``CanonRequest`` (``canon_requests()``; ``core.canon.canonize`` writes them in the turn's
commit) with the asserter's provenance, its beat's visibility (``ClaimRef.channel`` and
``scope``) and an anchor from the claim subject's neighborhood; a ``think`` claim never
canonizes, and a ``say`` claim only when it is about the speaker's own actions or backstory
(otherwise it stays an utterance label, FR-TURN-5). A ``Denied`` claim is never canonized:
it ``needs_repair`` with ``repair_tag = "unpermitted"`` (#151's ladder), except
``player_authority``, whose player text stands; it is listed in ``report()["denied"]`` with its
reason, and its verdict entry carries the permission code.

**Viewpoint-aware: the speaker's beliefs** (FR-VERIFY-2,5,10, FR-TURN-6,7,8; #166). A
character's first-person ``say`` claim (``verify(..., believer=, intent=)``; the player's own
``say`` never, FR-EPI-12) is checked twice from one normalization: against canon as above (its
**objective** relation) and, through a ``BeliefSource``, against #159's ``belief(speaker)``
holder at the say beat's position (its **belief** relation: the speaker's partition, then the
canon it may read; deterministic only, ``uncertain`` reads as ``unknown``). Neither overrides
the other. ``core.verify.speech_acts`` checks the declared intent and classifies the claim:
``sincere``, ``sincere_error`` (an established false belief, or a new one licensed by an
independently verified antecedent), ``lie`` (deceptive against a contrary belief the speaker
holds, or its own true age, which it knows: FR-CAT-12), ``joke`` or ``hallucination_repaired``
(no legitimate basis). A claim needs a licence when the speaker's beliefs contradict it, or when
the record holds it beyond the speaker's view (omniscient canon, or another character's belief or
utterance: withheld content). A licence is citation-with-engine-fallback: the engine searches
the speaker's history with #165's ``could_have_known`` (a path is the licensing antecedent),
then the speaker's own earlier sincere word, then misinformation the speaker heard and repeats
(a ``said`` row or an unresolved exposure of the same proposition, accepted at ``believed``:
the assertion evidences acceptance); a ``think`` beat's ``cites`` are confirmed
when one names the antecedent the search found, never on their own (FR-EPI-13: no
self-attestation). A hallucination ``needs_repair`` whatever its declared intent (under
``safe_disposition = surface``, one canon does not contradict is surfaced instead); a sincere
error, a lie and a joke stand, and none of the last two canonizes. The dimensions ride on
``VerifyResult.speech`` and on ``turn_report.verify`` (omniscient reads only); the pipeline
writes what they license at the commit (``orchestrator.epistemic.speech``). Partitions off, or
verification off, there is no belief dimension and no classification.

**Within one turn** a ``Verifier`` is built per turn: identical ``(claim, records)`` requests
reuse the first validated answer (the structured call's deterministic step), escalations run one
after the other, and every residual that reached the gate leaves an ``EscalationRecord`` in
``escalations``: #155 writes them (its ``escalations`` table) in the turn's transaction, and
``story_verifier``'s gate reads #155's live per-shape thresholds (``gate_thresholds``).
``report()`` is ``turn_report.verify`` (``{claims, deterministic, escalated, verdicts}``, the
basis of P-05's escalation rate, plus ``denied``) and ``uncertain_claims()`` the surfaced
claims. A model verdict
rests on keyed reads (the deterministic fetch): a rebase that changes them regenerates the turn,
so a stale model verdict is never reused.

Log lines carry counts and codes only (invariant 3).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.canonize import CanonRequest, anchor_for, visibility_of
from epistrel.core.config_changes import read_section
from epistrel.core.epistemic import resurface
from epistrel.core.membership import Position as NarrativePosition
from epistrel.core.membership import step_of
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import (
    Bound,
    Granularity,
    Instant,
    Proposition,
    TemporalKind,
)
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT, VerifySettings
from epistrel.core.tables import entities
from epistrel.core.verify import permission as permissions
from epistrel.core.verify import rules, speech_acts, yield_queries
from epistrel.core.verify.introduce import Introductions
from epistrel.core.verify.neighborhood import Seed, fact_neighborhood
from epistrel.core.verify.normalize import Unnormalizable
from epistrel.core.verify.permission import (
    Asserter,
    Basis,
    Denied,
    EntityInfo,
    Permission,
    PermissionFacts,
    Permitted,
)
from epistrel.core.verify.rules import (
    CANON,
    ClosedSetRecord,
    Holder,
    Options,
    Record,
    Verdict,
    VerdictKind,
    VerdictReason,
)
from epistrel.observability import stages
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer, routing
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import Route
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelUnavailable
from epistrel.orchestrator.models.registry import (
    ENTAILMENT,
    EntailmentInputs,
    EntailmentRecord,
    EntailmentVerdict,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.verify.gate import (
    RESOLVED_IN_CORE,
    UNKNOWN,
    FixedThresholds,
    Gate,
    GateDecision,
    PredicateShape,
)

log = logging.getLogger(__name__)

SafeDisposition = Literal["repair", "surface"]
Source = Literal["deterministic", "entailment"]
ObjectiveRelation = Literal["true", "false", "unresolved"]

ENTAILMENT_UNAVAILABLE = "entailment_unavailable"
ENTAILMENT_BUDGET = "entailment_budget_exhausted"
UNCERTAIN_SURFACED = "uncertain_surfaced"
ENTAILMENT_RULE = "entailment"
"""The ``rule`` of a verdict the entailment model decided (it cites no record)."""
UNPERMITTED = "unpermitted"
HALLUCINATION = "hallucination"
"""#166: the repair tag of a first-person claim with no legitimate basis (FR-VERIFY-10)."""
RESOLUTION_CONFLICT = "resolution_conflict"
THINK_LEAK = "think_leak"
"""#172: the repair tag of a claim that repeats or narrates another character's private thought
(the thought side index or the inner-state check; FR-TURN-3, NFR-PRIV-1)."""
CONFABULATION = resurface.CONFABULATION
"""#172: the repair tag of a recall no record of the speaker's backs (FR-VERIFY-9)."""
RECALL_RULE = resurface.RECALL
"""#172: the rule of a grounded recall's ``supported`` verdict."""
HIDDEN_CONFLICT = "hidden_conflict"
"""#169 (FR-EPI-8): a character's claim the character's view leaves open but hidden canon
contradicts (a different value for a determined fact): ``contradicted``, citing nothing, so no
repair reads or substitutes the hidden value."""
"""#154: a dependence's binding of an open thunk that conflicts with recorded history
(``core.commit.reconcile``): ``contradicted``, repaired with an ``avoid_binding`` constraint."""
"""The repair tag of a novel claim its asserter may not introduce (#151's ladder)."""
NEVER_CANONIZES = frozenset({"think"})
"""Beat channels whose claims never canonize."""
SAY = "say"
PARTITIONS_MODULE = "partitions"
"""``character_knowledge.partitions``: the belief dimension runs only under it (#166)."""

BUILT_IN_PARAMS = ModelParams(temperature=0.0, max_tokens=100)
"""The entailment call's own params; the story's ``entailment`` route's params win."""
UNNORMALIZED_SHAPE = PredicateShape("auto", UNKNOWN, UNKNOWN, UNKNOWN)
"""The shape an escalation record gives a claim that did not normalize."""
NEIGHBORHOOD_DEPTH = 1
"""The neighborhood an entailment call adds to the fetched records: the claim subject's own
facts, in both directions."""


# -- settings --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifyConfig:
    """What one story's verification runs under: the profile's ``verification`` dials and the
    deployment's ``VerifySettings``."""

    level: str = "off"
    entailment_gate: float = 1.0
    entailment_min_confidence: float = 0.6
    safe_disposition: SafeDisposition = "repair"
    entailment_max_records: int = VerifySettings().entailment_max_records
    neighborhood_max_facts: int = VerifySettings().neighborhood_max_facts

    @property
    def on(self) -> bool:
        return self.level != "off"

    @property
    def escalation_on(self) -> bool:
        """Whether the profile's level is ``entailment`` (FR-PROF-2's ladder)."""
        return self.level == "entailment"

    @classmethod
    def of(cls, profile: ProfileSettings, deployment: VerifySettings | None = None) -> VerifyConfig:
        v = profile.verification
        d = deployment or VerifySettings()
        structured = profile.pipeline == "structured"
        return cls(
            level=v.level if structured else "off",
            entailment_gate=v.entailment_gate,
            entailment_min_confidence=v.entailment_min_confidence,
            safe_disposition=v.safe_disposition,
            entailment_max_records=d.entailment_max_records,
            neighborhood_max_facts=d.neighborhood_max_facts,
        )

    @classmethod
    async def load(cls, conn: AsyncConnection, story_id: str) -> VerifyConfig:
        """The story's effective configuration and the deployment's settings, read now."""
        settings = (await effective_configuration(conn, story_id)).settings
        return cls.of(settings, await read_section(conn, VerifySettings))


# -- results -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimRef:
    """Where a claim sits in the turn, for the turn report: its id (``<operation>:<n>``, minted
    at detection by #150), its beat and its character span in the beat, and the beat's channel
    (``say``, ``think``, ``do``, ``latent``, ``narration``) and, for a ``latent`` beat, its
    declared scope (the characters it names), which a canonized claim's visibility follows."""

    claim_id: str | None = None
    beat: int | None = None
    span: tuple[int, int] | None = None
    channel: str | None = None
    scope: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """One claim's verdict: #140's deterministic ``verdict`` and the ``kind`` it ended as, who
    decided it (``source``), whether the model was asked (``escalated``) and what it answered,
    and, for an undecided claim, its safe ``disposition``. ``sub_results`` settle a partly
    covered claim's remainders."""

    claim: Proposition | None
    """``None`` for a claim that did not normalize (``Verifier.unnormalizable``)."""
    verdict: Verdict
    kind: VerdictKind
    source: Source = "deterministic"
    escalated: bool = False
    decision: GateDecision | None = None
    model_verdict: str | None = None
    model_confidence: float | None = None
    model_uncertain: str | None = None
    disposition: SafeDisposition | None = None
    degradation: tuple[str, ...] = ()
    sub_results: tuple[VerifyResult, ...] = ()
    asserter: Asserter | None = None
    permission: Permission | None = None
    """A novel claim's permission (#144); ``None`` when it was not asked (not novel, or no
    asserter named)."""
    anchored_to: str | None = None
    """The fact a permitted claim's canonization anchors to (its neighborhood's)."""
    resolution_conflict: bool = False
    """#154: the claim binds an open thunk against recorded history (``reconcile``'s
    ``Conflict``); ``avoid`` is the entity it bound, when one."""
    avoid: str | None = None
    speech: SpeechAct | None = None
    """#166: a first-person ``say`` claim's belief dimension and classification (``None``: not
    a character's say, or partitions off)."""
    attribution: AttributionCheck | None = None
    """#167: the claim attributes its proposition to someone ("Bobby thinks …"): its belief
    relation over the speaker's nested beliefs and its objective relation (a label only)."""
    self_attested: bool = False
    """#168: the claim asserts what a rejected ``believe`` step of the render concluded, in the
    beats of the first ``act`` citing it (FR-TURN-12): self-attestation, a hallucination."""
    hidden_conflict: bool = False
    """#169: ``contradicted`` by canon hidden from the claim's viewpoint (``HIDDEN_CONFLICT``)."""
    think_leak: str | None = None
    """#172: the private record (``think:<ref>``) the claim repeats or narrates (``THINK_LEAK``):
    always repaired, never canonized."""
    recall: resurface.RecallCheck | None = None
    """#172: a first-person recall's check against the speaker's own records (FR-VERIFY-9):
    grounded (``supported``, rule ``recall``) or a confabulation (repaired)."""

    @property
    def confabulated(self) -> bool:
        """#172: an unbacked recall."""
        return self.recall is not None and not self.recall.grounded

    @property
    def reason(self) -> VerdictReason | None:
        return self.verdict.reason

    @property
    def rule(self) -> str | None:
        if self.recall is not None and self.recall.grounded:
            return RECALL_RULE
        if self.source == "entailment" and self.kind in (
            VerdictKind.SUPPORTED,
            VerdictKind.CONTRADICTED,
        ):
            return ENTAILMENT_RULE
        found = sorted(r.value for r in self.verdict.rules)
        return found[0] if found else None

    @property
    def denied(self) -> Denied | None:
        return self.permission if isinstance(self.permission, Denied) else None

    @property
    def permitted(self) -> Permitted | None:
        return self.permission if isinstance(self.permission, Permitted) else None

    @property
    def hallucinated(self) -> bool:
        """#166: the claim has no legitimate basis (``hallucination_repaired``); #168: or it
        asserts a self-attested belief."""
        return self.self_attested or (self.speech is not None and self.speech.decision.hallucinated)

    @property
    def stands(self) -> bool:
        """#166: a sincere error, a lie or a joke: it stands as said."""
        return self.speech is not None and self.speech.decision.stands

    @property
    def canonizes(self) -> bool:
        """Whether a permitted novel claim may canonize: never a lie, a joke or a
        hallucination (#166)."""
        if self.attribution is not None:
            return False  # #167: an attribution asserts someone's belief, never canon
        if self.think_leak is not None or self.recall is not None:
            return False  # #172: a private thought or a memory reference is never canon
        return self.speech is None or self.speech.decision.classification in (
            speech_acts.SINCERE,
            speech_acts.SINCERE_ERROR,
        )

    @property
    def needs_repair(self) -> bool:
        """Whether #151 repairs the claim: contradicted, undecided under ``repair``, or novel
        and not permitted (``repair_tag``); under partitions (#166) a hallucination always
        (unless surfaced), and never a sincere error, a lie or a joke."""
        if self.think_leak is not None or self.confabulated:
            return True  # #172: under every profile (the channel is private)
        if self.recall is not None:
            return False
        if self.hallucinated:
            return not self.surfaced
        if self.stands or self.attribution is not None:
            return False
        return (
            self.kind is VerdictKind.CONTRADICTED
            or self.disposition == "repair"
            or self.repair_tag is not None
        )

    @property
    def repair_tag(self) -> str | None:
        """``unpermitted`` for a denied novel claim #151 repairs (FR-VERIFY-12);
        ``resolution_conflict`` for a binding reconciliation rejected (#154); ``hallucination``
        for a first-person claim with no legitimate basis (#166)."""
        if self.think_leak is not None:
            return THINK_LEAK
        if self.confabulated:
            return CONFABULATION
        if self.resolution_conflict:
            return RESOLUTION_CONFLICT
        if self.hallucinated:
            return HALLUCINATION
        denied = self.denied
        return UNPERMITTED if denied is not None and denied.repaired else None

    @property
    def surfaced(self) -> bool:
        return self.disposition == "surface"

    @property
    def objective_relation(self) -> ObjectiveRelation:
        """``true`` for a supported claim, ``false`` for a contradicted one, else
        ``unresolved`` (an undecided claim; a novel one until it is canonized)."""
        if self.kind is VerdictKind.SUPPORTED:
            return "true"
        if self.kind is VerdictKind.CONTRADICTED:
            return "false"
        return "unresolved"

    def evidence_ids(self) -> list[str]:
        return [*self.verdict.cited(), *(i for s in self.sub_results for i in s.evidence_ids())]

    def as_payload(self, claim_id: str | None = None) -> dict[str, Any]:
        """A ``turn_report.verify.verdicts`` entry: ids and codes only."""
        return {
            "claim_id": claim_id,
            "kind": self.kind.value,
            "reason": None if self.reason is None else self.reason.value,
            "rule": self.rule,
            "evidence_ids": self.evidence_ids(),
            "escalated": self.escalated,
            "disposition": self.disposition,
            "permission": None if self.permission is None else self.permission.code(),
        } | (
            ({"cause": RESOLUTION_CONFLICT} if self.resolution_conflict else {})
            | ({"cause": HIDDEN_CONFLICT} if self.hidden_conflict else {})
            | ({"cause": THINK_LEAK, "private_ref": self.think_leak} if self.think_leak else {})
            | ({"recall": self.recall.payload()} if self.recall is not None else {})
            | ({"speech": self.speech.payload()} if self.speech is not None else {})
            | ({"attribution": self.attribution.payload()} if self.attribution is not None else {})
        )


@dataclass(frozen=True, slots=True)
class EscalationRecord:
    """One residual that reached the gate (FR-OBS-2; #155 writes it as an ``escalations``
    row): its gate reason and predicate shape, the deterministic confidence, the tentative
    verdict (the literal ``uncertain``), the model's verdict and confidence when it was asked
    (``None`` otherwise), how the claim ended and its disposition."""

    claim_id: str | None
    gate_reason: str
    predicate_shape: str
    deterministic_confidence: float
    threshold: float | None
    called: bool
    reused: bool
    model_verdict: str | None
    model_confidence: float | None
    model_uncertain: str | None
    final_kind: str
    disposition: SafeDisposition | None
    degradation: tuple[str, ...]
    tentative_verdict: str = VerdictKind.UNCERTAIN.value
    gate_outcome: str = "escalate"
    """The gate's decision (``gate.Outcome``): ``escalate``, ``below_gate`` (a tightened or
    lowered gate skipped the call), ``never_escalates`` or ``unnormalized``."""
    escalation_on: bool = True
    """Whether the profile's ``verification.level`` is ``entailment`` (#155 writes an
    ``escalations`` row only then: below it no claim is ever escalated)."""


type EscalationSink = Callable[[EscalationRecord], None]
"""Receives each ``EscalationRecord`` as it is made (#155's writer may register one)."""


# -- where claims are checked ---------------------------------------------------------------


class ClaimSource(Protocol):
    """Where a verifier gets the deterministic verdict and the records an entailment call is
    shown."""

    async def registry(self) -> Registry: ...

    async def deterministic(
        self,
        claim: Proposition,
        *,
        holder: Holder,
        viewpoint: str,
        position: tuple[int, int] | None,
    ) -> tuple[Verdict, list[Record]] | None: ...

    async def neighbors(
        self, claim: Proposition, *, viewpoint: str, position: tuple[int, int] | None, limit: int
    ) -> list[Record]: ...

    async def names(self, ids: Sequence[str]) -> Mapping[str, str]: ...

    async def time_frame(self) -> TimeFrame:
        """How the story's valid-times read in a prompt (its calendar anchor and start)."""
        ...

    async def permission(
        self,
        claim: Proposition,
        asserter: Asserter,
        *,
        position: tuple[int, int] | None,
        protected: bool,
        resolves: str | None = None,
    ) -> Permission | None: ...

    async def anchor(
        self, claim: Proposition, *, viewpoint: str, position: tuple[int, int] | None
    ) -> str | None: ...


@dataclass
class RecordsSource:
    """The holder's records passed in (pure: #140's ``check``). For an utterance holder, whose
    records #150 fetches, and for the unit tests. Permission is decided over the ``entities``,
    ``locked`` (subject, predicate) keys, ``placed`` subjects and arrival policy passed in;
    ``frame`` is the story's time frame (unanchored by default)."""

    records: Sequence[Record]
    registry_: Registry
    closed_sets: Sequence[ClosedSetRecord] = ()
    options: Options = field(default_factory=Options)
    entity_names: Mapping[str, str] = field(default_factory=dict)
    entities: Mapping[str, EntityInfo] = field(default_factory=dict)
    locked: frozenset[tuple[str, str]] = frozenset()
    placed: frozenset[str] = frozenset()
    allow_new_arrivals: bool = True
    frame: TimeFrame | None = None

    async def registry(self) -> Registry:
        return self.registry_

    async def time_frame(self) -> TimeFrame:
        return self.frame or UNANCHORED

    async def deterministic(
        self,
        claim: Proposition,
        *,
        holder: Holder,
        viewpoint: str,
        position: tuple[int, int] | None,
    ) -> tuple[Verdict, list[Record]] | None:
        del viewpoint
        found = rules.check(
            claim,
            self.records,
            registry=self.registry_,
            closed_sets=self.closed_sets,
            holder=holder,
            position=position,
            options=self.options,
        )
        return found, list(self.records)

    async def neighbors(
        self, claim: Proposition, *, viewpoint: str, position: tuple[int, int] | None, limit: int
    ) -> list[Record]:
        del claim, viewpoint, position, limit
        return []

    async def names(self, ids: Sequence[str]) -> Mapping[str, str]:
        return {i: self.entity_names[i] for i in ids if i in self.entity_names}

    async def permission(
        self,
        claim: Proposition,
        asserter: Asserter,
        *,
        position: tuple[int, int] | None,
        protected: bool,
        resolves: str | None = None,
    ) -> Permission | None:
        del position
        facts = PermissionFacts(
            entities=self.entities,
            predicate=self.registry_.get(claim.predicate_id),
            locked=(claim.subject_id, claim.predicate_id) in self.locked,
            placed=claim.subject_id in self.placed,
            allow_new_arrivals=self.allow_new_arrivals,
            protected=protected,
            resolves=resolves,
        )
        return permissions.permission(claim, asserter, permissions.owner_of(claim, facts), facts)

    async def anchor(
        self, claim: Proposition, *, viewpoint: str, position: tuple[int, int] | None
    ) -> str | None:
        del viewpoint, position
        return anchor_for(claim, [(r.fact_id, r.proposition) for r in self.records if r.fact_id])


class CanonSource:
    """Canon on the turn's connection: #140's ``verify`` (profile-gated, keyed in ``read_set``,
    recorded for the rebase re-check) and the claim subject's neighborhood."""

    def __init__(
        self,
        conn: AsyncConnection,
        read_set: StructuredReadSet,
        story_id: str,
        *,
        remember: bool = True,
    ) -> None:
        self.conn = conn
        self.read_set = read_set
        self.story_id = story_id
        self.remember = remember
        self._registry: Registry | None = None
        self._frame: TimeFrame | None = None

    async def registry(self) -> Registry:
        if self._registry is None:
            self._registry = await load(self.conn, self.story_id)
        return self._registry

    async def time_frame(self) -> TimeFrame:
        """The story's ``story_epoch`` and ``story_minute_zero``, read once, and only when a
        claim is rendered for a prompt or the turn report."""
        if self._frame is None:
            self._frame = TimeFrame.of(await load_time_basis(self.conn, self.story_id))
        return self._frame

    async def deterministic(
        self,
        claim: Proposition,
        *,
        holder: Holder,
        viewpoint: str,
        position: tuple[int, int] | None,
    ) -> tuple[Verdict, list[Record]] | None:
        fetched: list[Record] = []
        found = await rules.verify(
            self.conn,
            self.read_set,
            self.story_id,
            claim,
            holder=holder,
            position=position,
            through_position=position,
            viewpoint=viewpoint,
            remember=self.remember,
            fetched=fetched,
        )
        return None if found is None else (found, fetched)

    async def neighbors(
        self, claim: Proposition, *, viewpoint: str, position: tuple[int, int] | None, limit: int
    ) -> list[Record]:
        found = await fact_neighborhood(
            KeyedLookup(self.conn, self.read_set),
            self.story_id,
            Seed.entity(claim.subject_id),
            depth=NEIGHBORHOOD_DEPTH,
            viewpoint=viewpoint,
            through_position=position,
            max_facts=limit,
            registry=await self.registry(),
        )
        return [Record.of_fact(f) for f in found.facts()]

    async def permission(
        self,
        claim: Proposition,
        asserter: Asserter,
        *,
        position: tuple[int, int] | None,
        protected: bool,
        resolves: str | None = None,
    ) -> Permission | None:
        return await permissions.check(
            self.conn,
            self.read_set,
            self.story_id,
            claim,
            asserter,
            through_position=position,
            protected=protected,
            registry=await self.registry(),
            remember=self.remember,
            resolves=resolves,
        )

    async def anchor(
        self, claim: Proposition, *, viewpoint: str, position: tuple[int, int] | None
    ) -> str | None:
        """The anchor ``core.canon.canonize.anchor_for`` picks from the claim subject's
        neighborhood (context reads, never keyed)."""
        found = await fact_neighborhood(
            KeyedLookup(self.conn, self.read_set),
            self.story_id,
            Seed.entity(claim.subject_id),
            depth=NEIGHBORHOOD_DEPTH,
            viewpoint=viewpoint,
            through_position=position,
            registry=await self.registry(),
        )
        return anchor_for(claim, found.facts())

    async def names(self, ids: Sequence[str]) -> Mapping[str, str]:
        if not ids:
            return {}
        rows = await self.conn.execute(
            select(entities.c.id, entities.c.canonical_name).where(
                entities.c.story_id == self.story_id, entities.c.id.in_(list(ids))
            )
        )
        return {str(r.id): str(r.canonical_name) for r in rows}


# -- the speaker's beliefs (#166) -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Licence:
    """What licenses the belief a first-person claim expresses: the ``path`` the engine found
    (``canon``, ``exposure``, ``implied``, ``transition``, ``said``, ``said_before``: the
    speaker's own earlier sincere word, ``player``, ``not_applicable``), the transition ``kind``
    that writes the belief (``None``: nothing to write, the speaker holds it already or the
    check does not apply), the antecedent record (``table``, ``record_id``), the testimony's
    ``source``, the position the belief holds from (``held_from``; ``None``: the claim's), the
    path's record (``ref_id``) and whether a ``think``'s ``cites`` named it (``cited``)."""

    path: str
    kind: str | None = None
    table: str | None = None
    record_id: str | None = None
    source: str | None = None
    held_from: NarrativePosition | None = None
    ref_id: str | None = None
    cited: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "antecedent": None
            if self.record_id is None
            else {"table": self.table, "id": self.record_id},
            "cited": self.cited,
        }


@dataclass(frozen=True, slots=True)
class AttributionCheck:
    """An attribution claim's dimensions (#167; ``core.epistemic.nested``): whom it attributes
    the proposition to and how (``mode``, ``factive``), the speaker's nested beliefs it was
    checked over (``belief_ids``; ``None``: not checked, the ``nested_beliefs`` module being
    off) and the **objective relation**, whether the attributed holder actually believes it
    (computed by the Engine Core for the utterance label and the divergence records only; never
    a context input)."""

    attributed_holder: str | None
    mode: str = "believes"
    factive: bool = False
    belief_ids: tuple[str, ...] | None = None
    objective: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "attributed_holder": self.attributed_holder,
            "mode": self.mode,
            "factive": self.factive,
            "checked": self.belief_ids is not None,
            "belief_ids": list(self.belief_ids or ()),
            "objective_relation": self.objective,
        }


@dataclass(frozen=True, slots=True)
class SpeechAct:
    """One first-person claim's speech-act dimensions (#166): the speaker, the claim's position,
    the declared intent, the belief verdict, the classification (``decision``), the contrary
    beliefs the claim contradicts (strongest first), what canon holds beyond the speaker's view
    (``hidden``) and the licence the belief rests on."""

    speaker: str
    position: NarrativePosition
    declared: str
    belief: Verdict
    decision: speech_acts.Decision
    contrary: tuple[str, ...] = ()
    hidden: VerdictKind | None = None
    licence: Licence | None = None
    known: tuple[str, ...] = ()
    """The canon propositions the speaker holds of itself that the claim contradicts (its true
    age, FR-CAT-12), when no belief row does: a lie may deviate from them."""
    inferred: bool = False
    """#167: the intent was inferred ``deceptive``: the contrary belief is one the speaker keeps
    ``secret`` (its author act's ``disclosure``)."""

    @property
    def classification(self) -> str:
        return self.decision.classification

    @property
    def writes(self) -> bool:
        """Whether the commit writes the licensed belief (``orchestrator.epistemic.speech``)."""
        return (
            self.decision.write is not None
            and self.licence is not None
            and self.licence.kind is not None
        )

    def payload(self) -> dict[str, Any]:
        """The ``speech`` entry of a verdict: ids and codes only."""
        return {
            "belief_kind": self.belief.kind.value,
            "belief_relation": self.decision.belief_relation,
            "declared_intent": self.declared,
            "intent": self.decision.intent,
            "intent_corrected": self.decision.intent_corrected,
            "intent_inferred": self.inferred,
            "classification": self.decision.classification,
            "contrary_belief_ids": list(self.contrary),
            "contrary_known": list(self.known),
            "hidden": None if self.hidden is None else self.hidden.value,
            "licence": None if self.licence is None else self.licence.payload(),
            "belief_evidence": [e.as_payload() for e in self.belief.evidence],
        }


class BeliefSource:
    """The speaker's belief set on the turn's connection (#166): #159's ``belief(speaker)``
    holder through ``rules.verify`` (keyed in ``read_set``, recorded for the rebase re-check),
    what the record holds beyond the speaker's view (omniscient canon, keyed and not recorded,
    then another character's belief or utterance of it), and the licensing search
    (``replay.could_have_known``, #165, the speaker's own earlier word, then the misinformation
    the speaker heard). ``variant`` is the turn's (a claim's position is ``(turn, variant, 0,
    beat, 0)``)."""

    def __init__(
        self,
        conn: AsyncConnection,
        read_set: StructuredReadSet,
        story_id: str,
        *,
        settings: Any,
        variant: int = 0,
        remember: bool = True,
    ) -> None:
        self.conn = conn
        self.read_set = read_set
        self.story_id = story_id
        self.settings = settings
        self.variant = variant
        self.remember = remember
        self._registry: Registry | None = None
        self.staged: dict[str, list[tuple[Record, NarrativePosition]]] = {}
        """#168: each speaker's verified ``believe`` steps of the turn (not yet committed), at
        their step positions: the belief record set a later beat is checked against."""

    async def registry(self) -> Registry:
        if self._registry is None:
            self._registry = await load(self.conn, self.story_id)
        return self._registry

    def position(self, position: tuple[int, int] | None) -> NarrativePosition | None:
        if position is None:
            return None
        turn, beat = position
        return NarrativePosition(turn, self.variant, 0, beat, step_of(beat))

    def stage(self, believer: str, claim: Proposition, at: NarrativePosition, ref: str) -> None:
        """#168: ``believer`` holds ``claim`` from ``at`` (a verified render step, ``ref``)."""
        self.staged.setdefault(believer, []).append((Record(claim, f"step:{ref}"), at))

    async def check(
        self, claim: Proposition, believer: str, at: NarrativePosition
    ) -> Verdict | None:
        found = await rules.verify(
            self.conn,
            self.read_set,
            self.story_id,
            claim,
            holder=Holder.belief(believer),
            position=at,
            through_position=(at.turn, at.beat),
            viewpoint=f"{CHARACTER_VIEWPOINT_PREFIX}{believer}",
            remember=self.remember,
        )
        staged = [r for r, held in self.staged.get(believer, ()) if held.before(at)]
        if found is None or not staged:
            return found
        if found.kind not in (VerdictKind.NOVEL, VerdictKind.UNCERTAIN):
            return found
        verdict = rules.check(
            claim,
            staged,
            registry=await self.registry(),
            holder=Holder.belief(believer),
            position=at,
        )
        if verdict.kind in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED):
            return verdict
        return found

    async def hidden(
        self, claim: Proposition, believer: str, at: NarrativePosition
    ) -> VerdictKind | None:
        """What the record holds of ``claim`` beyond the speaker's view: canon's omniscient
        verdict when it settles the claim, else ``supported`` when another character holds or
        said it (withheld content: ``replay.held_elsewhere``), else ``None``."""
        from epistrel.core.epistemic import replay

        found = await rules.verify(
            self.conn,
            self.read_set,
            self.story_id,
            claim,
            through_position=(at.turn, at.beat),
            viewpoint=OMNISCIENT,
            remember=False,
        )
        if found is not None and found.kind in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED):
            return found.kind
        if await replay.held_elsewhere(
            self.conn, self.story_id, believer, claim, at, registry=await self.registry()
        ):
            return VerdictKind.SUPPORTED
        return None

    async def own_age(self, claim: Proposition, believer: str) -> bool:
        """Whether ``claim`` is about ``believer``'s own age: a character knows its true age
        (FR-CAT-12), so canon's ``age`` of itself is what it believes."""
        from epistrel.core.propositions.vocabulary import CORE_BY_NAME

        if claim.predicate_id != CORE_BY_NAME["age"].id:
            return False
        if claim.subject_id == believer:
            return True
        owner = await self.conn.scalar(
            select(entities.c.character_id).where(
                entities.c.story_id == self.story_id, entities.c.id == claim.subject_id
            )
        )
        return owner is not None and str(owner) == believer

    async def strongest(self, belief_ids: Sequence[str]) -> tuple[str, ...]:
        """``belief_ids`` strongest first, then the most recently held (a lie's contrary
        belief is the first)."""
        from epistrel.core.epistemic import partitions

        found = [
            b
            for i in dict.fromkeys(belief_ids)
            if (b := await partitions.read_belief(self.conn, self.story_id, i)) is not None
        ]
        ranked = sorted(
            found,
            key=lambda b: (
                -partitions.LEVELS.index(b.confidence_level),
                -(b.confidence_scalar or 0.0),
                tuple(-k for k in b.held_from.key()),
            ),
        )
        return tuple(b.id for b in ranked)

    async def secret(self, belief_ids: Sequence[str]) -> bool:
        """Whether one of ``belief_ids`` is a belief its holder keeps ``secret``: its author act
        (the ``belief_authored`` event or a seed its ``author`` transition cites) declared
        ``disclosure = secret`` (FR-SCN-8; #167's deceptive inference), or the belief itself
        keeps a seed's ``secret`` disclosure (``beliefs.disclosure``, #173)."""
        from sqlalchemy import and_

        from epistrel.core.tables import events
        from epistrel.core.tables.beliefs import belief_transitions, beliefs

        if not belief_ids:
            return False
        seeded = await self.conn.scalar(
            select(beliefs.c.id)
            .where(
                beliefs.c.story_id == self.story_id,
                beliefs.c.id.in_(list(belief_ids)),
                beliefs.c.disclosure == "secret",
            )
            .limit(1)
        )
        if seeded is not None:
            return True
        t = belief_transitions.c
        e = events.c
        found = await self.conn.scalar(
            select(t.id)
            .select_from(
                belief_transitions.join(
                    events,
                    and_(e.story_id == t.story_id, e.id == t.antecedent_ref["id"].astext),
                )
            )
            .where(
                t.story_id == self.story_id,
                t.belief_id.in_(list(belief_ids)),
                t.kind == "author",
                t.antecedent_ref["table"].astext == "events",
                e.payload["disclosure"].astext == "secret",
            )
            .limit(1)
        )
        return found is not None

    async def attribution(
        self,
        claim: Proposition,
        believer: str,
        found: Any,
        at: NarrativePosition,
    ) -> tuple[VerdictKind, tuple[str, ...], tuple[str, ...]]:
        """An attribution claim's belief verdict for its speaker (#167): over the speaker's own
        nested (or ``said``) rows attributed to the holder, or, for a self-report ("I figured
        …"), the speaker's own beliefs; with the rows it rests on and the contrary ones."""
        from epistrel.core.epistemic import nested

        if found.holder_id == believer and found.mode == "believes":
            verdict = await self.check(claim, believer, at)
            if verdict is None:
                return VerdictKind.NOVEL, (), ()
            cited = tuple(e.belief_id for e in verdict.evidence if e.belief_id is not None)
            contrary = cited if verdict.kind is VerdictKind.CONTRADICTED else ()
            return verdict.kind, cited, contrary
        relation = await nested.attribution_belief_relation(
            self.conn,
            self.story_id,
            believer,
            str(found.holder_id),
            claim,
            at,
            mode=found.mode,
            registry=await self.registry(),
        )
        return relation.verdict, relation.belief_ids, relation.contrary

    async def attributed_licence(
        self, claim: Proposition, believer: str, found: Any, at: NarrativePosition
    ) -> Licence | None:
        """An attribution's licensing path (#165's replay over nested propositions,
        ``nested.could_have_attributed``): a nested belief the speaker held, or a speech act it
        perceived the holder sincerely make. Nothing new is written: the path is the record."""
        from epistrel.core.epistemic import nested, replay

        if found.holder_id is None or found.mode != "believes":
            return None
        if found.holder_id == believer:
            return await self.licence(claim, believer, at)
        answer = await nested.could_have_attributed(
            self.conn,
            self.story_id,
            believer,
            str(found.holder_id),
            claim,
            at,
            registry=await self.registry(),
        )
        if isinstance(answer, replay.Known):
            return Licence("attributed", ref_id=answer.path.ref_id)
        return None

    async def objective(self, claim: Proposition, found: Any, at: NarrativePosition) -> str | None:
        """The attribution's objective relation (#167): whether the attributed holder actually
        believes (or said) it, a label computed by the Engine Core (``None``: no holder)."""
        from epistrel.core.epistemic import nested

        if found.holder_id is None:
            return None
        return await nested.attribution_objective_relation(
            self.conn,
            self.story_id,
            str(found.holder_id),
            claim,
            at,
            mode=found.mode,
            registry=await self.registry(),
        )

    async def licence(
        self,
        claim: Proposition,
        believer: str,
        at: NarrativePosition,
        cites: Sequence[str] = (),
    ) -> Licence | None:
        """The claim's licensing antecedent, if one exists (see the module docstring)."""
        from epistrel.core.epistemic import replay

        answer = await replay.could_have_known(
            self.conn,
            self.story_id,
            believer,
            claim,
            at,
            settings=self.settings,
            registry=await self.registry(),
            read_set=self.read_set,
        )
        found: Licence | None
        if isinstance(answer, replay.NotApplicable):
            found = Licence("not_applicable")
        elif isinstance(answer, replay.Known):
            found = await self._of_path(answer.path, believer)
        elif await replay.said_before(
            self.conn, self.story_id, believer, claim, at, registry=await self.registry()
        ):
            found = Licence("said_before")  # the speaker's own earlier word: nothing new
        else:
            found = await self._misinformed(claim, believer, at)
        if found is not None and cites:
            named = {i for i in (found.record_id, found.ref_id) if i is not None}
            found = dataclasses.replace(found, cited=bool(named & set(cites)))
        return found

    async def _of_path(self, path: Any, believer: str) -> Licence | None:
        from epistrel.core.epistemic import exposure

        if path.kind in ("player", "transition", "staged"):
            # #168: a verified render step of this turn is written by the step itself.
            return Licence(path.kind, ref_id=path.ref_id)
        if path.kind == "canon":
            return Licence("canon", "discovery", "facts", path.ref_id, ref_id=path.ref_id)
        if path.kind == "implied":
            return Licence("implied", "inference", "beliefs", path.ref_id, ref_id=path.ref_id)
        if path.kind == "exposure" and path.ref_id is not None:
            (e,) = await exposure.exposures_of(
                self.conn, self.story_id, believer, ids=[path.ref_id]
            ) or (None,)
            if e is not None:
                return self._heard(e)
        return None

    @staticmethod
    def _heard(e: Any) -> Licence:
        return Licence(
            "exposure",
            "testimony" if e.source is not None else "observation",
            "events",
            e.source_event_id,
            source=e.source,
            held_from=e.position,
            ref_id=e.id,
        )

    async def _misinformed(
        self, claim: Proposition, believer: str, at: NarrativePosition
    ) -> Licence | None:
        """Misinformation the speaker heard and now repeats: a ``said`` row of the same
        proposition from someone else (its testimony's antecedent), else an unresolved exposure
        to it, before the claim (accepted at ``believed``: the assertion evidences it)."""
        from epistrel.core.enums import AntecedentKind, ExposureState
        from epistrel.core.epistemic import exposure, partitions, transitions
        from epistrel.core.propositions.registry import canonical

        registry = await self.registry()
        target = canonical(claim, registry)
        key = target.normalized_key()

        def same(p: Proposition) -> bool:
            stored = canonical(p, registry)
            return stored.normalized_key() == key and stored.polarity is target.polarity

        for said in await partitions.speech_acts(self.conn, self.story_id, believer):
            b = said.belief
            if said.speaker_id in (None, believer) or not b.held_from.before(at):
                continue
            if not same(b.proposition):
                continue
            produced = [
                t
                for t in await transitions.transitions_of(
                    self.conn, self.story_id, belief_ids=[b.id]
                )
                if t.id == b.provenance
            ]
            if not produced or produced[0].antecedent.kind is not AntecedentKind.RECORD:
                continue
            ref = produced[0].antecedent.ref
            return Licence(
                "said",
                "testimony",
                str(ref.get("table")),
                str(ref.get("id")),
                source=said.speaker_id,
                held_from=b.held_from,
                ref_id=b.id,
            )
        for e in await exposure.exposures_of(
            self.conn,
            self.story_id,
            believer,
            state=ExposureState.UNRESOLVED,
            as_of_turn=at.turn,
        ):
            if e.position.before(at) and same(e.proposition):
                return self._heard(e)
        return None


# -- rendering --------------------------------------------------------------------------------


DAY_MINUTES = 1440


@dataclass(frozen=True, slots=True)
class TimeFrame:
    """How a story-minute reads in a prompt (#460): the story's calendar anchor
    (``stories.story_epoch``) and where the story starts (``stories.story_minute_zero``).

    An anchored story reads in calendar dates (``2012-05-10``, ``2012-05-10 09:15``). An
    unanchored one reads as an offset from its start ("2 days 3 hours after the story's
    start"), because the engine never picks a date the story has not established
    (FR-CLOCK-3)."""

    epoch: datetime | None = None
    minute_zero: int = 0

    @classmethod
    def of(cls, basis: TimeBasis) -> TimeFrame:
        return cls(epoch=basis.epoch, minute_zero=basis.minute_zero)

    def moment(self, minute: int) -> str:
        """``minute`` as a date (with its time of day unless midnight), or as an offset from
        the story's start."""
        when = self._calendar(minute)
        if when is None:
            return self._offset(minute)
        if when.hour == 0 and when.minute == 0:
            return when.strftime("%Y-%m-%d")
        return when.strftime("%Y-%m-%d %H:%M")

    def instant(self, at: int, granularity: Granularity) -> str:
        """An instant at its own precision: ``in 2012``, ``in 2012-05``, ``on 2012-05-10``,
        ``at 2012-05-10 09:00 (to the hour)``, ``at 2012-05-10 09:15``."""
        when = self._calendar(at)
        if when is None:
            if granularity is Granularity.MINUTE:
                return f"at {self._offset(at)}"
            return f"at {self._offset(at)} (to the {granularity.value})"
        if granularity is Granularity.YEAR:
            return f"in {when:%Y}"
        if granularity is Granularity.MONTH:
            return f"in {when:%Y-%m}"
        if granularity is Granularity.DAY:
            return f"on {when:%Y-%m-%d}"
        if granularity is Granularity.HOUR:
            return f"at {when:%Y-%m-%d %H}:00 (to the hour)"
        return f"at {when:%Y-%m-%d %H:%M}"

    def _calendar(self, minute: int) -> datetime | None:
        if self.epoch is None:
            return None
        try:
            return self.epoch + timedelta(minutes=minute)
        except OverflowError:
            return None

    def _offset(self, minute: int) -> str:
        delta = minute - self.minute_zero
        if delta == 0:
            return "the story's start"
        days, rest = divmod(abs(delta), DAY_MINUTES)
        hours, minutes = divmod(rest, 60)
        parts = [
            f"{n} {unit}{'' if n == 1 else 's'}"
            for n, unit in ((days, "day"), (hours, "hour"), (minutes, "minute"))
            if n
        ]
        return f"{' '.join(parts)} {'after' if delta > 0 else 'before'} the story's start"


UNANCHORED = TimeFrame()
"""The frame of a story with no calendar anchor that starts at story-minute 0."""


def _bound(bound: Bound, frame: TimeFrame, *, start: bool) -> str:
    """An exact bound as its moment; an approximate one as the range it lies in, earliest
    first ("sometime between A and B")."""
    if bound.inner == bound.outer:
        return frame.moment(bound.inner)
    early, late = (bound.outer, bound.inner) if start else (bound.inner, bound.outer)
    return f"sometime between {frame.moment(early)} and {frame.moment(late)}"


def render_time(p: Proposition, registry: Registry, frame: TimeFrame = UNANCHORED) -> str:
    """A proposition's valid-time as the model reads it (#460): ``from A until B`` (``from A
    on`` or ``until B`` with an open side; an approximate bound as ``sometime between X and
    Y``), an instant at its own precision (``on 2012-05-10``), and ``always`` for a permanent
    predicate or an unbounded interval. Dates follow ``frame`` (``TimeFrame``); story-minute
    offsets never reach a prompt."""
    predicate = registry.get(p.predicate_id)
    if predicate is not None and predicate.temporal_kind is TemporalKind.PERMANENT:
        return "always"
    vt = p.valid_time
    if isinstance(vt, Instant):
        return frame.instant(vt.at, vt.granularity)
    if vt.start is None and vt.end is None:
        return "always"
    if vt.start is None:
        assert vt.end is not None  # noqa: S101 - one side is set
        return f"until {_bound(vt.end, frame, start=False)}"
    if vt.end is None:
        return f"from {_bound(vt.start, frame, start=True)} on"
    return f"from {_bound(vt.start, frame, start=True)} until {_bound(vt.end, frame, start=False)}"


def render_proposition(p: Proposition, registry: Registry, names: Mapping[str, str]) -> str:
    """``<subject> [not] <predicate> <object>`` with entity names (ids when unnamed)."""
    predicate = registry.get(p.predicate_id)
    words = [names.get(p.subject_id, p.subject_id)]
    if p.polarity.value == "negated":
        words.append("not")
    words.append(predicate.name if predicate is not None else p.predicate_id)
    if p.object_id is not None:
        words.append(names.get(p.object_id, p.object_id))
    if p.object_set:
        words.append(", ".join(names.get(o, o) for o in p.object_set))
    if p.literal is not None:
        unit = f" {p.literal.unit}" if p.literal.unit else ""
        words.append(f"{p.literal.value}{unit}")
    for key, value in sorted(p.qualifiers.items()):
        words.append(f"{key}={names.get(value, value) if isinstance(value, str) else value}")
    return " ".join(str(w) for w in words)


def _ids_of(props: Sequence[Proposition]) -> list[str]:
    found: list[str] = []
    for p in props:
        found.append(p.subject_id)
        if p.object_id is not None:
            found.append(p.object_id)
        found += list(p.object_set or ())
        found += [v for v in p.qualifiers.values() if isinstance(v, str)]
    return list(dict.fromkeys(found))


TEXT_MAX = 1_000
HOLDER_MAX = 200
TIME_MAX = 200


def _clip(text: str, limit: int = TEXT_MAX) -> str:
    """``text`` cut to what ``EntailmentInputs`` accepts (a long name never fails a turn)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _digest(inputs: EntailmentInputs) -> str:
    body = json.dumps(inputs.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


# -- the verifier -----------------------------------------------------------------------------


_WORST = {VerdictKind.CONTRADICTED: 3, VerdictKind.UNCERTAIN: 2, VerdictKind.NOVEL: 1}


@dataclass(frozen=True, slots=True)
class _At:
    """Whose records, as which viewpoint, through which position: one ``verify`` call's."""

    viewpoint: str
    position: tuple[int, int] | None
    holder: Holder


class Verifier:
    """One turn's verifier (see the module docstring)."""

    def __init__(
        self,
        source: ClaimSource,
        config: VerifyConfig,
        *,
        models: ModelAccessLayer | None,
        budget: TurnBudget,
        gate: Gate | None = None,
        routes: Mapping[str, Route] | None = None,
        on_escalation: EscalationSink | None = None,
        beliefs: BeliefSource | None = None,
    ) -> None:
        self.source = source
        self.beliefs = beliefs
        """The speakers' belief sets (#166); ``None``: no belief dimension (partitions off)."""
        self.config = config
        self.models = models
        self.budget = budget
        self.gate = gate or Gate(config.entailment_gate)
        self.routes = routes
        self.on_escalation = on_escalation
        self.results: list[tuple[ClaimRef, VerifyResult]] = []
        self.escalations: list[EscalationRecord] = []
        self.rendered: dict[int, str | None] = {}
        self._answers: dict[str, EntailmentVerdict] = {}
        self.entailment_s = 0.0
        """Seconds spent in entailment calls (#155's ``latency_ms_by_stage.entailment``)."""
        self.introductions: Introductions | None = None
        """The turn's minted places and objects (#479), named in prompts and the report."""

    async def verify(
        self,
        claim: Proposition,
        viewpoint: str = OMNISCIENT,
        position: tuple[int, int] | None = None,
        *,
        holder: Holder = CANON,
        ref: ClaimRef | None = None,
        asserter: Asserter | None = None,
        protected: bool = False,
        resolves: str | None = None,
        believer: str | None = None,
        intent: str | None = None,
        cites: Sequence[str] = (),
    ) -> VerifyResult | None:
        """``claim``'s verdict as ``viewpoint`` may know it, at narrative ``position`` (canon
        written at or before it); ``None`` when the story's verification is off. A claim that
        ends ``novel`` passes ``asserter``'s permission check (``protected``: the claim's text
        matched protected content, #148's detector; ``resolves``: the open thunk the claim fills,
        #146's resolution exemption) before it can be canonized or repaired. ``believer`` (#166:
        the speaker of a first-person ``say``) adds the belief dimension and the classification,
        with the beat's declared ``intent`` and its ``think``'s ``cites``."""
        if not self.config.on:
            return None
        checking = time.perf_counter()
        found = await self.source.deterministic(
            claim, holder=holder, viewpoint=viewpoint, position=position
        )
        if found is None:
            return None
        # NFR-PERF-2's span: the deterministic path alone, before any model fallback (#153).
        stages.observe("verify", time.perf_counter() - checking)
        verdict, fetched = found
        where = ref or ClaimRef()
        at = _At(viewpoint, position, holder)
        result = await self._settle(claim, verdict, fetched, at, where, top=True)
        if (
            result.kind is VerdictKind.NOVEL
            and viewpoint != OMNISCIENT
            and believer is None
            and where.channel not in (SAY, *NEVER_CANONIZES)
        ):
            result = await self._hidden(result, claim, holder, position)
        if asserter is not None and result.kind is VerdictKind.NOVEL:
            result = await self._permit(
                result, claim, asserter, at, protected=protected, resolves=resolves
            )
        if believer is not None and self.beliefs is not None:
            result = await self._speech(result, claim, believer, intent, cites, position)
        self.results.append((where, result))
        if result.surfaced:
            self.rendered[id(result)] = await self._render_claim(claim)
        log.debug(
            "verify: %s from %s (escalated=%s, disposition=%s)",
            result.kind.value,
            result.source,
            result.escalated,
            result.disposition,
        )
        return result

    async def _hidden(
        self,
        result: VerifyResult,
        claim: Proposition,
        holder: Holder,
        position: tuple[int, int] | None,
    ) -> VerifyResult:
        """#169 (FR-EPI-8): a novel claim that canon hidden from its viewpoint contradicts (the
        engine's check, not the character's) would give a determined fact a second value: it
        is ``contradicted``, citing nothing (``HIDDEN_CONFLICT``)."""
        found = await self.source.deterministic(
            claim, holder=holder, viewpoint=OMNISCIENT, position=position
        )
        if found is None or found[0].kind is not VerdictKind.CONTRADICTED:
            return result
        verdict = Verdict(VerdictKind.CONTRADICTED, holder=holder, position=found[0].position)
        return VerifyResult(claim, verdict, VerdictKind.CONTRADICTED, hidden_conflict=True)

    def adopt_answers(self, other: Verifier) -> None:
        """Reuse ``other``'s validated entailment answers (the same turn's): a repair's
        re-verification (#151) of a claim the turn already escalated, against the same records,
        makes no second call."""
        for key, answer in other._answers.items():
            self._answers.setdefault(key, answer)

    def unnormalizable(
        self,
        found: Unnormalizable,
        *,
        ref: ClaimRef | None = None,
        disposition: SafeDisposition | None = None,
    ) -> VerifyResult | None:
        """A claim #140's ``normalize`` could not turn into a proposition: ``uncertain`` with
        its reason, which takes the safe disposition directly (there is no normalized claim to
        show the model), or ``disposition`` when the caller names one (#479, ADR-163: an
        extraction claim whose weak relation names something untracked is surfaced);
        ``None`` when the story's verification is off."""
        if not self.config.on:
            return None
        where = ref or ClaimRef()
        verdict = found.verdict()
        decision = GateDecision(
            "unnormalized", found.reason, UNNORMALIZED_SHAPE, verdict.confidence, None
        )
        result = self._safe(None, verdict, decision, where, disposition=disposition)
        self.results.append((where, result))
        return result

    # -- private thoughts and recall (#172) -----------------------------------------------

    def leaked(self, private_ref: str, *, ref: ClaimRef | None = None) -> VerifyResult | None:
        """A claim that repeats or narrates another character's private thought
        (``private_ref``: ``think:<record>``): ``uncertain``, always repaired (``THINK_LEAK``,
        FR-TURN-3), never canonized; ``None`` when the story's verification is off."""
        if not self.config.on:
            return None
        where = ref or ClaimRef()
        result = VerifyResult(
            None, Verdict(VerdictKind.UNCERTAIN), VerdictKind.UNCERTAIN, think_leak=private_ref
        )
        self.results.append((where, result))
        return result

    async def verify_recall(
        self,
        content: str,
        speaker: str,
        position: tuple[int, int] | None,
        *,
        ref: ClaimRef | None = None,
        claim_id: str | None = None,
    ) -> VerifyResult | None:
        """A first-person recall of ``content`` by ``speaker`` (FR-VERIFY-9;
        ``core.epistemic.resurface``): ``supported`` with rule ``recall`` when one of the
        speaker's dated records backs it, else a confabulation (``novel``, repaired, an
        escalation record with gate reason ``confabulation``). ``None`` when verification is
        off or the story has no per-character partitions (no record of the speaker's own)."""
        if not self.config.on or self.beliefs is None:
            return None
        where = ref or ClaimRef()
        turn = 0 if position is None else position[0]
        check = await resurface.check_recall(
            self.beliefs.conn, self.beliefs.story_id, speaker, turn=turn, content=content
        )
        kind = VerdictKind.SUPPORTED if check.grounded else VerdictKind.NOVEL
        result = VerifyResult(None, Verdict(kind), kind, recall=check)
        self.results.append((where, result))
        if not check.grounded:
            record = EscalationRecord(
                claim_id=claim_id,
                gate_reason=CONFABULATION,
                predicate_shape=UNNORMALIZED_SHAPE.key(),
                deterministic_confidence=1.0,
                threshold=None,
                called=False,
                reused=False,
                model_verdict=None,
                model_confidence=None,
                model_uncertain=None,
                final_kind=kind.value,
                disposition="repair",
                degradation=(),
                tentative_verdict=kind.value,
                gate_outcome=CONFABULATION,
                escalation_on=True,
            )
            self.escalations.append(record)
            if self.on_escalation is not None:
                self.on_escalation(record)
        return result

    # -- the speaker's beliefs (#166) -----------------------------------------------------

    async def _speech(
        self,
        result: VerifyResult,
        claim: Proposition,
        believer: str,
        intent: str | None,
        cites: Sequence[str],
        position: tuple[int, int] | None,
    ) -> VerifyResult:
        """``result`` with the claim's belief dimension and classification (see the module
        docstring)."""
        beliefs = self.beliefs
        at = None if beliefs is None else beliefs.position(position)
        if beliefs is None or at is None:
            return result
        found = await beliefs.check(claim, believer, at)
        if found is None:
            return result
        declared = intent or "unspecified"
        contrary: tuple[str, ...] = ()
        known: tuple[str, ...] = ()
        if found.kind is VerdictKind.CONTRADICTED:
            contrary = await beliefs.strongest(
                [e.belief_id for e in found.evidence if e.belief_id is not None]
            )
            if not contrary and await beliefs.own_age(claim, believer):
                # FR-CAT-12: a character knows its own true age (canon it holds of itself).
                known = tuple(
                    e.proposition_id
                    for e in found.evidence
                    if e.fact_id is not None and e.proposition_id is not None
                )
        hidden = None
        open_ = result.kind not in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED)
        if open_ and declared != speech_acts.JOKING:
            # #171: asked for a belief-consistent claim too, so an established false belief
            # beyond the speaker's view ("I've never met that stranger", canon holding that the
            # stranger is Bobby) is a sincere error, labelled false, and stands.
            hidden = await beliefs.hidden(claim, believer, at)
        # #167: contradicting a belief the speaker keeps secret is a deception, whatever the
        # render declared (FR-SCN-8's disclosure; a joke stays a joke).
        intent_in = declared
        inferred = False
        if contrary and declared in ("sincere", "unspecified") and await beliefs.secret(contrary):
            intent_in, inferred = speech_acts.DECEPTIVE, True
        inputs = speech_acts.Inputs(
            intent=intent_in,
            canon=result.kind,
            belief=found.kind,
            contrary=bool(contrary or known),
            hidden=hidden,
        )
        licence = None
        if speech_acts.needs_licence(inputs):
            licence = await beliefs.licence(claim, believer, at, cites)
        decision = speech_acts.classify(inputs, licensed=licence is not None)
        speech = SpeechAct(
            believer, at, declared, found, decision, contrary, hidden, licence, known, inferred
        )
        found_result = dataclasses.replace(result, speech=speech)
        if (
            decision.hallucinated
            and self.config.safe_disposition == "surface"
            and result.kind is not VerdictKind.CONTRADICTED
        ):
            found_result = dataclasses.replace(
                found_result,
                disposition="surface",
                degradation=(*result.degradation, UNCERTAIN_SURFACED),
            )
        log.debug("verify: speech act %s (belief %s)", decision.classification, found.kind.value)
        return found_result

    # -- attributions (#167) ---------------------------------------------------------------

    async def verify_attribution(
        self,
        claim: Proposition,
        found: Any,
        position: tuple[int, int] | None,
        *,
        believer: str | None,
        ref: ClaimRef | None = None,
        intent: str | None = None,
    ) -> VerifyResult | None:
        """An attribution claim ("Bobby thinks it was Marcus"; ``found``: its
        ``ClaimAttribution``): never checked against canon as the speaker's assertion (its
        verdict stays ``novel`` and it never canonizes). Under per-character partitions, its
        objective relation is labelled; under ``nested_beliefs`` the speaker's (``believer``)
        belief relation is computed over its own nested beliefs only and the claim classified
        (``core.verify.speech_acts``: no legitimate basis → a hallucination, repaired). With the
        module off the claim is exempt from the hallucination rule (``unknown``, sincere).
        ``None`` when verification is off."""
        from epistrel.core.epistemic import nested

        if not self.config.on:
            return None
        where = ref or ClaimRef()
        holder = Holder.belief(believer) if believer is not None else CANON
        base = Verdict(VerdictKind.NOVEL, holder=holder, position=position)
        result = VerifyResult(claim, base, VerdictKind.NOVEL)
        beliefs = self.beliefs
        at = None if beliefs is None else beliefs.position(position)
        if beliefs is None or at is None:
            check_ = AttributionCheck(found.holder_id, found.mode, found.factive)
            result = dataclasses.replace(result, attribution=check_)
            self.results.append((where, result))
            return result
        objective = await beliefs.objective(claim, found, at)
        on = nested.nested_on(beliefs.settings)
        if believer is None:
            check_ = AttributionCheck(found.holder_id, found.mode, found.factive, None, objective)
            result = dataclasses.replace(result, attribution=check_)
            self.results.append((where, result))
            return result
        declared = intent or "unspecified"
        kind: VerdictKind = VerdictKind.NOVEL
        cited: tuple[str, ...] = ()
        contrary: tuple[str, ...] = ()
        if on and found.holder_id is not None:
            kind, cited, contrary = await beliefs.attribution(claim, believer, found, at)
        check_ = AttributionCheck(
            found.holder_id,
            found.mode,
            found.factive,
            cited if on and found.holder_id is not None else None,
            objective,
        )
        settles = {"true": VerdictKind.SUPPORTED, "false": VerdictKind.CONTRADICTED}
        inputs = speech_acts.Inputs(
            intent=declared,
            canon=VerdictKind.NOVEL,
            belief=kind,
            contrary=bool(contrary),
            # What the attributed holder actually holds is beyond the speaker's view.
            hidden=settles.get(objective or "") if on else None,
        )
        licence = None
        if on and speech_acts.needs_licence(inputs):
            licence = await beliefs.attributed_licence(claim, believer, found, at)
        decision = speech_acts.classify(inputs, licensed=licence is not None)
        if decision.classification == speech_acts.SINCERE_ERROR:
            # The attribution's truth is a label, never its classification (FR-BELIEF-15).
            decision = dataclasses.replace(decision, classification=speech_acts.SINCERE)
        decision = dataclasses.replace(decision, write=None)
        speech = SpeechAct(
            believer,
            at,
            declared,
            Verdict(kind, holder=holder, position=position),
            decision,
            contrary,
            None,
            licence,
        )
        result = dataclasses.replace(result, speech=speech, attribution=check_)
        if decision.hallucinated and self.config.safe_disposition == "surface":
            result = dataclasses.replace(
                result,
                disposition="surface",
                degradation=(*result.degradation, UNCERTAIN_SURFACED),
            )
        self.results.append((where, result))
        log.debug("verify: attribution %s", decision.classification)
        return result

    # -- novelty and permission (#144) ----------------------------------------------------

    async def _permit(
        self,
        result: VerifyResult,
        claim: Proposition,
        asserter: Asserter,
        at: _At,
        *,
        protected: bool,
        resolves: str | None = None,
    ) -> VerifyResult:
        found = await self.source.permission(
            claim, asserter, position=at.position, protected=protected, resolves=resolves
        )
        anchor = None
        if isinstance(found, Permitted):
            anchor = await self.source.anchor(claim, viewpoint=at.viewpoint, position=at.position)
        return dataclasses.replace(result, asserter=asserter, permission=found, anchored_to=anchor)

    def canon_requests(self) -> list[CanonRequest]:
        """The turn's permitted novel claims to canonize at its commit, in verification order
        (``core.canon.canonize.canonize_all`` dedupes identical ones): never a denied claim, a
        ``think`` claim, or a ``say`` claim that is not about the speaker's own actions or
        backstory."""
        found: list[CanonRequest] = []
        for ref, r in self.results:
            permitted = r.permitted
            if permitted is None or r.claim is None or r.asserter is None:
                continue
            if r.kind is not VerdictKind.NOVEL or ref.channel in NEVER_CANONIZES:
                continue
            if not r.canonizes:  # #166: a lie, a joke or a hallucination never canonizes
                continue
            if ref.channel == SAY and permitted.basis is not Basis.OWN:
                continue
            found.append(
                CanonRequest(
                    claim=r.claim,
                    provenance=r.asserter.provenance,
                    visibility=visibility_of(ref.channel, ref.scope),
                    anchored_to=r.anchored_to,
                    beat=ref.beat,
                    claim_id=ref.claim_id,
                )
            )
        return found

    async def denied(self) -> list[dict[str, Any]]:
        """``turn_report.verify.denied``: each novel claim its asserter may not introduce,
        rendered, with its asserter, reason, beat and span (never canonized; #151 repairs every
        one but a ``player_authority`` denial)."""
        found: list[dict[str, Any]] = []
        for ref, r in self.results:
            denied = r.denied
            if denied is None or r.asserter is None:
                continue
            found.append(
                {
                    "claim": await self._render_claim(r.claim),
                    "claim_id": ref.claim_id,
                    "asserter": r.asserter.key(),
                    "reason": denied.reason.value,
                    "repaired": denied.repaired,
                    "beat": ref.beat,
                    "span": None if ref.span is None else list(ref.span),
                }
            )
        return found

    # -- the residual ---------------------------------------------------------------------

    async def _settle(
        self,
        claim: Proposition,
        verdict: Verdict,
        fetched: list[Record],
        at: _At,
        ref: ClaimRef,
        *,
        top: bool,
    ) -> VerifyResult:
        if verdict.kind is not VerdictKind.UNCERTAIN or verdict.reason is None:
            return VerifyResult(claim, verdict, verdict.kind)
        if verdict.reason in RESOLVED_IN_CORE:
            if not top:
                return VerifyResult(claim, verdict, VerdictKind.NOVEL)
            return await self._remainders(claim, verdict, fetched, at, ref)
        registry = await self.source.registry()
        shape = PredicateShape.of(claim, registry.get(claim.predicate_id))
        decision = self.gate.decide(verdict.reason, shape, verdict.confidence)
        if not decision.escalate or not self.config.escalation_on:
            return self._safe(claim, verdict, decision, ref)
        return await self._escalate(claim, verdict, decision, fetched, at, ref)

    async def _remainders(
        self,
        claim: Proposition,
        verdict: Verdict,
        fetched: list[Record],
        at: _At,
        ref: ClaimRef,
    ) -> VerifyResult:
        subs = tuple(
            [
                await self._settle(rest, rv, fetched, at, ref, top=False)
                for rest, rv in zip(
                    verdict.remainder_claims, verdict.remainder_verdicts, strict=True
                )
            ]
        )
        kinds = [s.kind for s in subs]
        if not subs:
            kind = VerdictKind.UNCERTAIN  # #140 always splits a remainder off; never assume one
        elif all(k is VerdictKind.SUPPORTED for k in kinds):
            kind = VerdictKind.SUPPORTED
        else:
            kind = max(
                (k for k in kinds if k is not VerdictKind.SUPPORTED), key=lambda k: _WORST[k]
            )
        disposition = self.config.safe_disposition if kind is VerdictKind.UNCERTAIN else None
        degradation = tuple(dict.fromkeys(d for s in subs for d in s.degradation))
        decided_by_model = any(s.source == "entailment" and s.kind is kind for s in subs)
        return VerifyResult(
            claim,
            verdict,
            kind,
            source="entailment" if decided_by_model else "deterministic",
            escalated=any(s.escalated for s in subs),
            disposition=disposition,
            degradation=degradation,
            sub_results=subs,
        )

    def _safe(
        self,
        claim: Proposition | None,
        verdict: Verdict,
        decision: GateDecision,
        ref: ClaimRef,
        *,
        called: bool = False,
        reused: bool = False,
        answer: EntailmentVerdict | None = None,
        model_uncertain: str | None = None,
        degradation: tuple[str, ...] = (),
        disposition: SafeDisposition | None = None,
    ) -> VerifyResult:
        disposition = disposition or self.config.safe_disposition
        if disposition == "surface":
            degradation = (*degradation, UNCERTAIN_SURFACED)
        result = VerifyResult(
            claim,
            verdict,
            VerdictKind.UNCERTAIN,
            escalated=called,
            decision=decision,
            model_verdict=None if answer is None else answer.verdict,
            model_confidence=None if answer is None else answer.confidence,
            model_uncertain=model_uncertain,
            disposition=disposition,
            degradation=degradation,
        )
        self._record(result, ref, called=called, reused=reused)
        return result

    async def _escalate(
        self,
        claim: Proposition,
        verdict: Verdict,
        decision: GateDecision,
        fetched: list[Record],
        at: _At,
        ref: ClaimRef,
    ) -> VerifyResult:
        if self.models is None:
            return self._safe(claim, verdict, decision, ref, degradation=(ENTAILMENT_UNAVAILABLE,))
        inputs = await self._inputs(claim, fetched, at)
        key = _digest(inputs)
        reused = key in self._answers
        route = (self.routes or {}).get(ENTAILMENT.name)
        params = BUILT_IN_PARAMS.merged(route.params if route is not None else None)
        asking = time.perf_counter()
        try:
            with routing(self.routes or {}):
                answer = await self.models.structured_call(
                    ENTAILMENT.name,
                    EntailmentVerdict,
                    inputs,
                    params=params,
                    deterministic_first=lambda: self._answers.get(key),
                    budget=self.budget,
                )
        except BudgetExhausted:
            return self._safe(
                claim, verdict, decision, ref, called=True, degradation=(ENTAILMENT_BUDGET,)
            )
        except ModelUnavailable:
            return self._safe(
                claim, verdict, decision, ref, called=True, degradation=(ENTAILMENT_UNAVAILABLE,)
            )
        finally:
            self.entailment_s += time.perf_counter() - asking
        if isinstance(answer, Uncertain):
            degradation = (ENTAILMENT_BUDGET,) if answer.reason == "budget_exhausted" else ()
            return self._safe(
                claim,
                verdict,
                decision,
                ref,
                called=answer.attempts > 0,
                model_uncertain=answer.reason,
                degradation=degradation,
            )
        self._answers[key] = answer
        if answer.confidence < self.config.entailment_min_confidence or answer.verdict == "novel":
            return self._safe(
                claim, verdict, decision, ref, called=True, reused=reused, answer=answer
            )
        kind = VerdictKind.SUPPORTED if answer.verdict == "supported" else VerdictKind.CONTRADICTED
        result = VerifyResult(
            claim,
            verdict,
            kind,
            source="entailment",
            escalated=True,
            decision=decision,
            model_verdict=answer.verdict,
            model_confidence=answer.confidence,
        )
        self._record(result, ref, called=True, reused=reused)
        return result

    async def _inputs(self, claim: Proposition, fetched: list[Record], at: _At) -> EntailmentInputs:
        limit = self.config.entailment_max_records
        chosen = list({r.proposition_id: r for r in fetched}.values())[:limit]
        if len(chosen) < limit:
            seen = {r.proposition_id for r in chosen}
            more = await self.source.neighbors(
                claim,
                viewpoint=at.viewpoint,
                position=at.position,
                limit=self.config.neighborhood_max_facts,
            )
            chosen += [r for r in more if r.proposition_id not in seen][: limit - len(chosen)]
        registry = await self.source.registry()
        names = await self._names(_ids_of([claim, *(r.proposition for r in chosen)]))
        frame = await self.source.time_frame()
        holder = at.holder.key()
        claim_text = (
            f"{render_proposition(claim, registry, names)} [{render_time(claim, registry, frame)}]"
        )
        return EntailmentInputs(
            claim=_clip(claim_text),
            records=[
                EntailmentRecord(
                    text=_clip(render_proposition(r.proposition, registry, names)),
                    holder=_clip(holder, HOLDER_MAX),
                    valid_time=_clip(render_time(r.proposition, registry, frame), TIME_MAX),
                )
                for r in chosen
            ],
        )

    async def _names(self, ids: Sequence[str]) -> Mapping[str, str]:
        """Entity names for a prompt or the report: the story's, and the turn's minted places
        and objects (#479) by the name the claim gave them."""
        minted = {} if self.introductions is None else self.introductions.names()
        return {**minted, **await self.source.names([i for i in ids if i not in minted])}

    async def _render_claim(self, claim: Proposition | None) -> str | None:
        if claim is None:
            return None
        registry = await self.source.registry()
        names = await self._names(_ids_of([claim]))
        frame = await self.source.time_frame()
        return (
            f"{render_proposition(claim, registry, names)} [{render_time(claim, registry, frame)}]"
        )

    def _record(self, result: VerifyResult, ref: ClaimRef, *, called: bool, reused: bool) -> None:
        decision = result.decision
        if decision is None:
            return
        record = EscalationRecord(
            claim_id=ref.claim_id,
            gate_reason=decision.reason.value,
            predicate_shape=decision.shape.key(),
            deterministic_confidence=decision.confidence,
            threshold=decision.threshold,
            called=called,
            reused=reused,
            model_verdict=result.model_verdict,
            model_confidence=result.model_confidence,
            model_uncertain=result.model_uncertain,
            final_kind=result.kind.value,
            disposition=result.disposition,
            degradation=result.degradation,
            gate_outcome=decision.outcome,
            escalation_on=self.config.escalation_on,
        )
        self.escalations.append(record)
        if self.on_escalation is not None:
            self.on_escalation(record)

    # -- the turn report ------------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """``turn_report.verify``: how many claims were checked, decided without a model call,
        and escalated, one entry per claim (ids and codes only) and the denied novel claims'
        codes (``denied``: ids, asserters and reasons; ``await denied()`` renders them)."""
        escalated = sum(1 for _, r in self.results if r.escalated)
        speech = [r.speech for _, r in self.results if r.speech is not None]
        found: dict[str, Any] = {}
        if speech:
            found["speech"] = {
                "classified": len(speech),
                "classifications": {
                    c: n
                    for c in speech_acts.CLASSIFICATIONS
                    if (n := sum(1 for s in speech if s.classification == c))
                },
                "intent_corrected": sum(1 for s in speech if s.decision.intent_corrected),
                "licensed": sum(1 for s in speech if s.writes),
            }
        return found | {
            "claims": len(self.results),
            "deterministic": len(self.results) - escalated,
            "escalated": escalated,
            "verdicts": [r.as_payload(ref.claim_id) for ref, r in self.results],
            "denied": [
                {
                    "claim_id": ref.claim_id,
                    "asserter": r.asserter.key(),
                    "reason": d.reason.value,
                    "beat": ref.beat,
                    "span": None if ref.span is None else list(ref.span),
                }
                for ref, r in self.results
                if (d := r.denied) is not None and r.asserter is not None
            ],
        }

    def uncertain_claims(self) -> list[dict[str, Any]]:
        """The turn report's ``uncertain_claims``: each surfaced claim, rendered, with where it
        is and why the engine could not decide it."""
        return [
            {
                "claim": self.rendered.get(id(r), ref.claim_id),
                "claim_id": ref.claim_id,
                "span": None if ref.span is None else list(ref.span),
                "beat": ref.beat,
                "reason": "hallucination"
                if r.hallucinated
                else (None if r.reason is None else r.reason.value),
                "confidence": r.verdict.confidence,
            }
            for ref, r in self.results
            if r.surfaced
        ]

    def degradation(self) -> list[str]:
        return list(dict.fromkeys(d for _, r in self.results for d in r.degradation))


async def story_verifier(
    conn: AsyncConnection,
    read_set: StructuredReadSet,
    story_id: str,
    *,
    models: ModelAccessLayer | None,
    budget: TurnBudget,
    gate: Gate | None = None,
    remember: bool = True,
    variant: int = 0,
) -> Verifier:
    """A verifier over the story's canon under its effective configuration, routed by its
    ``models.purposes`` (#325); under per-character partitions (#166) also over each speaker's
    beliefs (``BeliefSource``; ``variant``: the turn's)."""
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.orchestrator.models.config import routes_of

    settings = (await effective_configuration(conn, story_id)).settings
    config = VerifyConfig.of(settings, await read_section(conn, VerifySettings))
    if gate is None and config.escalation_on:
        # #155: the shapes an operator tightened (a promotion, or a ``live`` write), read
        # per verifier: one small read per turn, so a change applies to the next turn.
        tightened = await yield_queries.live_thresholds(conn)
        gate = Gate(config.entailment_gate, FixedThresholds(tightened)) if tightened else None
    beliefs = (
        BeliefSource(
            conn, read_set, story_id, settings=settings, variant=variant, remember=remember
        )
        if config.on and module_on(settings, PARTITIONS_MODULE)
        else None
    )
    return Verifier(
        CanonSource(conn, read_set, story_id, remember=remember),
        config,
        models=models,
        budget=budget,
        gate=gate,
        routes=routes_of(settings),
        beliefs=beliefs,
    )


__all__ = [
    "BUILT_IN_PARAMS",
    "ENTAILMENT_BUDGET",
    "ENTAILMENT_RULE",
    "ENTAILMENT_UNAVAILABLE",
    "HALLUCINATION",
    "RESOLUTION_CONFLICT",
    "UNANCHORED",
    "UNCERTAIN_SURFACED",
    "UNPERMITTED",
    "BeliefSource",
    "CanonSource",
    "ClaimRef",
    "ClaimSource",
    "EscalationRecord",
    "EscalationSink",
    "Licence",
    "RecordsSource",
    "SafeDisposition",
    "SpeechAct",
    "TimeFrame",
    "Verifier",
    "VerifyConfig",
    "VerifyResult",
    "render_proposition",
    "render_time",
    "story_verifier",
]
