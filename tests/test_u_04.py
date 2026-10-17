"""U-04 (M4): verification set-logic over normalized propositions, deterministic first (docs/04
§3; #142). Every U-04 (M4) part lives in this file (traceability counts a test ID per milestone
in one file); #144's ``test_u_04__canonize`` joins it.

The verifier runs over records passed in (``RecordsSource``: #140's pure ``check``) and a stub
Model Access Layer with no database, so the model calls are counted exactly:

- ``test_u_04``: ``supported``, ``contradicted`` and ``novel`` come from the rules with no model
  call, and so does ``uncertain`` when the profile has no entailment escalation; a partly
  covered claim settles from its remainders. Complement: with every ``uncertain`` verdict
  escalating, a conclusive verdict still makes zero calls.
- ``test_u_04__escalation``: each gate reason, (a) an unresolved predicate, (b) world knowledge
  and weak semantics, (c) approximate bounds, escalates at the launch threshold, 1.0. Coverage
  and granularity never do (complement), and neither do the reasons that never escalate. A
  low-confidence or unparseable answer takes the safe disposition, and so does every residual
  below ``verification.level = entailment``, with zero calls.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.verify.normalize import Unnormalizable
from epistrel.core.verify.rules import Options, Rule, VerdictKind, VerdictReason
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.verify.gate import (
    ESCALATES,
    NEVER_ESCALATES,
    RESOLVED_IN_CORE,
    FixedThresholds,
    Gate,
    PredicateShape,
)
from epistrel.orchestrator.verify.verifier import (
    RecordsSource,
    Verifier,
    VerifyConfig,
    VerifyResult,
)
from tests import verify_support as vs

NAMES = {
    vs.BOBBY: "Bobby",
    vs.ANN: "Ann",
    vs.OHIO: "Ohio",
    vs.BOSTON: "Boston",
    vs.KITCHEN: "kitchen",
    vs.GARDEN: "garden",
    vs.HOUSE: "house",
    vs.SMITH: "Smith",
    vs.SMITH_2: "Smithson",
}

HOUSE = [
    vs.prop(vs.KITCHEN, vs.LOCATED_IN, vs.HOUSE),
    vs.prop(vs.GARDEN, vs.LOCATED_IN, vs.HOUSE),
]
KITCHEN = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(0, 100))
STRAY = "01JP00000000000000000000ZZ"


models = vs.stub_models


def verifier(
    mal: ModelAccessLayer | None,
    canon: Sequence[Proposition],
    *,
    level: str = "entailment",
    gate: Gate | None = None,
    strict_weak: bool = False,
    **config: Any,
) -> Verifier:
    source = RecordsSource(
        vs.records(*canon),
        vs.REGISTRY,
        options=Options(strict_weak=strict_weak),
        entity_names=NAMES,
    )
    return Verifier(
        source,
        VerifyConfig(level=level, **config),
        models=mal,
        budget=TurnBudget(),
        gate=gate,
    )


def answer(verdict: str, confidence: float) -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence})


def calls(mal: ModelAccessLayer) -> int:
    return sum(1 for c in mal.stub.calls if c.purpose == "entailment")


async def _verify(v: Verifier, claim: Proposition) -> VerifyResult:
    found = await v.verify(claim)
    assert found is not None
    return found


@pytest.mark.part("M4", "FR-PROP-1; FR-VERIFY-1,3")
async def test_u_04() -> None:
    """The three-way verdict with evidence, plus ``uncertain``, from the rules alone: Bobby in
    the kitchen 0-100 supports 10-20 (rule ``coverage``, citing the record), contradicts the
    garden at 50-60 (``functional_key``, a repair), and says nothing of Ann (``novel``). A
    50-150 claim is partly covered: the covered part is supported, and its remainder is novel,
    so the claim is ``novel`` with no escalation. Under ``verification.level = extraction``
    Boston (world knowledge) stays ``uncertain`` with the safe disposition. Complement: at
    ``entailment``, with every ``uncertain`` verdict escalating, none of the conclusive verdicts
    calls the model."""
    mal = models()
    v = verifier(mal, [KITCHEN, *HOUSE])

    supported = await _verify(v, vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(10, 20)))
    assert (supported.kind, supported.source, supported.rule) == (
        VerdictKind.SUPPORTED,
        "deterministic",
        Rule.COVERAGE.value,
    )
    assert supported.evidence_ids()
    assert supported.objective_relation == "true"

    contradicted = await _verify(v, vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.GARDEN, vs.span(50, 60)))
    assert (contradicted.kind, contradicted.rule) == (
        VerdictKind.CONTRADICTED,
        Rule.FUNCTIONAL_KEY.value,
    )
    assert contradicted.needs_repair
    assert contradicted.objective_relation == "false"

    novel = await _verify(v, vs.prop(vs.ANN, vs.LOCATED_AT, vs.GARDEN, vs.span(0, 10)))
    assert (novel.kind, novel.evidence_ids(), novel.disposition) == (VerdictKind.NOVEL, [], None)

    partial = await _verify(v, vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(50, 150)))
    assert (partial.verdict.kind, partial.verdict.reason) == (
        VerdictKind.UNCERTAIN,
        VerdictReason.PARTIAL_COVERAGE,
    )
    assert partial.verdict.covered == ((50, 100),)
    assert (partial.kind, partial.escalated) == (VerdictKind.NOVEL, False)
    assert [s.kind for s in partial.sub_results] == [VerdictKind.NOVEL]

    # The complement: four conclusive verdicts, every one decided with no model call.
    assert calls(mal) == 0
    assert v.report()["claims"] == 4
    assert v.report()["deterministic"] == 4
    assert v.report()["escalated"] == 0
    assert v.escalations == []

    boston = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.BOSTON, vs.years(2011, 2012))
    ohio = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.years(2010, 2014))
    declared = verifier(mal, [ohio], level="extraction")
    undecided = await _verify(declared, boston)
    assert (undecided.kind, undecided.reason, undecided.disposition) == (
        VerdictKind.UNCERTAIN,
        VerdictReason.WORLD_KNOWLEDGE,
        "repair",
    )
    assert undecided.objective_relation == "unresolved"
    assert calls(mal) == 0

    # Verification off (a Lite story): inert, nothing decided.
    assert await verifier(mal, [KITCHEN], level="off").verify(KITCHEN) is None


def _reasons() -> dict[VerdictReason, tuple[Proposition, list[Proposition], bool]]:
    """One claim per gate reason, over records that make ``check`` answer it (and whether the
    check needs ``strict_weak``)."""
    ohio = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.years(2010, 2014))
    approximate = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.approx((0, 10), (90, 100)))
    smith = vs.prop(vs.BOBBY, vs.APPRENTICED, vs.SMITH, vs.span(0, 100))
    return {
        VerdictReason.UNRESOLVED_PREDICATE: (vs.prop(vs.BOBBY, STRAY, vs.OHIO), [], False),
        VerdictReason.WORLD_KNOWLEDGE: (
            vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.BOSTON, vs.years(2011, 2012)),
            [ohio],
            False,
        ),
        VerdictReason.WEAK_SEMANTICS: (
            vs.prop(vs.BOBBY, vs.APPRENTICED, vs.SMITH_2, vs.span(50, 60)),
            [smith],
            True,
        ),
        VerdictReason.APPROXIMATE_BOUNDS: (
            vs.prop(
                vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.approx((95, 98), (110, 120)), negated=True
            ),
            [approximate],
            False,
        ),
    }


@pytest.mark.part("M4", "FR-VERIFY-3")
async def test_u_04__escalation() -> None:
    """The gate, at the launch threshold 1.0 (``verification.entailment_gate``'s default):

    - every reason FR-VERIFY-3 names escalates: (a) ``unresolved_predicate``, (b)
      ``world_knowledge`` and ``weak_semantics``, (c) ``approximate_bounds``; the model is
      shown the claim and the holder's records, and a validated, confident answer decides it;
    - coverage and granularity never escalate (complement): a partly covered claim and a
      coarser record settle with no call, and the gate refuses both reasons outright, like the
      reasons that never escalate (an unresolved entity, an unresolved time, an argument type);
    - a low-confidence ``supported`` (FR-VERIFY-8) and an unparseable answer take the safe
      disposition (``repair`` by default), never a silent ``supported``;
    - below ``verification.level = entailment``, every residual takes the safe disposition with
      zero calls; a tightened shape below the confidence does too.
    """
    assert VerifyConfig().entailment_gate == 1.0
    cases = _reasons()
    assert set(cases) == set(ESCALATES)
    for reason, (claim, canon, strict) in cases.items():
        mal = models()
        mal.stub.script("entailment", [answer("contradicted", 0.9)])
        v = verifier(mal, canon, strict_weak=strict)
        found = await _verify(v, claim)
        assert found.verdict.reason is reason, reason
        assert found.decision is not None
        assert found.decision.threshold == 1.0
        assert (found.kind, found.source, found.escalated, found.rule) == (
            VerdictKind.CONTRADICTED,
            "entailment",
            True,
            "entailment",
        ), reason
        assert calls(mal) == 1, reason
        (call,) = mal.stub.calls
        user = call.messages[-1].content
        assert user.startswith("Claim: Bobby"), reason
        assert "Ann" not in " ".join(m.content for m in call.messages)
        (record,) = v.escalations
        assert (record.gate_reason, record.called, record.model_verdict) == (
            reason.value,
            True,
            "contradicted",
        )
        assert record.tentative_verdict == "uncertain"
        assert v.report()["escalated"] == 1

    # Complement: coverage and granularity never reach the model.
    mal = models()
    v = verifier(mal, [KITCHEN])
    partial = await _verify(v, vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(50, 150)))
    nine = Instant.of(vs.minute("2012-05-10T09:00"), Granularity.HOUR)
    day = Instant.of(vs.minute("2012-05-10T00:00"), Granularity.DAY)
    there = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, nine)
    refined = await _verify(verifier(mal, [there.model_copy(update={"valid_time": day})]), there)
    assert refined.verdict.reason is VerdictReason.GRANULARITY_REFINEMENT
    assert (partial.kind, refined.kind) == (VerdictKind.NOVEL, VerdictKind.NOVEL)
    assert not partial.escalated
    assert not refined.escalated
    assert calls(mal) == 0
    gate = Gate(1.0)
    shape = PredicateShape("core", "functional", "fluent", "affirmed")
    for reason in RESOLVED_IN_CORE | NEVER_ESCALATES:
        assert not gate.should_escalate(reason, shape, 0.0), reason
    for reason in ESCALATES:
        assert gate.should_escalate(reason, shape, 0.5), reason
    for reason in NEVER_ESCALATES:
        never = verifier(mal, []).unnormalizable(Unnormalizable(reason))
        assert never is not None
        assert (never.kind, never.disposition) == (
            VerdictKind.UNCERTAIN,
            "repair",
        )
    assert calls(mal) == 0

    # FR-VERIFY-8: a low-confidence supported and an unparseable answer take the safe
    # disposition; neither ends as supported.
    boston, canon, _ = cases[VerdictReason.WORLD_KNOWLEDGE]
    for scripted in (
        [answer("supported", 0.55)],
        ["not json", "still not", "never"],
        [answer("certainly", 0.99)] * 3,
        [answer("novel", 0.95)],
    ):
        mal = models()
        mal.stub.script("entailment", scripted)
        v = verifier(mal, canon)
        found = await _verify(v, boston)
        assert (found.kind, found.disposition, found.needs_repair) == (
            VerdictKind.UNCERTAIN,
            "repair",
            True,
        ), scripted
        assert found.escalated
        assert calls(mal) >= 1
    surfaced = verifier(models(), canon, safe_disposition="surface")
    found = await _verify(surfaced, boston)  # the stub's default: novel at confidence 0
    assert (found.kind, found.disposition, found.needs_repair) == (
        VerdictKind.UNCERTAIN,
        "surface",
        False,
    )
    assert "uncertain_surfaced" in found.degradation
    (listed,) = surfaced.uncertain_claims()
    assert listed["claim"].startswith("Bobby located_at Boston")
    assert listed["reason"] == "world_knowledge"

    # Below verification.level = entailment: the safe disposition, zero calls.
    for level in ("declared", "extraction"):
        mal = models()
        for reason, (claim, records, strict) in cases.items():
            found = await _verify(verifier(mal, records, level=level, strict_weak=strict), claim)
            assert (found.kind, found.disposition, found.escalated) == (
                VerdictKind.UNCERTAIN,
                "repair",
                False,
            ), (level, reason)
        assert calls(mal) == 0

    # A shape tightened below the deterministic confidence takes the safe disposition at once.
    mal = models()
    located = PredicateShape.of(boston, vs.REGISTRY.get(vs.LOCATED_AT))
    tight = Gate(1.0, FixedThresholds({located.key(): 0.5}))
    found = await _verify(verifier(mal, canon, gate=tight), boston)
    assert found.decision is not None
    assert found.decision.outcome == "below_gate"
    assert (found.kind, found.escalated, calls(mal)) == (VerdictKind.UNCERTAIN, False, 0)
