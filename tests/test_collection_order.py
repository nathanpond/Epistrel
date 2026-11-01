from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, grouped_args

CONFORMANCE = "tests/conformance"


def test_grouped_args_makes_each_directory_contiguous() -> None:
    """#677: a re-entered directory's paths join its first group; everything else keeps the
    order it was given in."""
    interleaved = [
        f"{CONFORMANCE}/test_c_02.py",
        "tests/test_provider_manifest.py",
        f"{CONFORMANCE}/test_c_28.py::test_c_28[rest]",
        "tests/perf/test_p_05.py",
        "tests/test_markers.py",
        "tests/perf/test_scale_cache.py",
    ]
    assert grouped_args(interleaved, REPO_ROOT) == [
        f"{CONFORMANCE}/test_c_02.py",
        f"{CONFORMANCE}/test_c_28.py::test_c_28[rest]",
        "tests/test_provider_manifest.py",
        "tests/perf/test_p_05.py",
        "tests/perf/test_scale_cache.py",
        "tests/test_markers.py",
    ]
    # Already grouped (and directory) arguments come back unchanged.
    grouped = ["tests/test_markers.py", CONFORMANCE, f"{CONFORMANCE}/test_c_28.py", "tests/perf"]
    assert grouped_args(grouped, REPO_ROOT) == grouped
    # Absolute and relative spellings of one directory are one group.
    mixed = [str(REPO_ROOT / CONFORMANCE / "test_c_02.py"), "tests/test_markers.py"]
    mixed.append(f"{CONFORMANCE}/test_c_28.py")
    assert grouped_args(mixed, REPO_ROOT) == [mixed[0], mixed[2], mixed[1]]


def _collected(pytester: pytest.Pytester, *paths: str) -> list[str]:
    # A subprocess, as in tests/test_markers.py (#311).
    result = pytester.runpytest_subprocess(
        "--collect-only",
        "-q",
        "--rootdir",
        str(REPO_ROOT),
        "-p",
        "no:cacheprovider",
        *(str(REPO_ROOT / Path(path)) for path in paths),
    )
    return [line for line in result.outlines if "::" in line]


def test_a_subdirectory_fixture_survives_interleaved_paths(pytester: pytest.Pytester) -> None:
    """#677: ``surface`` (tests/conformance/conftest.py) parametrizes test_c_28 when a root
    test file sits between two conformance files on the command line; without the grouping
    the test is collected unparametrized and errors with ``fixture 'surface' not found``."""
    collected = _collected(
        pytester,
        f"{CONFORMANCE}/test_c_03.py",
        "tests/test_provider_manifest.py",
        f"{CONFORMANCE}/test_c_28.py",
    )
    c_28 = [line for line in collected if "test_c_28.py::test_c_28[" in line]
    assert {line.rsplit("[", 1)[1] for line in c_28} == {"rest]", "library]"}
    assert not [line for line in collected if line.endswith("test_c_28.py::test_c_28")]
    assert any("test_provider_manifest.py::" in line for line in collected)
