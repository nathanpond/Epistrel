"""Q-04: knowledge-asymmetry correctness (docs/04 §8; FR-EPI-1,5, NFR-QUAL-1; #179).

Each case asks one character about another character's private fact, in a scene of
``evals.fixtures`` (every private fact of the fixture established, each its holder's alone),
through ``Engine.respond``: the player's question, the asked character rendered, the output
filtered for the player's character (``player_view = character``). The answer's correct form
requires **not** knowing the fact, so the player-facing text (the composed message and the
``say`` beats the player received) must hold none of the case's forbidden forms
(``fixtures.disclosures``: #148's whole-word folded matcher over the case's own list, never
the pipeline's own detectors). A behavioural backstop to PRIV (#170), never a proof.

- ``nfr_qual_1.asymmetry`` (observed-zero rule, NFR-QUAL-8): a disclosure in the final
  player-facing output. Zero on every seed over at least 300 cases passes, reported as "0
  observed in n cases" with the one-sided 95 % upper bound; one disclosure fails.
- ``q04.control_detected`` (report-only): the negative controls (the holder asked, answering
  with the fact) are detected, so a clean run is visibly not a blind oracle.
- ``q04.render_repaired`` (report-only): the turn repaired a render before the output (a leak
  the pipeline caught is not a violation; it is reported here).
- ``q04.model_error`` (report-only; ``Suite.model_error_metric``): the case ended in a model
  error. It observes nothing else (an error is not a disclosure); the zero rule is judged on the
  completed cases, and a seed short of 300 of them is insufficient evidence, never a pass.

The fact's holder, present in a withheld case's scene, is a fixed ``silent`` participant
(``fixtures.build_scene``): it is never narrated reacting or escalated to its own render, so
the case measures one character's knowledge reaching another, never a holder voicing its own
secret (the owner's decision of 2026-10-24).

The stub answers each case with its authored clean render (``stub_answer``); E-08's injected
violation (``leak_answer``) is the private fact paraphrased so that no protected entity name
is in it, which the pipeline cannot catch and the oracle does. CI gates on the stub run, which
runs the full set whatever ``--sample`` says (``--full q04``: a zero-tolerance metric needs its
300 cases).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from epistrel.core.tables import events
from epistrel.evals import fixtures
from epistrel.evals.authoring.asymmetry import render
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.orchestrator.lite.schemas import RespondRequest, RespondResult
from epistrel.orchestrator.models.registry import GENERATION

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.models.stub import StubAdapter

ASYMMETRY = "nfr_qual_1.asymmetry"
CONTROL = "q04.control_detected"
REPAIRED = "q04.render_repaired"
MODEL_ERROR = "q04.model_error"
INJECTED_CASE = "asymmetry-family-0007"
"""E-08's injected violation: the case the scripted stub leaks on (on one seed)."""


class AsymmetryInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str
    fact: str
    holder: str
    asker: str
    viewpoint: str
    template: Literal["direct", "leading", "gossip"]
    question: str = Field(min_length=1, max_length=500)
    forbidden: list[str] = Field(min_length=1)


class AsymmetryCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: AsymmetryInputs
    expected: Literal["withheld", "control"]
    stub_answer: str

    @property
    def control(self) -> bool:
        return self.expected == "control"


async def repaired(ctx: CaseContext, result: RespondResult) -> bool:
    """Whether the turn repaired a render before releasing it (a ``render_rejected`` event)."""
    operation = next((b.operation_id for b in result.beats), None)
    if operation is None:
        return False
    async with ctx.engine.db.connect() as conn:
        found = await conn.scalar(
            select(func.count())
            .select_from(events)
            .where(events.c.operation_id == operation, events.c.type == "render_rejected")
        )
    return bool(found)


async def ask(ctx: CaseContext, scene: fixtures.Scene, say: str, renders: str) -> RespondResult:
    """One structured turn: the player says ``say``, ``renders`` is rendered, the output read
    as the player's character under ``player_view = character``."""
    request = RespondRequest.model_validate(
        {
            "actor_character_id": scene.ids[scene.player],
            "player_input": {"say": say},
            "render_character_ids": [scene.ids[renders]],
            "output_viewpoint": scene.viewpoint(),
            "player_view": "character",
        }
    )
    return await ctx.engine.respond(ctx.principal, scene.story_id, request)


@register
class Q04Suite(Suite):
    name = "q04"
    title = "Q-04: knowledge asymmetry (NFR-QUAL-1)"
    set_path = "asymmetry/cases.jsonl"
    min_cases = 300
    ci_gate = True
    case_model = AsymmetryCase
    guarantees = ("paraphrase_detection",)
    metrics = (
        Metric(
            ASYMMETRY,
            Rule.zero,
            0.0,
            Direction.at_most,
            "the player-facing answer disclosed another character's private fact",
        ),
        Metric(
            CONTROL,
            Rule.informational,
            description="a negative control (the holder states the fact) was detected",
        ),
        Metric(
            REPAIRED,
            Rule.informational,
            direction=Direction.at_most,
            description="the turn repaired a render before releasing it",
        ),
        Metric(
            MODEL_ERROR,
            Rule.informational,
            direction=Direction.at_most,
            description="the case ended in a model error (reported, not scored as a disclosure)",
        ),
    )
    model_error_metric = MODEL_ERROR
    reading_notes = (
        "Q-04 is a behavioural backstop to PRIV: it measures the final player-facing output "
        "of a turn whose correct answer requires not knowing another character's private "
        "fact. A clean run is reported as an observed count with its one-sided 95% upper "
        "bound, never as proof that no disclosure can happen; a render the pipeline repaired "
        "before the output is reported (`q04.render_repaired`), not counted.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, AsymmetryCase)  # noqa: S101 - load() validated it
        fixture = fixtures.FIXTURES[case.inputs.fixture_id]
        fact = fixture.fact(case.inputs.fact)
        present = (case.inputs.viewpoint, fact.holder)
        silent = () if case.control else (fact.holder,)
        scene = await fixtures.build_scene(ctx, fixture, [fact], present=present, silent=silent)
        result = await ask(ctx, scene, case.inputs.question, case.inputs.viewpoint)
        text = fixtures.player_text(result)
        hits = fixtures.disclosures(text, case.inputs.forbidden)
        private = fixtures.private_channels(result)
        disclosed = bool(hits or private)
        return Outcome(
            {
                ASYMMETRY: None if case.control else not disclosed,
                CONTROL: disclosed if case.control else None,
                REPAIRED: not await repaired(ctx, result),
            },
            output=text,
            detail={
                "fixture": fixture.id,
                "fact": case.inputs.fact,
                "forms_found": hits,
                "private_channels": private,
                "audience_filter": result.audience_filter,
                "degradation": result.degradation,
            },
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, AsymmetryCase)  # noqa: S101 - load() validated it
        return {GENERATION.name: case.stub_answer}

    @staticmethod
    def leak_answer(case: AsymmetryCase) -> str:
        """E-08's injected violation for ``case``: a render saying the fact's ``leak``."""
        fact = fixtures.FIXTURES[case.inputs.fixture_id].fact(case.inputs.fact)
        return render(fact.leak)

    def inject(self, stub: StubAdapter, case: AsymmetryCase, seed: int) -> None:
        """Script ``stub`` to leak on ``case`` on ``seed`` only (E-08)."""
        stub.case(case.id, self.leak_answer(case), purpose=GENERATION.name, seed=seed)


__all__ = [
    "ASYMMETRY",
    "CONTROL",
    "INJECTED_CASE",
    "MODEL_ERROR",
    "REPAIRED",
    "AsymmetryCase",
    "Q04Suite",
    "ask",
]
