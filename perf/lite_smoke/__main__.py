"""``uv run python -m perf.lite_smoke --minutes 30 --host-class mac|reference|ci
[--scenario lite|structured]`` (P-04; #76, #153).

See the package docstring. The steps: a fresh ``epistrel-perf`` database; the seed (the cached
``pg_dump`` restored when its key matches, else seeded through the library and dumped); the
engine (stub model, 2 s per call); a ``/metrics`` snapshot; the open-loop Locust run beside the
5 s sampler; another snapshot; the queues drained; every submitted turn reconciled against
``history``; the report written; the seeded stories deleted and the stack stopped. Exit 1 only
when the host class is ``reference`` and the gate fails. Locust's own HTML report is kept in the
run's working directory (``build/perf/run-<stamp>/locust.html``).

``--scenario structured`` (#153) seeds ``perf.lite_smoke.structured``'s stories (cached as
``structured-<key>.dump``), layers ``compose.structured.yml`` over the stack, plays one
single-character turn per story every 60 s and writes ``<stamp>-structured.md``.
``--memories N`` scales its stories down for a demo run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from perf.lite_smoke import (
    CACHE_DIR,
    COMPOSE_FILES,
    FULL_MINUTES,
    HOST_CLASSES,
    INTERVAL_S,
    PROJECT,
    REPO_ROOT,
    RESULTS_DIR,
    SCENARIOS,
    SCRATCH_DIR,
    STOP_BEFORE_S,
    STORIES,
    STRUCTURED_INTERVAL_S,
    STUB_LATENCY_S,
    Target,
)
from perf.lite_smoke import report as reporting
from perf.lite_smoke.sample import Sampler, scrape, stage_buckets
from perf.lite_smoke.seed import StoryRef, cache_key, read_stories, seed, write_stories

TICK = re.compile(r"\[tick (\d+)\]")
DRAIN_S = 120.0
FILES: list[Path] = list(COMPOSE_FILES)
"""The stack's compose files: the Lite stack's, plus ``compose.structured.yml`` for the
structured variant (``main`` sets it)."""


def log(message: str) -> None:
    print(f"[lite_smoke {datetime.now(UTC):%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def compose(*args: str, stdin: Any = None, stdout: Any = None) -> None:
    command = ["docker", "compose", "-p", PROJECT]
    for path in FILES:
        command += ["-f", str(path)]
    subprocess.run(  # noqa: S603 - fixed argv
        [*command, *args], cwd=REPO_ROOT, check=True, stdin=stdin, stdout=stdout
    )


def request(target: Target, method: str, path: str, **query: Any) -> Any:
    url = f"{target.base_url}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    sent = urllib.request.Request(url, method=method, headers=target.headers())  # noqa: S310
    with urllib.request.urlopen(sent, timeout=60) as answer:  # noqa: S310
        body = answer.read()
    return json.loads(body) if body else None


def wait_healthy(target: Target, timeout_s: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(f"{target.base_url}/health", timeout=5) as answer:  # noqa: S310
                if json.loads(answer.read())["status"] in ("ok", "degraded"):
                    return
        except urllib.error.URLError, OSError, ValueError, KeyError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("the engine did not become healthy")
        time.sleep(2)


def migrate(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    command.upgrade(config, "head")


def prepare_stories(
    target: Target,
    *,
    stories: int,
    turns: int,
    use_compose: bool,
    scenario: str = "lite",
    spec: Any = None,
) -> list[StoryRef]:
    """The seeded stories: restored from the cached dump when its key matches, else seeded
    (and, with the compose stack, dumped for the next run). ``spec`` is the structured
    variant's story spec."""
    if scenario == "structured":
        from perf.lite_smoke import structured

        key = structured.cache_key(stories, spec)
    else:
        key = cache_key(stories, turns)
    dump = CACHE_DIR / f"{scenario}-{key}.dump"
    listing = CACHE_DIR / f"{scenario}-{key}.json"
    if use_compose and dump.exists() and listing.exists():
        log(f"restoring the cached seed {dump.name}")
        with dump.open("rb") as source:
            compose(
                "exec",
                "-T",
                "db",
                "pg_restore",
                "-U",
                "epistrel",
                "-d",
                "epistrel",
                "--no-owner",
                "--exit-on-error",
                stdin=source,
            )
        # pg_restore leaves the planner without statistics; without them the first commits
        # pick plans that take seconds until autovacuum analyzes the tables.
        compose(
            "exec", "-T", "db", "vacuumdb", "-U", "epistrel", "-d", "epistrel", "--analyze-only"
        )
        return read_stories(listing)
    migrate(target.dsn)
    started = time.monotonic()
    if scenario == "structured":
        from perf.lite_smoke import structured

        log(f"seeding {stories} structured stories of {spec} (cache key {key})")
        found = asyncio.run(structured.seed(target, stories=stories, spec=spec, log=log))
    else:
        log(f"seeding {stories} stories of {turns} turns (cache key {key})")
        found = asyncio.run(seed(target, stories=stories, turns=turns, log=log))
    log(f"seeded in {time.monotonic() - started:.0f} s")
    if use_compose:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        partial = dump.with_suffix(".partial")
        with partial.open("wb") as sink:
            compose(
                "exec",
                "-T",
                "db",
                "pg_dump",
                "-U",
                "epistrel",
                "-d",
                "epistrel",
                "-Fc",
                stdout=sink,
            )
        partial.rename(dump)
        write_stories(listing, found)
    return found


def drain(target: Target, timeout_s: float = DRAIN_S) -> None:
    """Wait for every story's queue to empty (turns still queued at the end settle)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        depths = [
            value
            for (name, _), value in scrape(target.base_url).items()
            if name == "epistrel_turn_queue_depth"
        ]
        if not any(depths):
            time.sleep(5)  # the running turn of each story commits too
            return
        time.sleep(2)


def reconcile(target: Target, story: StoryRef, submitted: list[int]) -> dict[str, list[int]]:
    """The story's ticks as ``history`` holds them: submitted but absent (lost), or present
    more than once or never submitted (duplicated)."""
    viewpoint = f"character:{story.player_id}"
    seen: Counter[int] = Counter()
    before: int | None = None
    while True:
        query: dict[str, Any] = {"limit": 1000, "viewpoint": viewpoint}
        if before is not None:
            query["before_turn"] = before
        page = request(target, "GET", f"/stories/{story.story_id}/history", **query)
        reached = False
        for item in page["items"]:
            if item.get("turn") is not None and item["turn"] <= story.turns:
                reached = True
                continue
            if item.get("beat") == 1:
                found = TICK.search(item.get("text") or "")
                if found:
                    seen[int(found.group(1))] += 1
        before = page.get("next_before_turn")
        if reached or before is None:
            break
    wanted = set(submitted)
    return {
        "lost": sorted(t for t in wanted if seen[t] == 0),
        "duplicated": sorted(t for t, n in seen.items() if n > 1 or t not in wanted),
    }


def delete_stories(target: Target, stories: list[StoryRef]) -> list[str]:
    """Delete the seeded stories after the report is written; answers the ones that could not
    be deleted. A story still busy with a turn the drain did not wait out answers 423 (or any
    HTTP error): it is logged and left (the compose stack's volume goes with it), so the run's
    report and exit status stand."""
    left: list[str] = []
    for story in stories:
        try:
            request(target, "DELETE", f"/stories/{story.story_id}")
        except urllib.error.HTTPError as exc:
            log(f"story {story.story_id} not deleted: HTTP {exc.code}")
            left.append(story.story_id)
    return left


def host_description() -> str:
    cpus = os.cpu_count() or 0
    memory = ""
    try:
        if sys.platform == "darwin":
            raw = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            memory = f", {int(raw) / 2**30:.0f} GB RAM"
        else:
            with open("/proc/meminfo") as meminfo:
                kib = int(meminfo.readline().split()[1])
            memory = f", {kib / 2**20:.0f} GB RAM"
    except OSError, ValueError, IndexError:
        pass
    return f"{platform.system()} {platform.machine()}, {cpus} CPUs{memory}"


def run_locust(
    target: Target,
    work: Path,
    minutes: float,
    start: float,
    *,
    interval_s: float = INTERVAL_S,
    scenario: str = "lite",
) -> int:
    env = {
        **os.environ,
        "EPISTREL_PERF_STORIES": str(work / "stories.json"),
        "EPISTREL_PERF_RECORDS": str(work / "records.jsonl"),
        "EPISTREL_PERF_INTERVAL": str(interval_s),
        "EPISTREL_PERF_SCENARIO": scenario,
        "EPISTREL_PERF_START": str(start),
        "EPISTREL_PERF_SUBMIT_UNTIL": str(minutes * 60 - STOP_BEFORE_S),
    }
    users = len(json.loads((work / "stories.json").read_text()))
    command = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(REPO_ROOT / "perf" / "lite_smoke" / "locustfile.py"),
        "--headless",
        "--users",
        str(users),
        "--spawn-rate",
        str(users),
        "--run-time",
        f"{max(1, round(minutes * 60))}s",
        "--stop-timeout",
        "5",
        "--host",
        target.base_url,
        "--only-summary",
        "--csv",
        str(work / "locust"),
        "--html",
        str(work / "locust.html"),
    ]
    return subprocess.run(command, cwd=REPO_ROOT, env=env, check=False).returncode  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m perf.lite_smoke", description=__doc__)
    parser.add_argument("--minutes", type=float, default=FULL_MINUTES)
    parser.add_argument("--host-class", choices=HOST_CLASSES, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="lite")
    parser.add_argument("--stories", type=int, default=STORIES)
    parser.add_argument(
        "--turns", type=int, default=None, help="seeded turns per story (demo runs; default 10,000)"
    )
    parser.add_argument(
        "--memories",
        type=int,
        default=None,
        help="memories per structured story (demo runs; default 10,000 with 20,000 events)",
    )
    parser.add_argument("--out", type=Path, default=None, help="report directory")
    parser.add_argument(
        "--no-compose", action="store_true", help="use the running target (EPISTREL_PERF_*)"
    )
    parser.add_argument("--keep", action="store_true", help="keep the stories and the stack")
    args = parser.parse_args(argv)
    if args.minutes * 60 <= STOP_BEFORE_S:
        parser.error("--minutes must be more than 2 (submissions stop 2 minutes before the end)")
    target = Target.from_env()
    use_compose = not args.no_compose
    scenario: str = args.scenario
    spec: Any = None
    interval_s = INTERVAL_S
    if scenario == "structured":
        from perf.lite_smoke import structured

        FILES[:] = [*COMPOSE_FILES, structured.COMPOSE_FILE]
        spec = structured.SPEC
        if args.memories is not None:
            spec = type(spec).scaled(args.memories)
        interval_s = STRUCTURED_INTERVAL_S
    started = datetime.now(UTC)
    stamp = started.strftime("%Y-%m-%d-%H%M")
    full = args.minutes >= FULL_MINUTES
    out = args.out or (RESULTS_DIR if full else SCRATCH_DIR / "results")
    work = SCRATCH_DIR / f"run-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if sha and dirty:
        sha += " + uncommitted changes"

    if use_compose:
        log("starting a fresh epistrel-perf database")
        compose("down", "-v", "--remove-orphans")
        compose("up", "-d", "--wait", "db")
    from epistrel.testing import perf

    turns = spec.turns if spec is not None else (args.turns or perf.TURNS)
    stories = prepare_stories(
        target,
        stories=args.stories,
        turns=turns,
        use_compose=use_compose,
        scenario=scenario,
        spec=spec,
    )
    write_stories(work / "stories.json", stories)
    try:
        if use_compose:
            log("starting the engine (stub model, 2 s per call)")
            compose("up", "-d", "--build", "--wait", "engine")
        wait_healthy(target)
        before = scrape(target.base_url)
        start = time.time()
        sampler = Sampler(target.base_url, target.dsn, clock=lambda: time.time() - start)
        sampler.start()
        log(f"driving {len(stories)} stories for {args.minutes:g} minutes")
        try:
            code = run_locust(
                target, work, args.minutes, start, interval_s=interval_s, scenario=scenario
            )
        finally:
            sampler.stop()
        after = scrape(target.base_url)
        log(f"locust exited {code}; draining the queues")
        drain(target)
        records = [
            json.loads(line)
            for line in (work / "records.jsonl").read_text().splitlines()
            if line.strip()
        ]
        reconciled = {
            s.story_id: reconcile(
                target,
                s,
                [
                    r["tick"]
                    for r in records
                    if r["story_id"] == s.story_id and r["event"] == "submit"
                ],
            )
            for s in stories
        }
        info = reporting.RunInfo(
            started=started.strftime("%Y-%m-%d %H:%M UTC"),
            minutes=args.minutes,
            interval_s=interval_s,
            stories=len(stories),
            host_class=args.host_class,
            host=host_description(),
            engine_sha=sha or "unknown",
            stub_latency_s=STUB_LATENCY_S,
            turns=turns,
            scenario=scenario,
            events=spec.events if spec is not None else None,
            memories=spec.memories if spec is not None else 0,
        )
        evaluation = reporting.evaluate(
            info,
            [(s.story_id, s.index) for s in stories],
            records,
            sampler.samples,
            stage_buckets(before, after),
            reconciled,
        )
        out.mkdir(parents=True, exist_ok=True)
        page = out / f"{stamp}-{scenario}.md"
        page.write_text(reporting.markdown(evaluation))
        page.with_suffix(".json").write_text(
            json.dumps(reporting.sidecar(evaluation, records, sampler.samples), indent=1) + "\n"
        )
        verdict = reporting.PASS if evaluation.passed else reporting.FAIL
        log(f"{verdict}: {page}")
        if not args.keep:
            delete_stories(target, stories)
    finally:
        if use_compose and not args.keep:
            compose("down", "-v", "--remove-orphans")
    return reporting.exit_code(evaluation)


if __name__ == "__main__":
    sys.exit(main())
