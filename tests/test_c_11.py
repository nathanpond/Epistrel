"""C-11 — license and CI gate audit (NFR-MAINT-1,3; docs/04 §5, §13; #77).

NFR-MAINT-1: the dependency graph is Apache-2.0 compatible; NFR-MAINT-3: the test gates are
enforced in CI. Each gate is proved twice, against a fixture violation (which must **fail**,
run first) and against the repository (which must pass): a gate that passes everything proves
nothing. Then ``ci.yml`` is read to show every gate is a step of the job the required ``gate``
check depends on, so a red gate blocks the merge, the publish and the release.

The six gates run as CI runs them (``python -m tools.gates <check>``), except the license
violation: ``check-licenses`` reads the licenses of an installed environment, so its violation
is a fixture record list judged by the same ``evaluate`` over the real runtime closure and
policy. The real ``check-licenses`` reuses the runtime environment at
``EPISTREL_RUNTIME_ENV`` (default ``.venv-runtime``) when it exists and syncs it otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from tools.gates.deps import closure, load_lock
from tools.gates.licenses import evaluate, load_licenses_config

from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURES = REPO_ROOT / "tests" / "gates" / "fixtures"
RUNTIME_ENV = Path(os.environ.get("EPISTREL_RUNTIME_ENV") or REPO_ROOT / ".venv-runtime")
GATES = (
    "check-assignment",
    "check-parts",
    "check-lexicon",
    "check-traceability",
    "check-deps",
    "check-licenses",
)
EXIT_OK, EXIT_FINDINGS = 0, 1


def _gate(*args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.gates", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=REPO_ROOT,
    )


def _violating_parts(real: Path, tmp_path: Path) -> Path:
    """The repository's parts plus one M2 part asserting an M5 capability, under a test id
    that docs/04 §15 does not list: a lexicon and a traceability violation."""
    parts: list[dict[str, Any]] = json.loads(real.read_text())
    parts.append(
        {
            "node_id": "tests/test_zz.py::test_zz_99",
            "test_id": "ZZ-99",
            "milestone": "M2",
            "requirements": ["NFR-OPS-1"],
            "interim_until": None,
            "source": (
                '@pytest.mark.part("M2", "NFR-OPS-1")\n'
                "def test_zz_99() -> None:\n"
                # Split so this file's own part does not use the M5 term it plants.
                "    assert be"
                "lief_of('Ada') is not None\n"
            ),
            "file": "tests/test_zz.py",
            "line": 1,
            "origin": "marker",
        }
    )
    path = tmp_path / "violating_parts.json"
    path.write_text(json.dumps(parts))
    return path


@pytest.mark.part("M2", "NFR-MAINT-1,3")
def test_c_11__gates_fail_on_a_violation_and_pass_today(tmp_path: Path) -> None:
    """Every gate exits 1 on its fixture violation (run first), then 0 on the repository.
    Complement: a gate that passed the violation would be no gate."""
    real_parts = tmp_path / "parts.json"

    # -- each violation fails
    unassigned = _gate(
        "--spec", str(FIXTURES / "unassigned.md"),
        "--pyproject", str(FIXTURES / "pyproject_m0.toml"),
        "check-assignment",
    )  # fmt: skip
    assert unassigned.returncode == EXIT_FINDINGS, unassigned.stdout
    bad_milestone = _gate("check-parts", "--tests", str(FIXTURES / "tests_bad_milestone"))
    assert bad_milestone.returncode == EXIT_FINDINGS, bad_milestone.stdout
    assert "bad milestone" in bad_milestone.stdout
    redis = _gate("check-deps", "--lock", str(FIXTURES / "locks" / "redis_runtime.lock"))
    assert redis.returncode == EXIT_FINDINGS, redis.stdout
    assert "redis" in redis.stdout

    # -- the repository passes (check-parts also writes the parts the next two read)
    results = {
        "check-assignment": _gate("check-assignment"),
        "check-parts": _gate("check-parts", "--json", str(real_parts)),
        "check-deps": _gate("check-deps"),
    }
    results["check-lexicon"] = _gate("check-lexicon", "--parts", str(real_parts))
    results["check-traceability"] = _gate("check-traceability", "--parts", str(real_parts))

    violating = _violating_parts(real_parts, tmp_path)
    lexicon = _gate("check-lexicon", "--parts", str(violating))
    assert lexicon.returncode == EXIT_FINDINGS, lexicon.stdout
    assert "M5) in an M2 part" in lexicon.stdout
    trace = _gate("check-traceability", "--parts", str(violating))
    assert trace.returncode == EXIT_FINDINGS, trace.stdout
    assert "missing from §15: NFR-OPS-1 ← test_zz_99" in trace.stdout

    for name, result in results.items():
        assert result.returncode == EXIT_OK, f"{name}:\n{result.stdout}"


@pytest.mark.part("M2", "NFR-MAINT-1,3")
def test_c_11__licenses_fail_on_a_violation_and_pass_today() -> None:
    """A GPL license in the runtime closure is a finding (invariant 2); the real runtime closure
    has none. Complement: the fixture's permissive package is not reported."""
    config = load_licenses_config(REPO_ROOT / "pyproject.toml")
    parents = closure(load_lock(REPO_ROOT / "uv.lock"))
    records = json.loads((FIXTURES / "licenses" / "gpl_runtime.json").read_text())
    report = evaluate(records, parents, config)
    assert not report.ok
    assert [f.split(":")[0] for f in report.findings] == ["fastapi"]
    assert "denied (GPL)" in report.findings[0]

    args = ["check-licenses", "--runtime-env", str(RUNTIME_ENV)]
    if RUNTIME_ENV.is_dir():
        args.append("--no-sync")
    real = _gate(*args)
    assert real.returncode == EXIT_OK, real.stdout


def _ci() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    return loaded


@pytest.mark.part("M2", "NFR-MAINT-1,3")
def test_c_11__ci_wires_every_gate_into_the_required_check() -> None:
    """Each of the six gates is a step of ``lint`` that runs even after an earlier step failed;
    every ``test`` shard measures core coverage and the ``coverage`` job gates the combined data
    at 90 % (#331); the required ``gate`` job needs every job, ``coverage`` included, and fails
    unless each succeeded. Complement: no gate step is allowed to fail (``continue-on-error``),
    and no shard applies a threshold of its own (a shard's partial coverage is not the gate)."""
    ci = _ci()
    jobs = ci["jobs"]
    lint_steps = jobs["lint"]["steps"]
    for gate in GATES:
        (step,) = [s for s in lint_steps if f"python -m tools.gates {gate}" in s.get("run", "")]
        assert step.get("if") == "always()", gate
        assert not step.get("continue-on-error"), gate
    assert all(
        not s.get("continue-on-error") for job in jobs.values() for s in job.get("steps", [])
    )
    (pytest_step,) = [s for s in jobs["test"]["steps"] if "pytest" in s.get("run", "")]
    assert "--cov=epistrel.core" in pytest_step["run"]
    assert "fail-under" not in pytest_step["run"]
    coverage = jobs["coverage"]
    assert coverage["needs"] == ["test"]
    assert coverage.get("if") is None, "the coverage gate must not run when a shard failed"
    runs = [s.get("run", "") for s in coverage["steps"]]
    combine = runs.index("uv run coverage combine")
    (report,) = [i for i, r in enumerate(runs) if "coverage report" in r]
    assert combine < report
    (threshold,) = [w for w in runs[report].split() if w.startswith("--fail-under=")]
    assert threshold == "--fail-under=90"
    gate = jobs["gate"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {"lint", "types", "test", "coverage", "docker"}
    summary = gate["steps"][-1]
    assert '[ "$r" = "success" ] ||' in summary["run"]
    for need in gate["needs"]:
        (var,) = [k for k, v in summary["env"].items() if v == f"${{{{ needs.{need}.result }}}}"]
        assert f'"${var}"' in summary["run"].split("for r in", 1)[1].split("; do", 1)[0], need


@pytest.mark.part("M2", "NFR-MAINT-1,3")
@pytest.mark.parametrize("workflow", ["publish-edge.yml", "release.yml"])
def test_c_11__publishing_waits_for_the_gate_and_the_smoke(workflow: str) -> None:
    """What is published passed ``ci.yml`` and the deployment smoke (C-10). Complement: the
    publish job cannot start without either."""
    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / workflow).read_text())
    jobs = wf["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert {"ci", "smoke"} <= set(jobs["publish"]["needs"])
    assert "ci" in jobs["smoke"]["needs"]
    runs = " ".join(s.get("run", "") for s in jobs["smoke"]["steps"])
    assert "pytest -m smoke" in runs
    assert "tests/smoke/compose.override.yml" in runs
