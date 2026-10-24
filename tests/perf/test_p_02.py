"""P-02: verify latency (docs/04 §11), the M4 part (NFR-PERF-2; #153). The deterministic
``verify`` path, with no model fallback, completes within p95 ≤ 50 ms.

The span of one claim is ``core.verify.rules.check_with`` (#140), as a structured turn runs it.
It covers the claim's normalization against the registry (``canonical``), the keyed fetch
through ``KeyedLookup`` (the subject's and object's ``same_as``, the claim's predicate in its
window, containment, exclusions and implication antecedents, the closed sets), and
``check``'s contradiction rules and coverage. The story's predicate registry is loaded once
beforehand, as a turn loads it once for all of its claims.

``test_p_02`` is the measurement on the scale fixture's canon (50k facts over 200 subjects). It
runs 2,000 claims drawn from the fixture's own plan (``scale.claims``): 40% supported, 30%
contradicted, 20% novel and 10% uncertain, checked one after another after 20 warm-up claims.
It gates the p95, and a miss is measured once more first. The test is ``perf_weekly``, run by
the weekly ``perf.yml`` on the GitHub-hosted runner. Its figures go to ``build/perf/p_02.json``.
``test_p_02__smoke`` is PR CI's ``perf_smoke`` run on the 1k fixture, with no timing bound
(#430, #432).

Both assert the claims' verdicts. Every claim gets the verdict it was built for, so the timed
path is the full deterministic one and not a short cut that answers ``uncertain`` early.

**could_have_known** (the M5 part: #165 AC6, measured by #182). The per-check budget of
FR-EPI-4's replay (``core.epistemic.replay.could_have_known``) is part of P-02's verify span, so it
gets the same p95 ≤ 50 ms on the same canon, over the scale fixture with an exposure-bearing
knowledge layer (``epistrel.testing.fixtures.knowledge``: per non-player character 200 beliefs
and 50 unresolved exposures). ``test_p_02__could_have_known`` runs 400 checks after 20 warm-up
ones, a quarter of each path (``knowledge.PROBE_KINDS``): a belief licenses it, canon the
character may read does, an unresolved exposure's dry run does, or no record does (every path
is read). The story's predicate registry and its settings with partitions on are loaded once,
as a turn loads them. It gates the p95, a miss measured once more first (``perf_weekly``;
``build/perf/p_02_could_have_known.json``). ``test_p_02__could_have_known_smoke`` is the
``perf_smoke`` run on the 1k fixture, with no timing bound. Both assert each check found the
path it was built for.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import pytest

from epistrel.core.epistemic import replay
from epistrel.core.propositions import registry as predicate_registry
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.verify import rules
from epistrel.testing.fixtures import knowledge, scale
from tests.perf.scale_support import (
    KNOWLEDGE,
    ScaleRun,
    gate,
    host_info,
    percentiles,
    record,
    server_info,
)

CLAIMS = 2_000
WARMUP = 20
SMOKE_CLAIMS = 200
BUDGET_MS = 50.0
"""NFR-PERF-2."""
CHECKS = 400
SMOKE_CHECKS = 40
EXPECTED: dict[str, tuple[str, str | None]] = {
    "believed": (replay.KNOWN, "transition"),
    "canon": (replay.KNOWN, "canon"),
    "exposed": (replay.KNOWN, "exposure"),
    "no_path": (replay.NO_PATH, None),
}
"""Each probe kind's answer and path kind."""


async def _measure(
    run: ScaleRun, n: int, *, warmup: int
) -> tuple[list[float], Counter[tuple[str, str]]]:
    """Check ``n`` claims (after ``warmup``): each one's milliseconds, and how often each
    intended kind got each verdict."""
    story = run.fixture.story
    built = scale.claims(run.fixture.spec, story, n)
    timings: list[float] = []
    outcomes: Counter[tuple[str, str]] = Counter()
    async with run.db.connect() as conn:
        registry = await predicate_registry.load(conn, story.story_id)
        for _, claim in built[:warmup]:
            await rules.check_with(
                KeyedLookup(conn, StructuredReadSet()), story.story_id, claim, registry=registry
            )
        for intended, claim in built:
            lookup = KeyedLookup(conn, StructuredReadSet())
            started = time.perf_counter()
            verdict = await rules.check_with(lookup, story.story_id, claim, registry=registry)
            timings.append((time.perf_counter() - started) * 1000.0)
            outcomes[(intended, verdict.kind.value)] += 1
    return timings, outcomes


async def _summary(
    run: ScaleRun, timings: list[float], outcomes: Counter[tuple[str, str]], attempts: list[float]
) -> dict[str, Any]:
    return {
        "test": "P-02",
        "requirement": "NFR-PERF-2",
        "budget_p95_ms": BUDGET_MS,
        "claims": len(timings),
        "facts": run.fixture.story.facts,
        "check_ms": percentiles(timings),
        "verdicts": {f"{i}->{v}": n for (i, v), n in sorted(outcomes.items())},
        "attempt_p95_ms": [round(a, 3) for a in attempts],
        "host": host_info(await server_info(run.db)),
    }


def _verdicts_as_built(outcomes: Counter[tuple[str, str]], n: int) -> None:
    """Every claim got the verdict it was built for, and every kind of the mix occurred."""
    wrong = {k: v for k, v in outcomes.items() if k[0] != k[1]}
    assert wrong == {}, wrong
    assert sum(outcomes.values()) == n
    assert {i for i, _ in outcomes} == {kind for kind, _ in scale.CLAIM_MIX}


@pytest.mark.perf_weekly
@pytest.mark.part("M4", "NFR-PERF-2")
async def test_p_02(scale_50k: ScaleRun) -> None:
    """2,000 deterministic checks over 50k facts of canon: p95 within 50 ms (a miss is measured
    once more first).

    Complement asserted: each claim got the verdict it was built for (a contradicted claim is
    never supported, a novel one never contradicted), with all four kinds present."""
    timings, outcomes = await _measure(scale_50k, CLAIMS, warmup=WARMUP)
    attempts = [percentiles(timings)["p95"]]
    if gate(attempts[0], BUDGET_MS):
        timings, outcomes = await _measure(scale_50k, CLAIMS, warmup=0)
        attempts.append(percentiles(timings)["p95"])
    summary = await _summary(scale_50k, timings, outcomes, attempts)
    record("p_02", summary)
    _verdicts_as_built(outcomes, CLAIMS)
    assert gate(attempts[-1], BUDGET_MS) == [], summary


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-PERF-2")
async def test_p_02__smoke(scale_1k: ScaleRun) -> None:
    """PR CI's subset: 200 checks over the 1k fixture's canon through the same measurement and
    summary, with no timing bound.

    Complement asserted: each claim got the verdict it was built for, with all four kinds
    present, and nearest-rank p50 ≤ p95 ≤ p99 ≤ max."""
    timings, outcomes = await _measure(scale_1k, SMOKE_CLAIMS, warmup=5)
    _verdicts_as_built(outcomes, SMOKE_CLAIMS)
    summary = await _summary(scale_1k, timings, outcomes, [percentiles(timings)["p95"]])
    record("p_02_smoke", summary)
    found = summary["check_ms"]
    assert found["p50"] <= found["p95"] <= found["p99"] <= found["max"]


async def _measure_known(
    run: ScaleRun, n: int, *, warmup: int
) -> tuple[list[float], dict[str, list[float]], Counter[tuple[str, str, str | None]]]:
    """``n`` could-have-known checks (after ``warmup``): each one's milliseconds, by probe kind
    too, and how often each kind got each answer and path."""
    fixture = run.fixture
    story = fixture.story
    probes = knowledge.probes(fixture.spec, story, KNOWLEDGE, n)
    timings: list[float] = []
    by_kind: dict[str, list[float]] = {}
    outcomes: Counter[tuple[str, str, str | None]] = Counter()
    async with run.db.connect() as conn:
        registry = await predicate_registry.load(conn, story.story_id)
        settings = await knowledge.partitioned(conn, story.story_id)

        async def check(p: knowledge.Probe) -> replay.Answer:
            return await replay.could_have_known(
                conn,
                story.story_id,
                p.holder,
                p.proposition,
                p.at,
                settings=settings,
                registry=registry,
            )

        for p in probes[:warmup]:
            await check(p)
        for p in probes:
            started = time.perf_counter()
            answer = await check(p)
            ms = (time.perf_counter() - started) * 1000.0
            timings.append(ms)
            by_kind.setdefault(p.kind, []).append(ms)
            path = getattr(answer, "path", None)
            outcomes[(p.kind, replay.outcome_of(answer), getattr(path, "kind", None))] += 1
    return timings, by_kind, outcomes


async def _known_summary(
    run: ScaleRun,
    timings: list[float],
    by_kind: dict[str, list[float]],
    outcomes: Counter[tuple[str, str, str | None]],
    attempts: list[float],
) -> dict[str, Any]:
    return {
        "test": "P-02 could_have_known",
        "requirement": "NFR-PERF-2",
        "budget_p95_ms": BUDGET_MS,
        "checks": len(timings),
        "facts": run.fixture.story.facts,
        "knowledge": {"beliefs": KNOWLEDGE.beliefs, "exposures": KNOWLEDGE.exposures},
        "check_ms": percentiles(timings),
        "by_kind_ms": {kind: percentiles(v) for kind, v in sorted(by_kind.items())},
        "answers": {f"{k}->{a}:{p}": n for (k, a, p), n in sorted(outcomes.items(), key=str)},
        "attempt_p95_ms": [round(a, 3) for a in attempts],
        "host": host_info(await server_info(run.db)),
    }


def _paths_as_built(outcomes: Counter[tuple[str, str, str | None]], n: int) -> None:
    """Every check found the path it was built for, and every kind occurred."""
    wrong = {k: v for k, v in outcomes.items() if EXPECTED[k[0]] != (k[1], k[2])}
    assert wrong == {}, wrong
    assert sum(outcomes.values()) == n
    assert {k for k, _, _ in outcomes} == set(knowledge.PROBE_KINDS)


@pytest.mark.perf_weekly
@pytest.mark.part("M5", "NFR-PERF-2")
async def test_p_02__could_have_known(scale_50k_knowledge: ScaleRun) -> None:
    """400 could-have-known checks over the 50k fixture's canon and its characters' beliefs and
    exposures: p95 within 50 ms (a miss is measured once more first).

    Complement asserted: each check found the path it was built for (a believed proposition by
    its transition, readable canon as canon, an exposure by its dry run, and an uncovered one
    no path at all), with all four kinds present."""
    run = scale_50k_knowledge
    timings, by_kind, outcomes = await _measure_known(run, CHECKS, warmup=WARMUP)
    attempts = [percentiles(timings)["p95"]]
    if gate(attempts[0], BUDGET_MS):
        timings, by_kind, outcomes = await _measure_known(run, CHECKS, warmup=0)
        attempts.append(percentiles(timings)["p95"])
    summary = await _known_summary(run, timings, by_kind, outcomes, attempts)
    record("p_02_could_have_known", summary)
    _paths_as_built(outcomes, CHECKS)
    assert gate(attempts[-1], BUDGET_MS) == [], summary


@pytest.mark.perf_smoke
@pytest.mark.part("M5", "NFR-PERF-2")
async def test_p_02__could_have_known_smoke(scale_1k_knowledge: ScaleRun) -> None:
    """PR CI's subset: 40 could-have-known checks over the 1k fixture with its knowledge layer,
    through the same measurement and summary, with no timing bound.

    Complement asserted: each check found the path it was built for, with all four kinds
    present, and nearest-rank p50 ≤ p95 ≤ p99 ≤ max."""
    run = scale_1k_knowledge
    timings, by_kind, outcomes = await _measure_known(run, SMOKE_CHECKS, warmup=4)
    _paths_as_built(outcomes, SMOKE_CHECKS)
    summary = await _known_summary(run, timings, by_kind, outcomes, [percentiles(timings)["p95"]])
    record("p_02_could_have_known_smoke", summary)
    found = summary["check_ms"]
    assert found["p50"] <= found["p95"] <= found["p99"] <= found["max"]
