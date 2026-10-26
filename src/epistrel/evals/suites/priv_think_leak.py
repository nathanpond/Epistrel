"""NFR-QUAL-1's private-thought metric (docs/04 §8, E-08 M5; FR-TURN-3, FR-RESP-12; #179).

Each case plays two turns of a scene of ``evals.fixtures`` through ``Engine.respond``. The
**prelude** is scripted on every run (``CaseContext.scripted``: its calls go to the run's stub,
even on a real model): the player asks the thinker if all is well, and the thinker renders the
case's ``think`` beside a harmless ``say``. The **measured turn** then renders another
character in answer to the player's question. Both turns are read as the player's character
(``player_view = character``), and their player-facing text must not disclose the thought:
none of the fact's forbidden forms of **any** category (``forbidden_forms``: the thought's
words, the fact's names and paraphrases, its narrated inner state; #562), no echo of the
thought's words (``fixtures.echoes``, #152's shingle detector), and no ``think`` or ``latent``
beat at all.

- ``nfr_qual_1.think_leak`` (observed-zero rule, NFR-QUAL-8): a disclosure in the final
  player-facing output of either turn; zero on every seed over at least 300 cases passes,
  reported with the one-sided 95 % upper bound, never as proof.
- ``priv_think_leak.verbatim`` / ``.paraphrase`` / ``.narrated`` (report-only): the same, over
  each category's cases (the category names the forms its cases were written around: the
  thought's words; the private fact's names and paraphrases; narrated inner state). Every case
  is checked against every category's forms, so each row sees a disclosure of any kind.
- ``priv_think_leak.control_detected`` (report-only): the negative controls (the thinker voices
  its own thought: its words, a paraphrase of the fact, or its narrated feeling; 32 of each,
  #575) are detected.

**What the bound covers** (#575): a disclosure is seen when the output holds one of the fact's
listed forms as a phrase, echoes the thought's words, or rewords a form, a feeling or the
thought within one sentence (``rewording``: inflections, word order, possessives, the lemma
table's synonyms and the emotion families). A paraphrase sharing no content word with any
listed form, feeling or thought, or one spread over several sentences, is not seen, so a clean
run's upper bound covers those phrasings only.
- ``priv_think_leak.model_error`` (report-only; ``Suite.model_error_metric``): the case ended in
  a model error. It observes nothing else (an error is not a disclosure); the zero rule is
  judged on the completed cases, and a seed short of 300 of them is insufficient evidence.

The thinker is a fixed ``silent`` participant (``fixtures.build_scene``): it renders the prelude
it is named for, and in the measured turn it is never narrated reacting or escalated to a render
of its own, so a case measures a thought crossing to another character, never the thinker
voicing it (the owner's decision of 2026-10-24).

On the stub both turns answer their authored renders (``stub_prelude``, ``stub_answer``), keyed
on the case and the player's line (``StubAdapter.when``). CI gates on the stub run of the full
set (``--full priv_think_leak``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.evals import fixtures, rewording
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.evals.suites.q04_asymmetry import ask
from epistrel.orchestrator.models.adapter import EVAL_CASE_HEADER
from epistrel.orchestrator.models.registry import GENERATION

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.models.stub import StubAdapter

THINK_LEAK = "nfr_qual_1.think_leak"
CONTROL = "priv_think_leak.control_detected"
MODEL_ERROR = "priv_think_leak.model_error"
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


def forbidden_forms(inputs: ThinkLeakInputs) -> list[str]:
    """Every form a disclosure of the case's thought may take, whatever the case's category
    (#562): the case's own ``forbidden`` and the union of its fact's forms: the thought's words
    (``think``), the fact's names and paraphrases (``forms``) and its narrated inner state
    (``feelings``). A paraphrase in a ``verbatim`` case is as much a disclosure as in a
    ``paraphrase`` one."""
    fact = fixtures.FIXTURES[inputs.fixture_id].fact(inputs.fact)
    return list(dict.fromkeys([*inputs.forbidden, fact.think, *fact.forms, *fact.feelings]))


def oracle(text: str, inputs: ThinkLeakInputs) -> tuple[list[str], bool]:
    """What ``text`` (the player-facing output) discloses of the case's thought: the forbidden
    forms it holds (``forbidden_forms``, every category's, as exact folded phrases), its
    reworded disclosures of the fact, the feeling or the thought (``rewording.reworded``: lemma
    and stem matching within a sentence, #575; named ``fact: ...``, ``feeling: ...`` or
    ``thought: ...``), and whether it echoes the thought's words (#152's shingle detector)."""
    fixture = fixtures.FIXTURES[inputs.fixture_id]
    hits = fixtures.disclosures(text, forbidden_forms(inputs))
    hits += rewording.reworded(text, fixture, fixture.fact(inputs.fact))
    return hits, fixtures.echoes(text, inputs.think_text, inputs.thinker)


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
    guarantees = ("paraphrase_detection",)
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
        Metric(
            MODEL_ERROR,
            Rule.informational,
            direction=Direction.at_most,
            description="the case ended in a model error (reported, not scored as a disclosure)",
        ),
    )
    model_error_metric = MODEL_ERROR
    reading_notes = (
        "The private-thought metric reads both turns of a case as the player's character: the "
        "scripted prelude that plants the thought (its `think` must be filtered out) and the "
        "measured turn, rendered by a character who never held it. A clean run is an observed "
        "count with its one-sided 95% upper bound, never proof that no thought can leak.",
        "The oracle sees a disclosure as one of the fact's listed forms (an exact folded "
        "phrase), an echo of the thought's words, or a rewording within one sentence by lemma "
        "and stem matching (#575): every content word of a multi-word form in any order and "
        "inflection, an emotion word of a listed feeling's family with a word of the fact's "
        "topic, or most of the thought's content words. The bound covers those phrasings only: "
        "a paraphrase that shares no content word with a listed form, feeling or thought, or "
        "spreads over several sentences, is not seen.",
    )

    def __init__(self) -> None:
        self._prelude: set[tuple[str, str]] = set()

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
        scene = await fixtures.build_scene(
            ctx, fixture, present=(inputs.thinker, inputs.renderer), silent=(inputs.thinker,)
        )
        self._script_prelude(ctx, case)
        with ctx.scripted():
            prelude = await ask(ctx, scene, inputs.prelude, inputs.thinker)
        measured = await ask(ctx, scene, inputs.player_input, inputs.renderer)
        text = "\n".join(fixtures.player_text(r) for r in (prelude, measured))
        hits, echoed = oracle(text, inputs)
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


__all__ = [
    "CATEGORY_METRICS",
    "CONTROL",
    "MODEL_ERROR",
    "THINK_LEAK",
    "PrivThinkLeakSuite",
    "ThinkLeakCase",
    "forbidden_forms",
    "oracle",
]
