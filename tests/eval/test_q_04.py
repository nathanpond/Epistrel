"""Q-04: knowledge-asymmetry correctness, M5 (docs/04 §8; FR-EPI-1,5, NFR-QUAL-1; #179).

The ``q04`` suite asks a character about another character's private fact through
``Engine.respond`` (per-character partitions, perception ``full``, the output read as the
player's character) and checks the player-facing answer against the case's own forbidden forms.
This test checks the labeled set's shape and plays a slice of it (every fixture, every
template, the negative controls) on the stub, two seeds; the full set's zero-tolerance run is
E-08 M5's (``tests/evals/test_e_08.py``) and CI's ``evals-zero-leak`` job."""

from __future__ import annotations

import re
from collections import Counter

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, CaseModel, Verdict, run
from epistrel.evals import fixtures as fx
from epistrel.evals.runner import CaseContext
from epistrel.evals.suites import Outcome
from epistrel.evals.suites.q04_asymmetry import (
    ASYMMETRY,
    CONTROL,
    INJECTED_CASE,
    REPAIRED,
    AsymmetryCase,
    Q04Suite,
    ask,
)
from tests.eval.suite_support import SEEDS, SETS, by_id
from tests.evals.support import stub_engine
from tests.structured_turn_support import schema_calls, text_of

pytestmark = pytest.mark.integration


def _slice(cases: list[AsymmetryCase]) -> list[AsymmetryCase]:
    """Per fixture: one case of each template on two facts, the injected case, and two
    controls."""
    picked: list[AsymmetryCase] = []
    for fixture in fx.FIXTURES:
        mine = [c for c in cases if c.inputs.fixture_id == fixture]
        facts = [f.key for f in fx.FIXTURES[fixture].facts[:2]]
        for fact in facts:
            for template in ("direct", "leading", "gossip"):
                picked.append(
                    next(
                        c
                        for c in mine
                        if not c.control and c.inputs.fact == fact and c.inputs.template == template
                    )
                )
        picked += [c for c in mine if c.control][:2]
    picked += [c for c in cases if c.id == INJECTED_CASE and c not in picked]
    return picked


@pytest.mark.part("M5", "FR-EPI-1,5; NFR-QUAL-1")
async def test_q_04(pg_url: str, engine: AsyncEngine) -> None:
    """The set: at least 300 asymmetry cases, four fixtures x eight private facts x the four
    characters who do not hold the fact x three templates, each asked of a non-holder, with its
    forbidden forms absent from the question and from the stub's clean answer; plus one
    negative control per fact, asked of its holder. On the stub, two seeds, the slice's
    player-facing answers disclose nothing (zero violations), nothing was repaired on the way,
    and every control is detected.

    Complement asserted: the same slice with the stub scripted to leak on one case on the
    second seed (the fact paraphrased, so no protected name is in it) shows exactly one
    violation, on that seed, found by the oracle in the player-facing text."""
    suite = SUITES["q04"]
    assert isinstance(suite, Q04Suite)
    cases = [c for c in suite.load(SETS) if isinstance(c, AsymmetryCase)]
    measured = [c for c in cases if not c.control]
    assert len(measured) == 4 * 8 * 4 * 3 >= 300
    shape = Counter((c.inputs.fixture_id, c.inputs.fact) for c in measured)
    assert set(shape.values()) == {4 * 3}
    for case in measured:
        assert case.inputs.viewpoint != case.inputs.holder
        assert case.inputs.asker == fx.FIXTURES[case.inputs.fixture_id].player.name
        assert fx.disclosures(case.inputs.question, case.inputs.forbidden) == []
        assert fx.disclosures(case.stub_answer, case.inputs.forbidden) == []
    controls = [c for c in cases if c.control]
    assert len(controls) == 32
    assert all(c.inputs.viewpoint == c.inputs.holder for c in controls)

    picked = _slice(cases)
    async with stub_engine(pg_url, engine) as lib:
        clean = await run(suite, AdapterConfig(), SEEDS, engine=lib, cases={suite.name: picked})
    assert clean.harness_errors == []
    metrics = by_id(clean.suites[0])
    asymmetry = metrics[ASYMMETRY]
    assert (asymmetry.violations, asymmetry.sample_size) == (0, sum(not c.control for c in picked))
    # Fewer than 300 cases: the observed-zero rule cannot pass on a slice.
    assert asymmetry.verdict is Verdict.failed
    assert metrics[CONTROL].mean == 1.0
    assert metrics[REPAIRED].mean == 0.0

    injected = next(c for c in picked if c.id == INJECTED_CASE)
    async with stub_engine(pg_url, engine) as lib:
        suite.inject(lib.models.stub, injected, SEEDS[1])
        leaked = await run(suite, AdapterConfig(), SEEDS, engine=lib, cases={suite.name: picked})
    leak = by_id(leaked.suites[0])[ASYMMETRY]
    assert leak.violations == 1
    assert leak.per_seed == [0.0, pytest.approx(1 / asymmetry.sample_size)]
    fact = fx.FIXTURES["family"].fact(injected.inputs.fact)
    assert fx.disclosures(fact.leak, injected.inputs.forbidden) != []


GUESSED_CASES = {
    "asymmetry-nested-0003": r"\bkeys?\b",
    "asymmetry-nested-0007": r"\bkeys?\b",
    "asymmetry-nested-0009": r"\bkeys?\b",
    "asymmetry-nested-0010": r"\bkeys?\b",
    "asymmetry-whisper-0078": r"\bpawn\w*",
}
"""The live runs' flagged answers that guessed the fact (``cbc7d954f0e8``, 2026-10-24 and
2026-10-25-2): asked how George gets in, Dana, Jenni and Bobby answered with a spare key; asked
about Marcus's father's watch, Bobby invented a thief who pawned it. Each maps to a pattern for
the fact's root word."""


class _Contexts(Q04Suite):
    """Q-04 with each character's render prompt recorded: the measured turn's scene built as
    ``Q04Suite.evaluate`` builds it (the fact established, the asked character and the holder
    present, the holder a fixed ``silent`` participant), the asked character rendered first,
    then every other character of the cast in turn, in the same scene."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts: dict[str, str] = {}

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, AsymmetryCase)
        fixture = fx.FIXTURES[case.inputs.fixture_id]
        fact = fixture.fact(case.inputs.fact)
        present = (case.inputs.viewpoint, fact.holder)
        scene = await fx.build_scene(ctx, fixture, [fact], present=present, silent=(fact.holder,))
        stub = ctx.engine.models.stub
        others = [n for n in fixture.names if n != case.inputs.viewpoint]
        for name in (case.inputs.viewpoint, *others):
            stub.calls.clear()
            await ask(ctx, scene, case.inputs.question, name)
            self.contexts[name] = "\n".join(
                text_of(c) for c in schema_calls(stub, "CharacterRender")
            )
        return Outcome({})


@pytest.mark.part("M5", "FR-EPI-1,5; NFR-QUAL-1")
@pytest.mark.parametrize("case_id", sorted(GUESSED_CASES))
async def test_q_04__guessed_cases_context(pg_url: str, engine: AsyncEngine, case_id: str) -> None:
    """The live runs' flagged answers in ``GUESSED_CASES`` are not context leaks: the asked
    character's generation context for the measured turn (per-character partitions, perception
    ``full``, ``core.epistemic.context.build_context`` through ``Engine.respond``) holds none of
    the fact's forms and not even its root word, and neither does any other non-holder
    character's context in that scene. The answers were the model's own guesses from the
    question; the owner ruled them not leaks (2026-10-25, #179).

    Complement asserted: the holder's own context, built the same way, holds the fact, so the
    probe can see it where it is."""
    suite = _Contexts()
    (case,) = [c for c in suite.load(SETS) if c.id == case_id]
    assert isinstance(case, AsymmetryCase)
    async with stub_engine(pg_url, engine) as lib:
        report = await run(
            suite, AdapterConfig(), SEEDS[:1], engine=lib, cases={suite.name: [case]}
        )
    assert report.harness_errors == []
    fixture = fx.FIXTURES[case.inputs.fixture_id]
    holder = fixture.fact(case.inputs.fact).holder
    assert set(suite.contexts) == set(fixture.names)
    root = re.compile(GUESSED_CASES[case_id], re.IGNORECASE)
    for name, context in suite.contexts.items():
        assert context, name
        if name == holder:
            continue
        assert fx.disclosures(context, case.inputs.forbidden) == [], name
        assert root.search(context) is None, name
    assert fx.disclosures(suite.contexts[holder], case.inputs.forbidden) != []
