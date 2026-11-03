"""#246 (owner decision, round two): the FR-STORE-32 operation is **pruning**, never
"compaction" (which readers confused with the Lite rolling summary). The spec, CLAUDE.md, the
README and the code carry no ``compact`` (any case); ``docs/05-adr-log.md`` (history), this file
and the erasure matcher's common-words list (where it is an ordinary English word) are exempt."""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import REPO_ROOT

TERM = re.compile("compact", re.IGNORECASE)
SCANNED = (
    "docs/01-whitepaper.md",
    "docs/02-technology-architecture.md",
    "docs/03-requirements-spec.md",
    "docs/04-testing-plan.md",
    "CLAUDE.md",
    "README.md",
)
TREES = ("src", "tests", "tools")
EXEMPT = frozenset(
    {
        "docs/05-adr-log.md",
        "tests/test_terminology.py",
        "src/epistrel/core/erasure/common_words.txt",
    }
)
SUFFIXES = frozenset({".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml", ".sql", ".j2"})


def _files(root: Path) -> list[Path]:
    found = [root / name for name in SCANNED if (root / name).exists()]
    for tree in TREES:
        found += [
            p
            for p in sorted((root / tree).rglob("*"))
            if p.is_file() and p.suffix in SUFFIXES and "__pycache__" not in p.parts
        ]
    return [p for p in found if p.relative_to(root).as_posix() not in EXEMPT]


def hits(root: Path = REPO_ROOT) -> list[str]:
    """Every ``path:line: text`` that names the old term."""
    found: list[str] = []
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TERM.search(line):
                found.append(f"{rel}:{number}: {line.strip()}")
    return found


def test_no_compaction_term() -> None:
    assert hits() == []


def test_the_guard_catches_the_old_term(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("# the rows were compacted\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "05-adr-log.md").write_text("ADR: compaction\n")
    assert hits(tmp_path) == ["tests/test_x.py:1: # the rows were compacted"]
