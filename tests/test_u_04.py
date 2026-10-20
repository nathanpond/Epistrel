"""U-04 (M4): verification set-logic over normalized propositions, deterministic first (docs/04
§3; #142, #144). Every U-04 (M4) part lives in this file (traceability counts a test ID per
milestone in one file).

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
- ``test_u_04__canonize`` (#144): a permitted ``novel`` verdict canonizes (FR-VERIFY-4). Over
  records passed in, Bobby's own novel claim is permitted and becomes a canon request with his
  provenance (``render``), its beat's visibility (``objective``; a ``latent`` beat's scope) and
  an anchor (his most recent fact with the same subject and predicate); a ``think`` claim and
  a ``say`` claim about someone else's texture never do, and neither does a denied claim
  (complement). Against PostgreSQL, ``canonize_all`` writes the requests through
  ``commit_fact`` in a turn's commit step: provenance, visibility and the anchor are recorded,
  identical claims are written once, and a place first named in the claim becomes a ``place``
  entity then.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon.canonize import CanonRequest, NewEntity, canonize_all
from epistrel.core.canon.facts import Fact, Visibility
from epistrel.core.enums import RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.propositions.model import (
    DEFAULT_EPOCH,
    Granularity,
    Instant,
    Interval,
    Proposition,
)
from epistrel.core.tables import entities
from epistrel.core.verify.normalize import Unnormalizable
from epistrel.core.verify.permission import Asserter, Basis, Denied, EntityInfo, Permitted, Reason
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
    TIME_UNDECIDABLE,
    ClaimRef,
    RecordsSource,
    TimeFrame,
    Verifier,
    VerifyConfig,
    VerifyResult,
    render_time,
)
from epistrel.orchestrator.verify.yields import flipped
from tests import canon_support as cs
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
        if reason in TIME_UNDECIDABLE:
            # #460: the bounds leave it open in the rules, and no model answer can close it.
            assert (found.kind, found.source, found.escalated, found.disposition) == (
                VerdictKind.UNCERTAIN,
                "deterministic",
                True,
                "repair",
            ), reason
        else:
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
        sent = " ".join(m.content for m in call.messages)
        assert "Ann" not in sent
        assert "T+" not in sent, reason
        (record,) = v.escalations
        assert (record.gate_reason, record.called, record.model_verdict) == (
            reason.value,
            True,
            "contradicted",
        )
        assert record.tentative_verdict == "uncertain"
        assert record.final_kind == found.kind.value
        assert v.report()["escalated"] == 1

    # #460 (FR-PROP-10, FR-VERIFY-8): over approximate bounds, a confident model answer either
    # way takes the safe disposition, and the escalation is recorded unflipped. The model reads
    # the bounds as ranges ("sometime between"), not as story-minute offsets.
    claim, canon, _ = cases[VerdictReason.APPROXIMATE_BOUNDS]
    for verdict in ("supported", "contradicted"):
        mal = models()
        mal.stub.script("entailment", [answer(verdict, 0.99)])
        v = verifier(mal, canon)
        found = await _verify(v, claim)
        assert (found.kind, found.disposition, found.needs_repair) == (
            VerdictKind.UNCERTAIN,
            "repair",
            True,
        ), verdict
        assert (found.model_verdict, found.model_confidence) == (verdict, 0.99)
        (record,) = v.escalations
        assert flipped(record) is False, verdict
        (call,) = mal.stub.calls
        sent = " ".join(m.content for m in call.messages)
        assert (
            "[from sometime between 1 hour 35 minutes after the story's start and 1 hour 38 "
            "minutes after the story's start until sometime between 1 hour 50 minutes after the "
            "story's start and 2 hours after the story's start]"
        ) in sent
        assert (
            "(from sometime between the story's start and 10 minutes after the story's start "
            "until sometime between 1 hour 30 minutes after the story's start and 1 hour 40 "
            "minutes after the story's start)"
        ) in sent

    # On a story with a calendar anchor the same renderer prints dates (E-01's "about 2008-2012
    # to about 2022-2024"), an instant at its own precision, and an open side as "from"/"until".
    calendar = TimeFrame(epoch=DEFAULT_EPOCH)

    def ohio(valid: Interval | Instant) -> Proposition:
        return vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, valid)

    def y(year: int) -> int:
        return vs.minute(f"{year}-01-01T00:00")

    lived = ohio(vs.approx((y(2008), y(2012)), (y(2022), y(2024))))
    assert render_time(lived, vs.REGISTRY, calendar) == (
        "from sometime between 2008-01-01 and 2012-01-01 "
        "until sometime between 2022-01-01 and 2024-01-01"
    )
    exact = ohio(vs.years(2010, 2014))
    assert render_time(exact, vs.REGISTRY, calendar) == "from 2010-01-01 until 2014-01-01"
    opened = ohio(Interval.of(vs.minute("2010-03-04T09:15"), None))
    assert render_time(opened, vs.REGISTRY, calendar) == "from 2010-03-04 09:15 on"
    assert render_time(ohio(Interval.of(None, y(2014))), vs.REGISTRY, calendar) == (
        "until 2014-01-01"
    )
    hour = ohio(Instant.of(vs.minute("2012-05-10T09:00"), Granularity.HOUR))
    assert render_time(hour, vs.REGISTRY, calendar) == "at 2012-05-10 09:00 (to the hour)"
    on_day = ohio(Instant.of(vs.minute("2012-05-10T00:00"), Granularity.DAY))
    assert render_time(on_day, vs.REGISTRY, calendar) == "on 2012-05-10"
    in_year = ohio(Instant.of(y(2012), Granularity.YEAR))
    assert render_time(in_year, vs.REGISTRY, calendar) == "in 2012"
    # Unanchored (FR-CLOCK-3: never a date the story has not set): offsets from its start.
    start = vs.minute("2012-05-10T00:00")
    assert render_time(on_day, vs.REGISTRY, TimeFrame(minute_zero=start)) == (
        "at the story's start (to the day)"
    )
    assert render_time(exact, vs.REGISTRY, TimeFrame(minute_zero=y(2010) + 90)) == (
        "from 1 hour 30 minutes before the story's start "
        "until 1460 days 22 hours 30 minutes after the story's start"
    )

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


@pytest.mark.integration
@pytest.mark.part("M4", "FR-VERIFY-4")
async def test_u_04__canonize(engine: AsyncEngine) -> None:
    """A permitted ``novel`` verdict canonizes with provenance, visibility and an anchor (see
    the module docstring)."""
    bobby = Asserter("character", vs.BOBBY)
    known = vs.records(KITCHEN, vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.GARDEN, vs.span(100, 200)))
    source = RecordsSource(
        known,
        vs.REGISTRY,
        entity_names=NAMES,
        entities={
            vs.BOBBY: EntityInfo(vs.BOBBY, "person", vs.BOBBY),
            vs.ANN: EntityInfo(vs.ANN, "person", vs.ANN),
            vs.KITCHEN: EntityInfo(vs.KITCHEN, "place"),
        },
        placed=frozenset({vs.BOBBY}),
    )
    v = Verifier(source, VerifyConfig(level="extraction"), models=None, budget=TurnBudget())
    later = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(300, 400))
    own = await v.verify(later, ref=ClaimRef("c:1", beat=1, channel="do"), asserter=bobby)
    assert own is not None
    assert (own.kind, own.permission) == (VerdictKind.NOVEL, Permitted(Basis.OWN))
    # The anchor: his same subject-and-predicate fact that starts last (the garden, 100-200).
    assert own.anchored_to == known[1].fact_id
    hidden = await v.verify(
        later,
        ref=ClaimRef("c:2", beat=2, channel="latent", scope=(vs.BOBBY,)),
        asserter=bobby,
    )
    thought = await v.verify(later, ref=ClaimRef("c:3", beat=3, channel="think"), asserter=bobby)
    texture = vs.prop(vs.KITCHEN, vs.LOCATED_IN, vs.HOUSE)
    said = await v.verify(texture, ref=ClaimRef("c:4", beat=4, channel="say"), asserter=bobby)
    other = await v.verify(
        vs.prop(vs.ANN, vs.LOCATED_AT, vs.GARDEN, vs.span(0, 10)),
        ref=ClaimRef("c:5", beat=5, channel="do"),
        asserter=bobby,
    )
    assert hidden is not None
    assert thought is not None
    assert said is not None
    assert said.permission == Permitted(Basis.TEXTURE)
    assert other is not None
    assert other.permission == Denied(Reason.OTHER_CHARACTER_SECRET)
    assert other.repair_tag == "unpermitted"
    unasked = await v.verify(KITCHEN, ref=ClaimRef("c:6", beat=6, channel="do"), asserter=bobby)
    assert unasked is not None
    assert (unasked.kind, unasked.permission) == (VerdictKind.SUPPORTED, None)
    requests = v.canon_requests()
    # Only the permitted novel claims of canonizing channels: not the think, the say about
    # texture, the denied claim, or the supported one (the complement).
    assert [r.claim_id for r in requests] == ["c:1", "c:2"]
    first, second = requests
    assert (first.provenance, first.visibility, first.anchored_to, first.beat) == (
        RecordProvenance.RENDER,
        Visibility(),
        known[1].fact_id,
        1,
    )
    assert second.visibility == Visibility(scope="characters", characters=(vs.BOBBY,))

    # Against PostgreSQL: canonize writes through commit_fact in a turn's commit step.
    w = await cs.world(engine)
    story = w.story_id
    anchor = await cs.commit(engine, story, cs.at(w.people["Bobby"], w.places["kitchen"], 0, 100))
    shed = "01JA00000000000000000000SH"
    claimed = cs.at(w.people["Bobby"], w.places["garden"], 100, 200)
    new_place = Proposition(
        subject_id=w.people["Bobby"],
        predicate_id=vs.LOCATED_AT,
        object_id=shed,
        valid_time=Interval.of(200, 300),
    )
    turn = [
        CanonRequest(claimed, RecordProvenance.RENDER, anchored_to=anchor.id, beat=1),
        CanonRequest(claimed, RecordProvenance.RENDER, anchored_to=anchor.id, beat=2),
        CanonRequest(
            new_place,
            RecordProvenance.DIRECTOR,
            visibility=Visibility(scope="characters", characters=(w.characters["Bobby"],)),
            introduces=(NewEntity(shed, "the shed"),),
        ),
    ]

    async def write(conn: AsyncConnection, op: Operation) -> list[Fact]:
        return await canonize_all(conn, story, turn, operation=op)

    op, written = await cs.run(engine, story, write)
    garden, in_shed = written  # the identical second request was written once
    assert (garden.provenance, garden.anchored_to, garden.beat, garden.operation_id) == (
        RecordProvenance.RENDER,
        anchor.id,
        1,
        op.operation_id,
    )
    assert (in_shed.provenance, in_shed.visibility.scope) == (
        RecordProvenance.DIRECTOR,
        "characters",
    )
    rows = await cs.fact_rows(engine, story)
    assert rows[garden.id].anchored_to == anchor.id
    assert rows[garden.id].provenance == "render"
    assert len(rows) == 3
    async with engine.connect() as conn:
        kind = await conn.scalar(select(entities.c.kind).where(entities.c.id == shed))
    assert kind == "place"
