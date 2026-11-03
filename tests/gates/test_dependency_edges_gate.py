"""check-dependency-edges (#220; FR-DIRECT-7): only ``core.edges.record_edges`` writes
``dependency_edges``. The repository passes; a planted module with each kind of write is
caught (watched failing first), and prose naming the table is not."""

from __future__ import annotations

from pathlib import Path

from tools.gates.__main__ import main as gates_main
from tools.gates.dependency_edges import main, scan, scan_source

PLANTED = '''"""Writes INSERT INTO dependency_edges in prose only here."""
from sqlalchemy import insert, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from epistrel.core.tables.edges import dependency_edges


async def write(conn, op):
    await conn.execute(insert(dependency_edges).values(id="x"))
    await conn.execute(pg_insert(dependency_edges).values(id="y"))
    await conn.execute(dependency_edges.insert())
    op.bulk_insert(dependency_edges, [])
    await conn.execute(text("INSERT INTO dependency_edges (id) VALUES ('z')"))
'''


def test_the_repository_writes_edges_through_record_edges() -> None:
    assert scan() == []
    assert main([]) == 0
    assert gates_main(["check-dependency-edges"]) == 0


def test_a_planted_writer_is_caught(tmp_path: Path) -> None:
    findings = list(scan_source("src/epistrel/planted.py", PLANTED))
    assert [f.what for f in findings] == [
        "insert(dependency_edges)",
        "insert(dependency_edges)",
        "dependency_edges.insert()",
        "bulk_insert(dependency_edges)",
        "SQL INSERT INTO dependency_edges",
    ]
    planted = tmp_path / "src" / "epistrel" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(PLANTED, encoding="utf-8")
    assert main([str(tmp_path)]) == 1
