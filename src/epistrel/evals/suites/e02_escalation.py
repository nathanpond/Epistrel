"""E-02: escalation yield and shadow tightening (docs/04 §8; NFR-PERF-5, FR-OBS-2,4,
FR-VERIFY-3,8; #155).

Every metric is ``informational`` (``report_only``): E-02's checks are instrumentation and
invariants, not a quality rate, and ~20 % escalation is a reference baseline, never a gate.
``tests/eval/test_e_02.py`` runs the suite against the stub and asserts no bad observation.

**Escalation cases** (``kind: escalation``, at least 300 escalations over the set): each plays
one claim per gate reason that escalates (an unresolved predicate, world knowledge, weak
semantics; an approximate-bounds residual never escalates and writes no row, #467, ADR-162)
through #142's ``Verifier`` (deterministic first, over records
passed in, ``verification.level = entailment``) with the case's scripted entailment answer
(a confident or low-confidence ``supported``/``contradicted``, a ``novel``, or an unparseable
answer), and writes its escalation records through #155's writer
(``orchestrator.verify.yields.write``) as one turn of the case's scratch story would. The
records are relabelled to the suite's dedicated test shape (``E02_SHAPE``), whose shadow
proposal ``setup`` puts in place and ``teardown`` removes, so a run never touches a real shape's
window. Observed per row:

- ``e02.instrumentation_complete``: every required field is set (claim id, gate reason,
  shape, deterministic confidence, threshold, tentative verdict, final kind, consequence, pass,
  ``at``); the model verdict is set exactly when the model gave a valid answer, and ``flipped``
  exactly when the model verdict is;
- ``e02.no_silent_supported`` (FR-VERIFY-8): a row that ends ``supported`` rests on a valid,
  confident model ``supported``; an uncertain, low-confidence, ``novel`` or unparseable answer
  never ends ``supported``;
- ``e02.shadow_recorded`` (FR-OBS-4): the live gate still escalated (``called``), the row
  carries the proposal's threshold, ``shadow_decision`` is what the tightened gate would have
  decided, and ``shadow_agreed`` is set exactly when it would have skipped, to whether the
  model's answer was left untaken;
- ``e02.yield_reported`` (NFR-PERF-5): the yield report over the case's window counts the
  case's escalations and flips for the test shape.

**Promotion cases** (``kind: promotion``): a shape of the case's own is proposed in shadow, a
window of ``cases`` would-have-skipped escalations (``agreed`` of them agreeing with the model)
is written, and ``POST /admin/verify/gate/{shape}/promote`` is called through the Engine:
``e02.promotion_rule`` is good when it is refused (409 ``gate_not_promotable`` with the window's
numbers) exactly below ``gate_promotion_min_cases`` or ``gate_promotion_min_agreement`` and
allowed above (complement). The shape is removed after the case either way.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.config_changes import read_section
from epistrel.core.errors import GateNotPromotable
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.propositions.model import (
    Interval,
    Layer,
    Polarity,
    Predicate,
    Proposition,
    story_minute,
)
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import CORE_BY_ID, CORE_BY_NAME
from epistrel.core.settings_schema import VerifySettings
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.tables import operations
from epistrel.core.tables.escalations import escalations
from epistrel.core.verify import yield_queries
from epistrel.core.verify.rules import Options, Record
from epistrel.evals.gate import Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.registry import ENTAILMENT
from epistrel.orchestrator.verify import yields
from epistrel.orchestrator.verify.verifier import (
    ClaimRef,
    RecordsSource,
    Verifier,
    VerifyConfig,
)

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext

INSTRUMENTATION = "e02.instrumentation_complete"
NO_SILENT_SUPPORTED = "e02.no_silent_supported"
SHADOW_RECORDED = "e02.shadow_recorded"
YIELD_REPORTED = "e02.yield_reported"
PROMOTION_RULE = "e02.promotion_rule"

E02_SHAPE = "eval|e02|shadow|affirmed"
"""The dedicated test shape the escalation cases' records are relabelled to."""
E02_PROPOSAL = 0.25
"""Below (b) and (c)'s deterministic 0.5 and above (a)'s 0.0: both shadow branches run."""
UNPARSEABLE = "unparseable"
CALL_DEADLINE_S = 300.0

CAST = (
    CharacterSpec(name="Wren", definition="A traveller.", true_age=29, is_player=True),
    CharacterSpec(name="Maren", definition="The innkeeper.", true_age=46),
)

# -- the claims: one per gate reason (#142's U-04 fixtures) ------------------------------------

STORY = "01JS0000000000000000000E02"
BOBBY = "01JA0000000000000000000001"
OHIO = "01JA0000000000000000000002"
SMITH = "01JA0000000000000000000003"
SMITH_2 = "01JA0000000000000000000004"
BOSTON = "01JA0000000000000000000008"
STRAY = "01JP00000000000000000000ZZ"
APPRENTICED = "01JP0000000000000000000001"
LOCATED_AT = CORE_BY_NAME["located_at"].id
REGISTRY = Registry(
    STORY,
    1,
    {
        **CORE_BY_ID,
        APPRENTICED: Predicate(
            id=APPRENTICED,
            story_id=STORY,
            name="apprenticed_with",
            layer=Layer.AUTO,
            qualifiers=None,
        ),
    },
    (),
)
NAMES = {BOBBY: "Bobby", OHIO: "Ohio", SMITH: "Smith", SMITH_2: "Smithson", BOSTON: "Boston"}

Reason = Literal["unresolved_predicate", "world_knowledge", "weak_semantics"]
REASONS: tuple[Reason, ...] = ("unresolved_predicate", "world_knowledge", "weak_semantics")
"""The gate reasons that escalate (``gate.ESCALATES``)."""


def _year(y: int) -> int:
    return story_minute(datetime(y, 1, 1, tzinfo=UTC))


def _prop(predicate: str, obj: str, when: Interval | None = None) -> Proposition:
    return Proposition(
        subject_id=BOBBY,
        predicate_id=predicate,
        object_id=obj,
        polarity=Polarity.AFFIRMED,
        valid_time=when if when is not None else Interval.of(None, None),
    )


def claim_for(reason: Reason) -> tuple[Proposition, list[Proposition], bool]:
    """The claim, the holder's records, and whether the check runs ``strict_weak``."""
    if reason == "unresolved_predicate":
        return _prop(STRAY, OHIO), [], False
    if reason == "world_knowledge":
        ohio = _prop(LOCATED_AT, OHIO, Interval.of(_year(2010), _year(2014)))
        return _prop(LOCATED_AT, BOSTON, Interval.of(_year(2011), _year(2012))), [ohio], False
    smith = _prop(APPRENTICED, SMITH, Interval.of(0, 100))
    return _prop(APPRENTICED, SMITH_2, Interval.of(50, 60)), [smith], True


# -- the cases ------------------------------------------------------------------------------


class E02Case(CaseModel):
    kind: Literal["escalation", "promotion"]
    verdict: Literal["supported", "contradicted", "novel", "unparseable"] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    cases: int | None = Field(default=None, ge=0, le=5_000)
    agreed: int | None = Field(default=None, ge=0, le=5_000)


REQUIRED = (
    "claim_id",
    "gate_reason",
    "predicate_shape",
    "deterministic_confidence",
    "threshold",
    "tentative_verdict",
    "final_kind",
    "consequence",
    "pass",
    "at",
)


async def _operation(db: AsyncEngine, story_id: str) -> str:
    async with db.connect() as conn:
        found = await conn.scalar(
            select(operations.c.operation_id)
            .where(operations.c.story_id == story_id, operations.c.status == "committed")
            .order_by(operations.c.started_at)
            .limit(1)
        )
    return str(found)


def _complete(row: Mapping[str, object]) -> bool:
    if any(row[name] is None for name in REQUIRED):
        return False
    if row["model_verdict"] is not None and (
        not row["called"] or row["model_uncertain"] is not None
    ):
        return False
    return (row["flipped"] is None) == (row["model_verdict"] is None)


def _not_silent(row: Mapping[str, object], floor: float) -> bool:
    if row["final_kind"] != "supported":
        return True
    confidence = row["model_confidence"]
    return (
        row["model_verdict"] == "supported"
        and row["model_uncertain"] is None
        and isinstance(confidence, float)
        and confidence >= floor
    )


def _shadowed(row: Mapping[str, object]) -> bool:
    confidence = row["deterministic_confidence"]
    threshold = row["threshold"]
    if not isinstance(confidence, float) or not isinstance(threshold, float):
        return False
    would_escalate = confidence < min(threshold, E02_PROPOSAL)
    if not row["called"] or row["shadow_threshold"] != E02_PROPOSAL:
        return False
    if row["shadow_decision"] is not would_escalate:
        return False
    if would_escalate:
        return row["shadow_agreed"] is None
    untaken = row["model_verdict"] is not None and row["flipped"] is False
    return row["shadow_agreed"] is (untaken and row["model_uncertain"] is None)


def _record_id(claim: int, n: int) -> str:
    return f"01JR{claim:02d}{n:020d}"


def _shape_of(case_id: str) -> str:
    return f"eval|e02p|{re.sub(r'[^a-z0-9_]', '_', case_id.lower())[:40]}|affirmed"


@register
class E02Suite(Suite):
    name = "e02"
    title = "E-02: escalation yield and shadow tightening"
    metrics = (
        Metric(
            INSTRUMENTATION,
            Rule.informational,
            description="every escalation record carries every required field",
        ),
        Metric(
            NO_SILENT_SUPPORTED,
            Rule.informational,
            description="an uncertain, low-confidence or unparseable entailment answer never "
            "ends supported (FR-VERIFY-8)",
        ),
        Metric(
            SHADOW_RECORDED,
            Rule.informational,
            description="the live gate is unchanged and the record holds what the shadowed "
            "tightening would have decided",
        ),
        Metric(
            YIELD_REPORTED,
            Rule.informational,
            description="the yield report counts the case's escalations and flips",
        ),
        Metric(
            PROMOTION_RULE,
            Rule.informational,
            description="promotion is refused below the rule and allowed above it",
        ),
    )
    reading_notes = (
        "E-02 is instrumentation, not a quality rate: every metric is report-only, and "
        "`tests/eval/test_e_02.py` asserts none has a bad observation. ~20 % overall "
        "escalation is NFR-PERF-5's reference baseline, reported by "
        "`GET /admin/evals/escalation-yield`, never a gate.",
    )
    case_model = E02Case

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        await engine.set_verify_gate(
            ctx, yield_queries.GateRequest(shape=E02_SHAPE, threshold=E02_PROPOSAL)
        )

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        await engine.remove_verify_gate(ctx, E02_SHAPE)

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, E02Case)  # noqa: S101 - load() validated it as case_model
        if case.kind == "promotion":
            return await self._promotion(ctx, case)
        return await self._escalations(ctx)

    async def _escalations(self, ctx: CaseContext) -> Outcome:
        db = ctx.engine.db
        created = await ctx.story(StorySpec(characters=list(CAST)))
        story_id = created.story_id
        operation = await _operation(db, story_id)
        started = datetime.now(UTC) - timedelta(seconds=1)
        config = VerifyConfig(level="entailment")
        verifier = Verifier(
            RecordsSource([], REGISTRY, entity_names=NAMES),
            config,
            models=ctx.models,
            budget=TurnBudget(deadline_s=CALL_DEADLINE_S),
        )
        for n, reason in enumerate(REASONS):
            claim, canon, strict = claim_for(reason)
            verifier.source = RecordsSource(
                [
                    Record(p, f"01JR{n:02d}{i:020d}", f"01JR{n:02d}{i:020d}")
                    for i, p in enumerate(canon)
                ],
                REGISTRY,
                options=Options(strict_weak=strict),
                entity_names=NAMES,
            )
            await verifier.verify(claim, ref=ClaimRef(claim_id=f"{operation}:{n}"))
        verifier.escalations[:] = [
            dataclasses.replace(r, predicate_shape=E02_SHAPE) for r in verifier.escalations
        ]
        async with db.begin() as conn:
            await yields.write(
                conn,
                story_id=story_id,
                operation_id=operation,
                position=(None, None),
                passes=[yields.VerifyPass(verifier, yields.RENDER)],
                repaired={},
            )
            rows = [
                dict(r._mapping)
                for r in await conn.execute(
                    select(escalations).where(escalations.c.story_id == story_id)
                )
            ]
            report = await yield_queries.yield_report(
                conn, window_from=started, window_to=datetime.now(UTC) + timedelta(seconds=1)
            )
        cluster = next((c for c in report.by_shape if c.shape == E02_SHAPE), None)
        reported = (
            cluster is not None
            and len(rows) == len(REASONS)
            and cluster.escalations == len(rows)
            and cluster.flips == sum(1 for r in rows if r["flipped"] is True)
        )
        floor = config.entailment_min_confidence
        return Outcome(
            {
                INSTRUMENTATION: len(rows) == len(REASONS) and all(map(_complete, rows)),
                NO_SILENT_SUPPORTED: all(_not_silent(r, floor) for r in rows),
                SHADOW_RECORDED: bool(rows) and all(map(_shadowed, rows)),
                YIELD_REPORTED: reported,
            },
            output=json.dumps(
                [
                    {k: r[k] for k in ("gate_reason", "model_verdict", "final_kind", "flipped")}
                    for r in rows
                ]
            ),
        )

    async def _promotion(self, ctx: CaseContext, case: E02Case) -> Outcome:
        db = ctx.engine.db
        shape = _shape_of(case.id)
        cases, agreed = case.cases or 0, case.agreed or 0
        created = await ctx.story(StorySpec(characters=list(CAST)))
        operation = await _operation(db, created.story_id)
        await ctx.engine.set_verify_gate(
            ctx.principal, yield_queries.GateRequest(shape=shape, threshold=E02_PROPOSAL)
        )
        try:
            async with db.begin() as conn:
                settings = await read_section(conn, VerifySettings)
                await insert_derived(
                    conn,
                    escalations,
                    [
                        {
                            "id": new_ulid(),
                            "story_id": created.story_id,
                            "operation_id": operation,
                            "claim_id": f"{operation}:{n}",
                            "gate_reason": "world_knowledge",
                            "predicate_shape": shape,
                            "deterministic_confidence": 0.5,
                            "threshold": 1.0,
                            "called": True,
                            "model_verdict": "novel" if n < agreed else "contradicted",
                            "model_confidence": 0.9,
                            "flipped": n >= agreed,
                            "final_kind": "uncertain" if n < agreed else "contradicted",
                            "consequence": "none",
                            "shadow_threshold": E02_PROPOSAL,
                            "shadow_decision": False,
                            "shadow_agreed": n < agreed,
                        }
                        for n in range(cases)
                    ],
                )
            expected = (
                cases >= settings.gate_promotion_min_cases
                and cases > 0
                and agreed / cases >= settings.gate_promotion_min_agreement
            )
            try:
                written = await ctx.engine.promote_verify_gate(ctx.principal, shape)
                good = expected and written.live_threshold == E02_PROPOSAL
                detail: dict[str, object] = {"promoted": True}
            except GateNotPromotable as refused:
                good = not expected and refused.details.get("cases") == cases
                detail = dict(refused.details)
        finally:
            await ctx.engine.remove_verify_gate(ctx.principal, shape)
        return Outcome({PROMOTION_RULE: good}, output=json.dumps(detail), detail=detail)

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, E02Case)  # noqa: S101 - load() validated it as case_model
        if case.kind != "escalation" or case.verdict is None:
            return {}
        if case.verdict == UNPARSEABLE:
            return {ENTAILMENT.name: "not json"}
        return {
            ENTAILMENT.name: json.dumps({"verdict": case.verdict, "confidence": case.confidence})
        }


__all__ = [
    "E02_PROPOSAL",
    "E02_SHAPE",
    "INSTRUMENTATION",
    "NO_SILENT_SUPPORTED",
    "PROMOTION_RULE",
    "SHADOW_RECORDED",
    "YIELD_REPORTED",
    "E02Case",
    "E02Suite",
    "claim_for",
]
