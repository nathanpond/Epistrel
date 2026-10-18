"""P-08: end-to-end turn latency (docs/04), the M2 part: p50/p95 Lite turn latency and model
calls per turn by purpose on a real model tier. Marked ``live_model``: CI never runs it; the
model matrix tool measures the same rows and publishes them, reported, not gated (#54).

The calls are asserted against each row's design, not the mean (#308): a flagged draft's
regeneration and re-check, or a call the turn deadline cuts, adds calls without the design
changing. Those extra calls are reported with the breakdown (#303) and bounded by each turn's
call cap; ``check_rows`` is the assertion, tested without a model in
``tests/test_model_matrix.py`` and on scripted stub turns in
``tests/integration/test_model_matrix_run.py``."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.tools import model_matrix as mm
from tests.auth_support import auth_settings
from tests.live_support import live_target
from tests.model_support import config_key, restored_model_config

DESIGNS: dict[str, dict[str, int]] = {
    "off": {"generation": 1},
    "minors": {"generation": 1, "guardrail_check": 2},
}
"""Each row's designed calls per turn: one ``generation`` call, plus the seeded minors policy's
input and output checks with the policy on (#57's input gate and #68's output gate; #287)."""


def check_rows(rows: Mapping[str, Sequence[mm.TurnCalls]]) -> dict[str, mm.CallBreakdown]:
    """P-08's calls assertion over the measured turns of each row: every turn keeps its row's
    design (``mm.design_problems``: no check skipped, no draft released unchecked, no call
    outside the design, without a deadline or error), each turn recorded its call cap, so
    the extra calls are bounded by it, and the policy row is designed to make more calls per
    turn than the row without it. Returns each row's breakdown, the extra calls reported."""
    assert set(rows) == set(DESIGNS)
    breakdowns = {}
    for policies, turns in rows.items():
        assert turns, policies
        breakdown = mm.call_breakdown(turns)
        problems = mm.design_problems(turns, DESIGNS[policies])
        report = json.dumps(asdict(breakdown), sort_keys=True)
        assert problems == [], f"{policies}: {problems}; breakdown {report}"
        assert all(turn.max_calls is not None for turn in turns), policies
        assert breakdown.designed_calls_per_turn == DESIGNS[policies]
        breakdowns[policies] = breakdown
    off, minors = (breakdowns[p].designed_calls_per_turn for p in ("off", "minors"))
    assert sum(minors.values()) > sum(off.values())
    return breakdowns


@pytest.mark.live_model
@pytest.mark.part("M2", "NFR-PERF-8")
async def test_p_08(pg_url: str, engine: AsyncEngine) -> None:
    """Over 20 Lite turns after one warm-up turn, for policies off and for the minors policy on:
    every turn is timed, the nearest-rank p50 is at most the p95, and every turn keeps its
    row's design (``DESIGNS``): one generation call, plus the minors policy's input and output
    checks with the policy on. Extra calls (a flagged draft regenerated and re-checked, a call
    the 45 s turn deadline cut) are reported and stay within each turn's call cap (#308).

    Complement asserted: the policy row is designed to make more calls per turn than the row
    without it."""
    target = live_target()
    settings = auth_settings(pg_url, model_adapter="openai", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            await mm.configure(epistrel, target)
            measured = {}
            for policies in mm.POLICY_SETS:
                measured[policies] = await mm.measure_turns(
                    epistrel, mm.RespondDriver(), policies, mm.DEFAULT_TURNS
                )
        finally:
            await epistrel.models.aclose()
    for latencies, _ in measured.values():
        assert len(latencies) == mm.DEFAULT_TURNS
        assert mm.nearest_rank(latencies, 50) <= mm.nearest_rank(latencies, 95)
    breakdowns = check_rows({policies: turns for policies, (_, turns) in measured.items()})
    for row, breakdown in breakdowns.items():
        print(f"P-08 {row}: {json.dumps(asdict(breakdown), sort_keys=True)}")
