"""The verifier over real canon (#142): validation of the entailment result and the safe
disposition (FR-VERIFY-3,8), the records the model is shown, the profile's dials, and the
escalation hook #155 writes from.

- On ``Structured (M4)`` (verification at ``entailment``, gate 1.0), a conclusive claim makes no
  model call, and an ``uncertain`` one is sent to the ``entailment`` purpose with the claim and
  the holder's records only. The deterministic check's reads are query keys of the turn's read
  set; the neighborhood's are retrieved rows.
- FR-VERIFY-8: an unparseable answer, an answer below ``uncertain_below`` or below
  ``verification.entailment_min_confidence``, a model ``novel``, an unreachable model and an
  exhausted budget all take the safe disposition. None of them ends as ``supported``, and the
  turn goes on.
- ``verification.safe_disposition``: ``repair`` (the default) needs a repair; ``surface`` lists
  the claim for the author in ``uncertain_claims``. Both leave an escalation record.
- Lite (verification ``off``) is inert: nothing is read and nothing is called.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.readset import StructuredReadSet
from epistrel.core.verify.rules import VerdictKind, VerdictReason
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.verify.verifier import (
    ENTAILMENT_BUDGET,
    ENTAILMENT_UNAVAILABLE,
    ClaimRef,
    EscalationRecord,
    Verifier,
    VerifyResult,
    story_verifier,
)
from tests.canon_support import World, at, commit, world
from tests.memory_support import use_structured
from tests.verify_support import stub_models

pytestmark = pytest.mark.integration


def answer(verdict: str, confidence: float) -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence})


def entailment_calls(mal: ModelAccessLayer) -> list[Any]:
    return [c for c in mal.stub.calls if c.purpose == "entailment"]


async def _story(engine: AsyncEngine, *, structured: bool = True) -> World:
    """Bobby in the kitchen 0-100, Ann in the school (unrelated to Bobby)."""
    w = await world(engine)
    if structured:
        await use_structured(engine, w.story_id)
    await commit(engine, w.story_id, at(w.people["Bobby"], w.places["kitchen"], 0, 100))
    await commit(engine, w.story_id, at(w.people["Ann"], w.places["school"], 0, 100))
    return w


async def _override(engine: AsyncEngine, story_id: str, settings: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO story_profile_overrides (story_id, settings, revision) VALUES "
                "(:s, CAST(:d AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE SET "
                "settings = EXCLUDED.settings, revision = story_profile_overrides.revision + 1"
            ),
            {"s": story_id, "d": json.dumps(settings)},
        )


async def _verify(
    engine: AsyncEngine,
    w: World,
    claim: Any,
    mal: ModelAccessLayer | None,
    *,
    budget: TurnBudget | None = None,
    viewpoint: str = "omniscient",
    sink: list[EscalationRecord] | None = None,
) -> tuple[VerifyResult | None, Verifier, StructuredReadSet]:
    read_set = StructuredReadSet()
    async with engine.connect() as conn:
        v = await story_verifier(
            conn, read_set, w.story_id, models=mal, budget=budget or TurnBudget()
        )
        if sink is not None:
            v.on_escalation = sink.append
        found = await v.verify(claim, viewpoint, ref=ClaimRef("op:1", beat=2, span=(0, 12)))
    return found, v, read_set


def _garden(w: World) -> Any:
    """Bobby in the garden while canon has him in the kitchen: two places no containment fact
    places, so ``uncertain(world_knowledge)``."""
    return at(w.people["Bobby"], w.places["garden"], 50, 60)


async def test_only_the_uncertain_residual_reaches_the_model(engine: AsyncEngine) -> None:
    """A supported claim makes no call. The garden claim is ``uncertain(world_knowledge)``; the
    model is shown it and Bobby's records (the kitchen), never Ann's, and its validated,
    confident ``contradicted`` decides it (rule ``entailment``, a repair). The deterministic
    fetch is keyed; the neighborhood the model also sees is retrieved, not keyed."""
    w = await _story(engine)
    mal = stub_models()
    inside, _, _ = await _verify(engine, w, at(w.people["Bobby"], w.places["kitchen"], 10, 20), mal)
    assert inside is not None
    assert inside.kind is VerdictKind.SUPPORTED
    assert entailment_calls(mal) == []

    mal.stub.script("entailment", [answer("contradicted", 0.9)])
    sink: list[EscalationRecord] = []
    found, v, read_set = await _verify(engine, w, _garden(w), mal, sink=sink)
    assert found is not None
    assert found.verdict.reason is VerdictReason.WORLD_KNOWLEDGE
    assert (found.kind, found.source, found.rule, found.needs_repair) == (
        VerdictKind.CONTRADICTED,
        "entailment",
        "entailment",
        True,
    )
    (call,) = entailment_calls(mal)
    sent = "\n".join(m.content for m in call.messages)
    assert "Claim: Bobby located_at garden [T+50..T+60]" in sent
    assert "[canon] Bobby located_at kitchen (T+0..T+100)" in sent
    assert "Ann" not in sent
    assert "school" not in sent
    assert call.fields is not None
    assert set(call.fields) <= {
        "claim",
        "records",
        "records.text",
        "records.holder",
        "records.valid_time",
    }
    assert read_set.query_keys
    assert read_set.retrieved
    (record,) = sink
    assert record == v.escalations[0]
    assert (record.claim_id, record.gate_reason, record.predicate_shape) == (
        "op:1",
        "world_knowledge",
        "core|functional|fluent|affirmed",
    )
    assert (record.called, record.model_verdict, record.final_kind) == (
        True,
        "contradicted",
        "contradicted",
    )
    assert v.report() == {
        "claims": 1,
        "deterministic": 0,
        "escalated": 1,
        "verdicts": [
            {
                "claim_id": "op:1",
                "kind": "contradicted",
                "reason": "world_knowledge",
                "rule": "entailment",
                "evidence_ids": [],
                "escalated": True,
                "disposition": None,
            }
        ],
    }


@pytest.mark.parametrize(
    ("scripted", "uncertain"),
    [
        (["{not json", "still not", "never"], "parse_error"),
        ([answer("supported", 0.4)], "low_confidence"),
        ([answer("supported", 0.55)], None),
        ([answer("novel", 0.99)], None),
        ([answer("probably", 0.99)] * 3, "schema_error"),
    ],
)
async def test_an_entailment_result_is_validated_never_trusted(
    engine: AsyncEngine, scripted: list[str], uncertain: str | None
) -> None:
    """FR-VERIFY-8: an unparseable answer, one below ``uncertain_below`` (0.5), a ``supported``
    below ``verification.entailment_min_confidence`` (0.6), a ``novel`` and a verdict outside
    the enum each take the safe disposition (``repair``): never a silent ``supported``."""
    w = await _story(engine)
    mal = stub_models()
    mal.stub.script("entailment", scripted)
    found, v, _ = await _verify(engine, w, _garden(w), mal)
    assert found is not None
    assert (found.kind, found.disposition, found.needs_repair) == (
        VerdictKind.UNCERTAIN,
        "repair",
        True,
    )
    assert found.objective_relation == "unresolved"
    assert found.model_uncertain == uncertain
    assert found.escalated
    (record,) = v.escalations
    assert record.final_kind == "uncertain"
    assert record.disposition == "repair"


async def test_an_unreachable_model_or_an_empty_budget_degrades(engine: AsyncEngine) -> None:
    """The model unavailable (every attempt a 500) or the turn's budget spent: the claim takes
    the safe disposition with a degradation, and the turn goes on (no exception). Identical
    claims within one verifier reuse the first validated answer: one call for two claims."""
    w = await _story(engine)
    mal = stub_models()
    mal.stub.fault("entailment", "http_500", n=10)
    down, _, _ = await _verify(engine, w, _garden(w), mal)
    assert down is not None
    assert (down.kind, down.disposition, down.degradation) == (
        VerdictKind.UNCERTAIN,
        "repair",
        (ENTAILMENT_UNAVAILABLE,),
    )

    spent = stub_models()
    broke, _, _ = await _verify(engine, w, _garden(w), spent, budget=TurnBudget(max_calls=0))
    assert broke is not None
    assert (broke.kind, broke.degradation) == (VerdictKind.UNCERTAIN, (ENTAILMENT_BUDGET,))
    assert entailment_calls(spent) == []

    once = stub_models()
    once.stub.script("entailment", [answer("contradicted", 0.8)])
    read_set = StructuredReadSet()
    async with engine.connect() as conn:
        v = await story_verifier(conn, read_set, w.story_id, models=once, budget=TurnBudget())
        first = await v.verify(_garden(w))
        second = await v.verify(_garden(w))
    assert first is not None
    assert second is not None
    assert first.kind is second.kind is VerdictKind.CONTRADICTED
    assert len(entailment_calls(once)) == 1
    assert [r.reused for r in v.escalations] == [False, True]


async def test_the_profile_decides_the_disposition_and_the_level(engine: AsyncEngine) -> None:
    """``verification.safe_disposition = surface`` lets the beat stand and lists the claim (its
    rendering, beat, span, reason and confidence) in ``uncertain_claims``. At
    ``verification.level = extraction`` an uncertain claim takes the disposition with no call,
    still leaving an escalation record (no model verdict). Lite is inert."""
    w = await _story(engine)
    await _override(engine, w.story_id, {"verification": {"safe_disposition": "surface"}})
    mal = stub_models()
    found, v, _ = await _verify(engine, w, _garden(w), mal)  # the stub: novel at confidence 0
    assert found is not None
    assert (found.kind, found.disposition, found.needs_repair) == (
        VerdictKind.UNCERTAIN,
        "surface",
        False,
    )
    assert v.uncertain_claims() == [
        {
            "claim": "Bobby located_at garden [T+50..T+60]",
            "claim_id": "op:1",
            "span": [0, 12],
            "beat": 2,
            "reason": "world_knowledge",
            "confidence": 0.5,
        }
    ]
    assert "uncertain_surfaced" in v.degradation()

    await _override(engine, w.story_id, {"verification": {"level": "extraction"}})
    quiet = stub_models()
    found, v, _ = await _verify(engine, w, _garden(w), quiet)
    assert found is not None
    assert (found.kind, found.disposition, found.escalated) == (
        VerdictKind.UNCERTAIN,
        "repair",
        False,
    )
    assert entailment_calls(quiet) == []
    (record,) = v.escalations
    assert (record.called, record.model_verdict) == (False, None)

    lite = await _story(engine, structured=False)
    found, v, read_set = await _verify(engine, lite, _garden(lite), quiet)
    assert found is None
    assert read_set.query_keys == []
    assert v.report()["claims"] == 0
    assert entailment_calls(quiet) == []
