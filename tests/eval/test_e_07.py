"""E-07: structured-output robustness (docs/04 §8; FR-MODEL-6,7, NFR-QUAL-5; #157). The ``e07``
suite plays structured turns (``evals/sets/structured_output``) on the stub with garbage
injected into some renders: the retry recovers a render garbled once, a render garbled on every
attempt fails the turn through the degradation path, and nothing garbled is ever released."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites.e07_structured_output import (
    FIRST_ATTEMPT,
    NO_SILENT,
    VALID_AFTER_RETRY,
    E07Suite,
    Injection,
    OutputCase,
)
from tests.eval.suite_support import SETS, assert_floors_pass, by_id, strict_verdict, stub_run

pytestmark = pytest.mark.integration


@pytest.mark.part("M4", "FR-MODEL-6,7; NFR-QUAL-5")
async def test_e_07(pg_url: str, engine: AsyncEngine) -> None:
    """200 labeled turns, 10 % with injected garbage: on the stub every turn not garbled on
    every attempt is released with a valid render (valid after retry 100 % on both seeds; the
    floor passes), every plain turn's render is valid on its first attempt, and every turn
    garbled on every attempt fails with ``invalid_structure`` — never a silent acceptance.

    Complement asserted: a plain turn whose render is garbled on every attempt (the label
    still says plain) is not released and fails the floor under the strict rule."""
    suite = SUITES["e07"]
    cases = [c for c in suite.load(SETS) if isinstance(c, OutputCase)]
    assert len(cases) >= 200
    injected = [c for c in cases if c.inputs.inject != "none"]
    assert len(injected) / len(cases) == pytest.approx(0.1)
    chosen = [*[c for c in cases if c.inputs.inject == "none"][:16], *injected]
    result = await stub_run(pg_url, engine, suite, cases=chosen)
    assert_floors_pass(result)
    metrics = by_id(result)
    always = sum(c.inputs.inject == "always" for c in chosen)
    assert metrics[VALID_AFTER_RETRY].sample_size == len(chosen) - always
    assert (metrics[NO_SILENT].sample_size, metrics[NO_SILENT].mean) == (always, 1.0)
    assert metrics[FIRST_ATTEMPT].mean == 1.0

    plain = next(c for c in chosen if c.inputs.inject == "none")
    complement = await stub_run(pg_url, engine, Garbling(plain.id), cases=chosen)
    assert by_id(complement)[VALID_AFTER_RETRY].mean == pytest.approx(
        1 - 1 / (len(chosen) - always)
    )
    assert strict_verdict(complement, VALID_AFTER_RETRY) is Verdict.failed


class Garbling(E07Suite):
    """E-07 that garbles every render attempt of one case its label calls plain."""

    def __init__(self, target: str) -> None:
        super().__init__()
        self.target = target

    def injection(self, case: OutputCase) -> Injection:
        return "always" if case.id == self.target else super().injection(case)
