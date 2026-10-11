"""The crash points (``epistrel.testing.crash``, re-exported by ``testing.faults``; #75): a
configured hit ends the process with status 137 and nothing else runs; unconfigured, every hit
is a no-op. NFR-REL-1 is exercised end to end by X-03."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from epistrel.testing import crash, faults

CHILD = (
    "from epistrel.testing import crash\n"
    "for _ in range(3):\n"
    "    crash.hit('before_commit', 'turn')\n"
    "    print('hit', flush=True)\n"
    "crash.hit('before_commit', 'control')\n"
    "print('alive', flush=True)\n"
)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("EPISTREL_")}
    return subprocess.run(
        [sys.executable, "-c", CHILD],
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_crash_at_names_a_point_and_an_occurrence() -> None:
    plan = faults.crash_at("during_projection", 3, "turn")
    assert plan.env() == {
        "EPISTREL_TEST_FAULTS": "1",
        "EPISTREL_TEST_CRASH_AT": "during_projection:3:turn",
    }
    assert crash.parse("before_lock:2") == faults.crash_at("before_lock", 2)
    assert set(faults.POINTS) == {
        "before_lock",
        "after_events",
        "during_projection",
        "before_commit",
        "after_commit_before_response",
    }
    with pytest.raises(ValueError, match="unknown crash point"):
        faults.crash_at("mid_air")
    with pytest.raises(ValueError, match="nth"):
        faults.crash_at("before_lock", 0)
    with pytest.raises(ValueError, match="point"):
        crash.parse("before_lock")


def test_the_nth_hit_of_the_kind_ends_the_process_with_137() -> None:
    done = _run(faults.crash_at("before_commit", 2, "turn").env())
    assert done.returncode == crash.EXIT_STATUS
    assert done.stdout.splitlines() == ["hit", "EPISTREL-FAULT crash before_commit turn"]


def test_a_point_never_reached_n_times_is_a_run_without_a_crash() -> None:
    """Complement: an unreached ``nth``, another kind, or faults not enabled, all survive."""
    for env in (
        faults.crash_at("before_commit", 5, "turn").env(),
        faults.crash_at("before_commit", 1, "genesis").env(),
        {"EPISTREL_TEST_CRASH_AT": "before_commit:1"},
    ):
        done = _run(env)
        assert done.returncode == 0, (env, done.stderr)
        assert done.stdout.splitlines() == ["hit", "hit", "hit", "alive"]


def test_without_kind_every_hit_of_the_point_counts() -> None:
    done = _run(faults.crash_at("before_commit", 4).env())
    assert done.returncode == crash.EXIT_STATUS
    assert done.stdout.splitlines()[-1] == "EPISTREL-FAULT crash before_commit control"


def test_hits_are_no_ops_in_the_test_process() -> None:
    crash.reset()
    assert crash.armed() is None
    crash.hit("before_lock", "turn")
