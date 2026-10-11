"""The model matrix tool's library (NFR-PORT-3, NFR-PERF-8; #54): percentiles, rows, merge and
rendering, and E-06's prompt-parameter check against the stub. The end-to-end run against a
local OpenAI-compatible server is ``tests/integration/test_model_matrix_run.py``."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.capability import check_prompt_params, context_prompt
from epistrel.orchestrator.models.metrics import CallLog, ModelCall
from epistrel.tools import model_matrix as mm
from tests.auth_support import auth_settings


def row(**changes: Any) -> mm.Row:
    values: dict[str, Any] = {
        "tier": "local",
        "model": "m",
        "policies": "off",
        "measured_at": "2026-10-11",
        "sha": "abc1234",
        "hardware": "laptop",
        "server_version": "server 1",
        "turn_path": mm.TURN_PATH,
        "protocol": True,
        "protocol_reason": None,
        "capability": {
            "generation": {"capable": True, "checks": [{"name": "context", "passed": True}]},
            "guardrail_check": {
                "capable": False,
                "checks": [
                    {"name": "context", "passed": True},
                    {"name": "structured_output", "passed": False},
                ],
            },
        },
        "e06": {"passed": True, "lengths": {"32": 30}, "identical": 10, "samples": 10},
        "turns": 3,
        "p50_ms": 1200,
        "p95_ms": 2500,
        "calls_per_turn": {"generation": 1.0},
        "latencies_ms": [1000, 1200, 2500],
    }
    values.update(changes)
    return mm.Row(**values)


def entry(purpose: str) -> ModelCall:
    return ModelCall(
        purpose=purpose, model="m", attempt=1, outcome="ok", latency_ms=1, tokens_in=1, tokens_out=1
    )


def test_nearest_rank() -> None:
    values = [15, 20, 35, 40, 50]
    assert mm.nearest_rank(values, 50) == 35
    assert mm.nearest_rank(values, 95) == 50
    assert mm.nearest_rank(values, 100) == 50
    assert mm.nearest_rank(list(range(1, 21)), 95) == 19
    assert mm.nearest_rank([7], 50) == 7
    with pytest.raises(ValueError, match="no values"):
        mm.nearest_rank([], 50)
    with pytest.raises(ValueError, match="percentile"):
        mm.nearest_rank(values, 0)


def test_calls_per_turn_by_purpose() -> None:
    one, three = CallLog(), CallLog()
    one.calls.append(entry("generation"))
    three.calls += [entry("guardrail_check"), entry("generation"), entry("guardrail_check")]
    assert mm.calls_per_turn([one, three]) == {"generation": 1.0, "guardrail_check": 1.0}
    assert mm.calls_per_turn([three]) == {"generation": 1.0, "guardrail_check": 2.0}


def test_merge_replaces_same_key_and_keeps_others() -> None:
    other_tier = asdict(row(tier="hosted", model="big"))
    stale = asdict(row(p50_ms=9999))
    merged = mm.merge([other_tier, stale], [row(), row(policies="minors")])
    assert [r["policies"] for r in merged if r["tier"] == "local"] == ["minors", "off"]
    assert [r["p50_ms"] for r in merged if r["tier"] == "local"] == [1200, 1200]
    assert any(r["tier"] == "hosted" for r in merged)
    assert len(merged) == 3


def test_render_two_rows(tmp_path: Path) -> None:
    rows = mm.merge(
        [],
        [row(), row(policies="minors", calls_per_turn={"generation": 1.0, "guardrail_check": 2.0})],
    )
    markdown, raw = tmp_path / "m.md", tmp_path / "m.json"
    mm.write_matrix(rows, markdown, raw)
    text = markdown.read_text("utf-8")
    assert "**Reported, not gated.**" in text
    assert "| local: m | policies off | ✓ |" in text
    assert "| local: m | minors policy on | ✓ |" in text
    assert "✗ (context ✓; structured_output ✗)" in text
    assert "3 (generation 1, guardrail_check 2)" in text
    assert "1.20 s | 2.50 s" in text
    assert "`abc1234`" in text
    assert json.loads(raw.read_text("utf-8"))["rows"][0]["latencies_ms"] == [1000, 1200, 2500]
    assert mm.load_rows(raw) == rows
    assert mm.load_rows(tmp_path / "absent.json") == []


def test_render_empty() -> None:
    text = mm.render([])
    assert "No tier has been measured yet." in text
    assert "model-matrix.json" in text


def test_parser_purpose_model() -> None:
    args = mm.parser().parse_args(
        ["--base-url", "http://h/v1", "--model", "m", "--purpose-model", "guardrail_check=small"]
    )
    assert dict(args.purpose_model) == {"guardrail_check": "small"}
    assert args.tier == "local"
    assert args.turns == mm.DEFAULT_TURNS
    for bad in ("guardrail_check", "nope=m", "generation="):
        with pytest.raises(SystemExit):
            mm.parser().parse_args(["--base-url", "u", "--model", "m", "--purpose-model", bad])


def test_turns_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    assert mm.main(["--base-url", "u", "--model", "m", "--turns", "0"]) == 2
    assert "--turns" in capsys.readouterr().err


def test_cast_has_a_minor_only_with_the_policy() -> None:
    assert min(c.true_age for c in mm.cast("minors")) < 18
    assert min(c.true_age for c in mm.cast("off")) >= 18
    assert mm.policy_set("off") == []
    assert [p.name for p in mm.policy_set("minors")] == ["minors"]


def test_context_prompt_size() -> None:
    text = "".join(m.content for m in context_prompt(8000))
    assert 8000 * 4 <= len(text) < 8000 * 4 + 500


async def test_e06_check_against_the_stub(dummy_dsn: str) -> None:
    """The deterministic stub honours ``max_tokens`` within its short answers and is fully
    repeatable at temperature 0. Complement: a nondeterministic stub is not repeatable."""
    layer = ModelAccessLayer(auth_settings(dummy_dsn, model_adapter="stub"))
    layer.bind(None)
    result = await check_prompt_params(layer)
    assert result.passed
    assert result.identical == result.samples == 10
    layer.stub.nondeterministic = True
    noisy = await check_prompt_params(layer)
    assert not noisy.repeatable
    assert not noisy.passed
    layer.stub.reset()
    layer.stub.fault("generation", "http_500", n=2)
    failing = await check_prompt_params(layer, samples=2)
    assert failing.lengths == {32: None, 128: None}
    assert not failing.lengths_ok
