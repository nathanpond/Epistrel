"""Q-06: relationship recall (docs/04 §9; FR-RET-6; #913). Informational: every metric is
report-only, with no floor, and the suite never gates CI or a release.

Each case asks the innkeeper Maren about one person's relationship in
``evals.relationship_world``'s harbour town, through ``Engine.respond``: the player's
question, Maren rendered, the output read as the player's character. It measures what a
render answers from the relationships retrieval gives it, and so what ``memory.episodic.
profile_scope`` changes (#905, ADR-193): run it with and without ``run --dial
memory.episodic.profile_scope=full`` and compare.

A relationship reaches the render by three routes, and the setting governs one of them:

- the asked person's **standing profile** (FR-RET-6): under ``message`` (the default) it keeps
  only the relationships among the message's people (the resolved entities, the located
  episodes' participants and the speaker); under ``full``, every one;
- the **canon arm**: the asked person's current canon as a subject, at most
  ``CANON_PER_ENTITY`` (20) facts. A **deep** case's asked person first knows 24 townsfolk, so
  the arm is full of those and carries no relationship; in a **shallow** case it may
  (``canon_carries`` in each case's detail);
- the fact's own **semantic memory** (an author fact is one), which the alias and lexical arms
  find by the person's name and the question's words, under either setting.

An **undescribed** case's answer is neither named nor described by a relation noun, so the
``message`` profile leaves its relationship out. The controls keep it under both settings: a
**described** case names the answer by a relation noun ("Bobby's spouse"), which the resolve
stage resolves to the person, a **named** case names both people, and a **speaker** case's
answer is Maren, the render's speaker.

**Scoring** (deterministic, on the player-facing text, ``fixtures.player_text``, with #148's
whole-word folded matcher), in order:

1. an expected name (``expected`` without the self-references and the speaker's own name, which
   every composed message carries) is present → **correct**; for
   a named case, when the distractor is present too, **unclear** (the scorer reads no
   negation);
2. a "don't know" phrase (``DONT_KNOW``) → **don't know**;
3. a speaker case's self-reference (``me``, ``myself``) → **correct**;
4. another cast member's name (the question's own names left out) → **wrong**;
5. otherwise **unclear**: an answer the scorer cannot place, reported, never judged by a model.

Per kind (``undescribed`` at each depth, ``described``, ``named``, ``speaker``) the suite
reports the share of its
cases that were correct, don't-know and wrong; ``q06.unclear`` over every case; and
``q06.model_error``. The stub answers every case with its authored correct render.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.evals import fixtures
from epistrel.evals import relationship_world as world
from epistrel.evals.authoring.relationships import SELF_FORMS
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.evals.suites.q04_asymmetry import ask
from epistrel.orchestrator.models.registry import GENERATION

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

Kind = Literal["undescribed", "described", "named", "speaker"]
Depth = Literal["shallow", "deep"]
Scored = Literal["correct", "dont_know", "wrong", "unclear"]

DONT_KNOW: tuple[str, ...] = (
    "don't know",
    "do not know",
    "dunno",
    "no idea",
    "not sure",
    "not certain",
    "couldn't say",
    "can't say",
    "couldn't tell you",
    "can't tell you",
    "wouldn't know",
    "no clue",
    "haven't a clue",
    "beats me",
    "can't recall",
    "don't recall",
    "can't remember",
    "don't remember",
)
"""Phrases of a render that does not give an answer (folded whole-word match)."""

GROUPS: tuple[str, ...] = (
    "undescribed_shallow",
    "undescribed_deep",
    "described",
    "named",
    "speaker",
)
OUTCOMES: tuple[str, ...] = ("correct", "dont_know", "wrong")
UNCLEAR = "q06.unclear"
MODEL_ERROR = "q06.model_error"


def metric_id(group: str, outcome: str) -> str:
    return f"q06.{group}.{outcome}"


class RelationshipInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Kind
    depth: Depth
    about: str
    predicate: str
    question: str = Field(min_length=1, max_length=500)
    expected: list[str] = Field(min_length=1)
    distractor: str | None = None


class RelationshipCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: RelationshipInputs
    expected: Literal["correct"]
    stub_answer: str

    @property
    def group(self) -> str:
        kind = self.inputs.kind
        return f"{kind}_{self.inputs.depth}" if kind == "undescribed" else kind


def score(inputs: RelationshipInputs, text: str) -> tuple[Scored, list[str]]:
    """The answer's outcome and the forms that decided it (module docstring's order)."""
    # The speaker's own name is in every composed message ("Maren: …"): a speaker case counts
    # only her self-references.
    names = [e for e in inputs.expected if e not in SELF_FORMS and e != world.SPEAKER]
    hits = fixtures.disclosures(text, names)
    if hits:
        if inputs.distractor is not None and fixtures.disclosures(text, [inputs.distractor]):
            return "unclear", [*hits, inputs.distractor.casefold()]
        return "correct", hits
    unsure = fixtures.disclosures(text, DONT_KNOW)
    if unsure:
        return "dont_know", unsure
    if inputs.kind == "speaker":
        own = fixtures.disclosures(text, [e for e in inputs.expected if e in SELF_FORMS])
        if own:
            return "correct", own
    left_out = {world.SPEAKER, world.PLAYER.name, inputs.about}
    others = [n for n in world.names() if n not in left_out and n not in inputs.expected]
    wrong = fixtures.disclosures(text, others)
    if wrong:
        return "wrong", wrong
    return "unclear", []


def _metrics() -> tuple[Metric, ...]:
    described = {
        "correct": "the answer named the relationship's other person",
        "dont_know": "the answer said it did not know",
        "wrong": "the answer named someone else",
    }
    labels = {
        "undescribed_shallow": "answer neither named nor described, shallow story",
        "undescribed_deep": "answer neither named nor described, deep story (no canon route)",
        "described": "answer described by a relation noun (control)",
        "named": "both people named (control)",
        "speaker": "the answer is the speaker (control)",
    }
    found = [
        Metric(
            metric_id(group, outcome),
            Rule.informational,
            direction=Direction.at_least if outcome == "correct" else Direction.at_most,
            description=f"{labels[group]}: {described[outcome]}",
        )
        for group in GROUPS
        for outcome in OUTCOMES
    ]
    found += [
        Metric(
            UNCLEAR,
            Rule.informational,
            direction=Direction.at_most,
            description="the deterministic scorer could not place the answer (no model judges it)",
        ),
        Metric(
            MODEL_ERROR,
            Rule.informational,
            direction=Direction.at_most,
            description="the case ended in a model error",
        ),
    ]
    return tuple(found)


@register
class Q06Suite(Suite):
    name = "q06"
    title = "Q-06: relationship recall (FR-RET-6; informational)"
    set_path = "relationships/cases.jsonl"
    min_cases = 30
    case_model = RelationshipCase
    metrics = _metrics()
    model_error_metric = MODEL_ERROR
    reading_notes = (
        "Q-06 is informational: no floor, never a gate. It compares a render's answers about "
        "relationships across `memory.episodic.profile_scope` runs (`run --dial "
        "memory.episodic.profile_scope=full` against the default `message`); read the "
        "`undescribed_deep` rows first, whose relationship a `message`-scoped profile leaves "
        "out and the canon arm cannot carry (the fact's own semantic memory still may). "
        "`described`, `named` and `speaker` are controls both settings keep in scope. Scoring is "
        "deterministic (an expected name, a don't-know phrase, another cast name); "
        "`q06.unclear` counts the answers it could not place.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, RelationshipCase)  # noqa: S101 - load() validated it
        inputs = case.inputs
        deep = (inputs.about,) if inputs.depth == "deep" else ()
        scene = await world.build_scene(ctx, deep=deep)
        result = await ask(ctx, scene, inputs.question, world.SPEAKER)
        text = fixtures.player_text(result)
        outcome, forms = score(inputs, text)
        # An ``at_most`` row reports the share of its bad observations: good is "not this one".
        observations: dict[str, bool | None] = {
            metric_id(case.group, o): (outcome == o) if o == "correct" else (outcome != o)
            for o in OUTCOMES
        }
        observations[UNCLEAR] = outcome != "unclear"
        answer = next((e for e in inputs.expected if e not in SELF_FORMS), inputs.expected[0])
        return Outcome(
            observations,
            output=text,
            detail={
                "kind": inputs.kind,
                "depth": inputs.depth,
                "outcome": outcome,
                "forms_found": forms,
                "canon_carries": inputs.depth == "shallow"
                and answer != world.SPEAKER
                and world.canon_carries(scene.entities, inputs.about, answer),
                "degradation": result.degradation,
            },
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, RelationshipCase)  # noqa: S101 - load() validated it
        return {GENERATION.name: case.stub_answer}


__all__ = [
    "DONT_KNOW",
    "GROUPS",
    "MODEL_ERROR",
    "OUTCOMES",
    "UNCLEAR",
    "Q06Suite",
    "RelationshipCase",
    "RelationshipInputs",
    "metric_id",
    "score",
]
