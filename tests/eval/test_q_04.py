"""Q-04: knowledge-asymmetry correctness, M5 (docs/04 §8; FR-EPI-1,5, NFR-QUAL-1; #179).

The ``q04`` suite asks a character about another character's private fact through
``Engine.respond`` (per-character partitions, perception ``full``, the output read as the
player's character) and checks the player-facing answer against the case's own forbidden forms.
This test checks the labeled set's shape and plays a slice of it (every fixture, every
template, the negative controls) on the stub, two seeds; the full set's zero-tolerance run is
E-08 M5's (``tests/evals/test_e_08.py``) and CI's ``evals-zero-leak`` job."""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Verdict, run
from epistrel.evals import fixtures as fx
from epistrel.evals.suites.q04_asymmetry import (
    ASYMMETRY,
    CONTROL,
    INJECTED_CASE,
    REPAIRED,
    AsymmetryCase,
    Q04Suite,
)
from tests.eval.suite_support import SEEDS, SETS, by_id
from tests.evals.support import stub_engine

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
