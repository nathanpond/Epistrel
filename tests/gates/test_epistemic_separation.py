"""check-epistemic-separation (#159; FR-EPI-1): the canon store never reads the belief tables and
the epistemic model never reads ``facts``. The repository passes, and the scan catches each kind
of read on each side, and nothing in prose."""

from __future__ import annotations

from pathlib import Path

from tools.gates.__main__ import main as gates_main
from tools.gates.epistemic import CANON_SIDE, EPISTEMIC_SIDE, main, scan

CANON_READS_BELIEFS = '''"""Canon that mentions SELECT * FROM beliefs in prose only here."""
from sqlalchemy import select, text

from epistrel.core.epistemic import partitions
from epistrel.core.tables import beliefs
import epistrel.core.epistemic


def reads(conn):
    a = select(beliefs).where(beliefs.c.holder_character_id == "x")
    b = text("SELECT b.id FROM facts f JOIN belief_state b ON b.belief_id = f.id")
    return a, b, partitions
'''

PARTITION_READS_FACTS = '''"""A partition that says FROM facts in prose only."""
from sqlalchemy import select, text

from epistrel.core.canon import facts as store
from epistrel.core.tables.facts import facts


def reads(conn):
    return select(facts.c.id), text("SELECT id FROM facts"), store
'''

CLEAN = '''"""Reads its own table only (``FROM facts`` and ``FROM beliefs`` in prose are fine)."""
from epistrel.core.tables import propositions


def reads():
    return propositions.c.id
'''


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_repository_keeps_canon_and_partitions_apart() -> None:
    assert scan() == []
    assert main([]) == 0
    assert gates_main(["check-epistemic-separation"]) == 0


def test_a_canon_read_of_the_belief_tables_is_found(tmp_path: Path) -> None:
    root = _tree(tmp_path, {f"{CANON_SIDE.package}/reads.py": CANON_READS_BELIEFS})
    found = scan(root, (CANON_SIDE,))
    lines = sorted(int(f.split(":")[1]) for f in found)
    assert lines == [4, 5, 6, 10, 11]
    assert all("FR-EPI-1" in f for f in found)


def test_a_partition_read_of_facts_is_found(tmp_path: Path) -> None:
    root = _tree(tmp_path, {f"{EPISTEMIC_SIDE.package}/reads.py": PARTITION_READS_FACTS})
    found = scan(root, (EPISTEMIC_SIDE,))
    assert sorted(int(f.split(":")[1]) for f in found) == [4, 5, 9, 9]  # facts.c and SQL


def test_prose_and_other_tables_are_not_reads(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {f"{CANON_SIDE.package}/clean.py": CLEAN, f"{EPISTEMIC_SIDE.package}/clean.py": CLEAN},
    )
    assert scan(root) == []
