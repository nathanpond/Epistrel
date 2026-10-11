"""P-04: the reference workload (docs/04), the M2 part: the Lite variant (NFR-PERF-4; #76).

The workload itself is ``uv run python -m perf.lite_smoke --minutes 30 --host-class …``: ten
20,000-event stories driven open-loop by Locust against the compose stack for 30 minutes, too
long and too heavy for a test session. Its evidence is the committed report in
``perf/results/``. What runs here, in PR CI:

- ``test_p_04``: a full-length report (30 minutes, 10,000-turn stories) newer than 30 days is
  committed, states its host class and verdict, and has its raw JSON sidecar;
- ``test_p_04__gate``: the report's gate logic (``perf.lite_smoke.report``) over a synthetic run:
  a run that keeps up passes, and one story falling behind fails the run although the aggregate
  keeps up;
- ``test_p_04__measurements``: the engine measurements the run reads: a queued turn reports its
  queue wait and its delay to the first model call (respond result and ``GET /operations``), the
  per-story queue-depth gauge counts it while it waits, and every stage of
  ``epistrel_turn_stage_seconds`` is observed once per committed turn.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from perf.lite_smoke import RESULTS_DIR, report
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.observability import metrics
from epistrel.observability.stages import STAGES
from epistrel.testing import perf
from tests.auth_support import auth_settings
from tests.respond_support import FIRST, PLAYER, body, new_story, respond, respond_client
from tests.story_support import READ

FRESH = timedelta(days=30)
FULL_MINUTES = 30


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
    now = datetime.now(UTC)
    fresh = []
    for path in reports:
        found = report.header(path.read_text())
        assert found.host_class in ("mac", "reference", "ci"), path
        assert path.with_suffix(".json").exists(), f"{path.name} has no JSON sidecar"
        full = found.minutes >= FULL_MINUTES and found.turns == perf.TURNS
        if full and now - found.started <= FRESH:
            fresh.append((path, found))
    assert fresh, (
        "no full-length P-04 report from the last 30 days in perf/results/: run "
        "`uv run python -m perf.lite_smoke --minutes 30 --host-class <class>` and commit it"
    )
    sidecar = json.loads(fresh[-1][0].with_suffix(".json").read_text())
    assert sidecar["info"]["host_class"] == fresh[-1][1].host_class
    assert sidecar["passed"] == (fresh[-1][1].verdict == report.PASS)

    text = fresh[-1][0].read_text()
    with pytest.raises(ValueError, match="host class"):
        report.header(text.replace("(host class ", "(class "))
    stale = text.replace(fresh[-1][1].started.strftime("%Y-%m-%d %H:%M"), "2000-01-01 00:00", 1)
    assert now - report.header(stale).started > FRESH


def _run(info: report.RunInfo, *, late_story: str | None = None) -> list[dict[str, Any]]:
    """Synthetic driver records: every story submits every 15 s until 2 minutes before the end
    and each turn settles 2.1 s later, except ``late_story``, whose turns take 40 s each from
    minute 6 on (a backlog builds, and waits pass one interval)."""
    records: list[dict[str, Any]] = []
    for index, story in enumerate(("s1", "s2")):
        free = 0.0
        tick = 0
        while (due := index * 7.5 + tick * info.interval_s) <= info.end_s - 120:
            took = 40.0 if story == late_story and due >= 360 else 2.1
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

    slow = {"0.1": 0.0, "0.25": 70.0, "0.5": 100.0, "+Inf": 100.0}
    assert not report.evaluate(info, stories, _run(info), samples, {"commit": slow}, {}).passed


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
