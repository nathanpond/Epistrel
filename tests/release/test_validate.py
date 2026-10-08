from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tools.release.validate import ReleaseError, parse_tag, validate

from tests.conftest import REPO_ROOT


def test_first_stable_release_is_highest_everywhere() -> None:
    out = validate("v0.1.0", "0.1.0", [])
    assert out.lines() == [
        "version=0.1.0",
        "prerelease=false",
        "highest=true",
        "highest_minor=true",
        "highest_major=true",
        "already_released=false",
    ]


def test_prerelease_never_moves_floating_tags() -> None:
    out = validate("v0.1.1-rc.1", "0.1.1rc1", ["v0.1.0"])
    assert out.version == "0.1.1-rc.1"
    assert out.prerelease is True
    assert (out.highest, out.highest_minor, out.highest_major) == (False, False, False)


def test_lower_stable_version_publishes_without_latest() -> None:
    out = validate("v0.1.5", "0.1.5", ["v0.2.0", "v0.1.4"])
    assert out.highest is False
    assert out.highest_minor is True  # highest within 0.1.x
    assert out.highest_major is False  # 0.2.0 exists


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v0.1.0-beta.1", "v0.1.0rc1", "release-1"])
def test_bad_tag_shapes_are_refused(tag: str) -> None:
    with pytest.raises(ReleaseError, match="must look like"):
        parse_tag(tag)


def test_tag_must_match_pyproject_version() -> None:
    with pytest.raises(ReleaseError, match=r"pyproject\.toml says 0\.1\.0"):
        validate("v0.2.0", "0.1.0", [])
    assert validate("v1.2.3-rc.1", "1.2.3rc1", []).version == "1.2.3-rc.1"  # PEP 440 forms agree


def test_rc_for_an_already_released_stable_is_refused() -> None:
    with pytest.raises(ReleaseError, match="already released"):
        validate("v0.1.0-rc.2", "0.1.0rc2", ["v0.1.0"])


def test_rerun_of_a_released_tag_is_allowed_and_flagged() -> None:
    out = validate("v0.1.0", "0.1.0", ["v0.1.0"])
    assert out.already_released is True
    assert out.highest is True


def test_cli_prints_outputs_and_exit_codes(tmp_path: Path) -> None:
    released = tmp_path / "released.txt"
    released.write_text("v0.0.1\nnot-a-tag\n")
    ok = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.release",
            "validate",
            "--tag",
            "v0.1.0",
            "--released-file",
            str(released),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert ok.returncode == 0, ok.stdout
    assert "highest=true" in ok.stdout.splitlines()
    bad = subprocess.run(
        [sys.executable, "-m", "tools.release", "validate", "--tag", "v9.9.9"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert bad.returncode == 1
    assert bad.stdout.startswith("error: tag v9.9.9 is version 9.9.9 but pyproject.toml says")
