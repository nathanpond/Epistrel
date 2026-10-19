from __future__ import annotations

import pytest

from tests.conftest import REPO_ROOT

# The inner collections run in a subprocess. In process, pytester restores its `sys.modules`
# snapshot afterwards and drops the modules the inner run imported first, while module-level
# state they registered elsewhere survives: a later re-import of `epistrel.core.schema` then
# redeclared `schema_meta` on the surviving `MetaData` (#311).


def test_not_integration_selects_no_integration_tests(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest_subprocess(
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
    result = pytester.runpytest_subprocess(
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
    assert not any("test_e_06" in line or _live_p_08(line) for line in default)
    assert any("test_p_05" in line for line in default)
    selected = _collected(pytester, "-m", "live_model")
    assert any("test_e_06" in line for line in selected)
    assert any(_live_p_08(line) for line in selected)


def _live_p_08(line: str) -> bool:
    """The live P-08 run itself, not #153's stub-driven ``test_p_08__structured``."""
    return line.endswith("::test_p_08") or "::test_p_08[" in line
