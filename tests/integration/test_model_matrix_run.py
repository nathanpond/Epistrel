"""The model matrix tool end to end against a local OpenAI-compatible server (#54): protocol,
capability per purpose, E-06, and timed Lite turns for both rows, then the files it writes.

The real measurement runs against LM Studio on the owner's machine (``test_p_08``, marked
``live_model``); this run proves the tool's plumbing in CI with a 3-turn row."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.tools import model_matrix as mm
from tests.auth_support import auth_settings
from tests.model_support import config_key, mock_openai, restored_model_config


async def test_matrix_run_against_a_local_server(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two rows: one ``generation`` call per turn with policies off, and three with the minors
    policy on (input check, generation, output check).

    Complement asserted: the endpoint saw ``GET /v1/models`` (the protocol probe) as well as
    completions, so protocol was measured rather than assumed."""
    settings = auth_settings(pg_url, model_adapter="openai", config_key=config_key())
    with mock_openai(models=("local-model",)) as mock:
        async with restored_model_config(engine):
            epistrel = Engine(settings, db=engine)
            try:
                target = mm.Target(
                    base_url=f"{mock.root}/v1",
                    model="local-model",
                    api_key="lm-studio",
                    hardware="test runner",
                    server_version="mock",
                )
                rows = await mm.run_matrix(epistrel, target, turns=3)
            finally:
                await epistrel.models.aclose()
    assert rows is not None
    assert [r.policies for r in rows] == ["off", "minors"]
    assert any(r.path == "/v1/models" for r in mock.requests)
    for r in rows:
        assert r.protocol
        assert r.turns == 3
        assert len(r.latencies_ms) == 3
        assert r.p50_ms is not None
        assert r.p95_ms is not None
        assert r.p50_ms <= r.p95_ms
        assert all(cell["capable"] for cell in r.capability.values())
        assert r.e06["passed"]
        assert r.turn_path == mm.TURN_PATH
    assert rows[0].calls_per_turn == {"generation": 1.0}
    assert rows[1].calls_per_turn == {"generation": 1.0, "guardrail_check": 2.0}

    markdown, raw = tmp_path / "model-matrix.md", tmp_path / "model-matrix.json"
    mm.write_matrix(mm.merge(mm.load_rows(raw), rows), markdown, raw)
    text = markdown.read_text("utf-8")
    assert "| local: local-model | policies off | ✓ |" in text
    assert "| local: local-model | minors policy on | ✓ |" in text


async def test_unreachable_endpoint_writes_nothing(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Complement asserted: an unreachable endpoint exits 1 before any turn or file write, while
    a reachable one (the test above) writes both files."""
    with mock_openai() as mock:
        closed = mock.root
    args = argparse.Namespace(
        base_url=f"{closed}/v1",
        model="m",
        api_key="k",
        tier="local",
        hardware="",
        server_version="",
        purpose_model=[],
        database_url=pg_url,
        turns=2,
        output=tmp_path / "model-matrix.md",
        raw=tmp_path / "model-matrix.json",
    )
    async with restored_model_config(engine):
        assert await mm._main(args) == mm.EX_UNAVAILABLE
    assert not args.output.exists()
    assert not args.raw.exists()
