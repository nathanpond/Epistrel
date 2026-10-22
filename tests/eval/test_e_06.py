"""E-06: generation conformance (docs/04).

The M2 part (``test_e_06``): Lite output honours the prompt parameters on a real model. Marked
``live_model``: CI never runs it; the model matrix tool runs the same check
(``capability.check_prompt_params``) and records it per tier, reported, not gated (#54).

The M4 part (``test_e_06__structured``, #157): the ``e06`` suite plays structured turns
(``evals/sets/generation_conformance``) on scratch stories against the stub, scripted per case:
valid say/think/do/latent structure, the requested length and temperature-0 repeatability."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites.e06_conformance import (
    CONFORMANCE,
    ConformanceCase,
    E06Suite,
    within_length,
)
from epistrel.orchestrator.models.capability import E06_THRESHOLD, check_prompt_params
from tests.eval.suite_support import SETS, assert_floors_pass, by_id, strict_verdict, stub_run
from tests.live_support import live_layer

STRUCTURED_SAMPLE = 30


@pytest.mark.live_model
@pytest.mark.part("M2", "FR-RESP-3")
async def test_e_06() -> None:
    """For each ``max_tokens`` length the reply's ``tokens_out`` stays within it, and at
    temperature 0 at least 80 % of 10 samples are identical.

    Complement asserted: the lengths differ, so the cap is what bounds each reply."""
    layer = live_layer()
    try:
        result = await check_prompt_params(layer)
    finally:
        await layer.aclose()
    assert len(set(result.lengths)) > 1
    for cap, tokens_out in result.lengths.items():
        assert tokens_out is not None, f"no answer at max_tokens={cap}"
        assert tokens_out <= cap
    assert result.identical / result.samples >= E06_THRESHOLD


class ThinkOnly(E06Suite):
    """E-06 whose stub renders only a ``think`` beat: no say or do, not a valid turn."""

    def stub_answers(self, case: object) -> dict[str, str]:
        return {
            "generation": json.dumps(
                {"beats": [{"kind": "think", "text": "Nothing to say."}], "introduces": []}
            )
        }


@pytest.mark.integration
@pytest.mark.part("M4", "FR-TURN-1")
@pytest.mark.part("M9", "NFR-QUAL-7")
async def test_e_06__structured(pg_url: str, engine: AsyncEngine) -> None:
    """200 labeled structured turns over every length and temperature, 40 of them repeated at
    temperature 0: on a seeded sample of the stub run every released turn is a valid
    say/think/do structure within its requested length, and each repeat gives the same message
    (conformance 100 % on both seeds; the floor passes).

    Complements asserted: a message past 125 % of its token budget is out of length, and a
    render of a lone ``think`` beat fails the conformance floor under the strict rule."""
    suite = SUITES["e06"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ConformanceCase)]
    assert len(cases) >= 200
    assert sum(c.inputs.repeat for c in cases) >= 30
    result = await stub_run(pg_url, engine, suite, sample=STRUCTURED_SAMPLE)
    assert_floors_pass(result)
    assert by_id(result)[CONFORMANCE].mean == 1.0
    repeats = [c for c in cases if c.inputs.repeat][:2]
    repeated = await stub_run(pg_url, engine, suite, cases=repeats)
    assert by_id(repeated)["e06.repeatable"].mean == 1.0

    assert within_length("x" * 4 * 150, "short")
    assert not within_length("x" * (4 * 188 + 4), "short")
    assert not within_length("x" * 4 * 126, 100)
    complement = await stub_run(pg_url, engine, ThinkOnly(), cases=cases[:3])
    assert by_id(complement)["e06.structure_valid"].mean == 0.0
    assert strict_verdict(complement, CONFORMANCE) is Verdict.failed
