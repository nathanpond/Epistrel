"""Q-06: relationship recall (docs/04 §9; FR-RET-6; #913), informational.

The ``q06`` suite asks the innkeeper Maren about a relationship in
``evals.relationship_world``'s harbour town through ``Engine.respond`` and scores her
player-facing answer as correct, don't-know, wrong or unclear. These tests check the labeled
set's shape and the scorer, play a slice of the set on the stub, two seeds, with every outcome
scripted, and show that the set's kinds split ``memory.episodic.profile_scope`` as labeled: on
Maren's render context, the asked person's standing-profile line carries an undescribed
case's relationship only under ``--dial memory.episodic.profile_scope=full``, and every
control's under either setting."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, CaseModel, run
from epistrel.evals import fixtures as fx
from epistrel.evals import relationship_world as world
from epistrel.evals.authoring.asymmetry import render
from epistrel.evals.authoring.relationships import SELF_FORMS
from epistrel.evals.runner import CaseContext
from epistrel.evals.suites import Outcome
from epistrel.evals.suites.q04_asymmetry import ask
from epistrel.evals.suites.q06_relationships import (
    GROUPS,
    MODEL_ERROR,
    UNCLEAR,
    Q06Suite,
    RelationshipCase,
    metric_id,
    score,
)
from epistrel.orchestrator.models.registry import GENERATION
from tests.eval.suite_support import SEEDS, SETS, by_id
from tests.evals.support import stub_engine
from tests.structured_turn_support import schema_calls, text_of

pytestmark = pytest.mark.integration

FULL = {"memory": {"episodic": {"profile_scope": "full"}}}
SHAPE = {
    ("undescribed", "shallow"): 11,
    ("undescribed", "deep"): 11,
    ("described", "deep"): 13,
    ("named", "deep"): 8,
    ("speaker", "deep"): 3,
}


def _cases() -> list[RelationshipCase]:
    suite = SUITES["q06"]
    assert isinstance(suite, Q06Suite)
    return [c for c in suite.load(SETS) if isinstance(c, RelationshipCase)]


def _slice(cases: list[RelationshipCase]) -> list[RelationshipCase]:
    """The first case of each kind at each depth."""
    picked: dict[tuple[str, str], RelationshipCase] = {}
    for case in cases:
        picked.setdefault((case.inputs.kind, case.inputs.depth), case)
    return list(picked.values())


def _names(case: RelationshipCase) -> list[str]:
    return [e for e in case.inputs.expected if e not in SELF_FORMS]


@pytest.mark.part("M10", "FR-RET-6")
async def test_q_06(pg_url: str, engine: AsyncEngine) -> None:
    """The set: 46 cases (``SHAPE``), each addressed to Maren, none naming its own answer (a
    named case names it beside its distractor), and each stub answer scored correct. On the
    stub, two seeds, a slice of every kind at each depth answers correctly on every seed: each
    group's ``correct`` row is 1 and its ``dont_know`` and ``wrong`` rows 0, nothing is
    unclear and no model error.

    Complement asserted: the same slice with the stub scripted, on the second seed, to answer
    one case "I couldn't tell you", one with its distractor's name and one with neither,
    scores them don't-know, wrong and unclear, on that seed only."""
    cases = _cases()
    assert Counter((c.inputs.kind, c.inputs.depth) for c in cases) == SHAPE
    for case in cases:
        assert case.inputs.question.startswith(f"{world.SPEAKER}, ")
        own = [n for n in _names(case) if n != world.SPEAKER]
        hits = fx.disclosures(case.inputs.question, own)
        assert hits == ([own[0].casefold()] if case.inputs.kind == "named" else []), case.id
        assert score(case.inputs, case.stub_answer)[0] == "correct", case.id

    picked = _slice(cases)
    suite = SUITES["q06"]
    async with stub_engine(pg_url, engine) as lib:
        clean = await run(suite, AdapterConfig(), SEEDS, engine=lib, cases={suite.name: picked})
    assert clean.harness_errors == []
    metrics = by_id(clean.suites[0])
    for group in GROUPS:
        assert metrics[metric_id(group, "correct")].mean == 1.0, group
        assert metrics[metric_id(group, "dont_know")].mean == 0.0, group
        assert metrics[metric_id(group, "wrong")].mean == 0.0, group
    assert metrics[UNCLEAR].mean == 0.0
    assert metrics[MODEL_ERROR].mean == 0.0

    undescribed, named, speaker = (
        next(c for c in picked if c.inputs.kind == kind)
        for kind in ("undescribed", "named", "speaker")
    )
    answers = {
        undescribed.id: render("I couldn't tell you, love."),
        named.id: render(f"That'd be {named.inputs.distractor}, surely."),
        speaker.id: render("Now there's a question for a quiet evening."),
    }
    async with stub_engine(pg_url, engine) as lib:
        for case_id, answer in answers.items():
            lib.models.stub.case(case_id, answer, purpose=GENERATION.name, seed=SEEDS[1])
        scripted = await run(suite, AdapterConfig(), SEEDS, engine=lib, cases={suite.name: picked})
    assert scripted.harness_errors == []
    metrics = by_id(scripted.suites[0])
    group = f"undescribed_{undescribed.inputs.depth}"
    assert metrics[metric_id(group, "dont_know")].per_seed == [0.0, 1.0]
    assert metrics[metric_id("named", "wrong")].per_seed == [0.0, 1.0]
    assert metrics[metric_id("speaker", "correct")].per_seed == [1.0, 0.0]
    assert metrics[UNCLEAR].per_seed == [0.0, pytest.approx(1 / len(picked))]


def test_q_06__scoring() -> None:
    """``score`` in its order: an expected name is correct (even inside a hedge), a don't-know
    phrase without one is don't-know, a speaker case's self-reference is correct, another cast
    name is wrong, and anything else unclear; a named case naming both its answer and its
    distractor is unclear (the scorer reads no negation)."""
    cases = {c.id: c for c in _cases()}
    undescribed = cases["relationships-undescribed-shallow-0001"].inputs
    named = cases["relationships-named-deep-0001"].inputs
    speaker = cases["relationships-speaker-deep-0001"].inputs
    assert undescribed.question == "Maren, who is Bobby married to?"
    assert score(undescribed, "Carol, I think, though I'm not sure.")[0] == "correct"
    assert score(undescribed, "No idea, dear.")[0] == "dont_know"
    assert score(undescribed, "Bobby? He's wed to Ines.")[0] == "wrong"
    assert score(undescribed, "Bobby keeps to himself.")[0] == "unclear"
    assert score(named, "Carol, of course.")[0] == "correct"
    assert score(named, f"{named.distractor}, of course.")[0] == "wrong"
    assert score(named, f"Carol, not {named.distractor}.")[0] == "unclear"
    assert score(speaker, "That's me.")[0] == "correct"
    assert score(speaker, "Beats me.")[0] == "dont_know"
    assert score(speaker, "Ask Hugo.")[0] == "wrong"


class _Contexts(Q06Suite):
    """Q-06 with Maren's render prompt recorded for each case, by case id."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts: dict[str, str] = {}

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, RelationshipCase)
        deep = (case.inputs.about,) if case.inputs.depth == "deep" else ()
        scene = await world.build_scene(ctx, deep=deep)
        stub = ctx.engine.models.stub
        stub.calls.clear()
        await ask(ctx, scene, case.inputs.question, world.SPEAKER)
        prompts = [text_of(c) for c in schema_calls(stub, "CharacterRender")]
        self.contexts[case.id] = "\n".join(prompts)
        return Outcome({})


def _profile_line(context: str, name: str) -> str:
    """The render context's standing-profile line of ``name`` (``pipeline.profile_text``)."""
    lines = [line for line in context.splitlines() if line.startswith(f"- {name}: ")]
    assert len(lines) == 1, (name, lines)
    return lines[0]


@pytest.mark.part("M10", "FR-RET-6")
async def test_q_06__splits_the_profile_scope(pg_url: str, engine: AsyncEngine) -> None:
    """Run with ``dials`` (``run --dial memory.episodic.profile_scope=full``), the asked
    person's standing-profile line in Maren's render context names the answer, for every deep
    undescribed case and a slice of each control kind. Run as the preset is (``message``, the
    default), each described, named and speaker case's line still names it: the resolve stage
    resolves the relation noun, the question names both people, the answer is the speaker.

    Complement asserted: under ``message`` no undescribed case's line names its answer, the
    relationship the set labels as the one the scope leaves out. (It may still reach her
    through the fact's semantic memory; Q-06's live runs measure whether the answer does.)"""
    deep = [c for c in _cases() if c.inputs.depth == "deep"]
    picked = [c for c in deep if c.inputs.kind == "undescribed"]
    for kind, n in (("described", 2), ("named", 1), ("speaker", 1)):
        picked += [c for c in deep if c.inputs.kind == kind][:n]

    async def contexts(dials: Mapping[str, Any] | None) -> dict[str, str]:
        suite = _Contexts()
        async with stub_engine(pg_url, engine) as lib:
            report = await run(
                suite,
                AdapterConfig(),
                SEEDS[:1],
                engine=lib,
                cases={suite.name: picked},
                dials=dials,
            )
        assert report.harness_errors == []
        return suite.contexts

    full, scoped = await contexts(FULL), await contexts(None)
    for case in picked:
        whole = _profile_line(full[case.id], case.inputs.about)
        narrow = _profile_line(scoped[case.id], case.inputs.about)
        assert fx.disclosures(whole, _names(case)), (case.id, whole)
        if case.inputs.kind == "undescribed":
            assert fx.disclosures(narrow, _names(case)) == [], (case.id, narrow)
        else:
            assert fx.disclosures(narrow, _names(case)), (case.id, narrow)
