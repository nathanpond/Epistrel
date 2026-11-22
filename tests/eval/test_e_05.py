"""E-05: salience detection (docs/04 §8; FR-COMMIT-3, NFR-QUAL-4; #157; NFR-QUAL-4 gates from M11
per ADR-164, and this stub test runs in CI either way). The ``e05`` suite runs
its labeled renders (``evals/sets/salience``) through #146's ``detect_salience`` on the stub,
scripted per case: recall and precision clear their floors, with F-beta (false negatives x3)
reported."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Verdict, run
from epistrel.evals.suites.e05_salience import PRECISION, RECALL, SalienceCase, score
from tests.eval.suite_support import (
    SEEDS,
    SETS,
    assert_floors_pass,
    by_id,
    flipped,
    frozen,
    strict_verdict,
    stub_run,
)
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration


@pytest.mark.part("M4", "FR-COMMIT-3")
@pytest.mark.part("M11", "NFR-QUAL-4")
async def test_e_05(pg_url: str, engine: AsyncEngine) -> None:
    """300 labeled renders, 180 with a salient span: on the stub every labeled span is found
    and every predicted span is a labeled one (recall and precision 100 % on both seeds; both
    floors pass), and texture-only renders get none.

    Complements asserted: a span is a hit when it shares half of the shorter span on the same
    beat, and not on another beat; a copy of the set that labels one salient render as texture
    only fails the precision floor under the strict rule."""
    suite = SUITES["e05"]
    cases = [c for c in suite.load(SETS) if isinstance(c, SalienceCase)]
    assert len(cases) >= 300
    assert sum(1 for c in cases if c.expected) >= 150
    result = await stub_run(pg_url, engine, suite)
    assert_floors_pass(result)
    metrics = by_id(result)
    assert metrics[RECALL].sample_size == sum(len(c.expected) for c in cases)

    label = next(c for c in cases if c.expected).expected[0]
    assert score([label], [(label.beat, label.start, label.end)]) == (1, 0, 1, 0)
    assert score([label], [(label.beat + 1, label.start, label.end)]) == (0, 1, 0, 1)
    target = next(c for c in cases if c.expected)
    wrong = flipped(cases, target.id, expected=[])
    complement = await stub_run(pg_url, engine, frozen(suite, cases), cases=wrong)
    assert strict_verdict(complement, PRECISION) is Verdict.failed


async def _played_bias(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, preset: str
) -> list[tuple[str, dict[str, Any]]]:
    """E-05 on the stub under ``preset`` for one salient case: the ``(bias, routes)`` each
    ``detect_salience`` call was played with."""
    from epistrel.evals.suites import e05_salience
    from epistrel.orchestrator.commit.salience import detect_salience as original

    seen: list[tuple[str, dict[str, Any]]] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((kwargs.get("bias", "balanced"), dict(kwargs.get("routes") or {})))
        return await original(*args, **kwargs)

    monkeypatch.setattr(e05_salience, "detect_salience", spy)
    suite = SUITES["e05"]
    cases = [c for c in suite.load(SETS) if isinstance(c, SalienceCase) and c.expected][:1]
    async with stub_engine(pg_url, engine) as lib:
        report = await run(
            suite,
            AdapterConfig(preset=preset),  # type: ignore[arg-type]
            SEEDS[:1],
            engine=lib,
            sets_dir=SETS,
            cases={suite.name: cases},
        )
    assert report.errors == [], report.errors
    return seen


@pytest.mark.part("M11", "NFR-QUAL-4")
async def test_e_05__m11_plays_the_presets_salience(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#748: E-05 plays salience as the run's preset configures it: Max's cases are read with
    ``salience_bias = recall`` and the route Max's profile gives ``salience_detection``.

    Complement asserted: Full's are read with ``balanced`` (before #748 every preset was)."""
    from epistrel.evals.presets import PRESETS
    from epistrel.orchestrator.models.config import routes_of
    from epistrel.orchestrator.models.registry import SALIENCE_DETECTION

    played = await _played_bias(pg_url, engine, monkeypatch, "max")
    assert played
    want = routes_of(PRESETS["max"].effective()).get(SALIENCE_DETECTION.name)
    assert all(bias == "recall" for bias, _ in played), played
    assert all(routes.get(SALIENCE_DETECTION.name) == want for _, routes in played), played

    full = await _played_bias(pg_url, engine, monkeypatch, "full")
    assert full
    assert all(bias == "balanced" for bias, _ in full), full
