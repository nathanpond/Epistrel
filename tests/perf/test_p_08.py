"""P-08: end-to-end turn latency (docs/04), the M2 part: p50/p95 Lite turn latency and model
calls per turn by purpose on a real model tier. Marked ``live_model``: CI never runs it; the
model matrix tool measures the same rows and publishes them, reported, not gated (#54)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.tools import model_matrix as mm
from tests.auth_support import auth_settings
from tests.live_support import live_target
from tests.model_support import config_key, restored_model_config


@pytest.mark.live_model
@pytest.mark.part("M2", "NFR-PERF-8")
async def test_p_08(pg_url: str, engine: AsyncEngine) -> None:
    """Over 20 Lite turns after one warm-up turn, for policies off and for the minors policy on:
    every turn is timed, the nearest-rank p50 is at most the p95, and the calls per turn by
    purpose are one generation call, plus the seeded minors policy's input and output checks with
    the policy on (two ``guardrail_check`` calls: #57's input gate and #68's output gate; #287).

    Complement asserted: the policy row makes more calls per turn than the row without it."""
    target = live_target()
    settings = auth_settings(pg_url, model_adapter="openai", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            await mm.configure(epistrel, target)
            measured = {}
            for policies in mm.POLICY_SETS:
                latencies, logs = await mm.measure_turns(
                    epistrel, mm.RespondDriver(), policies, mm.DEFAULT_TURNS
                )
                measured[policies] = (latencies, mm.calls_per_turn(logs))
        finally:
            await epistrel.models.aclose()
    for latencies, _ in measured.values():
        assert len(latencies) == mm.DEFAULT_TURNS
        assert mm.nearest_rank(latencies, 50) <= mm.nearest_rank(latencies, 95)
    off, minors = measured["off"][1], measured["minors"][1]
    assert off == {"generation": 1.0}
    assert minors["guardrail_check"] == 2.0
    assert sum(minors.values()) > sum(off.values())
