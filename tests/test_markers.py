from __future__ import annotations

import pytest

from tests.conftest import REPO_ROOT


def test_not_integration_selects_no_integration_tests(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest(
        "--collect-only",
        "-q",
        "-m",
        "not integration",
        "--rootdir",
        str(REPO_ROOT),
        "-p",
        "no:cacheprovider",
        str(REPO_ROOT / "tests"),
    )
    collected = [line for line in result.outlines if "::" in line]
    assert collected, "nothing was collected at all"
    integration = [line for line in collected if "integration/" in line]
    assert integration == [], f"integration tests leaked into -m 'not integration':\n{integration}"


def _collected(pytester: pytest.Pytester, *args: str) -> list[str]:
    result = pytester.runpytest(
        "--collect-only",
        "-q",
        *args,
        "--rootdir",
        str(REPO_ROOT),
        "-p",
        "no:cacheprovider",
        str(REPO_ROOT / "tests" / "eval"),
        str(REPO_ROOT / "tests" / "perf"),
    )
    return [line for line in result.outlines if "::" in line]


def test_live_model_tests_run_only_when_selected(pytester: pytest.Pytester) -> None:
    """``live_model`` tests (E-06, P-08) are deselected by default and collected when ``-m``
    names the marker (#54). The other tests there (P-05, P-09: #69) run by default."""
    default = _collected(pytester)
    assert not any("test_e_06" in line or "test_p_08" in line for line in default)
    assert any("test_p_05" in line for line in default)
    selected = _collected(pytester, "-m", "live_model")
    assert any("test_e_06" in line for line in selected)
    assert any("test_p_08" in line for line in selected)
