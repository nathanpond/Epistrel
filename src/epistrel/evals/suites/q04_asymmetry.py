"""Q-04: knowledge-asymmetry correctness (docs/04 §8; FR-EPI-1,5, NFR-QUAL-1; #179).

Each case asks one character about another character's private fact, in a scene of
``evals.fixtures`` (every private fact of the fixture established, each its holder's alone),
through ``Engine.respond``: the player's question, the asked character rendered, the output
filtered for the player's character (``player_view = character``). The answer's correct form
requires **not** knowing the fact, so the player-facing text (the composed message and the
``say`` beats the player received) must hold none of the case's forbidden forms
(``fixtures.disclosures``: #148's whole-word folded matcher over the case's own list, never
the pipeline's own detectors). A behavioural backstop to PRIV (#170), never a proof.

- ``nfr_qual_1.asymmetry`` (observed-zero rule, NFR-QUAL-8): a **context-grounded** disclosure
  in the final player-facing output (#540, ADR-205). Zero on every seed over at least 300 cases
  passes, reported as "0 observed in n cases" with the one-sided 95 % upper bound; one
  disclosure fails. A forbidden form in the answer counts only when the asked character's
  generation context held the fact: one of its forms or one of its ``roots`` (the same
  matcher), in any ``generation`` or ``regeneration`` call that rendered that character during
  the measured turn, rejected attempts included, with the player's own question taken out (and
  a root the question names dropped). No recorded call for the character counts as grounded
  (conservative), and a ``think`` or ``latent`` in the player-facing result is always a
  disclosure.
- ``q04.ungrounded_guess`` (report-only, #540): a withheld case whose answer held a forbidden
  form the asked character's context never held, the model's own guess from the question.
  Its denominator is the completed withheld cases.
- ``q04.control_detected`` (report-only): the negative controls (the holder asked, answering
  with the fact) are detected, so a clean run is visibly not a blind oracle.
- ``q04.render_repaired`` (report-only): the turn repaired a render before the output (a leak
  the pipeline caught is not a violation; it is reported here).
- ``q04.render_error_rate`` (report-only, target ≤ 1 %, #528; ``Suite.model_error_metric``):
  a withheld case ended in a model error (a control and a guardrail refusal are left out). It
  observes nothing else (an error is not a disclosure); the zero rule is judged on the
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

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from epistrel.core.tables import events
from epistrel.evals import fixtures
from epistrel.evals.authoring.asymmetry import render
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.orchestrator.lite.schemas import RespondRequest, RespondResult
from epistrel.orchestrator.models.call_trace import CALL_PROMPTS
from epistrel.orchestrator.models.registry import GENERATION, REGENERATION

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.models.stub import StubAdapter

ASYMMETRY = "nfr_qual_1.asymmetry"
CONTROL = "q04.control_detected"
REPAIRED = "q04.render_repaired"
UNGROUNDED = "q04.ungrounded_guess"
MODEL_ERROR = "q04.render_error_rate"
RENDER_ERROR_TARGET = 0.01
"""The owner's target for the row (#528, 2026-11-09): at most 1 % of withheld cases end in a
model error. Reported and checked at the exit run, never a floor."""
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


RENDER_PURPOSES = frozenset({GENERATION.name, REGENERATION.name})


def character_context(
    prompts: Sequence[tuple[str, tuple[Any, ...]]], name: str, question: str
) -> str | None:
    """What ``name``'s render calls held (#540): the messages of every recorded ``generation``
    or ``regeneration`` call whose system prompt renders ``name`` (``## You are <name>``), with
    the player's own question taken out; ``None`` when no such call was recorded."""
    marker = f"## You are {name}\n"
    found = [
        messages
        for purpose, messages in prompts
        if purpose in RENDER_PURPOSES
        and any(m.role == "system" and marker in m.content for m in messages)
    ]
    if not found:
        return None
    text = "\n".join(m.content for messages in found for m in messages)
    return text.replace(question, " ")


def grounded(context: str | None, fact: fixtures.PrivateFact, inputs: AsymmetryInputs) -> bool:
    """Whether ``context`` held the fact (#540): one of the case's forbidden forms or one of the
    fact's roots, by ``fixtures.disclosures``' matcher; a root the question itself names is
    dropped. No context recorded counts as grounded (conservative)."""
    if context is None:
        return True
    roots = [r for r in fact.roots if not fixtures.disclosures(inputs.question, [r])]
    return bool(
        fixtures.disclosures(context, inputs.forbidden) or fixtures.disclosures(context, roots)
    )


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
            UNGROUNDED,
            Rule.informational,
            direction=Direction.at_most,
            description="the answer guessed the fact: a forbidden form the asked character's "
            "context never held (reported, not scored as a disclosure)",
        ),
        Metric(
            MODEL_ERROR,
            Rule.informational,
            direction=Direction.at_most,
            description="a withheld case ended in a model error (reported, not scored as a "
            "disclosure; a guardrail refusal is left out)",
            target=RENDER_ERROR_TARGET,
        ),
    )
    model_error_metric = MODEL_ERROR

    def model_error_counted(self, case: CaseModel) -> bool:
        """#528: the row's denominator is the withheld cases (a control is left out)."""
        return not getattr(case, "control", False)

    reading_notes = (
        "Q-04 is a behavioural backstop to PRIV: it measures the final player-facing output "
        "of a turn whose correct answer requires not knowing another character's private "
        "fact. A clean run is reported as an observed count with its one-sided 95% upper "
        "bound, never as proof that no disclosure can happen; a render the pipeline repaired "
        "before the output is reported (`q04.render_repaired`), not counted.",
    )

    def __init__(self) -> None:
        self._injected: set[tuple[str, int]] = set()

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, AsymmetryCase)  # noqa: S101 - load() validated it
        fixture = fixtures.FIXTURES[case.inputs.fixture_id]
        fact = fixture.fact(case.inputs.fact)
        present = (case.inputs.viewpoint, fact.holder)
        silent = () if case.control else (fact.holder,)
        scene = await fixtures.build_scene(ctx, fixture, [fact], present=present, silent=silent)
        prompts: list[tuple[str, tuple[Any, ...]]] = []
        token = CALL_PROMPTS.set(prompts)
        try:
            result = await ask(ctx, scene, case.inputs.question, case.inputs.viewpoint)
        finally:
            CALL_PROMPTS.reset(token)
        text = fixtures.player_text(result)
        hits = fixtures.disclosures(text, case.inputs.forbidden)
        private = fixtures.private_channels(result)
        is_grounded: bool | None = None
        if hits and not case.control:
            context = character_context(prompts, case.inputs.viewpoint, case.inputs.question)
            injected = (case.id, ctx.seed) in self._injected
            is_grounded = injected or grounded(context, fact, case.inputs)
        disclosed = bool(private or (hits and (case.control or is_grounded)))
        guessed = bool(hits) and is_grounded is False and not private
        return Outcome(
            {
                ASYMMETRY: None if case.control else not disclosed,
                CONTROL: disclosed if case.control else None,
                REPAIRED: not await repaired(ctx, result),
                UNGROUNDED: None if case.control else not guessed,
            },
            output=text,
            detail={
                "fixture": fixture.id,
                "fact": case.inputs.fact,
                "forms_found": hits,
                "grounded": is_grounded,
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
        """Script ``stub`` to leak on ``case`` on ``seed`` only (E-08). The injection stands
        for the fact reaching the asked character's context (#540's planner), so it is scored
        as grounded: it stays a violation."""
        stub.case(case.id, self.leak_answer(case), purpose=GENERATION.name, seed=seed)
        self._injected.add((case.id, seed))


__all__ = [
    "ASYMMETRY",
    "CONTROL",
    "INJECTED_CASE",
    "MODEL_ERROR",
    "RENDER_ERROR_TARGET",
    "REPAIRED",
    "UNGROUNDED",
    "AsymmetryCase",
    "Q04Suite",
    "ask",
    "character_context",
    "grounded",
]
