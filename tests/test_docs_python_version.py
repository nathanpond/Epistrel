"""One Python version everywhere: 3.14, nothing says 3.12."""

from __future__ import annotations

import tomllib

from tests.conftest import REPO_ROOT

FORBIDDEN = ("3.12+", "Python 3.12")


def test_docs_and_readme_name_no_other_python_version() -> None:
    offenders = []
    for path in [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]:
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if any(token in line for token in FORBIDDEN):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


def test_pyproject_requires_python_3_14() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert project["requires-python"] == ">=3.14"
