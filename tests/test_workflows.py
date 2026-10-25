"""Every GitHub Actions workflow is pinned, least-privilege, and downloads nothing floating."""

from __future__ import annotations

import ast
import fnmatch
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import REPO_ROOT

WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
PINNED = re.compile(r"@v\d+(\.\d+){0,2}$")  # a major tag, or an exact release where no major exists
LOCAL = ("./", "docker://")


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def _scalars(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [s for v in node.values() for s in _scalars(v)]
    if isinstance(node, list):
        return [s for v in node for s in _scalars(v)]
    return [node]


def _uses(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [str(node["uses"])] if "uses" in node else []
        return found + [u for v in node.values() for u in _uses(v)]
    if isinstance(node, list):
        return [u for v in node for u in _uses(v)]
    return []


def test_there_are_workflows() -> None:
    assert WORKFLOWS, "no workflow files under .github/workflows/"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_uses_is_pinned_to_a_major(path: Path) -> None:
    unpinned = [u for u in _uses(_load(path)) if not u.startswith(LOCAL) and not PINNED.search(u)]
    assert unpinned == [], f"{path.name}: pin at a version tag (@vN or @vN.N.N): {unpinned}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_top_level_permissions_declared(path: Path) -> None:
    workflow = _load(path)
    assert "permissions" in workflow, f"{path.name}: declare top-level permissions:"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_nothing_floats_to_latest(path: Path) -> None:
    floating = [s for s in _scalars(_load(path)) if isinstance(s, str) and s.strip() == "latest"]
    assert floating == [], f"{path.name}: a value is `latest`; pin an exact release"


def _workflow(name: str) -> dict[str, Any]:
    return _load(REPO_ROOT / ".github" / "workflows" / name)


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    """The `on:` block — PyYAML parses the bare key `on` as the boolean True."""
    raw: dict[Any, Any] = workflow
    triggers: dict[str, Any] = raw["on"] if "on" in raw else raw[True]
    return triggers


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return steps


def test_publish_edge_never_cancels_and_reuses_the_gate() -> None:
    wf = _workflow("publish-edge.yml")
    assert wf["concurrency"] == {"group": "publish-edge", "cancel-in-progress": False}
    assert wf["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert "ref" in _on(wf)["workflow_dispatch"]["inputs"]
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "read", "packages": "write"}


def test_release_triggers_only_on_v_tags_and_withholds_latest_by_flavor() -> None:
    wf = _workflow("release.yml")
    assert _on(wf) == {"push": {"tags": ["v*"]}}
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"
    meta = next(s for s in _steps(wf, "publish") if "metadata-action" in str(s.get("uses")))
    assert "latest=auto" in meta["with"]["flavor"]
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "write", "packages": "write"}
    assert "permissions" not in wf["jobs"]["validate"] or "write" not in str(
        wf["jobs"]["validate"]["permissions"]
    )


def test_rollback_is_dispatch_only_with_the_right_inputs() -> None:
    wf = _workflow("rollback.yml")
    inputs = _on(wf)["workflow_dispatch"]["inputs"]
    assert list(_on(wf)) == ["workflow_dispatch"]
    assert inputs["good_version"]["required"] is True
    assert inputs["bad_version"]["required"] is False
    assert wf["concurrency"] == {"group": "release-latest", "cancel-in-progress": False}
    assert wf["jobs"]["rollback"]["if"] == "github.ref == 'refs/heads/main'"


def test_chaos_runs_weekly_and_on_dispatch_and_reports_a_red_run() -> None:
    """#75: the whole `chaos` suite weekly (Monday 04:00 UTC) and on dispatch with a seed and an
    iteration count; 60 minutes; a red run files or comments on an issue."""
    wf = _workflow("chaos.yml")
    triggers = _on(wf)
    assert triggers["schedule"] == [{"cron": "0 4 * * 1"}]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"seed", "iterations"}
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["chaos"]
    assert job["timeout-minutes"] == 60
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    run = next(s for s in _steps(wf, "chaos") if s.get("id") == "chaos")
    assert "-m chaos" in run["run"]
    assert run["env"]["EPISTREL_CHAOS_ITERATIONS"] == "${{ inputs.iterations || '500' }}"
    report = next(s for s in _steps(wf, "chaos") if s.get("name") == "Report a red run")
    assert "Weekly chaos run failed" in report["run"]


CHAOS_SEEDED = ("test_x_03.py", "test_x_04.py", "test_x_05.py")


def _seed_print_leads() -> dict[str, list[str]]:
    """Per seeded chaos file, the whitespace each `seed=` print starts with. Each seed print
    must also be the first print in its function (asserted here): it is the line pytest's `-s`
    output prefixes."""
    leads: dict[str, list[str]] = {}
    for name in CHAOS_SEEDED:
        tree = ast.parse((REPO_ROOT / "tests" / "chaos" / name).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            prints = sorted(
                (
                    c
                    for c in ast.walk(fn)
                    if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Name)
                    and c.func.id == "print"
                    and isinstance(c.args[0], ast.JoinedStr)
                ),
                key=lambda c: c.lineno,
            )
            for i, call in enumerate(prints):
                fstring = call.args[0]
                assert isinstance(fstring, ast.JoinedStr)
                parts = [v.value for v in fstring.values if isinstance(v, ast.Constant)]
                if not any("seed=" in str(p) for p in parts):
                    continue
                assert i == 0, f"{name}:{call.lineno}: the seed print is not {fn.name}'s first"
                first = fstring.values[0]
                text = str(first.value) if isinstance(first, ast.Constant) else ""
                leads.setdefault(name, []).append(text[: len(text) - len(text.lstrip())])
    return leads


def test_chaos_seed_lines_start_a_line_of_their_own() -> None:
    """#309: under `-s` pytest writes the file path or progress dots ahead of a test's first
    print, so each seed print starts with a newline to stand on a line of its own."""
    leads = _seed_print_leads()
    assert set(leads) == set(CHAOS_SEEDED)
    late = {n: ls for n, ls in leads.items() if not all(lead.startswith("\n") for lead in ls)}
    assert late == {}, "a chaos seed print does not start with a newline"


def _chaos_greps() -> dict[str, str]:
    """The `grep -E` pattern of each chaos.yml step that reads the seed lines, by step name."""
    steps = {s.get("name"): s.get("run", "") for s in _steps(_workflow("chaos.yml"), "chaos")}
    found = {}
    for name, needle in (("Summarise", "X-0"), ("Report a red run", "seed=")):
        (pattern,) = [
            p
            for p in re.findall(r"grep -E '([^']+)' build/chaos/chaos.log", steps[name])
            if needle in p
        ]
        found[name] = pattern
    return found


def test_chaos_greps_find_the_seeds_in_pytest_s_output() -> None:
    """#309: the step summary and a red run's issue list every seed, whatever pytest writes
    before a test's first print, and the summary leaves out a failed test's source lines."""
    leads = _seed_print_leads()
    seeds = [
        ("tests/chaos/test_x_03.py ", "test_x_03.py", "X-03 seed=1883324247 iterations=500 ops=10"),
        ("..", "test_x_04.py", "X-04 seed=1869950226 iterations=500 keys=20"),
        (".", "test_x_04.py", "X-04 slow-engine seed=1869950226 iterations=500 keys=6"),
        ("tests/chaos/test_x_05.py ", "test_x_05.py", "X-05 seed=1072740067 iterations=500"),
    ]
    lines = [
        (p, n, seed, seed.split(" seed=")[0] + " outcomes={'ok': 500}") for p, n, seed in seeds
    ]
    log = "".join(f"{p}{leads[n][0]}{seed}\n{tally}\n" for p, n, seed, tally in lines)
    log += '>       print(f"X-03 seed={SEED} iterations={ITERATIONS} ops={OPS}")\n'
    log += "E       AssertionError: seed=1883324247: no crash point fired\n"
    expected = {
        "Summarise": [line for _, _, seed, tally in lines for line in (seed, tally)],
        "Report a red run": [seed for _, _, seed, _ in lines],
    }
    grep = shutil.which("grep")
    assert grep is not None, "the workflow's greps need a grep on PATH"
    for step, pattern in _chaos_greps().items():
        out = subprocess.run(
            [grep, "-E", pattern], input=log, capture_output=True, text=True, check=False
        ).stdout.splitlines()
        assert out == expected[step], f"{step}: {pattern!r} printed {out}"


def test_perf_runs_nightly_and_on_dispatch_and_reports_a_red_run() -> None:
    """#76: P-03 at full size and its quick subset every night (03:00 UTC) and on dispatch, with
    the benchmark timings uploaded; a red run files or comments on an issue."""
    wf = _workflow("perf.yml")
    triggers = _on(wf)
    assert {"cron": "0 3 * * *"} in triggers["schedule"]
    assert "workflow_dispatch" in triggers
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["perf"]
    assert "'0 3 * * *'" in job["if"]
    assert "inputs.suite == 'nightly'" in job["if"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    run = next(s for s in _steps(wf, "perf") if s.get("id") == "perf")
    assert '-m "perf_quick or perf_long"' in run["run"]
    assert "--benchmark-json=" in run["run"]
    report = next(s for s in _steps(wf, "perf") if s.get("name") == "Report a red run")
    assert "Nightly perf run failed" in report["run"]


def test_perf_runs_the_scale_suite_weekly_and_opens_a_docs_pr() -> None:
    """#153: the scale suite every Sunday (03:00 UTC) and on dispatch (``suite: weekly``) on the
    GitHub-hosted runner, labelled ``github-runner``, with the fixture cached by its key; only
    the ``perf_weekly`` tests run there; a red run files or comments on an issue; the docs job
    renders docs/perf.md and docs/capacity.md red or green and opens the rolling PR, and is the
    only job that may write contents or pull requests.

    Complement asserted: the nightly job does not run on the weekly schedule, the weekly job not
    on the nightly one, and PR CI names no ``perf_weekly``."""
    wf = _workflow("perf.yml")
    triggers = _on(wf)
    assert {"cron": "0 3 * * 0"} in triggers["schedule"]
    assert triggers["workflow_dispatch"]["inputs"]["suite"]["options"] == ["nightly", "weekly"]
    weekly, docs = wf["jobs"]["weekly"], wf["jobs"]["weekly-docs"]
    assert "'0 3 * * 0'" in weekly["if"]
    assert "'0 3 * * *'" not in weekly["if"]
    assert "'0 3 * * 0'" not in wf["jobs"]["perf"]["if"]
    assert weekly["permissions"] == {"contents": "read", "issues": "write"}
    assert weekly["timeout-minutes"] == 180
    assert weekly["env"]["EPISTREL_TEST_PERF_HOST"] == "github-runner"
    assert "EPISTREL_TEST_PERF_CACHE_DIR" in weekly["env"]
    steps = _steps(wf, "weekly")
    (cache,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")]
    assert "steps.fixture.outputs.key" in cache["with"]["key"]
    run = next(s for s in steps if s.get("id") == "weekly")
    assert "-m perf_weekly" in run["run"]
    for name in ("test_p_01.py", "test_p_02.py", "test_p_06.py"):
        assert f"tests/perf/{name}" in run["run"]
    report = next(s for s in steps if s.get("name") == "Report a red run")
    assert "Weekly perf run failed" in report["run"]
    assert docs["needs"] == ["weekly", "weekly-structured", "weekly-multi"]
    assert "failure" in docs["if"]
    assert docs["permissions"] == {"contents": "write", "pull-requests": "write"}
    others = {n: j.get("permissions", {}) for n, j in wf["jobs"].items() if n != "weekly-docs"}
    assert all(p.get("contents") == "read" for p in others.values()), others
    assert all("pull-requests" not in p for p in others.values()), others
    docs_steps = _steps(wf, "weekly-docs")
    assert any("python -m epistrel.tools.perf_docs" in s.get("run", "") for s in docs_steps)
    (pr,) = [s for s in docs_steps if str(s.get("uses", "")).startswith("peter-evans/")]
    assert pr["with"]["branch"] == "perf/weekly"
    assert pr["with"]["labels"] == "performance"
    assert set(pr["with"]["add-paths"].split()) == {
        "docs/perf.md",
        "docs/capacity.md",
        "perf/results/*-structured.md",
        "perf/results/*-structured.json",
        "perf/results/*-multi.md",
        "perf/results/*-multi.json",
    }
    (pytest_step,) = [
        s for s in _steps(_workflow("ci.yml"), "test") if "pytest" in s.get("run", "")
    ]
    assert "perf_weekly" not in pytest_step["run"]


def test_perf_runs_the_structured_workload_weekly() -> None:
    """#153 (P-04 M4): the structured variant runs on the weekly schedule and on dispatch, on the
    GitHub-hosted runner as host class ``ci`` for 30 minutes, with Locust from the ``perf``
    group and its seed cached by its key; it fails on failed, lost or duplicated turns (the
    gate itself is reported there, not enforced), uploads the report, its sidecar and Locust's
    HTML report, and files or comments on the weekly issue when red; the docs job commits the
    report.

    Complement asserted: it does not run on the nightly schedule, and it may not write
    contents."""
    wf = _workflow("perf.yml")
    job = wf["jobs"]["weekly-structured"]
    assert "'0 3 * * 0'" in job["if"]
    assert "inputs.suite == 'weekly'" in job["if"]
    assert "'0 3 * * *'" not in job["if"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    assert job["timeout-minutes"] == 180
    steps = _steps(wf, "weekly-structured")
    assert any(s.get("run") == "uv sync --frozen --group perf" for s in steps)
    (cache,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")]
    assert cache["with"]["path"] == "build/perf/cache"
    assert "steps.seed.outputs.key" in cache["with"]["key"]
    run = next(s for s in steps if s.get("id") == "structured")
    assert (
        "python -m perf.lite_smoke --scenario structured --minutes 30 --host-class ci" in run["run"]
    )
    check = next(s for s in steps if s.get("id") == "turns")
    for field in ('f["failed"]', 'f["lost"]', 'f["duplicated"]', "sys.exit(1"):
        assert field in check["run"]
    (upload,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert upload["with"]["name"] == "perf-structured"
    assert any("locust.html" in s.get("run", "") for s in steps)
    report = next(s for s in steps if s.get("name") == "Report a red run")
    assert "Weekly perf run failed" in report["run"]
    docs_steps = _steps(wf, "weekly-docs")
    assert any(s.get("with", {}).get("name") == "perf-structured" for s in docs_steps)
    assert any("perf/results/" in s.get("run", "") for s in docs_steps)


def test_perf_runs_the_multi_character_workload_weekly() -> None:
    """#182 (P-04 M5): the multi-character variant runs on the weekly schedule and on dispatch,
    after the structured one, on the GitHub-hosted runner as host class ``ci`` for 30 minutes,
    with Locust from the ``perf`` group and its own seed cached by its key; it fails on failed,
    lost or duplicated turns and on a reduced, deferred or escalation-deferred character (the
    gate itself is reported there, not enforced), uploads the report, its sidecar and Locust's
    HTML report, and files or comments on the weekly issue when red; the docs job downloads it
    and commits the report. PR CI's test job syncs the ``perf`` group for the multi smoke run.

    Complement asserted: it does not run on the nightly schedule, and it may not write
    contents."""
    wf = _workflow("perf.yml")
    job = wf["jobs"]["weekly-multi"]
    assert job["needs"] == ["weekly-structured"]
    assert job["if"].startswith("always() && (")
    assert "'0 3 * * 0'" in job["if"]
    assert "inputs.suite == 'weekly'" in job["if"]
    assert "'0 3 * * *'" not in job["if"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    assert job["timeout-minutes"] == 180
    steps = _steps(wf, "weekly-multi")
    assert any(s.get("run") == "uv sync --frozen --group perf" for s in steps)
    (cache,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")]
    assert cache["with"]["path"] == "build/perf/cache"
    assert cache["with"]["key"] == "p04-multi-${{ steps.seed.outputs.key }}"
    run = next(s for s in steps if s.get("id") == "multi")
    assert "python -m perf.lite_smoke --scenario multi --minutes 30 --host-class ci" in run["run"]
    check = next(s for s in steps if s.get("id") == "turns")
    for field in ('f["failed"]', 'f["lost"]', 'f["duplicated"]', "reduced, deferred", "sys.exit(1"):
        assert field in check["run"]
    (upload,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert upload["with"]["name"] == "perf-multi"
    assert any("locust.html" in s.get("run", "") for s in steps)
    report = next(s for s in steps if s.get("name") == "Report a red run")
    assert "Weekly perf run failed" in report["run"]
    docs_steps = _steps(wf, "weekly-docs")
    assert any(s.get("with", {}).get("name") == "perf-multi" for s in docs_steps)
    assert any("build/perf/multi/*-multi.md" in s.get("run", "") for s in docs_steps)
    ci_steps = _steps(_workflow("ci.yml"), "test")
    assert any(s.get("run") == "uv sync --frozen --group perf" for s in ci_steps)


def test_redteam_runs_nightly_and_on_dispatch_and_reports_a_red_run() -> None:
    """#145: R-01's full corpus (`redteam_long`) and the PR subset every night (03:30 UTC) and
    on dispatch, with the log uploaded; a red run files or comments on an issue. PR CI names
    no `redteam_long`, so the conftest deselects the full corpus there (the complement)."""
    wf = _workflow("redteam.yml")
    triggers = _on(wf)
    assert triggers["schedule"] == [{"cron": "30 3 * * *"}]
    assert "workflow_dispatch" in triggers
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["redteam"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    run = next(s for s in _steps(wf, "redteam") if s.get("id") == "redteam")
    assert '-m "redteam or redteam_long"' in run["run"]
    report = next(s for s in _steps(wf, "redteam") if s.get("name") == "Report a red run")
    assert "Nightly red-team run failed" in report["run"]
    # #170: PRIV's extended run (5,000 examples), reported like the corpus.
    priv = next(s for s in _steps(wf, "redteam") if s.get("id") == "priv")
    assert priv["env"] == {"EPISTREL_HYPOTHESIS_PROFILE": "nightly"}
    assert "tests/property/test_priv.py" in priv["run"]
    assert "steps.priv.outcome == 'failure'" in report["if"]
    (pytest_step,) = [
        s for s in _steps(_workflow("ci.yml"), "test") if "pytest" in s.get("run", "")
    ]
    assert "redteam_long" not in pytest_step["run"]


def test_soak_runs_weekly_and_on_dispatch_and_reports_a_red_run() -> None:
    """#147: X-01's soak every Sunday (05:00 UTC) and on dispatch, under a two-hour budget, with
    the log uploaded; a red run files or comments on an issue. PR CI names no `soak`, so the
    conftest deselects it there (the complement)."""
    wf = _workflow("soak.yml")
    triggers = _on(wf)
    assert triggers["schedule"] == [{"cron": "0 5 * * 0"}]
    assert "workflow_dispatch" in triggers
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["soak"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    assert job["timeout-minutes"] == 120
    run = next(s for s in _steps(wf, "soak") if s.get("id") == "soak")
    assert "-m soak" in run["run"]
    report = next(s for s in _steps(wf, "soak") if s.get("name") == "Report a red run")
    assert "Weekly soak run failed" in report["run"]
    (pytest_step,) = [
        s for s in _steps(_workflow("ci.yml"), "test") if "pytest" in s.get("run", "")
    ]
    assert "soak" not in pytest_step["run"]


def test_pr_ci_runs_only_the_quick_chaos_tests() -> None:
    """Complement of the weekly run: the gate deselects `chaos` tests not marked `chaos_quick`,
    and (#76) `perf_long` tests not marked `perf_quick`."""
    (pytest_step,) = [
        s for s in _steps(_workflow("ci.yml"), "test") if "pytest" in s.get("run", "")
    ]
    assert (
        '-m "not live_model and (not chaos or chaos_quick) and (not perf_long or perf_quick)"'
        in pytest_step["run"]
    )


def test_p04_expiry_reminds_daily_and_files_one_issue() -> None:
    """#283: every day (06:00 UTC) and on dispatch, `perf.lite_smoke.freshness` judges the newest
    full-length P-04 report; when the reminder is due, one "P-04 report expires" issue is opened
    with the rerun command.

    Complement asserted: the issue step runs only when the reminder is due, and files nothing
    while such an issue is open."""
    wf = _workflow("p04-expiry.yml")
    triggers = _on(wf)
    assert triggers["schedule"] == [{"cron": "0 6 * * *"}]
    assert "workflow_dispatch" in triggers
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["remind"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    check = next(s for s in _steps(wf, "remind") if s.get("id") == "freshness")
    assert "python -m perf.lite_smoke.freshness" in check["run"]
    assert '"$GITHUB_OUTPUT"' in check["run"]
    remind = next(s for s in _steps(wf, "remind") if s.get("name") == "Open the reminder")
    assert remind["if"] == "steps.freshness.outputs.due == 'true'"
    script = remind["run"]
    assert "--search 'in:title \"P-04 report expires\"'" in script
    assert script.index("exit 0") < script.index("gh issue create")
    assert "uv run python -m perf.lite_smoke --minutes 30 --host-class mac" in script
    assert '--title "$title"' in script


def _shards() -> list[dict[str, str]]:
    matrix = _workflow("ci.yml")["jobs"]["test"]["strategy"]["matrix"]
    shards: list[dict[str, str]] = matrix["shard"]
    return shards


def _test_files_under(path: Path) -> set[Path]:
    """The test modules pytest would collect under ``path`` (``python_files`` is the default
    ``test_*.py``; ``norecursedirs`` keeps the gate fixtures out)."""
    if path.is_file():
        return {path}
    fixtures = REPO_ROOT / "tests" / "gates" / "fixtures"
    return {p for p in path.rglob("test_*.py") if fixtures not in p.parents}


def _module_tests(path: Path) -> set[str]:
    """A test module's top-level test functions and classes: the nodes a ``path::name`` shard
    argument can name."""
    tree = ast.parse(path.read_text())
    return {
        node.name
        for node in tree.body
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        )
        or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
    }


Node = tuple[Path, str]


def _shard_nodes(spec: str) -> set[Node]:
    """The tests one shard's arguments select, with pytest's own semantics: positional paths or
    ``path::name`` node ids, minus ``--ignore`` paths, minus files whose absolute path (or a
    parent's) matches an ``--ignore-glob`` pattern made absolute against the repository root,
    minus ``--deselect`` node ids. A module is its top-level tests (``_module_tests``)."""
    selected: set[Node] = set()
    ignored: list[Path] = []
    globs: list[str] = []
    deselected: set[Node] = set()
    for word in spec.split():
        if word.startswith("--ignore="):
            ignored.append(REPO_ROOT / word.removeprefix("--ignore="))
        elif word.startswith("--ignore-glob="):
            globs.append(str(REPO_ROOT / word.removeprefix("--ignore-glob=")))
        elif word.startswith("--deselect="):
            path, _, name = word.removeprefix("--deselect=").partition("::")
            assert name in _module_tests(REPO_ROOT / path), f"no test to deselect {word!r}"
            deselected.add((REPO_ROOT / path, name))
        else:
            assert not word.startswith("-"), f"unexpected shard option {word!r}"
            path, _, name = word.partition("::")
            target = REPO_ROOT / path
            assert target.exists(), f"shard path {word!r} does not exist"
            if name:
                assert name in _module_tests(target), f"shard node {word!r} does not exist"
                selected.add((target, name))
            else:
                selected |= {(f, t) for f in _test_files_under(target) for t in _module_tests(f)}

    def kept(path: Path) -> bool:
        chain = [path, *path.parents]
        if any(p in ignored for p in chain):
            return False
        return not any(fnmatch.fnmatch(str(p), g) for p in chain for g in globs)

    return {n for n in selected if kept(n[0])} - deselected


def _shard_files(spec: str) -> set[Path]:
    """The test modules one shard runs any test of."""
    return {path for path, _ in _shard_nodes(spec)}


def _every_node() -> set[Node]:
    return {(f, t) for f in _test_files_under(REPO_ROOT / "tests") for t in _module_tests(f)}


def test_ci_shards_partition_the_test_files() -> None:
    """#331: the sharded `test` job runs every test under tests/ in exactly one shard. A module
    may be split across shards by node id (`path::test`, `--deselect=path::test`; #179's E-08).

    Complement asserted: a test no shard names (or two shards both name) is reported; the
    shard names are unique, since each names its coverage data file."""
    shards = _shards()
    names = [s["name"] for s in shards]
    assert len(names) == len(set(names)), names
    assert len(shards) > 1, names
    owners: dict[Node, list[str]] = {}
    for shard in shards:
        for node in _shard_nodes(shard["paths"]):
            owners.setdefault(node, []).append(shard["name"])
    every = _every_node()

    def show(node: Node) -> str:
        return f"{node[0].relative_to(REPO_ROOT)}::{node[1]}"

    missing = sorted(show(n) for n in every - set(owners))
    twice = {show(n): o for n, o in owners.items() if len(o) > 1}
    assert missing == [], f"no CI shard runs {missing}"
    assert twice == {}, f"more than one CI shard runs {twice}"
    assert set(owners) == every


def test_ci_shard_node_split_is_checked() -> None:
    """The node-id split is not vacuous: E-08's think-leak test runs in its own shard and the
    rest of the module in another; without the `--deselect`, both shards would run it."""
    by_name = {s["name"]: s["paths"] for s in _shards()}
    e08 = REPO_ROOT / "tests" / "evals" / "test_e_08.py"
    think = (e08, "test_e_08__m5_think_leak")
    assert _shard_nodes(by_name["eval-e08-think"]) == {think}
    rest = _shard_nodes(by_name["eval-e08"])
    assert think not in rest
    assert rest | {think} == {(e08, t) for t in _module_tests(e08)}
    undeselected = by_name["eval-e08"].split(" --deselect=")[0]
    assert think in _shard_nodes(undeselected)


def test_ci_shard_partition_check_sees_a_gap() -> None:
    """The partition check is not vacuous: without the catch-all shard, modules go missing; the
    two integration halves are disjoint and together cover tests/integration."""
    by_name = {s["name"]: _shard_files(s["paths"]) for s in _shards()}
    integration = _test_files_under(REPO_ROOT / "tests" / "integration")
    halves = [files for name, files in by_name.items() if name.startswith("integration-")]
    assert len(halves) == 2
    assert all(halves)
    assert halves[0].isdisjoint(halves[1])
    assert halves[0] | halves[1] == integration
    others = set().union(*(files for name, files in by_name.items() if name != "rest"))
    assert by_name["rest"]
    assert by_name["rest"].isdisjoint(others)
    assert _test_files_under(REPO_ROOT / "tests") - others == by_name["rest"]


def test_scenario_and_perf_share_a_ci_shard() -> None:
    """P-05 (tests/perf) gates the turn census of the S-tests (tests/scenario) run in the same
    session, so the two directories must not be split across shards."""
    scenario = _test_files_under(REPO_ROOT / "tests" / "scenario")
    p05 = REPO_ROOT / "tests" / "perf" / "test_p_05.py"
    (shard,) = [s["name"] for s in _shards() if p05 in _shard_files(s["paths"])]
    assert scenario <= _shard_files(next(s["paths"] for s in _shards() if s["name"] == shard))


def test_coverage_is_gated_once_on_the_combined_shards() -> None:
    """#331: each shard writes its own coverage data file without a per-shard threshold; the
    `coverage` job combines them all and fails under 90 %; `gate` is red if it does not
    succeed."""
    wf = _workflow("ci.yml")
    test = wf["jobs"]["test"]
    assert test["strategy"]["fail-fast"] is False
    (pytest_step,) = [s for s in _steps(wf, "test") if "pytest" in s.get("run", "")]
    assert "$SHARD_PATHS" in pytest_step["run"]
    assert "set -f" in pytest_step["run"]
    assert pytest_step["env"]["SHARD_PATHS"] == "${{ matrix.shard.paths }}"
    assert pytest_step["env"]["COVERAGE_FILE"] == ".coverage.${{ matrix.shard.name }}"
    assert "--cov=epistrel.core" in pytest_step["run"]
    assert "--cov-fail-under" not in pytest_step["run"]
    upload = next(s for s in _steps(wf, "test") if "upload-artifact" in str(s.get("uses")))
    assert upload["with"]["path"] == ".coverage.${{ matrix.shard.name }}"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"

    coverage = wf["jobs"]["coverage"]
    assert coverage["needs"] == ["test"]
    runs = [s.get("run", "") for s in coverage["steps"]]
    download = next(s for s in coverage["steps"] if "download-artifact" in str(s.get("uses")))
    assert download["with"] == {"pattern": "coverage-*", "merge-multiple": True}
    assert "uv run coverage combine" in runs
    assert any(r.startswith("uv run coverage report") and "--fail-under=90" in r for r in runs)
    assert runs.index("uv run coverage combine") < next(
        i for i, r in enumerate(runs) if r.startswith("uv run coverage report")
    )
    assert "coverage" in wf["jobs"]["gate"]["needs"]
    assert "$COVERAGE" in wf["jobs"]["gate"]["steps"][0]["run"]


def _all_steps(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        found = [s for s in node.get("steps", ()) if isinstance(s, dict)]
        return found + [s for v in node.values() for s in _all_steps(v)]
    if isinstance(node, list):
        return [s for v in node for s in _all_steps(v)]
    return []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_apt_steps_cannot_eat_the_job_budget(path: Path) -> None:
    """#500: a stalled mirror held the ffmpeg install for about 21 minutes and the shards hit
    their 25. A step that runs apt-get or pulls an image has its own ``timeout-minutes``, bounds
    every such call with ``timeout`` (apt-get also with apt's stall timeout), and retries."""
    for step in _all_steps(_load(path)):
        run = str(step.get("run", ""))
        apt = re.findall(r"^.*\bapt-get\b.*$", run, flags=re.MULTILINE)
        pulls = re.findall(r"^.*\bdocker pull\b.*$", run, flags=re.MULTILINE)
        if not apt and not pulls:
            continue
        assert isinstance(step.get("timeout-minutes"), int), step
        assert all(re.search(r"\btimeout\b[^|;&]*(apt-get|docker pull)", c) for c in apt + pulls), (
            step
        )
        if apt:
            assert "Acquire::http::Timeout=" in run, step
            assert "Acquire::Retries=" in run, step
        assert re.search(r"\bfor attempt in\b", run), step


def test_ci_installs_ffmpeg_from_a_pinned_image_not_apt() -> None:
    """#500: the shards' ffmpeg is a static build pinned by digest, not the Ubuntu mirror's."""
    (step,) = [
        s
        for s in _all_steps(_load(REPO_ROOT / ".github" / "workflows" / "ci.yml"))
        if s.get("name") == "install ffmpeg"
    ]
    assert "apt-get" not in str(step["run"]), step
    assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", step["env"]["FFMPEG_IMAGE"]), step
