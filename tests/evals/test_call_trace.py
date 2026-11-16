"""The eval run's per-call trace (#737): one record per model call, keyed to its journal case,
beside the journal; clean across a cancelled and resumed run; no secret in it; no effect on
fingerprints, reports or journal rows; nothing a deployed engine can turn on.

No test here carries a part marker: this is harness tooling, not a requirement's behaviour.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.evals import cli, trace
from epistrel.evals.journal import Journal, case_digest
from epistrel.evals.report import Report
from epistrel.evals.runner import AdapterConfig, Progress, run_fingerprint, run_suites
from epistrel.evals.suites import CaseModel
from epistrel.evals.trace import CallTrace, TraceFile, structured_mode, trace_path
from epistrel.orchestrator.models.call_trace import CALL_TRACE, Exchange
from epistrel.tools import model_matrix
from tests.auth_support import auth_settings
from tests.evals.support import SyntheticZeroSuite, synthetic_cases
from tests.model_support import config_key, mock_openai, restored_model_config

MODEL = "trace-model"
PLANTED_KEY = "sk-planted-0123456789abcdef"
FINGERPRINT = "sha256:" + "cd" * 32


class SecondSuite(SyntheticZeroSuite):
    name = "synthetic_zero_b"
    title = "A second synthetic suite (tests only)"


SUITES = [SyntheticZeroSuite(), SecondSuite()]


def _cases(n: int) -> dict[str, list[CaseModel]]:
    return {s.name: synthetic_cases(n) for s in SUITES}


def _config(root: str, api_key: str = PLANTED_KEY) -> AdapterConfig:
    return AdapterConfig(adapter="openai", base_url=f"{root}/v1", model=MODEL, api_key=api_key)


def _trace(path: Path, config: AdapterConfig) -> CallTrace:
    return CallTrace(
        model=MODEL,
        engine_sha=model_matrix.engine_sha(),
        file=TraceFile(path),
        preset=config.preset,
        fingerprint=FINGERPRINT,
        secrets=(config.api_key,),
    )


def _journal(path: Path) -> Journal:
    return Journal.open(path, model=MODEL, fingerprint=FINGERPRINT)


def _completions(mock: Any) -> list[Any]:
    return [r for r in mock.requests if r.path.endswith("/chat/completions")]


def _records(path: Path) -> list[dict[str, Any]]:
    # Every line parses (``read`` raises on a half line).
    return list(trace.read(path))


async def _engine(pg_url: str, engine: AsyncEngine) -> Engine:
    return Engine(auth_settings(pg_url, model_adapter="openai", config_key=config_key()), db=engine)


@pytest.mark.integration
async def test_a_run_traces_each_model_call_with_its_journal_key(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two suites x 3 cases x 2 seeds, one ``generation`` call each: 12 records, one per call
    the endpoint answered for a case (its protocol probe is not a ``send`` and is not traced),
    each keyed to the journal row of its case, with the request as sent and the reply. The
    complement: the endpoint saw exactly 12 case completions."""
    path = tmp_path / "full-x.calls.jsonl.gz"
    journal = _journal(tmp_path / "full-x.jsonl")
    with mock_openai(models=(MODEL,)) as mock:
        async with restored_model_config(engine):
            lib = await _engine(pg_url, engine)
            try:
                config = _config(mock.root)
                await run_suites(
                    SUITES,
                    config,
                    [1, 2],
                    engine=lib,
                    cases=_cases(3),
                    journal=journal,
                    trace=_trace(path, config),
                )
            finally:
                await lib.models.aclose()
    records = _records(path)
    assert len(_completions(mock)) == 12 + 1  # and the preflight probe
    assert len(records) == 12
    journalled = {(s, c, d, seed) for (s, _f, c, d, seed) in journal.entries}
    assert {(r["suite"], r["case"], r["digest"], r["seed"]) for r in records} == journalled
    for r in records:
        assert r["v"] == trace.TRACE_VERSION
        assert r["phase"] == "case"
        assert (r["purpose"], r["attempt"], r["call"], r["http"]) == ("generation", 1, 1, 1)
        assert r["judge"] is False
        assert r["structured_mode"] is None
        assert r["complete"] is True
        assert r["fingerprint"] == FINGERPRINT
        assert r["engine_sha"] == model_matrix.engine_sha()
        assert r["messages"][-1] == {"role": "user", "content": "Say something harmless."}
        assert r["response"]["text"].startswith(f"[mock {MODEL}]")
        assert r["response"]["finish_reason"] == "stop"
        assert r["response"]["text_len"] == len(r["response"]["text"])
        assert r["error"] is None
        case = next(c for c in _cases(3)[r["suite"]] if c.id == r["case"])
        assert r["digest"] == case_digest(case)
    sent = [r.body for r in _completions(mock)[1:]]
    assert sorted(json.dumps(b["messages"], sort_keys=True) for b in sent) == sorted(
        json.dumps(r["messages"], sort_keys=True) for r in records
    )


@pytest.mark.integration
async def test_a_cancelled_run_resumes_into_a_clean_trace(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """A run cancelled mid-suite (a second Ctrl-C) and resumed from its journal: every line
    parses, no ``(key, purpose, attempt, http)`` repeats, the cases the resume took from the
    journal add nothing, and the trace covers exactly the journalled cases."""
    path = tmp_path / "full-y.calls.jsonl.gz"
    journal_file = tmp_path / "full-y.jsonl"

    class CancelAfter(Progress):
        def __init__(self, n: int) -> None:
            self.n, self.played = n, 0
            self.task: asyncio.Task[Any] | None = None

        def case_done(
            self, suite: str, seed: int, latency_ms: int, *, reused: bool, error: bool
        ) -> None:
            self.played += 1
            if self.played == self.n and self.task is not None:
                self.task.cancel()

    with mock_openai(models=(MODEL,)) as mock:
        async with restored_model_config(engine):
            lib = await _engine(pg_url, engine)
            try:
                config = _config(mock.root)
                cancel = CancelAfter(4)
                first = asyncio.ensure_future(
                    run_suites(
                        SUITES,
                        config,
                        [1, 2],
                        engine=lib,
                        cases=_cases(3),
                        journal=_journal(journal_file),
                        trace=_trace(path, config),
                        progress=cancel,
                    )
                )
                cancel.task = first
                with pytest.raises(asyncio.CancelledError):
                    await first
                carried = _journal(journal_file)
                assert len(carried.entries) == 4
                await run_suites(
                    SUITES,
                    config,
                    [1, 2],
                    engine=lib,
                    cases=_cases(3),
                    journal=carried,
                    trace=_trace(path, config),
                )
            finally:
                await lib.models.aclose()
    records = _records(path)
    keys = Counter(
        (r["suite"], r["case"], r["digest"], r["seed"], r["purpose"], r["attempt"], r["http"])
        for r in records
    )
    assert keys
    assert max(keys.values()) == 1
    assert carried.reused == 4
    assert len(records) == len(carried.entries) == 12
    assert len({r["session"] for r in records}) == 2
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        assert all(json.loads(line) for line in handle)


def test_a_torn_last_member_is_cut_on_open(tmp_path: Path) -> None:
    """A process killed mid-write leaves a torn gzip member; the next open cuts it, so the file
    reads as whole records (and an intact file is left byte for byte)."""
    path = tmp_path / "t.calls.jsonl.gz"
    TraceFile(path).write(['{"a": 1}', '{"a": 2}'])
    intact = path.read_bytes()
    assert TraceFile(path).repair() == 0
    assert path.read_bytes() == intact
    torn = gzip.compress(b'{"a": 3}\n{"a": 4}\n', mtime=0)
    with path.open("ab") as handle:
        handle.write(torn[: len(torn) // 2])
    opened = TraceFile(path)
    assert path.read_bytes() == intact
    opened.write(['{"a": 5}'])
    assert [r["a"] for r in trace.read(path)] == [1, 2, 5]


@pytest.mark.integration
async def test_no_secret_reaches_a_trace(pg_url: str, engine: AsyncEngine, tmp_path: Path) -> None:
    """The run's key never appears in the decompressed trace, nor an ``Authorization`` or
    ``x-api-key`` value, even when the model's reply echoes the key (the scrub). The
    complement: the endpoint did receive the key as ``Authorization: Bearer …``."""
    path = tmp_path / "full-z.calls.jsonl.gz"
    with mock_openai(models=(MODEL,), script=[f"my key is {PLANTED_KEY}"] * 3) as mock:
        async with restored_model_config(engine):
            lib = await _engine(pg_url, engine)
            try:
                config = _config(mock.root)
                await run_suites(
                    [SUITES[0]],
                    config,
                    [1],
                    engine=lib,
                    cases={SUITES[0].name: synthetic_cases(2)},
                    trace=_trace(path, config),
                )
            finally:
                await lib.models.aclose()
    assert any(r.authorization == f"Bearer {PLANTED_KEY}" for r in mock.requests)
    text = gzip.decompress(path.read_bytes()).decode("utf-8")
    assert text.count("\n") == 2
    assert PLANTED_KEY not in text
    assert "Bearer" not in text
    assert "authorization" not in text.lower()
    assert "x-api-key" not in text.lower()
    assert trace.REDACTED in text


@pytest.mark.integration
async def test_tracing_changes_no_fingerprint_report_or_journal_row(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """The same run with and without a trace: the same fingerprint, the same suite results and
    the same journal rows (but for when each was written); without one (``--no-trace``), no
    trace file exists at all, not even an empty one."""
    traced_path = tmp_path / "traced.calls.jsonl.gz"
    untraced_path = tmp_path / "untraced.calls.jsonl.gz"
    reports: list[Report] = []
    with mock_openai(models=(MODEL,)) as mock:
        async with restored_model_config(engine):
            lib = await _engine(pg_url, engine)
            try:
                config = _config(mock.root)
                for name, sink in (
                    ("traced", _trace(traced_path, config)),
                    ("untraced", None),
                ):
                    reports.append(
                        await run_suites(
                            SUITES,
                            config,
                            [1],
                            engine=lib,
                            cases=_cases(2),
                            journal=_journal(tmp_path / f"{name}.jsonl"),
                            trace=sink,
                        )
                    )
            finally:
                await lib.models.aclose()
    traced, untraced = reports
    assert traced.fingerprint == untraced.fingerprint == run_fingerprint(MODEL)
    assert [s.model_dump(exclude={"latency"}) for s in traced.suites] == [
        s.model_dump(exclude={"latency"}) for s in untraced.suites
    ]

    def rows(name: str) -> list[dict[str, Any]]:
        lines = (tmp_path / f"{name}.jsonl").read_text("utf-8").splitlines()
        return sorted(
            (
                {k: v for k, v in json.loads(x).items() if k not in ("at", "latency_ms")}
                for x in lines
            ),
            key=json.dumps,
        )

    assert rows("traced") == rows("untraced")
    assert traced_path.is_file()
    assert not untraced_path.exists()


def test_no_trace_flag_turns_it_off_and_stub_runs_trace_nothing(tmp_path: Path) -> None:
    """``--no-trace`` is accepted by ``run``, ``calibrate-judge`` and ``certify`` and yields no
    trace; a stub run (it sends nothing) has none either; without the flag a real run's trace
    lands at ``<root>/<model>/run-<fp12>.calls.jsonl.gz``."""
    parser = cli.parser()
    for argv in (
        ["run", "--suite", "e01", "--no-trace"],
        ["calibrate-judge", "--no-trace"],
        ["certify", "--presets", "full", "--no-trace"],
    ):
        assert parser.parse_args(argv).no_trace is True
    real = AdapterConfig(adapter="openai", base_url="http://x/v1", model=MODEL, api_key="k")
    on = argparse.Namespace(no_trace=False, trace_dir=tmp_path)
    off = argparse.Namespace(no_trace=True, trace_dir=tmp_path)
    assert cli._trace(off, real, lambda root: root / "x.calls.jsonl.gz") is None
    assert cli._trace(on, AdapterConfig(), lambda root: root / "x.calls.jsonl.gz") is None
    found = cli._trace(on, real, lambda root: trace.run_trace_path(root, MODEL, FINGERPRINT))
    assert found is not None
    assert found.file is not None
    assert found.file.path == tmp_path / MODEL / f"run-{'cd' * 6}.calls.jsonl.gz"
    assert not found.file.path.exists()  # created by the first record, not before
    assert trace_path(tmp_path / "m" / "full-abc.jsonl") == tmp_path / "m" / (
        "full-abc.calls.jsonl.gz"
    )


def test_records_carry_the_mode_the_judge_flag_and_capped_text(tmp_path: Path) -> None:
    """The sink on its own: the structured mode read from the body, ``judge`` from the
    harness's marker, an image's ``data:`` URL elided, a long reply capped and marked, the
    provider's error body and token breakdown kept, a call outside a case under its phase."""
    path = tmp_path / "s.calls.jsonl.gz"
    sink = CallTrace(model=MODEL, engine_sha="abc", file=TraceFile(path))
    strict = {"type": "json_schema", "json_schema": {"name": "V", "strict": True, "schema": {}}}
    assert structured_mode({"response_format": strict}, "V") == "json_schema_strict"
    assert structured_mode({"response_format": {"json_schema": {}}}, "V") == "json_schema"
    assert structured_mode({}, "V") == "parse_validate_retry"
    assert structured_mode({}, None) is None
    data_url = "data:image/png;base64," + "A" * 100
    image: dict[str, Any] = {"type": "image_url", "image_url": {"url": data_url}}
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}, image]}],
        "max_completion_tokens": 64,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    long = "y" * (trace.TEXT_CAP + 10)
    ok = Exchange(
        body=body,
        response={
            "text": long,
            "text_len": len(long),
            "finish_reason": "length",
            "tokens_out": 64,
            "completion_tokens_details": {"reasoning_tokens": 60},
        },
        latency_ms=5,
    )
    failed = Exchange(
        body={"model": MODEL, "messages": []},
        error={"type": "BadRequestError", "status": 400, "body": {"error": "strict: required"}},
    )
    token = CALL_TRACE.set(sink)
    try:
        with sink.case(trace.CaseKey("s", None, "c", "d", 1)) as held, trace.judging():
            sink.record({"purpose": "quality_judge", "attempt": 1, "schema": "V"}, [ok])
        sink.finish(held, complete=False)
        with sink.phase("calibration"):
            sink.record({"purpose": "generation", "attempt": 2, "raised": "X"}, [failed])
    finally:
        CALL_TRACE.reset(token)
    first, second = trace.read(path)
    assert first["judge"] is True
    assert first["complete"] is False
    assert first["structured_mode"] == "parse_validate_retry"
    assert first["truncated"] is True
    assert len(first["response"]["text"]) == trace.TEXT_CAP
    assert first["response"]["completion_tokens_details"] == {"reasoning_tokens": 60}
    assert first["params"] == {
        "max_completion_tokens": 64,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    part = first["messages"][0]["content"][1]["image_url"]["url"]
    assert part["elided"] == "data_url"
    assert part["length"] == len(data_url)
    assert second["phase"] == "calibration"
    assert second["case"] is None
    assert second["judge"] is False
    assert second["complete"] is True
    assert second["error"]["status"] == 400
    assert second["raised"] == "X"


def test_a_deployed_engine_has_no_trace_setting() -> None:
    """Complement of the harness's switch: the engine's settings model has no field that could
    turn a call trace on, and outside the harness the hook is off."""
    assert not [f for f in Settings.model_fields if "trace" in f.lower()]
    assert CALL_TRACE.get() is None
