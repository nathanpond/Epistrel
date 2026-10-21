"""E-03: consolidation faithfulness (docs/04 §8; FR-MEM-3,6, NFR-QUAL-3; #157).

Each case is one source window on its own scratch story (``Structured (M4)``, automatic
consolidation pushed out of reach): the window's episodes are written as their characters' ``do``
turns through the turn commit's episodic writer, then #143's ``consolidate_window`` runs the
consolidation job on it in process — the ``consolidation`` purpose, ``derive``'s support and
modality rules, and the consolidated writer. Every reflection it wrote is then judged against
the window's episodes by the ``entailment`` purpose (the verifier's validation: ``supported`` at
or above the profile's 0.6 minimum confidence counts as entailed).

A free-text reflection carries no subject/relation/object split, so the deterministic arm the
planner described (normalizing the reflection through #140) has nothing to normalize; the
judge is the model arm. When the judge is the model under test (a single-model run), the
report says so: a model may be lenient with its own reflections, and the labeled pairs below
measure how far its judging can be trusted. A run with a second judge model (``--judge-model``,
#475) sends these judge calls to it through ``CaseContext.judge_models``. The consolidation
itself stays on the run model.

- ``nfr_qual_3.faithfulness`` (rate, ≥ 98 %): every reflection the window produced is entailed
  by its episodes (a window that produced none is not observed).
- ``e03.produced`` (report-only): the consolidation wrote at least one reflection.
- ``e03.judge_agreement`` (report-only): the judge called the labeled ``entailed`` reflection
  entailed and the labeled ``non_entailed`` one not.

The stub is scripted per case (the labeled reflection with its supports; the judge's verdict
per text), so CI gates on its stub run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from epistrel.core.enums import MemoryKind
from epistrel.core.memory.consolidation import Window
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.tables.memories import memories
from epistrel.evals import scenes
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import EVAL_CASE_HEADER
from epistrel.orchestrator.models.registry import (
    CONSOLIDATION,
    ENTAILMENT,
    EntailmentInputs,
    EntailmentRecord,
    EntailmentVerdict,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.verify.verifier import VerifyConfig

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.models.stub import StubAdapter

FAITHFULNESS = "nfr_qual_3.faithfulness"
PRODUCED = "e03.produced"
JUDGE_AGREEMENT = "e03.judge_agreement"
DIALS = {"memory": {"consolidation": {"window_memories": 1_000, "cadence_turns": 1_000}}}
"""Automatic consolidation never fires on a case's story: the case runs its window itself."""
MIN_CONFIDENCE = VerifyConfig().entailment_min_confidence
JUDGE_DEADLINE_S = 300.0
STUB_CONFIDENCE = 0.9


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Episode(_Model):
    by: str
    text: str = Field(min_length=1, max_length=1000)


class WindowInputs(_Model):
    actor: str
    other: str
    episodes: list[Episode] = Field(min_length=2, max_length=20)


class WindowLabels(_Model):
    entailed: str
    non_entailed: str
    supports: list[int]
    question: str


class ConsolidationCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: WindowInputs
    expected: WindowLabels


def cast_of(actor: str, other: str) -> list[CharacterSpec]:
    """The player (Cal) and a window's two characters."""
    return [
        CharacterSpec(name="Cal", definition="A traveller.", true_age=30, is_player=True),
        CharacterSpec(name=actor, definition="A villager.", true_age=34),
        CharacterSpec(name=other, definition="A neighbour.", true_age=41),
    ]


async def judge(models: ModelAccessLayer, claim: str, sources: list[str]) -> bool:
    """Whether the ``entailment`` purpose finds ``claim`` entailed by ``sources``."""
    answer = await models.structured_call(
        ENTAILMENT.name,
        EntailmentVerdict,
        EntailmentInputs(claim=claim[:1000], records=[EntailmentRecord(text=s) for s in sources]),
        deterministic_first=lambda: None,
        budget=TurnBudget(deadline_s=JUDGE_DEADLINE_S),
    )
    if isinstance(answer, Uncertain):
        return False
    return answer.verdict == "supported" and answer.confidence >= MIN_CONFIDENCE


async def written_reflections(ctx: CaseContext, story_id: str) -> list[str]:
    async with ctx.engine.db.connect() as conn:
        rows = await conn.scalars(
            select(memories.c.text)
            .where(
                memories.c.story_id == story_id,
                memories.c.kind == MemoryKind.REFLECTION.value,
            )
            .order_by(memories.c.id)
        )
        return [str(r) for r in rows]


def case_line(case_id: str) -> str:
    return f"{EVAL_CASE_HEADER}: {case_id}"


@register
class E03Suite(Suite):
    name = "e03"
    title = "E-03: consolidation faithfulness (reflections entailed by their sources)"
    set_path = "consolidation/cases.jsonl"
    min_cases = 150
    guarantees = ("consolidation",)
    ci_gate = True
    judge_purposes = (ENTAILMENT.name,)
    case_model = ConsolidationCase
    metrics = (
        Metric(
            FAITHFULNESS,
            Rule.rate,
            0.98,
            Direction.at_least,
            "every reflection a window produced is entailed by its episodes",
        ),
        Metric(PRODUCED, Rule.informational, description="the window produced a reflection"),
        Metric(
            JUDGE_AGREEMENT,
            Rule.informational,
            description="the judge agreed with both labeled reflections of the window",
        ),
    )
    reading_notes = (
        "E-03's judge is the `entailment` purpose. On a single-model run it is the model under "
        "test judging its own reflections. With `--judge-model` (#475) it is that second model, "
        "and the suite's Judge line names it. `e03.judge_agreement` shows how well the judge "
        "judges the labeled entailed and non-entailed reflections. A window that produced no "
        "reflection is not counted in `nfr_qual_3.faithfulness`, but is in `e03.produced`.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ConsolidationCase)  # noqa: S101 - load() validated it
        created = await ctx.story(
            StorySpec(characters=cast_of(case.inputs.actor, case.inputs.other))
        )
        story = created.story_id
        await scenes.structured(ctx.engine, ctx.principal, story, DIALS)
        ids = {c.name: c.id for c in created.characters}
        written = await scenes.episodes(
            ctx.engine, story, [(ids[e.by], e.text) for e in case.inputs.episodes]
        )
        turns = [m.narrative_turn for m in written if m.narrative_turn is not None]
        sources = [m.text for m in written]
        run = await consolidate_window(
            ctx.engine.db, story, Window(min(turns), max(turns)), models=ctx.models
        )
        reflections = await written_reflections(ctx, story)
        judges = ctx.judge_models
        entailed = [await judge(judges, r, sources) for r in reflections]
        agreement = await judge(judges, case.expected.entailed, sources) and not (
            await judge(judges, case.expected.non_entailed, sources)
        )
        observations: dict[str, bool | None] = {
            PRODUCED: bool(reflections),
            JUDGE_AGREEMENT: agreement,
            FAITHFULNESS: all(entailed) if reflections else None,
        }
        detail: dict[str, Any] = {
            "outcome": str(run.outcome),
            "result": run.result,
            "entailed": entailed,
        }
        return Outcome(observations, output=json.dumps(reflections), detail=detail)

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        assert isinstance(case, ConsolidationCase)  # noqa: S101 - load() validated it
        labels = case.expected
        reflection = {
            "text": labels.entailed,
            "modality": "observation",
            "attributed_to": None,
            "supports": labels.supports,
        }
        stub.case(
            case.id,
            json.dumps({"facts": [], "reflections": [reflection]}),
            purpose=CONSOLIDATION.name,
        )
        line = case_line(case.id)
        for claim, verdict in ((labels.entailed, "supported"), (labels.non_entailed, "novel")):
            answer = json.dumps({"verdict": verdict, "confidence": STUB_CONFIDENCE})
            stub.when((line, f"Claim: {claim}"), answer, purpose=ENTAILMENT.name)


__all__ = ["FAITHFULNESS", "JUDGE_AGREEMENT", "PRODUCED", "ConsolidationCase", "E03Suite"]
