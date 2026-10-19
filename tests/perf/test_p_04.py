"""P-04: the reference workload (docs/04), the M2 part: the Lite variant (NFR-PERF-4; #76).

The workload itself is ``uv run python -m perf.lite_smoke --minutes 30 --host-class …``: ten
20,000-event stories driven open-loop by Locust against the compose stack for 30 minutes, too
long and too heavy for a test session. Its evidence is the committed report in
``perf/results/``. What runs here, in PR CI:

- ``test_p_04``: a full-length report (30 minutes, 10,000-turn stories) newer than 30 days is
  committed, states its host class and verdict, and has its raw JSON sidecar
  (``perf.lite_smoke.freshness``, which also drives the ``p04-expiry.yml`` reminder; #283);
- ``test_p_04__expiry``: that window, and the reminder's 14-day warning before it closes;
- ``test_p_04__gate``: the report's gate logic (``perf.lite_smoke.report``) over a synthetic run:
  a run that keeps up passes, and one story falling behind fails the run although the aggregate
  keeps up;
- ``test_p_04__measurements``: the engine measurements the run reads: a queued turn reports its
  queue wait and its delay to the first model call (respond result and ``GET /operations``), the
  per-story queue-depth gauge counts it while it waits, and every stage of
  ``epistrel_turn_stage_seconds`` is observed once per committed turn.

The M4 part, the structured variant (#153; ``python -m perf.lite_smoke --scenario structured``):

- ``test_p_04__structured``: a full-length structured report (30 minutes, ten 10,000-memory
  stories) newer than 30 days is committed, with its host class, verdict and sidecar; the
  stack's stub claims are ``structured_load``'s;
- ``test_p_04__structured_gate``: the structured gate over a synthetic run: the retrieval and
  verify p95 budgets pass and fail on their own, and one story falling behind fails the run;
- ``test_p_04__structured_measurements``: a structured turn observes the ``retrieve`` stage
  once per retrieval and ``verify`` once per claim checked, and a Lite turn neither.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
import yaml
from perf.lite_smoke import RESULTS_DIR, Target, freshness, report, structured
from perf.lite_smoke import __main__ as smoke
from perf.lite_smoke.seed import StoryRef
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.observability import metrics
from epistrel.observability.stages import STAGES, STRUCTURED_STAGES
from epistrel.testing import structured_load
from tests.auth_support import auth_settings
from tests.respond_support import FIRST, PLAYER, body, new_story, respond, respond_client
from tests.story_support import READ, WRITE
from tests.structured_turn_support import structured_story


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M2", "NFR-PERF-4")
def test_p_04() -> None:
    """A committed full-length Lite report newer than 30 days, with its host class, verdict
    and sidecar.

    Complement asserted: a report without a host class is refused by the same reader, and an
    old one does not count as fresh."""
    reports = sorted(RESULTS_DIR.glob("*-lite.md"))
    assert reports, f"no P-04 report in {RESULTS_DIR}: run `python -m perf.lite_smoke`"
    for path in reports:
        found = report.header(path.read_text())
        assert found.host_class in ("mac", "reference", "ci"), path
        assert path.with_suffix(".json").exists(), f"{path.name} has no JSON sidecar"
    now = datetime.now(UTC)
    current = freshness.status(now)
    rerun = (
        "no full-length P-04 report from the last 30 days in perf/results/: run "
        "`uv run python -m perf.lite_smoke --minutes 30 --host-class <class>` and commit it"
    )
    assert current.report is not None, rerun
    assert not current.expired, rerun
    newest = report.header(current.report.read_text())
    sidecar = json.loads(current.report.with_suffix(".json").read_text())
    assert sidecar["info"]["host_class"] == newest.host_class
    assert sidecar["passed"] == (newest.verdict == report.PASS)

    text = current.report.read_text()
    with pytest.raises(ValueError, match="host class"):
        report.header(text.replace("(host class ", "(class "))
    stale = text.replace(newest.started.strftime("%Y-%m-%d %H:%M"), "2000-01-01 00:00", 1)
    assert now - report.header(stale).started > freshness.FRESH


def _write_report(
    results: Path, text: str, started: datetime, swap: dict[str, str] | None = None
) -> Path:
    """A copy of a committed report, retitled to ``started`` and with ``swap`` applied."""
    title = report.TITLE.search(text)
    assert title is not None
    body = text.replace(title.group(1), started.strftime("%Y-%m-%d %H:%M"), 1)
    for old, new in (swap or {}).items():
        body = body.replace(old, new, 1)
    path = results / f"{started:%Y-%m-%d-%H%M}-lite.md"
    path.write_text(body)
    return path


@pytest.mark.part("M2", "NFR-PERF-4")
def test_p_04__expiry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#283: the reminder (``p04-expiry.yml``) is due 14 days before the newest full-length
    report's 30-day window closes, and on and after that day; the gate's window closes at 30
    days.

    Complement asserted: a shorter (demo) or smaller run never counts as the newest report, a
    file that is not a report is skipped, and with no full-length report the reminder is due
    and the gate's window is closed."""
    text = sorted(RESULTS_DIR.glob("*-lite.md"))[-1].read_text()
    started = datetime(2026, 10, 11, 21, 43, tzinfo=UTC)
    empty = freshness.status(started, tmp_path)
    assert (empty.report, empty.due, empty.expired) == (None, True, True)

    full = _write_report(tmp_path, text, started)
    later = started + timedelta(days=3)
    _write_report(tmp_path, text, later, {", 30 minutes,": ", 5 minutes,"})
    _write_report(tmp_path, text, later + timedelta(hours=1), {"10,000 turns": "1,000 turns"})
    (tmp_path / "2026-12-01-0000-lite.md").write_text("not a report\n")

    expires = started + freshness.FRESH
    assert expires == datetime(2026, 11, 10, 21, 43, tzinfo=UTC)
    early = freshness.status(expires - freshness.WARN - timedelta(minutes=1), tmp_path)
    assert (early.report, early.due, early.expired) == (full, False, False)
    warn = freshness.status(expires - freshness.WARN, tmp_path)
    assert (warn.due, warn.expired) == (True, False)
    last = freshness.status(expires, tmp_path)
    assert (last.due, last.expired) == (True, False)
    gone = freshness.status(expires + timedelta(minutes=1), tmp_path)
    assert (gone.due, gone.expired) == (True, True)

    rerun = _write_report(tmp_path, text, started + timedelta(days=20))
    renewed = freshness.status(expires, tmp_path)
    assert (renewed.report, renewed.due, renewed.expired) == (rerun, False, False)

    assert freshness.main(["--now", "2026-10-27T21:43:00Z", "--results", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"report={rerun.as_posix()}",
        "started=2026-10-31 21:43 UTC",
        "expires=2026-11-30",
        "due=false",
        "expired=false",
    ]


def _run(
    info: report.RunInfo, *, late_story: str | None = None, slow_s: float = 40.0
) -> list[dict[str, Any]]:
    """Synthetic driver records: every story submits every interval until 2 minutes before the
    end and each turn settles 2.1 s later, except ``late_story``, whose turns take ``slow_s``
    each from minute 6 on (past the interval, a backlog builds and waits pass one interval)."""
    records: list[dict[str, Any]] = []
    for index, story in enumerate(("s1", "s2")):
        free = 0.0
        tick = 0
        while (due := index * 7.5 + tick * info.interval_s) <= info.end_s - 120:
            took = slow_s if story == late_story and due >= 360 else 2.1
            start = max(due, free)
            free = start + took
            base = {"story_id": story, "tick": tick, "submitted": due}
            records.append({**base, "event": "submit"})
            records.append(
                {
                    **base,
                    "event": "finish",
                    "finished": free,
                    "outcome": "committed",
                    "status": 202,
                    "elapsed_ms": int(took * 1000),
                    "model_ms": 2000,
                    "queued_ms": int((start - due) * 1000),
                    "first_call_offset_ms": 20,
                }
            )
            tick += 1
    return records


@pytest.mark.part("M2", "NFR-PERF-4")
def test_p_04__gate() -> None:
    """A run that keeps up passes every criterion; the report renders every section.

    Complement asserted: when one story falls behind, its own criteria fail and so does the
    run, although the other story and every aggregate criterion still pass."""
    info = report.RunInfo(
        started="2026-10-11 00:00 UTC",
        minutes=10,
        interval_s=15.0,
        stories=2,
        host_class="mac",
        host="test",
        engine_sha="0" * 12,
        stub_latency_s=2.0,
    )
    stories = [("s1", 0), ("s2", 1)]
    commit = {"0.1": 70.0, "0.25": 78.0, "0.5": 78.0, "+Inf": 78.0}
    samples = [{"t": float(t), "queue": {}, "db_connections": 4} for t in range(0, 600, 5)]
    clean = report.evaluate(info, stories, _run(info), samples, {"commit": commit}, {})
    assert clean.passed, [c for s in clean.stories for c in s.criteria if not c.passed]
    page = report.markdown(clean)
    for heading in ("## Criteria", "## Per story", "## Queue depth over time", "## Resources"):
        assert heading in page
    assert "**Result: PASS** (host class mac: reported, not gated)" in page
    assert report.header(page).minutes == 10

    late = report.evaluate(
        info, stories, _run(info, late_story="s2"), samples, {"commit": commit}, {}
    )
    assert not late.passed
    assert all(c.passed for c in late.aggregate)
    assert late.stories[0].passed
    failed = {c.name for c in late.stories[1].criteria if not c.passed}
    assert failed >= {
        "turns waiting behind the running one, after 5 min",
        "longest wait to reach the queue's front",
    }

    assert report.exit_code(clean) == 0

    # The gate is enforced only on the reference host class (#76 AC2; #297): the same slow
    # commits FAIL on both, but only the reference run exits 1.
    slow = {"0.1": 0.0, "0.25": 70.0, "0.5": 100.0, "+Inf": 100.0}
    on_mac = report.evaluate(info, stories, _run(info), samples, {"commit": slow}, {})
    assert not on_mac.passed
    assert not info.enforced
    assert "**Result: FAIL** (host class mac: reported, not gated)" in report.markdown(on_mac)
    assert report.exit_code(on_mac) == 0
    reference = replace(info, host_class="reference")
    assert reference.enforced
    on_reference = report.evaluate(
        reference, stories, _run(reference), samples, {"commit": slow}, {}
    )
    assert not on_reference.passed
    assert "**Result: FAIL** (host class reference: gated (exit 1 on FAIL))" in report.markdown(
        on_reference
    )
    assert report.exit_code(on_reference) == 1
    passing = report.evaluate(reference, stories, _run(reference), samples, {"commit": commit}, {})
    assert passing.passed
    assert report.exit_code(passing) == 0


def _stage_count(stage: str) -> float:
    return REGISTRY.get_sample_value("epistrel_turn_stage_seconds_count", {"stage": stage}) or 0.0


@pytest.mark.part("M2", "NFR-PERF-4")
async def test_p_04__measurements(
    stub_settings: Settings, engine: AsyncEngine, pg_url: str
) -> None:
    """A turn queued behind a held one reports how long it waited and when its first model call
    started, in its result and on ``GET /operations/{id}``; the queue-depth gauge counts it
    while it waits; each committed turn observes every stage once.

    Complement asserted: the turn that played at once has no queue wait (``queued_ms`` null)
    and observes no ``queue_wait``, and once the queue drained the gauge has no row for it."""
    counts = {stage: _stage_count(stage) for stage in STAGES}
    metrics.GAUGES.bind(pg_url)
    try:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            stub = epistrel.models.stub
            story, ids = await new_story(
                client, PLAYER, FIRST, settings={"turn_concurrency": "queue"}
            )
            latch = stub.hold("generation")
            first = asyncio.ensure_future(respond(client, story, body(ids[PLAYER], "First.")))
            await asyncio.wait_for(latch.entered.wait(), 10)
            waiting = await respond(
                client,
                story,
                body(ids[PLAYER], "Second."),
                headers={"Prefer": "respond-async", "Idempotency-Key": "p04-queued"},
            )
            assert waiting.status_code == 202, waiting.text
            operation_id = waiting.json()["operation_id"]
            gauge = metrics.render_metrics().decode()
            assert f'epistrel_turn_queue_depth{{story_id="{story}"}} 1.0' in gauge
            await asyncio.sleep(0.3)
            latch.release()
            played = await first
            assert played.status_code == 200, played.text
            assert played.json()["queued_ms"] is None
            assert played.json()["first_call_offset_ms"] >= 0
            await asyncio.wait_for(epistrel.drainer.idle(), 30)
            view = await client.get(
                f"/operations/{operation_id}",
                params={"viewpoint": f"character:{ids[PLAYER]}"},
                headers=READ,
            )
            assert view.status_code == 200, view.text
            found = view.json()
            assert found["status"] == "committed"
            assert found["queued_ms"] >= 300, found
            assert found["first_call_offset_ms"] is not None
            stored = found["result"]["body"]
            assert stored["queued_ms"] == found["queued_ms"]
            assert stored["first_call_offset_ms"] == found["first_call_offset_ms"]
            assert stored["elapsed_ms"] >= stored["first_call_offset_ms"]
        gauge = metrics.render_metrics().decode()
        assert f'story_id="{story}"' not in gauge
    finally:
        metrics.GAUGES.unbind()
    observed = {stage: _stage_count(stage) - counts[stage] for stage in STAGES}
    assert observed == {stage: 1.0 if stage == "queue_wait" else 2.0 for stage in STAGES}


@pytest.mark.part("M4", "NFR-PERF-4")
def test_p_04__structured() -> None:
    """A committed full-length structured report newer than 30 days (#153), with its host class,
    verdict and sidecar, run with ``structured_load``'s claim mix (the compose file's JSON is
    the same list).

    Complement asserted: the Lite reports are not structured ones (each scenario's freshness
    reads its own files), and a structured report with fewer seeded turns is not full-length."""
    rerun = (
        "no full-length structured P-04 report from the last 30 days in perf/results/: run "
        "`uv run python -m perf.lite_smoke --scenario structured --minutes 30 --host-class "
        "<class>` and commit it"
    )
    current = freshness.status(datetime.now(UTC), scenario="structured")
    assert current.report is not None, rerun
    assert not current.expired, rerun
    text = current.report.read_text()
    newest = report.header(text)
    assert newest.scenario == "structured"
    assert newest.host_class in ("mac", "reference", "ci")
    sidecar = json.loads(current.report.with_suffix(".json").read_text())
    assert sidecar["info"]["scenario"] == "structured"
    assert sidecar["info"]["host_class"] == newest.host_class
    assert sidecar["passed"] == (newest.verdict == report.PASS)
    names = {c["name"] for c in sidecar["aggregate"]}
    assert {
        "retrieval p95 (server retrieve stage)",
        "deterministic verify p95 (server verify stage)",
    } <= names
    compose = yaml.safe_load(structured.COMPOSE_FILE.read_text())
    claims = json.loads(compose["services"]["engine"]["environment"]["EPISTREL_STUB_RENDER_CLAIMS"])
    assert claims == structured.RENDER_CLAIMS == structured_load.render_claims(0.1, 0.1)

    lite = freshness.status(datetime.now(UTC))
    assert lite.report is None or report.header(lite.report.read_text()).scenario == "lite"
    smaller = text.replace(f"- Stories: {newest.turns:,} turns", "- Stories: 500 turns", 1)
    assert not freshness.is_full(report.header(smaller))


def _structured_info(**changes: Any) -> report.RunInfo:
    info = report.RunInfo(
        started="2026-10-19 00:00 UTC",
        minutes=10,
        interval_s=60.0,
        stories=2,
        host_class="mac",
        host="test",
        engine_sha="0" * 12,
        stub_latency_s=2.0,
        turns=5_000,
        scenario="structured",
        events=20_000,
        memories=10_000,
    )
    return replace(info, **changes)


@pytest.mark.part("M4", "NFR-PERF-4")
def test_p_04__structured_gate() -> None:
    """The structured gate (#153): a run that keeps up within the retrieval (≤ 150 ms) and
    verify (≤ 50 ms) p95 budgets passes; the report names its scenario and both budgets.

    Complement asserted: a slow retrieval stage, a slow verify stage, or no claims checked at
    all fails the run while every other criterion passes; one story falling behind fails it
    although every aggregate criterion passes; the Lite gate has neither budget."""
    info = _structured_info(minutes=20)
    stories = [("s1", 0), ("s2", 1)]
    commit = {"0.1": 20.0, "0.25": 20.0, "+Inf": 20.0}
    fast = {"0.05": 97.0, "0.15": 100.0, "+Inf": 100.0}
    samples = [{"t": float(t), "queue": {}, "db_connections": 4} for t in range(0, 600, 5)]

    def run(**buckets: dict[str, float]) -> report.Evaluation:
        stage = {"commit": commit, "retrieve": fast, "verify": fast, **buckets}
        return report.evaluate(info, stories, _run(info), samples, stage, {})

    clean = run()
    assert clean.passed, [c for c in clean.aggregate if not c.passed]
    page = report.markdown(clean)
    assert page.startswith("# P-04 Structured reference workload: 2026-10-19 00:00")
    assert "100.0% of 100 retrievals ≤ 150 ms" in page
    assert "97.0% of 100 claims checked ≤ 50 ms" in page
    parsed = report.header(page)
    assert (parsed.scenario, parsed.turns, parsed.minutes) == ("structured", 5_000, 20)

    slow = {"0.05": 10.0, "0.15": 90.0, "+Inf": 100.0}
    for stage, name in (
        ("retrieve", "retrieval p95 (server retrieve stage)"),
        ("verify", "deterministic verify p95 (server verify stage)"),
    ):
        failed = run(**{stage: slow})
        assert not failed.passed
        assert [c.name for c in failed.aggregate if not c.passed] == [name]
        assert all(s.passed for s in failed.stories)
    unexercised = run(verify={})
    assert [c.measured for c in unexercised.aggregate if not c.passed] == [
        "no claims checked observed"
    ]

    within = {"commit": commit, "retrieve": fast, "verify": fast}
    behind = _run(info, late_story="s2", slow_s=100.0)
    late = report.evaluate(info, stories, behind, samples, within, {})
    assert not late.passed
    assert all(c.passed for c in late.aggregate)
    assert late.stories[0].passed
    assert not late.stories[1].passed

    lite = report.evaluate(replace(info, scenario="lite"), stories, _run(info), samples, within, {})
    assert len(lite.aggregate) == 3


def _observed(stages: tuple[str, ...]) -> dict[str, float]:
    return {stage: _stage_count(stage) for stage in stages}


@pytest.mark.part("M4", "NFR-PERF-4")
async def test_p_04__structured_measurements(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A structured turn (#153) observes ``retrieve`` once per retrieval and ``verify`` once per
    claim its deterministic path checks: with the stub's renders all denying the planted
    lantern, the turn's render and the repair's re-check each verify the claim.

    Complement asserted: a Lite turn observes neither stage, and a structured turn whose
    render declares no claim observes ``retrieve`` but not ``verify``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")
        await structured_load.plant(
            epistrel, RequestContext(principal=WRITE["X-Epistrel-Principal"]), story, ids.values()
        )
        before = _observed(STRUCTURED_STAGES)
        played = await respond(client, story, body(ids["Wren"], "Do you still have it?"))
        assert played.status_code == 200, played.text
        bare = {k: _stage_count(k) - before[k] for k in STRUCTURED_STAGES}
        assert bare["retrieve"] >= 1
        assert bare["verify"] == 0

        stub.render_claims = structured_load.render_claims(1.0)
        before = _observed(STRUCTURED_STAGES)
        played = await respond(client, story, body(ids["Wren"], "Is that your lantern?"))
        assert played.status_code == 200, played.text
        claimed = {k: _stage_count(k) - before[k] for k in STRUCTURED_STAGES}
        assert claimed["retrieve"] >= 1
        assert claimed["verify"] >= 1

        lite, lite_ids = await new_story(client, PLAYER, FIRST)
        before = _observed(STRUCTURED_STAGES)
        played = await respond(client, lite, body(lite_ids[PLAYER], "Hello."))
        assert played.status_code == 200, played.text
        assert {k: _stage_count(k) - before[k] for k in STRUCTURED_STAGES} == {
            "retrieve": 0.0,
            "verify": 0.0,
        }


def test_p_04__cleanup_tolerates_a_busy_story(monkeypatch: pytest.MonkeyPatch) -> None:
    """Found by #153's structured run: a story whose queue the drain did not wait out answers
    the cleanup's ``DELETE`` with 423, which used to abort the run after its report was written
    (exit 1 on a host class that is never gated). The story is now logged and left.

    Complement asserted: the other stories are still deleted."""
    deleted: list[str] = []

    def request(target: Target, method: str, path: str, **query: Any) -> None:
        if path.endswith("/busy"):
            raise urllib.error.HTTPError(path, 423, "Locked", Message(), None)
        deleted.append(path)

    monkeypatch.setattr(smoke, "request", request)
    stories = [StoryRef(sid, "p", "o", 10, i) for i, sid in enumerate(("a", "busy", "b"))]
    left = smoke.delete_stories(Target.from_env(), stories)
    assert left == ["busy"]
    assert deleted == ["/stories/a", "/stories/b"]
