"""C-08, observability contract (FR-OBS-1, NFR-SEC-4; #74): the invariant 3 guard.

``test_c_08`` runs the scenario suite (``tests/scenario``), C-03 (every ``Engine`` operation over
REST and the library) and the planted canaries (``canary_scenario.py``) in subprocesses at DEBUG
(parallel shards, each on its own database, their telemetry merged; #684),
with ``telemetry_canary`` capturing exactly what ``epistrel serve`` would write: the JSON log
lines, the finished spans (behind the production content filter) and the ``/metrics`` text. It
asserts that every operation produced a summary line, a span and a count, and that no planted
canary and no harvested piece of content (every request body, staged event payload and stub
reply of the run) appears anywhere in that telemetry. The complement runs the canary story again
with content debug logging on and finds the canaries: the scan can see content when it is there.

What this guard does NOT cover: code paths the scenario suite, C-03 and the canary story do not
reach (an error branch nobody triggers logs what it logs); third-party loggers below the
WARNING cap the formatter applies to the known chatty ones; text a log line's ``event`` carries
when a caller formats content into the message itself (the canary scan would see it only on a
reached path); and telemetry an operator adds outside Epistrel's handler and exporter. Metrics
cannot carry content by construction: ``test_metrics_labels`` checks every label value against
#44's grammar.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import pytest
from prometheus_client.parser import text_string_to_metric_families

from epistrel.core.ids import ULID_PATTERN
from epistrel.engine import operations
from tests.conformance.canary_scenario import CANARIES
from tests.conformance.conftest import migrated_database
from tests.conformance.telemetry_canary import CONTENT_DEBUG_VAR, OUT_VAR
from tests.conftest import NO_DOCKER_MESSAGE

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "tests.conformance.telemetry_canary"
CANARY_FILE = "tests/conformance/canary_scenario.py"
SUITE = ("tests/scenario", "tests/conformance/test_c_03.py", CANARY_FILE)
RUN_TIMEOUT_S = 1500
"""Each inner shard's bound (#684: the whole inner run reached it on CI as one process)."""
C08_SHARDS = 4
"""Parallel inner runs (the CI runner has four cores); ``EPISTREL_TEST_C08_SHARDS`` overrides."""
SHARDS_VAR = "EPISTREL_TEST_C08_SHARDS"
OVERRIDE_VAR = "EPISTREL_TEST_DATABASE_URL"
OVERRIDE_TARGET = "tests/integration/test_two_stage.py"
"""A module whose tests need only ``pg_url`` (and ``scratch_database``), no other container."""
NO_DOCKER_HOST = "unix:///nonexistent/docker.sock"
OVERRIDE_TIMEOUT_S = 120

ENUM = re.compile(r"^[a-z_]+$")
ULID = re.compile(ULID_PATTERN)
HTTP_STATUS = re.compile(r"^[1-5][0-9]{2}$")
MODEL_NAME = re.compile(r"^[a-z0-9._-]{1,100}$")
HEX_TRACE = re.compile(r"^[0-9a-f]{32}$")
HEX_SPAN = re.compile(r"^[0-9a-f]{16}$")
ISO_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
FORMATTED = {
    "operation_id": ULID,
    "story_id": ULID,
    "job_id": ULID,
    "trace_id": HEX_TRACE,
    "span_id": HEX_SPAN,
    "timestamp": ISO_TIME,
}
"""Log fields whose every value must match a format, so they need no content scan."""


@dataclass(frozen=True)
class Telemetry:
    raw_logs: list[str]
    spans: list[dict[str, Any]]
    metrics: str
    harvest: list[str]

    @property
    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.raw_logs]

    @property
    def blob(self) -> str:
        """Everything the run emitted, as one text."""
        return "\n".join([*self.raw_logs, json.dumps(self.spans), self.metrics])


def shards(paths: tuple[str, ...], count: int) -> list[list[str]]:
    """``paths`` split into at most ``count`` groups for parallel inner runs (#684): every test
    file under a directory in ``paths`` is packed, largest first, into the lightest group by
    size (a rough proxy for run time); a file named directly joins the lightest group too."""
    files: list[Path] = []
    for path in paths:
        found = REPO_ROOT / path
        files += sorted(found.glob("test_*.py")) if found.is_dir() else [found]
    groups: list[list[Path]] = [[] for _ in range(max(1, min(count, len(files))))]
    weights = [0] * len(groups)
    for file in sorted(files, key=lambda f: (-f.stat().st_size, f.name)):
        lightest = weights.index(min(weights))
        groups[lightest].append(file)
        weights[lightest] += file.stat().st_size
    return [[str(f.relative_to(REPO_ROOT)) for f in group] for group in groups if group]


def child_env() -> dict[str, str]:
    """This process's environment for an inner pytest run, without the variables that would
    steer it (``EPISTREL_*``, coverage's ``COV_CORE_*`` and ``PYTEST_*``)."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("EPISTREL_", "COV_CORE_", "PYTEST_"))
    }


def run_suite(pg_url: str, paths: tuple[str, ...], *, content_debug: bool) -> Telemetry:
    """Run ``paths`` under the capture plugin, split into parallel shards (``shards``), each
    on a fresh database; every shard must pass, and their telemetry is merged."""
    groups = shards(paths, int(os.environ.get(SHARDS_VAR, str(C08_SHARDS))))
    base = child_env()
    if content_debug:
        base[CONTENT_DEBUG_VAR] = "1"
    (REPO_ROOT / "build").mkdir(exist_ok=True)
    with ExitStack() as stack:
        running: list[tuple[subprocess.Popen[str], Path, IO[str]]] = []
        for group in groups:
            name = f"epistrel_c08_{secrets.token_hex(4)}"
            out = REPO_ROOT / "build" / f"{name}.json"
            stack.callback(out.unlink, missing_ok=True)
            env = {**base, OUT_VAR: str(out)}
            env[OVERRIDE_VAR] = stack.enter_context(migrated_database(pg_url, name))
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                PLUGIN,
                "-p",
                "no:cacheprovider",
                *group,
            ]
            # A file, not a pipe: a shard's output must never block it while another is awaited.
            log: IO[str] = stack.enter_context(tempfile.TemporaryFile("w+", encoding="utf-8"))
            proc = subprocess.Popen(
                command, cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, text=True
            )
            stack.callback(_stop, proc)
            running.append((proc, out, log))
        merged = Telemetry([], [], "", [])
        for proc, out, output in running:
            proc.wait(timeout=RUN_TIMEOUT_S)
            output.seek(0)
            assert proc.returncode == 0, _failures(output.read())
            document = json.loads(out.read_text(encoding="utf-8"))
            merged = Telemetry(
                merged.raw_logs + document["logs"],
                merged.spans + document["spans"],
                merged.metrics + document["metrics"],
                merged.harvest + document["harvest"],
            )
    return merged


def _failures(text: str) -> str:
    """A failed shard's report: its failures section when there is one, else its last lines."""
    start = text.find("= FAILURES =")
    return text[start : start + 12000] if start >= 0 else text[-8000:]


def _stop(proc: subprocess.Popen[str]) -> None:
    """Kill a shard still running when the run ends early (a timeout or a failed shard)."""
    if proc.poll() is None:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="module")
def scanned(pg_url: str) -> Telemetry:
    return run_suite(pg_url, SUITE, content_debug=False)


def _values(value: Any, key: str = "") -> Iterator[tuple[str, str]]:
    """Every ``(field, string)`` in a log line or span, at any depth."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from _values(item, str(name))
    elif isinstance(value, list):
        for item in value:
            yield from _values(item, key)


def leaks(telemetry: Telemetry) -> list[str]:
    """Every planted canary or harvested content string found in the telemetry."""
    found = [phrase for phrase in CANARIES.values() if phrase in telemetry.blob]
    texts: set[str] = set()
    for line in telemetry.logs:
        for field, value in _values(line):
            pattern = FORMATTED.get(field)
            if pattern is not None:
                assert pattern.match(value), f"{field}={value!r} is not an identifier"
            else:
                texts.add(value)
    for span in telemetry.spans:
        texts.update(value for _, value in _values(span))
    texts.update(telemetry.metrics.splitlines())
    found += [content for content in telemetry.harvest if any(content in t for t in texts)]
    return found


def label_problems(metrics: str) -> list[str]:
    """Label values outside #44's grammar: an enum, a ULID, an HTTP status (``status``) or a
    sanitised model name (``model``); histogram bucket bounds (``le``) are numbers."""
    problems: list[str] = []
    for family in text_string_to_metric_families(metrics):
        for sample in family.samples:
            for label, value in sample.labels.items():
                if label == "le":
                    continue
                ok = bool(ENUM.match(value) or ULID.match(value))
                ok = ok or (label == "status" and bool(HTTP_STATUS.match(value)))
                ok = ok or (label == "model" and bool(MODEL_NAME.match(value)))
                if not ok:
                    problems.append(f"{sample.name}{{{label}={value!r}}}")
    return problems


@pytest.mark.part("M2", "FR-OBS-1; NFR-SEC-4")
def test_c_08(scanned: Telemetry, pg_url: str) -> None:
    """Every operation emits a summary line, a span and a count; no canary and no harvested
    content appears in any log line, span or metric.

    Complement asserted: with operator-only content debug logging on, the canaries in the
    player input and the stub's reply **appear** in the logs (the scan can see content)."""
    logs = scanned.logs
    assert logs, "the run logged nothing"
    assert len(scanned.harvest) > 100, "the harvest saw too little content to mean anything"
    assert all({"event", "level", "logger", "timestamp"} <= line.keys() for line in logs)

    summaries = [line for line in logs if line.get("event") == "operation"]
    for line in summaries:
        assert {"operation_id", "kind", "duration_ms", "outcome"} <= line.keys(), line
        assert line["outcome"] in ("ok", "rejected", "error")
    logged = {line["kind"] for line in summaries}
    spanned = {span["name"] for span in scanned.spans}
    counted = {
        sample.labels["kind"]
        for family in text_string_to_metric_families(scanned.metrics)
        if family.name == "epistrel_operations"
        for sample in family.samples
    }
    every = set(operations())
    assert every - logged == set(), "operations without a summary line"
    assert every - spanned == set(), "operations without a span"
    assert every - counted == set(), "operations without a count"
    assert {"commit", "model_call"} <= spanned
    requests = [line for line in logs if line.get("event") == "request"]
    assert {line["route"] for line in requests} >= {"respond", "create_story", "fork"}
    assert any(span["kind"] == "SpanKind.SERVER" for span in scanned.spans)
    assert "epistrel_http_request_duration_seconds_bucket" in scanned.metrics
    assert not any(line.get("content_logging") for line in logs)

    assert leaks(scanned) == []

    debug = run_suite(pg_url, (CANARY_FILE,), content_debug=True)
    shown = [phrase for phrase in CANARIES.values() if phrase in "\n".join(debug.raw_logs)]
    assert {CANARIES["say"], CANARIES["reply"]} <= set(shown)
    assert any(line.get("content_logging") for line in debug.logs)
    found = leaks(debug)
    assert set(found) & set(debug.harvest), "the harvest scan found no content in debug logs"


def _run_child(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", OVERRIDE_TARGET]
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=OVERRIDE_TIMEOUT_S,
        check=False,
    )


@pytest.mark.part("M2", "FR-OBS-1; NFR-SEC-4")
def test_c_08__override_starts_no_container(scratch_database: str) -> None:
    """#99: with ``EPISTREL_TEST_DATABASE_URL`` set, the ``pg_url`` fixture uses that server and
    starts no container. A child run of ``OVERRIDE_TARGET`` points the override at a scratch
    database and Docker at a socket that does not exist, so any testcontainers start would fail
    the run; the child passes. No container is started, so there is none to tear down.

    Complement asserted: the same child without the override fails at fixture setup with
    ``NO_DOCKER_MESSAGE`` (the bogus Docker host does stop a container from starting)."""
    env = {**child_env(), "DOCKER_HOST": NO_DOCKER_HOST}
    used = _run_child({**env, OVERRIDE_VAR: scratch_database})
    assert used.returncode == 0, used.stdout[-8000:]
    assert re.search(r"\b\d+ passed\b", used.stdout), used.stdout[-2000:]

    refused = _run_child(env)
    assert refused.returncode != 0, refused.stdout[-2000:]
    assert NO_DOCKER_MESSAGE in refused.stdout, refused.stdout[-8000:]


def test_metrics_labels(scanned: Telemetry) -> None:
    """Every label value the whole run produced is an enum, a ULID, an HTTP status or a
    sanitised model name (#44's rule). Complement asserted: a prose label value is reported."""
    assert label_problems(scanned.metrics) == []
    families = {family.name for family in text_string_to_metric_families(scanned.metrics)}
    assert {
        "epistrel_model_calls",
        "epistrel_operations",
        "epistrel_http_request_duration_seconds",
    } <= families
    assert not families & {"process_cpu_seconds", "python_gc_objects_collected", "python_info"}

    planted = 'epistrel_operations_total{kind="a quiet harbour",outcome="ok"} 1.0\n'
    assert label_problems(planted) == ["epistrel_operations_total{kind='a quiet harbour'}"]
