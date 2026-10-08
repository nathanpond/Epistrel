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
