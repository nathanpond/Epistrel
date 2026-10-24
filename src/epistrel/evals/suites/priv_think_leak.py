"""NFR-QUAL-1's private-thought metric (docs/04 §8, E-08 M5; FR-TURN-3, FR-RESP-12; #179).

Each case plays two turns of a scene of ``evals.fixtures`` through ``Engine.respond``. The
**prelude** is scripted on every run (``CaseContext.scripted``: its calls go to the run's stub,
even on a real model): the player asks the thinker if all is well, and the thinker renders the
case's ``think`` beside a harmless ``say``. The **measured turn** then renders another
character in answer to the player's question. Both turns are read as the player's character
(``player_view = character``), and their player-facing text must not disclose the thought:
none of the case's forbidden forms (``fixtures.disclosures``), no echo of the thought's words
(``fixtures.echoes``, #152's shingle detector), and no ``think`` or ``latent`` beat at all.

- ``nfr_qual_1.think_leak`` (observed-zero rule, NFR-QUAL-8): a disclosure in the final
  player-facing output of either turn; zero on every seed over at least 300 cases passes,
  reported with the one-sided 95 % upper bound, never as proof.
- ``priv_think_leak.verbatim`` / ``.paraphrase`` / ``.narrated`` (report-only): the same, per
  category (the thought's words; the private fact's names and paraphrases; narrated inner
  state).
- ``priv_think_leak.control_detected`` (report-only): the negative controls (the thinker voices
  its own thought) are detected.

On the stub both turns answer their authored renders (``stub_prelude``, ``stub_answer``), keyed
on the case and the player's line (``StubAdapter.when``). CI gates on the stub run of the full
set (``--full priv_think_leak``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.evals import fixtures
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.evals.suites.q04_asymmetry import ask
from epistrel.orchestrator.models.adapter import EVAL_CASE_HEADER
from epistrel.orchestrator.models.registry import GENERATION

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.models.stub import StubAdapter

THINK_LEAK = "nfr_qual_1.think_leak"
CONTROL = "priv_think_leak.control_detected"
CATEGORY_METRICS = {
    "verbatim": "priv_think_leak.verbatim",
    "paraphrase": "priv_think_leak.paraphrase",
    "narrated": "priv_think_leak.narrated",
}

Category = Literal["verbatim", "paraphrase", "narrated"]


class ThinkLeakInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str
    fact: str
    thinker: str
    think_text: str = Field(min_length=1, max_length=500)
    prelude: str = Field(min_length=1, max_length=500)
    renderer: str
    player_input: str = Field(min_length=1, max_length=500)
    viewpoint: str
    category: Category
    forbidden: list[str] = Field(min_length=1)


class ThinkLeakCase(CaseModel):
    family: Literal["verbatim", "paraphrase", "narrated", "control"]
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: ThinkLeakInputs
    expected: Literal["withheld", "control"]
    stub_prelude: str
    stub_answer: str

    @property
    def control(self) -> bool:
        return self.expected == "control"


def case_line(case: CaseModel) -> str:
    """The ``X-Eval-Case`` line every model call of ``case`` carries."""
    return f"{EVAL_CASE_HEADER}: {case.id}"


@register
class PrivThinkLeakSuite(Suite):
    name = "priv_think_leak"
    title = "NFR-QUAL-1: private thoughts never reach the player's output"
    set_path = "think_leak/cases.jsonl"
    min_cases = 300
    ci_gate = True
    case_model = ThinkLeakCase
    dials = fixtures.M5_DIALS
    profile_label = fixtures.PROFILE_LABEL
    metrics = (
        Metric(
            THINK_LEAK,
            Rule.zero,
            0.0,
            Direction.at_most,
            "a turn's player-facing output disclosed another character's private thought",
        ),
        *(
            Metric(
                metric,
                Rule.informational,
                direction=Direction.at_most,
                description=f"the {category} category alone: a disclosure",
            )
            for category, metric in CATEGORY_METRICS.items()
        ),
        Metric(
            CONTROL,
            Rule.informational,
            description="a negative control (the thinker voices its thought) was detected",
        ),
    )
    reading_notes = (
        "The private-thought metric reads both turns of a case as the player's character: the "
        "scripted prelude that plants the thought (its `think` must be filtered out) and the "
        "measured turn, rendered by a character who never held it. A clean run is an observed "
        "count with its one-sided 95% upper bound, never proof that no thought can leak.",
    )

    def __init__(self) -> None:
        self._prelude: set[tuple[str, str]] = set()

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        fixtures.DELIVERY.open()

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        fixtures.DELIVERY.close()

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        """A stub run's two renders. The measured turn's prompt holds the prelude's line too
        (working memory), so its own answer is registered first and wins there."""
        assert isinstance(case, ThinkLeakCase)  # noqa: S101 - load() validated it
        stub.when((case_line(case), case.inputs.player_input), case.stub_answer, GENERATION.name)
        stub.when((case_line(case), case.inputs.prelude), case.stub_prelude, GENERATION.name)

    def _script_prelude(self, ctx: CaseContext, case: ThinkLeakCase) -> None:
        """A real-model run's prelude, on the stub its scripted block reaches (once a run)."""
        stub = ctx.scripted_stub
        if stub is ctx.engine.models.adapter or (ctx.run_id, case.id) in self._prelude:
            return  # a stub run scripted it with the case (``script_stub``)
        self._prelude.add((ctx.run_id, case.id))
        stub.when((case_line(case), case.inputs.prelude), case.stub_prelude, GENERATION.name)

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ThinkLeakCase)  # noqa: S101 - load() validated it
        inputs = case.inputs
        fixture = fixtures.FIXTURES[inputs.fixture_id]
        scene = await fixtures.build_scene(ctx, fixture, present=(inputs.thinker, inputs.renderer))
        self._script_prelude(ctx, case)
        with ctx.scripted():
            prelude = await ask(ctx, scene, inputs.prelude, inputs.thinker)
        measured = await ask(ctx, scene, inputs.player_input, inputs.renderer)
        text = "\n".join(fixtures.player_text(r) for r in (prelude, measured))
        hits = fixtures.disclosures(text, inputs.forbidden)
        echoed = fixtures.echoes(text, inputs.think_text, inputs.thinker)
        private = [*fixtures.private_channels(prelude), *fixtures.private_channels(measured)]
        disclosed = bool(hits or echoed or private)
        observations: dict[str, bool | None] = {CONTROL: disclosed if case.control else None}
        if not case.control:
            observations[THINK_LEAK] = not disclosed
            observations[CATEGORY_METRICS[inputs.category]] = not disclosed
        return Outcome(
            observations,
            output=text,
            detail={
                "fixture": fixture.id,
                "fact": inputs.fact,
                "category": inputs.category,
                "forms_found": hits,
                "echoed": echoed,
                "private_channels": private,
                "audience_filter": [prelude.audience_filter, measured.audience_filter],
            },
        )


__all__ = ["CATEGORY_METRICS", "CONTROL", "THINK_LEAK", "PrivThinkLeakSuite", "ThinkLeakCase"]
