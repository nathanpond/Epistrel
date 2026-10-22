"""``RepairLadder.run(beat, violations, budget) -> RepairResult``: correct one structured beat
whose claims ``needs_repair`` (FR-REPAIR-1..4, FR-VERIFY-12, FR-GUARD-6, NFR-PERF-7; #151).

**What goes in.** A beat of a structured turn and its violations: every claim #142's verifier
marked ``needs_repair`` — ``contradicted``, ``uncertain`` under the ``repair`` safe disposition,
or a novel claim #144 denied (``unpermitted``). Several violations of one beat are repaired
together.

**The context** (FR-REPAIR-1). For each contradicted claim the facts its verdict cites (and,
for a closed set it cites, the canon facts of the set's predicate for its subject: "Bobby's only
enrollment is OSU" brings his enrollment at OSU) and their dependency cluster, the union of
``core.verify.neighborhood.fact_neighborhood(depth 2)`` seeded at each of them, as the beat's
speaker may read them (the narrator's: omniscient), through the claim's position; a claim that
cites no fact seeds it at its subject. An undecided or unpermitted claim has no neighborhood:
its constraint is "do not assert …" ("do not introduce facts about …", with the reason, for an
unpermitted one).
The neighborhood's size (its facts, memories excluded) classifies the repair.

**The rungs**, strictly in order, an inapplicable one skipped (the pure parts are
``core.repair.rungs``):

1. *narrowing*: the sentences that carry the violations are dropped (a ``say``, ``do`` or
   narration beat that keeps at least one sentence), model-free;
2. *substitution*: a functional predicate whose verdict cites exactly one canon fact has the
   claim's object replaced by the canon value (its canonical name or literal), model-free;
3. *regeneration*: the ``regeneration`` purpose rewrites the beat in place (the generation
   inputs of the render it came from, the rejected text, the typed constraints, the
   contradicted facts and the neighborhood) through the Model Access Layer's
   ``structured_call`` (``RegeneratedBeat``). It starts only when the turn's budget can
   reserve, at once, the re-render, its extraction and one re-check per model-assisted output
   policy (``can_afford_unreserved`` + ``hold``), and while the turn has ``repair.max_attempts``
   left (each regeneration is one attempt; #53's retries are inside it). Otherwise the ladder
   stays deterministic: ``repair_deterministic`` (cause ``budget`` or ``max_attempts``);
4. *the safe fallback*: a ``say``, ``do`` or narration beat becomes the stored safe fallback
   line (the character's own, else the deployment's, else the last resort; the first that
   passes the output policies), ``safe_fallback`` (cause ``repair``); a ``think`` or ``latent``
   beat is dropped instead (reported, no fallback text). ``think`` and ``latent`` beats go
   straight to rung 3.

**Acceptance** (FR-REPAIR-2, FR-GUARD-6). Every rung's output passes the output gate (the
story's output policies, model-assisted ones included) and is detected again (#148: the
declared structure and the deterministic detectors for rungs 1 and 2, every layer for rung 3)
and verified again (#142, over the turn's overlay, reusing the turn's validated entailment
answers) before it is accepted. A correction with any claim that still ``needs_repair``, or one
the gate rejects, is not accepted: the next rung runs. A regeneration whose extraction could not
run is accepted on its deterministic re-verification, with ``repair_unextracted``.

**What comes out** (``RepairResult``): the accepted beat (``None`` for a dropped one) at the
original beat's position with its new claims (a claim rung 2 rewrote restates canon and is
never canonized, ``restates``), the rung, the attempts, every rejected text
(``RejectedRender``: the original beat and each failed candidate, written by the pipeline as
``render_rejected`` audit events at the commit), the FR-REPAIR-4 class
(``core.repair.rungs.classify``), the degradation and the violations still open (none once a
beat is accepted). Nothing from a rejected render is canonized: the pipeline drops its canon
requests.

Log lines carry counts and codes only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon.facts import Fact
from epistrel.core.config_changes import read_section
from epistrel.core.events.types import RenderRejected
from epistrel.core.propositions.model import Cardinality, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.repair.rungs import (
    FALLBACK,
    NARROWING,
    REGENERATION,
    SUBSTITUTION,
    Constraint,
    RepairClass,
    classify,
    locate,
    narrow,
    substitute,
)
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.settings_schema import NAME_PLACEHOLDER, OMNISCIENT, VerifySettings
from epistrel.core.tables import entities
from epistrel.core.tables.propositions import closed_sets
from epistrel.core.turns.beats import BEAT_TEXT_MAX, TurnBeat
from epistrel.core.verify.neighborhood import Seed, fact_neighborhood
from epistrel.core.verify.protected import viewpoint_of
from epistrel.core.verify.rules import Rule, VerdictKind
from epistrel.orchestrator.budget import REPAIR as BUDGET_REPAIR
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.lite.prompt import prompt_hash
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    REGENERATION as REGENERATION_PURPOSE,
)
from epistrel.orchestrator.models.registry import (
    REGENERATION_MAX_RECORDS,
    GenerationInputs,
    RegenerationInputs,
    RepairConstraint,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.policies.output_gate import LAST_RESORT, FallbackLines
from epistrel.orchestrator.structured.render import (
    MAX_PROPOSITIONS,
    IntentName,
    PropositionSketch,
)
from epistrel.orchestrator.verify.extraction import EXTRACTION_DEGRADED, CheckedClaim
from epistrel.orchestrator.verify.verifier import (
    RESOLUTION_CONFLICT,
    UNANCHORED,
    UNPERMITTED,
    TimeFrame,
    render_proposition,
    render_time,
)

log = logging.getLogger(__name__)

SAY, THINK, DO, LATENT, NARRATION = "say", "think", "do", "latent", "narration"
DETERMINISTIC_KINDS = frozenset({SAY, DO, NARRATION})
"""The beat kinds rungs 1, 2 and 4's fallback line apply to; ``think`` and ``latent`` go
straight to rung 3, then are dropped."""
NEIGHBORHOOD_DEPTH = 2
REPAIR_DETERMINISTIC = "repair_deterministic"
"""Degradation: a beat needed a regeneration the turn could not reserve (``budget``) or had no
attempt left for (``max_attempts``), so only the deterministic rungs and the fallback ran."""
REPAIR_UNEXTRACTED = "repair_unextracted"
"""Degradation: a regeneration's extraction could not run; its deterministic re-verification
stood."""
SAFE_FALLBACK = "safe_fallback"
"""Degradation: a beat became the stored safe fallback line (or, ``think``/``latent``, was
dropped)."""
REPAIR = "repair"


# -- the regeneration's answer -------------------------------------------------------------------


class RegeneratedBeat(BaseModel):
    """What one ``regeneration`` call returns (#151): the rewritten beat's text, the claims it
    declares and, for a ``say``, its intent. The beat keeps its kind, actor and position."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=BEAT_TEXT_MAX)
    propositions: list[PropositionSketch] = Field(default_factory=list, max_length=MAX_PROPOSITIONS)
    intent: IntentName | None = None

    @model_validator(mode="after")
    def _text(self) -> Self:
        if not self.text.strip():
            raise ValueError("text: empty")
        return self

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Message]) -> dict[str, Any]:
        """The stub's default (FR-MODEL-11): a text from the request's hash, no claims."""
        del messages
        return {"text": f"[stub regeneration {digest[:12]}]", "propositions": []}


# -- what the ladder works with ------------------------------------------------------------------


@dataclass(slots=True)
class Recheck:
    """A candidate beat detected and verified again (the pipeline's ``check_turn`` over that
    beat alone): the beat with its claims, the checked claims, the degradation and the
    pipeline's own record of the check (``value``), which it merges when the candidate is
    accepted."""

    beat: TurnBeat
    claims: list[CheckedClaim]
    degradation: list[str] = field(default_factory=list)
    value: Any = None

    @property
    def violations(self) -> list[CheckedClaim]:
        return [c for c in self.claims if c.result is not None and c.result.needs_repair]


Rechecker = Callable[[TurnBeat, Sequence[PropositionSketch], bool, int], Awaitable[Recheck]]
"""``recheck(candidate, sketches, backstop, rung)``: detect and verify the candidate beat
again; ``backstop`` ``False`` runs only the declared structure and the deterministic detectors;
``rung`` is the rung that made the candidate (#155 records its re-verification as the pass
``repair_<rung>``)."""
OutputCheck = Callable[[str], Awaitable[bool]]
"""Whether a text passes the story's output policies (the output gate's check)."""
InputsOf = Callable[[TurnBeat], GenerationInputs | None]
"""The generation inputs of the render a beat came from (``None``: no regeneration)."""


@dataclass(frozen=True, slots=True)
class RejectedRender:
    """One text the ladder rejected (FR-REPAIR-3): ``render_rejected``'s payload."""

    beat: int
    attempt: int
    rung: int
    text: str
    constraints: tuple[Constraint, ...] = ()
    verdicts: tuple[dict[str, Any], ...] = ()
    detected_claims: tuple[dict[str, Any], ...] = ()
    prompt_hash: str | None = None

    def event(self) -> RenderRejected:
        return RenderRejected(
            beat=self.beat,
            attempt=self.attempt,
            rung=self.rung,
            text=self.text,
            constraints=tuple(c.as_payload() for c in self.constraints),
            verdicts=self.verdicts,
            detected_claims=self.detected_claims,
            prompt_hash=self.prompt_hash,
        )


@dataclass(frozen=True, slots=True)
class Context:
    """FR-REPAIR-1's exposure for one beat: each violation's contradicted facts and the union
    of their neighborhoods (facts, ids and the lines a regeneration reads)."""

    contradicted: tuple[Fact, ...] = ()
    neighborhood: tuple[Fact, ...] = ()
    payloads: tuple[dict[str, Any], ...] = ()
    names: Mapping[str, str] = field(default_factory=dict)
    registry: Registry | None = None
    constraints: tuple[Constraint, ...] = ()
    frame: TimeFrame = UNANCHORED
    """How the facts' valid-times read in the regeneration prompt (#460)."""

    @property
    def size(self) -> int:
        """The neighborhood's size: its facts (memories excluded)."""
        return len({f.id for f in self.neighborhood})


@dataclass(slots=True)
class RepairResult:
    """What ``RepairLadder.run`` made of one beat (see the module docstring)."""

    beat: TurnBeat | None
    rung: int
    attempts: int
    rejected: list[RejectedRender]
    repair_class: RepairClass
    degradation: list[tuple[str, str]]
    remaining_violations: list[CheckedClaim]
    original: TurnBeat
    violations: list[CheckedClaim]
    context: Context
    recheck: Recheck | None = None
    sketches: list[PropositionSketch] = field(default_factory=list)

    def restates(self, claim: Proposition) -> bool:
        """Whether ``claim`` is one rung 2 rewrote to canon's value: it restates canon, so it is
        never canonized again (its time may be the turn's, not canon's)."""
        if self.rung != SUBSTITUTION:
            return False
        key = (claim.subject_id, claim.predicate_id, claim.qualifier_key())
        return any(
            (p.subject_id, p.predicate_id, p.qualifier_key()) == key
            for v in self.violations
            if (p := v.claim.proposition) is not None
        )

    @property
    def accepted(self) -> bool:
        """Whether a correction was accepted (rungs 1 to 3), not the fallback or a drop."""
        return self.rung < FALLBACK

    @property
    def dropped(self) -> bool:
        return self.beat is None

    def report(self) -> list[dict[str, Any]]:
        """``turn_report.repairs`` entries, one per violation: ids, codes and counts only."""
        return [
            {
                "beat": self.original.beat,
                "claim": v.claim_id,
                "verdict": v.verdict,
                "tag": None if v.result is None else v.result.repair_tag,
                "rung": self.rung,
                "class": self.repair_class.value,
                "attempts": self.attempts,
                "neighborhood_size": self.context.size,
                "neighborhood": list(self.context.payloads),
                "contradicted_facts": [f.id for f in self.context.contradicted],
                "accepted": self.accepted,
                "dropped": self.dropped,
                "rejected": len(self.rejected),
            }
            for v in self.violations
        ]


# -- the ladder ----------------------------------------------------------------------------------


def _verdicts(claims: Sequence[CheckedClaim]) -> tuple[dict[str, Any], ...]:
    return tuple(c.result.as_payload(c.claim_id) for c in claims if c.result is not None)


def _detected(claims: Sequence[CheckedClaim]) -> tuple[dict[str, Any], ...]:
    return tuple(c.claim.payload() for c in claims)


def _fact_ids(claim: CheckedClaim) -> list[str]:
    """The canon facts a claim's verdict cites, its remainders' included, in order."""
    result = claim.result
    if result is None:
        return []
    found: list[str] = []
    stack = [result]
    while stack:
        current = stack.pop(0)
        found += [e.fact_id for e in current.verdict.evidence if e.fact_id is not None]
        stack += list(current.sub_results)
    return list(dict.fromkeys(found))


def _closed_set_ids(claim: CheckedClaim) -> list[str]:
    result = claim.result
    if result is None:
        return []
    found: list[str] = []
    stack = [result]
    while stack:
        current = stack.pop(0)
        found += [e.closed_set_id for e in current.verdict.evidence if e.closed_set_id]
        stack += list(current.sub_results)
    return list(dict.fromkeys(found))


async def _closed_set_members(
    conn: AsyncConnection, lookup: KeyedLookup, story_id: str, claim: CheckedClaim
) -> list[str]:
    """The canon facts behind each closed set a verdict cites: the facts of its predicate for
    its subject (Bobby's enrollment at OSU, for "Bobby's only enrollment is OSU"), which the
    claim contradicts as much as the set does."""
    ids = _closed_set_ids(claim)
    if not ids:
        return []
    rows = await conn.execute(
        select(closed_sets.c.predicate_id, closed_sets.c.key).where(
            closed_sets.c.story_id == story_id, closed_sets.c.id.in_(ids)
        )
    )
    found: list[str] = []
    for row in rows:
        subject = (row.key or {}).get("subject_id")
        if subject is None:
            continue
        facts = await lookup.canon_rows(
            story_id, subject=str(subject), predicate=str(row.predicate_id)
        )
        found += [f.id for f in facts]
    return found


def _declared_index(claim: CheckedClaim) -> int | None:
    found = claim.claim.matched_declared_id
    if found is None or "." not in found:
        return None
    try:
        return int(found.rsplit(".", 1)[1])
    except ValueError:
        return None


class RepairLadder:
    """One turn's repair ladder (see the module docstring). Built per turn: its
    ``repair.max_attempts`` counts the turn's regenerations across every beat."""

    def __init__(
        self,
        db: AsyncEngine,
        models: ModelAccessLayer,
        story_id: str,
        *,
        turn: int,
        read_set: StructuredReadSet,
        max_attempts: int,
        recheck: Rechecker,
        output_ok: OutputCheck,
        fallback: FallbackLines,
        inputs_of: InputsOf,
        sketches_of: Mapping[int, Sequence[PropositionSketch]],
        params: ModelParams,
        extraction_calls: int,
        check_calls: int,
    ) -> None:
        self.db = db
        self.models = models
        self.story_id = story_id
        self.turn = turn
        self.read_set = read_set
        self.max_attempts = max_attempts
        self.recheck = recheck
        self.output_ok = output_ok
        self.fallback = fallback
        self.inputs_of = inputs_of
        self.sketches_of = sketches_of
        self.params = params
        self.extraction_calls = extraction_calls
        self.check_calls = check_calls
        self.attempts_used = 0
        self.cascade_threshold = VerifySettings().cascade_threshold
        self.max_facts = VerifySettings().neighborhood_max_facts

    @property
    def regeneration_cost(self) -> int:
        """What one regeneration reserves: the re-render, its extraction and one re-check per
        model-assisted output policy."""
        return 1 + self.extraction_calls + self.check_calls

    # -- the context ----------------------------------------------------------------------

    async def context(self, beat: TurnBeat, violations: Sequence[CheckedClaim]) -> Context:
        """FR-REPAIR-1's exposure (see the module docstring), read as the beat's speaker may."""
        async with self.db.connect() as conn:
            settings = await read_section(conn, VerifySettings)
            self.cascade_threshold = settings.cascade_threshold
            self.max_facts = settings.neighborhood_max_facts
            registry = await load_registry(conn, self.story_id)
            lookup = KeyedLookup(conn, self.read_set)
            viewpoint = self._viewpoint(beat)
            position = (self.turn, beat.beat)
            contradicted: dict[str, Fact] = {}
            neighbors: dict[str, Fact] = {}
            payloads: list[dict[str, Any]] = []
            for v in violations:
                if v.result is None or v.result.kind is not VerdictKind.CONTRADICTED:
                    continue
                cited = _fact_ids(v)
                members = await _closed_set_members(conn, lookup, self.story_id, v)
                found_facts = await lookup.canon_rows(
                    self.story_id,
                    viewpoint=viewpoint,
                    through_position=position,
                    ids=list(dict.fromkeys([*cited, *members])),
                )
                for fact in found_facts:
                    contradicted.setdefault(fact.id, fact)
                seeds = [Seed.fact(f.id) for f in found_facts]
                if not seeds and v.claim.proposition is not None:
                    seeds = [Seed.entity(v.claim.proposition.subject_id)]
                for seed in seeds:
                    found = await fact_neighborhood(
                        lookup,
                        self.story_id,
                        seed,
                        depth=NEIGHBORHOOD_DEPTH,
                        viewpoint=viewpoint,
                        through_position=position,
                        max_facts=self.max_facts,
                        registry=registry,
                    )
                    payloads.append(found.as_payload())
                    for fact in found.facts():
                        neighbors.setdefault(fact.id, fact)
            props = [f.proposition for f in (*contradicted.values(), *neighbors.values())]
            props += [v.claim.proposition for v in violations if v.claim.proposition is not None]
            names = await _names(conn, self.story_id, props)
            constraints = tuple([await self._constraint(v, registry, names) for v in violations])
            frame = TimeFrame.of(await load_time_basis(conn, self.story_id))
        return Context(
            contradicted=tuple(contradicted.values()),
            neighborhood=tuple(neighbors.values()),
            payloads=tuple(payloads),
            names=names,
            registry=registry,
            constraints=constraints,
            frame=frame,
        )

    def _viewpoint(self, beat: TurnBeat) -> str:
        if beat.narrator or beat.kind == NARRATION or beat.character_id is None:
            return OMNISCIENT
        return viewpoint_of(beat.character_id)

    async def _constraint(
        self, v: CheckedClaim, registry: Registry, names: Mapping[str, str]
    ) -> Constraint:
        claim = v.claim.proposition
        cites = tuple(v.result.evidence_ids()) if v.result is not None else ()
        if claim is None:
            return Constraint("do_not_assert", "Do not assert what this beat could not support.")
        if v.result is not None and v.result.repair_tag == RESOLUTION_CONFLICT:
            # #154: the beat bound an open thunk against recorded history; steer the binding
            # elsewhere (or leave it open).
            avoid = v.result.avoid
            name = names.get(avoid, avoid) if avoid is not None else None
            rendered = render_proposition(claim, registry, names)
            target = f"{name}" if name else "that value"
            return Constraint(
                "avoid_binding",
                f"avoid_binding: {target}. Recorded history rules out that {rendered}: settle "
                "it as something else, insignificant and with no history here, or leave it "
                "open.",
                cites,
            )
        if v.result is not None and v.result.repair_tag == UNPERMITTED:
            denied = v.result.denied
            reason = "" if denied is None else f" ({denied.reason.value.replace('_', ' ')})"
            subject = names.get(claim.subject_id, claim.subject_id)
            return Constraint(
                "do_not_assert",
                f"Do not introduce facts about {subject}: it is not this speaker's to tell"
                f"{reason}.",
                cites,
            )
        rendered = render_proposition(claim, registry, names)
        return Constraint("do_not_assert", f"Do not assert that {rendered}.", cites)

    # -- the run --------------------------------------------------------------------------

    async def run(
        self, beat: TurnBeat, violations: Sequence[CheckedClaim], budget: TurnBudget
    ) -> RepairResult:
        """Correct ``beat`` (see the module docstring)."""
        context = await self.context(beat, violations)
        sketches = list(self.sketches_of.get(beat.beat, ()))
        rejected: list[RejectedRender] = []
        degradation: list[tuple[str, str]] = []
        attempts = 0
        violating = {i for v in violations if (i := _declared_index(v)) is not None}

        def result(
            rung: int,
            accepted: TurnBeat | None,
            recheck: Recheck | None = None,
            chosen: Sequence[PropositionSketch] = (),
        ) -> RepairResult:
            original = RejectedRender(
                beat=beat.beat,
                attempt=0,
                rung=rung,
                text=beat.text,
                constraints=context.constraints,
                verdicts=_verdicts(violations),
                detected_claims=_detected(violations),
            )
            repair_class = classify(rung, context.size, self.cascade_threshold)
            final = (
                None
                if accepted is None
                else replace(accepted, repaired_from=(rung, repair_class.value))
            )
            log.debug(
                "repair: beat %d accepted at rung %d (%s), %d rejected",
                beat.beat,
                rung,
                repair_class.value,
                len(rejected) + 1,
            )
            return RepairResult(
                beat=final,
                rung=rung,
                attempts=attempts,
                rejected=[original, *rejected],
                repair_class=repair_class,
                degradation=degradation,
                remaining_violations=[],
                original=beat,
                violations=list(violations),
                context=context,
                recheck=recheck,
                sketches=list(chosen),
            )

        async def attempt(
            rung: int,
            candidate: TurnBeat,
            chosen: Sequence[PropositionSketch],
            *,
            backstop: bool,
            number: int = 0,
            hash_: str | None = None,
        ) -> Recheck | None:
            def reject(claims: Sequence[CheckedClaim] = ()) -> None:
                rejected.append(
                    RejectedRender(
                        beat=beat.beat,
                        attempt=number,
                        rung=rung,
                        text=candidate.text,
                        constraints=context.constraints,
                        verdicts=_verdicts(claims),
                        detected_claims=_detected(claims),
                        prompt_hash=hash_,
                    )
                )

            if not await self.output_ok(candidate.text):
                reject()
                return None
            found = await self.recheck(candidate, chosen, backstop, rung)
            if found.violations:
                reject(found.violations)
                return None
            return found

        # Rung 1: narrowing.
        if beat.kind in DETERMINISTIC_KINDS:
            spans = self._spans(beat, violations, sketches)
            text = None if spans is None else narrow(beat.text, spans)
            if text is not None:
                kept = [
                    s
                    for n, s in enumerate(sketches)
                    if n not in violating and locate(text, [s.object, s.subject]) is not None
                ]
                found = await attempt(
                    NARROWING, replace(beat, text=text, claims=()), kept, backstop=False
                )
                if found is not None:
                    return result(NARROWING, found.beat, found, kept)
        # Rung 2: substitution.
        if beat.kind in DETERMINISTIC_KINDS:
            swapped = self._substitution(beat, violations, sketches, context)
            if swapped is not None:
                text, chosen = swapped
                found = await attempt(
                    SUBSTITUTION, replace(beat, text=text, claims=()), chosen, backstop=False
                )
                if found is not None:
                    return result(SUBSTITUTION, found.beat, found, chosen)
        # Rung 3: regeneration, while the turn can reserve it and has attempts left.
        inputs = self.inputs_of(beat)
        while inputs is not None:
            if self.attempts_used >= self.max_attempts:
                degradation.append((REPAIR_DETERMINISTIC, "max_attempts"))
                break
            cost = self.regeneration_cost
            held = budget.held
            # Optional work (#156): never a reserved call, and only while it could start.
            if not budget.try_optional(BUDGET_REPAIR, cost):
                degradation.append((REPAIR_DETERMINISTIC, "budget"))
                break
            self.attempts_used += 1
            attempts += 1
            try:
                made = await self._regenerate(beat, inputs, context, budget)
                if made is None:
                    continue
                candidate, chosen, hash_ = made
                found = await attempt(
                    REGENERATION, candidate, chosen, backstop=True, number=attempts, hash_=hash_
                )
            finally:
                budget.release_to(min(budget.held, held))
            if found is not None:
                if EXTRACTION_DEGRADED in found.degradation:
                    degradation.append((REPAIR_UNEXTRACTED, "budget"))
                return result(REGENERATION, found.beat, found, chosen)
        # Rung 4: the safe fallback (a think or latent beat is dropped).
        degradation.append((SAFE_FALLBACK, REPAIR))
        if beat.kind not in DETERMINISTIC_KINDS:
            return result(FALLBACK, None)
        line = await self._fallback_line()
        return result(FALLBACK, replace(beat, text=line, claims=(), mentions=()))

    # -- the rungs ------------------------------------------------------------------------

    def _spans(
        self,
        beat: TurnBeat,
        violations: Sequence[CheckedClaim],
        sketches: Sequence[PropositionSketch],
    ) -> list[tuple[int, int]] | None:
        """Where each violation sits in the beat's text; ``None`` when one cannot be found."""
        found: list[tuple[int, int]] = []
        for v in violations:
            _, start, end = v.claim.span
            whole = (start, end) == (0, len(beat.text))
            if v.claim.layer != "declared" and 0 <= start < end <= len(beat.text) and not whole:
                found.append((start, end))
                continue
            surfaces: list[str | None] = []
            index = _declared_index(v)
            if index is not None and index < len(sketches):
                surfaces += [sketches[index].object, sketches[index].subject]
            if v.claim.sketch is not None:
                surfaces += [v.claim.sketch.object_text, v.claim.sketch.subject_text]
            span = locate(beat.text, surfaces)
            if span is None:
                return None
            found.append(span)
        return found

    def _substitution(
        self,
        beat: TurnBeat,
        violations: Sequence[CheckedClaim],
        sketches: Sequence[PropositionSketch],
        context: Context,
    ) -> tuple[str, list[PropositionSketch]] | None:
        """Rung 2's text and declared sketches, or ``None`` when it does not apply."""
        if context.registry is None:
            return None
        text = beat.text
        chosen = list(sketches)
        by_id = {f.id: f for f in context.contradicted}
        for v in violations:
            result = v.result
            claim = v.claim.proposition
            if result is None or claim is None or result.kind is not VerdictKind.CONTRADICTED:
                return None
            if result.source != "deterministic" or not result.verdict.rules <= {
                Rule.FUNCTIONAL_KEY
            }:
                return None
            predicate = context.registry.get(claim.predicate_id)
            if predicate is None or predicate.cardinality is not Cardinality.FUNCTIONAL:
                return None
            cited = _fact_ids(v)
            if len(cited) != 1 or cited[0] not in by_id:
                return None
            replacement = _surface(by_id[cited[0]].proposition, context.names)
            index = _declared_index(v)
            surfaces: list[str] = []
            if index is not None and index < len(chosen) and chosen[index].object:
                surfaces.append(str(chosen[index].object))
            if v.claim.sketch is not None and v.claim.sketch.object_text:
                surfaces.append(v.claim.sketch.object_text)
            surfaces.append(_surface(claim, context.names))
            swapped = None
            for surface in dict.fromkeys(s for s in surfaces if s):
                swapped = substitute(text, surface, replacement)
                if swapped is not None:
                    break
            if swapped is None or replacement is None:
                return None
            text = swapped
            if index is not None and index < len(chosen):
                chosen[index] = chosen[index].model_copy(update={"object": replacement})
        return text, chosen

    async def _regenerate(
        self,
        beat: TurnBeat,
        base: GenerationInputs,
        context: Context,
        budget: TurnBudget,
    ) -> tuple[TurnBeat, list[PropositionSketch], str] | None:
        """One ``regeneration`` call: the candidate beat, its declared sketches and the prompt
        hash, or ``None`` when the model gave nothing usable."""
        registry = context.registry
        assert registry is not None  # noqa: S101 - context() loads it

        def line(fact: Fact) -> str:
            p = fact.proposition
            rendered = render_proposition(p, registry, context.names)
            return f"{rendered} [{render_time(p, registry, context.frame)}]"[:1_000]

        contradicted = {f.id for f in context.contradicted}
        inputs = RegenerationInputs.model_validate(
            {
                **base.model_dump(mode="json"),
                "rejected_text": beat.text,
                "beat_kind": beat.kind,
                "constraints": [
                    RepairConstraint(kind=c.kind, text=c.text[:1_000], cites=list(c.cites)[:50])
                    for c in context.constraints
                ],
                "contradicted_facts": [line(f) for f in context.contradicted][
                    :REGENERATION_MAX_RECORDS
                ],
                "neighborhood": [line(f) for f in context.neighborhood if f.id not in contradicted][
                    :REGENERATION_MAX_RECORDS
                ],
            }
        )
        _, _, messages = self.models.prepare(REGENERATION_PURPOSE.name, inputs)
        hash_ = prompt_hash(messages)
        try:
            found = await self.models.structured_call(
                REGENERATION_PURPOSE.name,
                RegeneratedBeat,
                inputs,
                params=self.params,
                # The deterministic rungs (1 and 2) ran before this call.
                deterministic_first=lambda: None,
                budget=budget,
            )
        except ModelError:
            return None
        if isinstance(found, Uncertain):
            return None
        intent = beat.intent
        if beat.kind == SAY and found.intent is not None:
            intent = found.intent
        candidate = replace(beat, text=found.text.strip(), intent=intent, claims=(), mentions=())
        return candidate, list(found.propositions), hash_

    async def _fallback_line(self) -> str:
        """The first stored fallback line that passes the output policies (the last resort
        always stands)."""
        for source, text in self.fallback.candidates():
            if source == "last_resort" or await self.output_ok(text):
                return text
        return LAST_RESORT.replace(NAME_PLACEHOLDER, self.fallback.name)  # pragma: no cover


def _surface(p: Proposition, names: Mapping[str, str]) -> str:
    """How a proposition's object reads in prose: the entity's canonical name, the set's names
    joined, or the literal's text."""
    if p.object_id is not None:
        return names.get(p.object_id, p.object_id)
    if p.object_set:
        found = [names.get(o, o) for o in p.object_set]
        return found[0] if len(found) == 1 else ", ".join(found[:-1]) + " and " + found[-1]
    if p.literal is not None:
        value = p.literal.value
        text = str(value).lower() if isinstance(value, bool) else str(value)
        return f"{text} {p.literal.unit}" if p.literal.unit else text
    return ""


async def _names(
    conn: AsyncConnection, story_id: str, props: Sequence[Proposition]
) -> dict[str, str]:
    ids: list[str] = []
    for p in props:
        ids.append(p.subject_id)
        if p.object_id is not None:
            ids.append(p.object_id)
        ids += list(p.object_set or ())
        ids += [v for v in p.qualifiers.values() if isinstance(v, str)]
    if not ids:
        return {}
    rows = await conn.execute(
        select(entities.c.id, entities.c.canonical_name).where(
            entities.c.story_id == story_id, entities.c.id.in_(list(dict.fromkeys(ids)))
        )
    )
    return {str(r.id): str(r.canonical_name) for r in rows}


__all__ = [
    "DETERMINISTIC_KINDS",
    "REPAIR_DETERMINISTIC",
    "REPAIR_UNEXTRACTED",
    "SAFE_FALLBACK",
    "Context",
    "InputsOf",
    "OutputCheck",
    "Recheck",
    "Rechecker",
    "RegeneratedBeat",
    "RejectedRender",
    "RepairLadder",
    "RepairResult",
]
