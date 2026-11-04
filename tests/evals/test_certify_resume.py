"""Incremental eval runs (#730, #729, #235): the results journal keeps every played case as it
ends, a later run plays only what the journal lacks (any day, any engine commit), a stop lets
the cases in flight finish, progress lines carry an ETA, ``--resume`` skips a preset with a
record of its fingerprint whatever the record's date, and the embedding model can live on an
endpoint of its own.

No test here carries a part marker: this is harness tooling, not a requirement's behaviour.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.evals import cli
from epistrel.evals.certify_cli import _matching_record
from epistrel.evals.journal import Journal, case_digest, journal_path, transient
from epistrel.evals.progress import Printer, duration
from epistrel.evals.report import Resumed
from epistrel.evals.runner import (
    AdapterConfig,
    Progress,
    Stopped,
    run_fingerprint,
    run_suites,
)
from epistrel.evals.suites import CaseModel, Outcome
from epistrel.orchestrator.models.stub import STUB_MODEL
from epistrel.tools import model_matrix
from tests.evals.support import SyntheticZeroSuite, stub_engine, synthetic_cases

FINGERPRINT = "sha256:" + "ab" * 32


class NotedCase(CaseModel):
    note: str = ""


CASE = NotedCase(id="c-1")


def _open(path: Path, **options: object) -> Journal:
    return Journal.open(path, model="m", fingerprint=FINGERPRINT, **options)  # type: ignore[arg-type]


def _record(journal: Journal, case: CaseModel, seed: int, outcome: Outcome) -> None:
    journal.record(
        "s", None, case, seed, outcome, 1200, engine_sha="abc1234", model_version="v", preset="full"
    )


# -- the journal ------------------------------------------------------------------------------


def test_journal_keeps_each_case_and_reuses_it(tmp_path: Path) -> None:
    """A recorded case is found again by a later run (from the file, not memory), with its
    outcome and latency. Complements: another seed, an edited case and another configuration
    fingerprint are not found; a torn last line (a kill mid-write) is skipped."""
    path = journal_path(tmp_path, "claude-haiku-5-5", "full", FINGERPRINT)
    assert path == tmp_path / "claude-haiku-5-5" / f"full-{'ab' * 6}.jsonl"
    first = _open(path)
    _record(first, CASE, 1, Outcome({"s.ok": True}, output="text", detail={"k": 1}))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"suite": "s", "case"')  # torn

    again = _open(path)
    found = again.lookup("s", None, CASE, 1)
    assert found is not None
    assert (found.outcome.observations, found.outcome.output, found.latency_ms) == (
        {"s.ok": True},
        "text",
        1200,
    )
    assert (again.reused, sorted(again.reused_shas)) == (1, ["abc1234"])
    assert again.lookup("s", None, CASE, 2) is None
    assert again.lookup("s", None, NotedCase(id="c-1", note="edited"), 1) is None
    assert again.lookup("s", "sha256:other", CASE, 1) is None
    other = Journal.open(path, model="m", fingerprint="sha256:" + "cd" * 32)
    assert other.entries == {}


def test_journal_replays_transient_model_errors(tmp_path: Path) -> None:
    """A case a transient model error ended plays again by default; a lasting one (garbage)
    is kept. ``all`` replays both, ``none`` neither."""
    timeout = Outcome(
        {"s.ok": False}, detail={"model_error": "model_unavailable", "reason": "timeout"}
    )
    garbage = Outcome(
        {"s.ok": False}, detail={"model_error": "model_unavailable", "reason": "garbage"}
    )
    overloaded = Outcome(
        {"s.ok": False},
        detail={"model_error": "model_unavailable", "reason": "http_error", "provider_status": 529},
    )
    path = tmp_path / "j.jsonl"
    journal = _open(path)
    for seed, outcome in enumerate((timeout, garbage, overloaded), start=1):
        _record(journal, CASE, seed, outcome)
    assert [transient(o.detail) for o in (timeout, garbage, overloaded)] == [True, False, True]
    assert transient({"reason": "timeout"}) is False  # not a model error at all

    def kept(mode: str) -> list[int]:
        reopened = _open(path, retry_errors=mode)
        return [s for s in (1, 2, 3) if reopened.lookup("s", None, CASE, s) is not None]

    assert kept("transient") == [2]
    assert kept("all") == []
    assert kept("none") == [1, 2, 3]


def test_fresh_sets_the_journal_aside(tmp_path: Path) -> None:
    """``--fresh`` starts empty but never deletes results: the old file is renamed."""
    path = tmp_path / "j.jsonl"
    _record(_open(path), CASE, 1, Outcome({"s.ok": True}))
    assert _open(path, fresh=True).entries == {}
    assert (tmp_path / "j.1.jsonl").is_file()
    assert not path.exists()
    assert case_digest(CASE) == case_digest(NotedCase(id="c-1"))
    assert case_digest(CASE) != case_digest(NotedCase(id="c-1", note="x"))


# -- progress ---------------------------------------------------------------------------------


def test_progress_line_and_eta() -> None:
    """The rate counts only cases this session played (journalled ones are free) over the wall
    time since the first started; the ETA is the preset's remaining cases at that rate."""
    now = [100.0]
    out = io.StringIO()
    printer = Printer("full", 10, every_s=0, out=out, clock=lambda: now[0])
    printer.suite_started("e06", 5, 2)
    for _ in range(4):
        printer.case_done("e06", 1, 0, reused=True, error=False)
    assert printer.eta_s() is None
    assert "ETA —" in printer.line()
    now[0] = 160.0
    printer.case_done("e06", 1, 60_000, reused=False, error=True)  # started at 100
    now[0] = 220.0
    printer.case_done("e06", 2, 60_000, reused=False, error=False)
    # 2 played in 120 s: 1 case/min; 4 cases left: 4 minutes.
    assert printer.rate_per_s() == pytest.approx(2 / 120)
    assert printer.eta_s() == pytest.approx(240)
    line = printer.line()
    assert line == (
        "full · e06 · seed 2/2 · suite 6/10 · preset 6/10 (4 from the journal) · 1 errors · "
        "1.0 cases/min · ETA 4m"
    )
    assert [duration(s) for s in (5, 600, 3 * 3600 + 300, 2 * 86400 + 4 * 3600)] == [
        "5s",
        "10m",
        "3h 05m",
        "2d 4h",
    ]


# -- --resume and the embedding endpoint -------------------------------------------------------


def test_resume_matches_a_record_of_any_date(tmp_path: Path) -> None:
    """#730: ``--resume`` skips a preset whose record has its fingerprint, however old the
    record. Complement: another fingerprint is not skipped."""
    path = tmp_path / "full-abababababab.json"
    path.write_text(json.dumps({"fingerprint": FINGERPRINT, "run_at": "2026-11-01T09:00:00Z"}))
    assert _matching_record(path, FINGERPRINT)
    assert not _matching_record(path, "sha256:" + "cd" * 32)
    assert not _matching_record(tmp_path / "missing.json", FINGERPRINT)


def test_embedding_endpoint_and_key_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embedding model can sit on its own endpoint (LM Studio beside a hosted chat model);
    its key defaults to ``lm-studio`` there, else to ``--api-key``, which defaults to
    ``EPISTREL_EVALS_API_KEY``."""
    monkeypatch.setenv(cli.API_KEY_ENV, "sk-from-env")
    args = cli.parser().parse_args(
        [
            "certify",
            "--presets",
            "full",
            "--base-url",
            "https://api.anthropic.com/v1/",
            "--model",
            "claude-haiku-5-5",
            "--embedding-model",
            "nomic",
            "--embedding-base-url",
            "http://localhost:1235/v1/",
        ]
    )
    config = cli._config(args, "openai")
    assert (config.api_key, config.base_url) == ("sk-from-env", "https://api.anthropic.com/v1")
    assert (config.embedding_base_url, config.embedding_key) == (
        "http://localhost:1235/v1",
        "lm-studio",
    )
    assert AdapterConfig(api_key="k", embedding_model="e").embedding_key == "k"
    assert AdapterConfig(embedding_base_url="u", embedding_api_key="x").embedding_key == "x"


# -- a run that stops and carries on -----------------------------------------------------------


class StopAfter(Progress):
    """Sets ``stop`` once ``n`` cases have been played."""

    def __init__(self, stop: asyncio.Event, n: int) -> None:
        self.stop = stop
        self.n = n
        self.played = 0

    def case_done(
        self, suite: str, seed: int, latency_ms: int, *, reused: bool, error: bool
    ) -> None:
        if not reused:
            self.played += 1
            if self.played >= self.n:
                self.stop.set()


@pytest.mark.integration
async def test_a_stopped_run_carries_on_from_its_journal(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """A run stopped after 4 cases raises ``Stopped`` with exactly those 4 journalled (the
    cases in flight finish; no new one starts). The next run plays only the other 8, makes only
    their model calls, says so in ``Report.resumed``, and its results equal a run played in one
    go."""
    suite = SyntheticZeroSuite()
    cases = {suite.name: synthetic_cases(6)}
    seeds = [1, 2]
    path = tmp_path / "journal.jsonl"
    fingerprint = run_fingerprint(STUB_MODEL)

    def journal() -> Journal:
        return Journal.open(path, model=STUB_MODEL, fingerprint=fingerprint)

    def generation_calls(lib: Engine) -> int:
        return sum(1 for c in lib.models.stub.calls if c.purpose == "generation")

    async with stub_engine(pg_url, engine) as lib:
        stop = asyncio.Event()
        with pytest.raises(Stopped):
            await run_suites(
                [suite],
                AdapterConfig(),
                seeds,
                engine=lib,
                cases=cases,
                journal=journal(),
                stop=stop,
                progress=StopAfter(stop, 4),
            )
        assert len(path.read_text("utf-8").splitlines()) == 4
        before = generation_calls(lib)
        carried = journal()
        resumed = await run_suites(
            [suite], AdapterConfig(), seeds, engine=lib, cases=cases, journal=carried
        )
        assert generation_calls(lib) - before == 8 + 1  # and the run's preflight probe
        whole = await run_suites([suite], AdapterConfig(), seeds, engine=lib, cases=cases)
    assert (carried.reused, carried.played) == (4, 8)
    assert resumed.resumed == Resumed(
        cases_reused=4, cases_played=8, engine_shas=[model_matrix.engine_sha()]
    )
    assert whole.resumed is None
    assert len(path.read_text("utf-8").splitlines()) == 12
    (got,), (expected,) = resumed.suites, whole.suites
    assert [m.model_dump() for m in got.metrics] == [m.model_dump() for m in expected.metrics]
    assert got.cases == expected.cases == 6


@pytest.mark.integration
def test_certify_carries_on_from_its_journal(pg_url: str, tmp_path: Path) -> None:
    """The command end to end on the stub: a second ``certify`` of the same preset (``--force``
    over the first record) plays nothing again, every case coming from the journal under
    ``--journal-dir``, and its report says so."""
    args = [
        "certify",
        "--presets",
        "full",
        "--adapter",
        "stub",
        "--suite",
        "smoke",
        "--seeds",
        "2",
        "--sample",
        "3",
        "--database-url",
        pg_url,
        "--out",
        str(tmp_path / "out"),
        "--journal-dir",
        str(tmp_path / "journal"),
    ]
    assert cli.main(args) == 0
    (journalled,) = (tmp_path / "journal").rglob("*.jsonl")
    lines = journalled.read_text("utf-8").splitlines()
    assert lines
    assert cli.main([*args, "--force"]) == 0
    assert journalled.read_text("utf-8").splitlines() == lines  # nothing played again
    folder = tmp_path / "out" / "reports"
    (second,) = folder.glob("full-*-2.md")
    (first,) = (r for r in folder.glob("full-*.md") if r != second)
    resumed = f"**Resumed:** {len(lines)} case results from earlier sessions"
    assert resumed not in first.read_text("utf-8")
    assert resumed in second.read_text("utf-8")
