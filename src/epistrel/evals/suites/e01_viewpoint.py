"""E-01: entailment accuracy, M5's part (docs/04 §8; FR-VERIFY-2,5; NFR-QUAL-2 gates from M9,
ADR-164; #179, completing #166's AC5).

Each case is a character's first-person ``say`` claim checked by #142's ``Verifier`` as a
structured turn checks one under per-character partitions (#166): against canon (the
**objective** relation, ``RecordsSource`` over the case's canon) and against the speaker's own
belief partition, given inline (``InlineBeliefs``: the case's beliefs, then the canon the
speaker may read when they say nothing), with the beat's declared intent. The labeled set
(``evals/sets/entailment_viewpoint/cases.jsonl``) uses E-01's vocabulary
(``e01_entailment.Prop``) and labels each claim with the classification #166 owes it (``lie``,
``sincere_error`` or ``sincere``), its belief relation and its objective verdict. None is
repaired: a false contradiction here would "correct" an intentional lie or a false belief.

- ``nfr_qual_2.viewpoint.false_contradiction`` (rate, ≤ 2 %): the share of the claims the check
  repaired (``needs_repair``), over every case (none is labeled for repair).
- ``nfr_qual_2.viewpoint.false_support`` (rate, ≤ 1 %): over the claims whose objective label is
  not ``supported``, the share that ended ``supported``.
- ``e01_viewpoint.verdict_accuracy`` (report-only): the classification, the belief relation and
  the objective verdict all equal the label.
- ``e01_viewpoint.classification_accuracy`` (report-only): the classification alone.

The canon side of every case is settled deterministically (no residual), so the suite makes no
model call: its stub run and a real model's agree, and CI gates on it. NFR-QUAL-2's floors gate
from M9 on the reference model (ADR-164); the viewpoint rows are reported until then.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.membership import Position as NarrativePosition
from epistrel.core.propositions.model import Proposition
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX
from epistrel.core.verify.rules import Holder, Record, Verdict, VerdictKind, check
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.evals.suites.e01_entailment import (
    ENTITY_NAMES,
    FRAME,
    REGISTRY,
    Prop,
    entity_id,
    proposition,
)
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.verify.verifier import (
    BeliefSource,
    Licence,
    RecordsSource,
    Verifier,
    VerifyConfig,
    VerifyResult,
)

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.models.access import ModelAccessLayer

FALSE_CONTRADICTION = "nfr_qual_2.viewpoint.false_contradiction"
FALSE_SUPPORT = "nfr_qual_2.viewpoint.false_support"
ACCURACY = "e01_viewpoint.verdict_accuracy"
CLASSIFICATION_ACCURACY = "e01_viewpoint.classification_accuracy"
POSITION = (1, 1)
"""Every claim is the first beat of turn 1; its beliefs are held from before it."""
CALL_DEADLINE_S = 300.0

Classification = Literal["lie", "sincere_error", "sincere"]
Objective = Literal["supported", "contradicted", "novel"]
Relation = Literal["consistent", "inconsistent", "unknown"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Held(_Model):
    """One belief of the speaker's partition, kept ``secret`` or not (FR-SCN-8's disclosure)."""

    prop: Prop
    secret: bool = False


class ViewpointInputs(_Model):
    speaker: str
    claim: Prop
    canon: list[Prop] = Field(default_factory=list)
    beliefs: list[Held] = Field(min_length=1)
    intent: Literal["sincere", "deceptive", "unspecified"]
    note: str = Field(min_length=1, max_length=300)
    template: str


class ViewpointLabel(_Model):
    category: Classification
    objective: Objective
    belief_relation: Relation


class ViewpointCase(CaseModel):
    family: Literal["lie", "false_belief", "consistent"]
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: ViewpointInputs
    expected: ViewpointLabel


def _id(case_id: str, n: int, kind: str) -> str:
    digest = int(hashlib.sha256(f"{case_id}:{n}".encode()).hexdigest()[:15], 16)
    return f"01JE5{kind}{digest:020d}"[:26]


class InlineBeliefs(BeliefSource):
    """The speaker's belief partition given inline (no database): ``check`` over the case's
    beliefs, then over the canon the speaker may read when the beliefs settle nothing (#159's
    ``belief(speaker)`` holder); ``hidden`` over canon; a belief is ``secret`` as the case
    marks it. Nothing is licensed or written (no turn history exists)."""

    def __init__(self, beliefs: Sequence[tuple[Record, bool]], canon: Sequence[Record]) -> None:
        self.beliefs = [r for r, _ in beliefs]
        self.secrets = {r.belief_id for r, secret in beliefs if secret}
        self.canon = list(canon)
        self.variant = 0
        self.staged = {}

    async def registry(self) -> Any:
        return REGISTRY

    async def check(
        self, claim: Proposition, believer: str, at: NarrativePosition
    ) -> Verdict | None:
        holder = Holder.belief(believer)
        found = check(claim, self.beliefs, registry=REGISTRY, holder=holder, position=at)
        if found.kind in (VerdictKind.NOVEL, VerdictKind.UNCERTAIN) and self.canon:
            readable = check(claim, self.canon, registry=REGISTRY, holder=holder, position=at)
            if readable.kind in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED):
                return readable
        return found

    async def hidden(
        self, claim: Proposition, believer: str, at: NarrativePosition
    ) -> VerdictKind | None:
        found = check(claim, self.canon, registry=REGISTRY, position=at)
        if found.kind in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED):
            return found.kind
        return None

    async def own_age(self, claim: Proposition, believer: str) -> bool:
        return False

    async def strongest(self, belief_ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(belief_ids))

    async def secret(self, belief_ids: Sequence[str]) -> bool:
        return any(i in self.secrets for i in belief_ids)

    async def licence(
        self,
        claim: Proposition,
        believer: str,
        at: NarrativePosition,
        cites: Sequence[str] = (),
    ) -> Licence | None:
        return None


def _records(case: ViewpointCase) -> tuple[list[Record], list[tuple[Record, bool]]]:
    canon = [
        Record(proposition(p), _id(case.id, n, "P"), _id(case.id, n, "F"))
        for n, p in enumerate(case.inputs.canon)
    ]
    beliefs = [
        (
            Record(
                proposition(h.prop),
                _id(case.id, 100 + n, "P"),
                belief_id=_id(case.id, 100 + n, "B"),
            ),
            h.secret,
        )
        for n, h in enumerate(case.inputs.beliefs)
    ]
    return canon, beliefs


def score(case: ViewpointCase, final: str, classification: str | None, relation: str | None,
          repaired: bool) -> dict[str, bool | None]:  # fmt: skip
    """One case's observations of every metric (``None``: outside the denominator)."""
    label = case.expected
    return {
        FALSE_CONTRADICTION: not repaired,
        FALSE_SUPPORT: None if label.objective == "supported" else final != "supported",
        ACCURACY: (classification, relation, final)
        == (label.category, label.belief_relation, label.objective),
        CLASSIFICATION_ACCURACY: classification == label.category,
    }


async def check_claim(
    case: ViewpointCase, models: ModelAccessLayer | None, *, partitions: bool = True
) -> VerifyResult:
    """``case``'s claim through ``Verifier.verify`` as its speaker's first-person ``say`` (the
    module docstring); ``partitions=False`` checks it as a story with shared character
    knowledge would (no belief dimension: E-01 M5's complement)."""
    canon, beliefs = _records(case)
    source = RecordsSource(canon, REGISTRY, entity_names=ENTITY_NAMES, frame=FRAME)
    verifier = Verifier(
        source,
        VerifyConfig(level="entailment"),
        models=models,
        budget=TurnBudget(deadline_s=CALL_DEADLINE_S),
        beliefs=InlineBeliefs(beliefs, canon) if partitions else None,
    )
    speaker = entity_id(case.inputs.speaker)
    result = await verifier.verify(
        proposition(case.inputs.claim),
        f"{CHARACTER_VIEWPOINT_PREFIX}{speaker}",
        POSITION,
        believer=speaker,
        intent=case.inputs.intent,
    )
    if result is None:  # pragma: no cover - level entailment is never off
        raise RuntimeError("verification is off")
    return result


@register
class E01ViewpointSuite(Suite):
    name = "e01_viewpoint"
    title = "E-01 (M5): viewpoint-aware entailment, lies and false beliefs"
    set_path = "entailment_viewpoint/cases.jsonl"
    min_cases = 300
    ci_gate = True
    case_model = ViewpointCase
    metrics = (
        Metric(
            FALSE_CONTRADICTION,
            Rule.rate,
            0.02,
            Direction.at_most,
            "a legitimate lie, false belief or true belief was repaired as a contradiction",
        ),
        Metric(
            FALSE_SUPPORT,
            Rule.rate,
            0.01,
            Direction.at_most,
            "a claim canon does not support ended supported",
        ),
        Metric(
            ACCURACY,
            Rule.informational,
            description="classification, belief relation and objective verdict equal the label",
        ),
        Metric(
            CLASSIFICATION_ACCURACY,
            Rule.informational,
            description="the classification (lie, sincere error, sincere) equals the label",
        ),
    )
    reading_notes = (
        "E-01 (M5) checks a character's own claim twice, against canon and against its belief "
        "partition, with the declared intent. Every case stands as said (a lie, a sincere "
        "error or a true belief): a repair is a false contradiction. NFR-QUAL-2's floors gate "
        "these rows from M9 (ADR-164); the canon side is deterministic, so no model call is "
        "made.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ViewpointCase)  # noqa: S101 - load() validated it as case_model
        result = await check_claim(case, ctx.models)
        speech = result.speech
        classification = speech.classification if speech is not None else None
        relation = speech.decision.belief_relation if speech is not None else None
        final = result.kind.value
        repaired = result.needs_repair
        return Outcome(
            score(case, final, classification, relation, repaired),
            output=f"{final}/{classification}/{relation}",
            detail={
                "classification": classification,
                "belief_relation": relation,
                "objective": final,
                "repaired": repaired,
                "intent_corrected": speech.decision.intent_corrected if speech else None,
                "intent_inferred": speech.inferred if speech else None,
            },
        )


__all__ = [
    "ACCURACY",
    "CLASSIFICATION_ACCURACY",
    "FALSE_CONTRADICTION",
    "FALSE_SUPPORT",
    "E01ViewpointSuite",
    "InlineBeliefs",
    "ViewpointCase",
    "check_claim",
    "score",
]
