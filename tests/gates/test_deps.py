from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tools.gates.deps import DepsError, check_deps, closure, load_deny, load_lock

from tests.conftest import REPO_ROOT

LOCKS = Path(__file__).parent / "fixtures" / "locks"
DENY = {"model_runtimes": ["torch", "transformers"], "infra": ["redis", "celery", "kombu"]}


def _cli(lock: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.gates", "check-deps", "--lock", str(lock)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )


def test_closure_follows_dependencies_and_extras_but_not_dev_groups() -> None:
    parents = closure(load_lock(LOCKS / "clean.lock"))
    assert set(parents) == {"fastapi", "starlette", "anyio", "psycopg", "tzdata", "psycopg-binary"}
    assert parents["psycopg-binary"] == {"psycopg"}  # reached through the [binary] extra
    assert parents["fastapi"] == {"epistrel"}
    assert "redis" not in parents  # dev-only: never part of the runtime closure
    assert "pytest" not in parents


def test_clean_lock_passes() -> None:
    report = check_deps(load_lock(LOCKS / "clean.lock"), DENY)
    assert report.findings == []
    assert report.summary() == "6 runtime packages, 0 denied"
    assert _cli(LOCKS / "clean.lock").returncode == 0


def test_redis_as_runtime_dependency_is_denied_and_names_the_invariant() -> None:
    report = check_deps(load_lock(LOCKS / "redis_runtime.lock"), DENY)
    assert report.findings == ["DENIED redis [infra, invariant 5] via (direct)"]
    assert _cli(LOCKS / "redis_runtime.lock").returncode == 1


def test_torch_as_runtime_dependency_is_denied() -> None:
    report = check_deps(load_lock(LOCKS / "torch_runtime.lock"), DENY)
    assert report.findings == ["DENIED torch [model_runtimes, invariant 1] via (direct)"]


def test_transitive_hit_names_the_direct_dependency() -> None:
    report = check_deps(load_lock(LOCKS / "transitive.lock"), DENY)
    assert report.findings == [
        "DENIED celery [infra, invariant 5] via (direct)",
        "DENIED kombu [infra, invariant 5] via celery",
    ]


@pytest.mark.parametrize(("lock", "message"), [("dangling", "ghost"), ("noroot", "editable")])
def test_structural_lock_problems_exit_two(lock: str, message: str) -> None:
    with pytest.raises(DepsError, match=message):
        closure(load_lock(LOCKS / f"{lock}.lock"))
    assert _cli(LOCKS / f"{lock}.lock").returncode == 2


def test_deny_config_is_validated(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text('[tool.epistrel.gates.deny]\nmodel_runtimes = ["torch"]\ninfra = ["redis"]\n')
    assert load_deny(good) == {"model_runtimes": ["torch"], "infra": ["redis"]}
    for body in (
        "[tool.epistrel.gates]\ncurrent_milestone = 0\n",  # no deny section
        "[tool.epistrel.gates.deny]\nmodel_runtimes = []\ninfra = []\n",  # vacuous
        '[tool.epistrel.gates.deny]\nmodel_runtimes = ["torch"]\ninfra = ["redis"]\n'
        'bogus = ["x"]\n',
        '[tool.epistrel.gates.deny]\nmodel_runtimes = "torch"\ninfra = ["redis"]\n',
    ):
        bad = tmp_path / "bad.toml"
        bad.write_text(body)
        with pytest.raises(DepsError):
            load_deny(bad)


def test_real_lock_passes_today() -> None:
    proc = _cli(REPO_ROOT / "uv.lock")
    assert proc.returncode == 0, proc.stdout
    assert proc.stdout.strip().splitlines()[-1].endswith(", 0 denied")
