from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tools.release.validate import ReleaseError, parse_tag, read_released, retag, validate

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
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    released = tmp_path / "released.json"
    released.write_text(
        json.dumps(
            [
                {"tagName": "v0.0.1", "isDraft": False, "isPrerelease": False},
                {"tagName": "not-a-tag", "isDraft": False, "isPrerelease": False},
            ]
        )
    )
    base = [sys.executable, "-m", "tools.release", "validate", "--pyproject", str(pyproject)]
    ok = subprocess.run(
        [*base, "--tag", "v0.1.0", "--released-file", str(released)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert ok.returncode == 0, ok.stdout
    assert "highest=true" in ok.stdout.splitlines()
    bad = subprocess.run(
        [*base, "--tag", "v9.9.9"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert bad.returncode == 1
    assert bad.stdout.startswith("error: tag v9.9.9 is version 9.9.9 but pyproject.toml says 0.1.0")


def _gh(*tags: str, demoted: tuple[str, ...] = (), drafts: tuple[str, ...] = ()) -> str:
    """`gh release list --json tagName,isDraft,isPrerelease` output for these tags."""
    return json.dumps(
        [
            {
                "tagName": t,
                "isDraft": t in drafts,
                "isPrerelease": t in demoted or "-rc." in t,
            }
            for t in (*tags, *drafts)
        ]
    )


def test_released_file_drops_drafts_and_marks_only_stable_prereleases_demoted() -> None:
    """#96: a stable tag flagged prerelease is one a rollback demoted; an rc is not demoted."""
    text = _gh("v0.1.0", "v0.1.1", "v0.2.0-rc.1", demoted=("v0.1.1",), drafts=("v0.3.0",))
    assert read_released(text) == (["v0.1.0", "v0.1.1", "v0.2.0-rc.1"], {"v0.1.1"})
    with pytest.raises(ReleaseError, match="gh release list"):
        read_released("v0.1.0\n")


@pytest.mark.parametrize(
    ("released", "expected"),
    [
        (("v0.1.0", "v0.1.1", "v0.2.0"), ["0.1"]),
        (("v0.1.0", "v0.1.1"), ["latest", "0.1", "0"]),
        (("v0.1.1", "v1.0.0"), ["0.1", "0"]),
    ],
    ids=["below-a-newer-minor", "highest-everywhere", "below-a-newer-major"],
)
def test_retag_picks_the_floating_tags_the_good_version_heads(
    released: tuple[str, ...], expected: list[str]
) -> None:
    """#96 AC: re-promoting 0.1.1 re-points exactly the floating tags it is the highest of."""
    assert retag("0.1.1", *read_released(_gh(*released))) == expected


def test_retag_counts_a_demoted_good_version_but_no_other_demoted_release() -> None:
    """#96: the good version is a candidate while demoted; a demoted 0.1.2 or 0.2.0 is not higher,
    and an rc never is."""
    text = _gh(
        "v0.1.0",
        "v0.1.1",
        "v0.1.2",
        "v0.2.0",
        "v0.3.0-rc.1",
        demoted=("v0.1.1", "v0.1.2", "v0.2.0"),
    )
    assert retag("0.1.1", *read_released(text)) == ["latest", "0.1", "0"]
    # Every release above 0.1.0 is demoted, so re-promoting 0.1.0 instead takes all three back.
    assert retag("0.1.0", *read_released(text)) == ["latest", "0.1", "0"]
    # An undemoted 0.1.2 holds `0.1` and `0`; a demoted 0.2.0 still holds nothing.
    text = _gh("v0.1.1", "v0.1.2", "v0.2.0", demoted=("v0.1.1", "v0.2.0"))
    assert retag("0.1.1", *read_released(text)) == []


def test_retag_refuses_an_unpublished_or_prerelease_good_version() -> None:
    with pytest.raises(ReleaseError, match="not published"):
        retag("0.1.1", *read_released(_gh("v0.1.0", drafts=("v0.1.1",))))
    with pytest.raises(ReleaseError, match=r"must look like|bare X\.Y\.Z"):
        retag("0.1.1-rc.1", *read_released(_gh("v0.1.1-rc.1")))


def test_validate_highest_ignores_demoted_releases_as_retag_does() -> None:
    """#96: validate's floating-tag flags follow retag's rule, so a re-run agrees with a
    rollback."""
    text = _gh("v0.1.0", "v0.1.1", demoted=("v0.1.1",))
    released, demoted = read_released(text)
    out = validate("v0.1.0", "0.1.0", released, demoted)
    assert (out.highest, out.highest_minor, out.highest_major) == (True, True, True)
    assert out.already_released is True
    assert retag("0.1.0", released, demoted) == ["latest", "0.1", "0"]
    rerun_of_demoted = validate("v0.1.1", "0.1.1", released, demoted)
    assert rerun_of_demoted.already_released is True  # a demoted release is still released


def test_cli_retag_prints_one_tags_line(tmp_path: Path) -> None:
    released = tmp_path / "released.json"
    released.write_text(_gh("v0.1.0", "v0.1.1", "v0.2.0"))
    base = [sys.executable, "-m", "tools.release", "retag", "--released-file", str(released)]
    ok = subprocess.run(
        [*base, "--good", "0.1.1"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert (ok.returncode, ok.stdout) == (0, "tags=0.1\n")
    missing = subprocess.run(
        [*base, "--good", "0.0.9"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert missing.returncode == 1
    assert missing.stdout == "error: release v0.0.9 is not published\n"
