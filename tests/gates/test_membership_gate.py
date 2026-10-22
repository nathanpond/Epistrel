"""check-membership (#130; FR-STORE-29, FR-BITEMP-5): derived records' membership is read only
through ``epistrel.core.membership``. The repository passes, and the scan catches each kind of
raw filter, a stale allow-list entry, and nothing in prose."""

from __future__ import annotations

from pathlib import Path

from tools.gates.membership import ALLOWED, HELPER_MODULE, main, scan

VIOLATIONS = '''"""A module that mentions membership = 'active' in prose only here."""
from sqlalchemy import select, text


def reads(facts, membership):
    a = select(facts).where(facts.c.membership == "active")
    b = select(facts).where(facts.c["membership"] == "active")
    c = membership == "active"
    d = text("SELECT * FROM facts f WHERE f.membership = 'active'")
    e = text("SELECT * FROM facts WHERE membership IN ('active', 'dormant')")
    f = text("SELECT * FROM facts WHERE membership <> 'discarded'")
    return a, b, c, d, e, f
'''

CLEAN = '''"""Writes are not filters: ``membership = 'active'`` in a docstring is prose."""


def writes(table, update):
    return update(table).values(membership="active")
'''


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_repository_reads_derived_membership_only_through_the_helpers() -> None:
    assert scan() == []
    assert main([]) == 0


def test_each_raw_filter_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/epistrel/core/retrieval.py": VIOLATIONS})
    findings = scan(root, allowed={})
    lines = sorted(int(f.split(":")[1]) for f in findings)
    assert lines == [6, 7, 8, 9, 10, 11], findings
    assert all("use core.membership" in f for f in findings)


def test_the_helper_module_and_allowed_modules_pass_and_prose_is_ignored(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            HELPER_MODULE: VIOLATIONS,
            "src/epistrel/core/old.py": VIOLATIONS,
            "src/epistrel/core/writer.py": CLEAN,
        },
    )
    assert scan(root, allowed={"src/epistrel/core/old.py": "operations"}) == []


def test_a_stale_allow_list_entry_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/epistrel/core/writer.py": CLEAN})
    findings = scan(root, allowed={"src/epistrel/core/writer.py": "operations"})
    assert findings == [
        "src/epistrel/core/writer.py: listed in tools.gates.membership.ALLOWED but filters no "
        "membership; remove it"
    ]


def test_the_allow_list_is_frozen_to_m2_and_m3_modules() -> None:
    """No module that ships derived records is on the list: they use the helpers."""
    assert HELPER_MODULE not in ALLOWED
    assert all(reason for reason in ALLOWED.values())
