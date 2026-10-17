"""check-keyed-readers (#136; FR-CONC-11): a module listed in ``core.readset.KEYED_READERS``
reads ``facts``/``propositions`` only through ``KeyedLookup``. The repository passes, the
test-only keyed reader of the read-set tests passes, and the scan catches each kind of direct
read, a listed module that does not exist and a listed exempt module, and nothing in prose."""

from __future__ import annotations

from pathlib import Path

from tools.gates.__main__ import main as gates_main
from tools.gates.keyed_readers import EXEMPT, READSET_MODULE, listed, main, path_of, scan

from epistrel.core import readset

TEST_READER = "tests.readset_support"
"""The keyed reader the read-set tests drive until #140's verify lands."""

VIOLATIONS = '''"""A verifier that mentions SELECT * FROM facts in prose only here."""
from sqlalchemy import select, text

from epistrel.core.canon.facts import canon_at
from epistrel.core.canon import facts as store
from epistrel.core.tables import facts, propositions
import epistrel.core.canon.facts


def reads(conn):
    a = select(facts).where(facts.c.subject_id == "x")
    b = select(propositions.c.id)
    c = text("SELECT f.id FROM facts f")
    d = text("SELECT p.id FROM events e JOIN propositions p ON p.id = e.id")
    return a, b, c, d, canon_at, store
'''

CLEAN = '''"""Reads facts through KeyedLookup only (``FROM facts`` in prose is fine)."""
from epistrel.core.readset import KeyedLookup


async def verify(conn, read_set, story_id, predicate, subject):
    return await KeyedLookup(conn, read_set).canon_at(
        story_id, predicate=predicate, subjects=subject
    )
'''


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_repository_reads_verdict_facts_only_through_keyed_lookup() -> None:
    assert listed() == readset.KEYED_READERS
    assert scan() == []
    assert main([]) == 0
    assert gates_main(["check-keyed-readers"]) == 0


def test_the_test_only_keyed_reader_passes() -> None:
    assert scan(readers=[*readset.KEYED_READERS, TEST_READER]) == []


def test_each_direct_read_is_reported(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            READSET_MODULE: 'KEYED_READERS: tuple[str, ...] = ("epistrel.core.verify.rules",)\n',
            "src/epistrel/core/verify/rules.py": VIOLATIONS,
        },
    )
    findings = scan(root)
    lines = sorted(int(f.split(":")[1]) for f in findings)
    assert lines == [4, 5, 6, 7, 11, 12, 13, 14], findings
    assert all("only through KeyedLookup" in f for f in findings)


PROJECTION_VIOLATIONS = '''"""Reads the projections directly (prose: FROM placement)."""
from sqlalchemy import select, text

from epistrel.core.projections.structured import profile_view, scene_view
from epistrel.core.tables.projections import placement, relationships


def reads(conn):
    a = select(relationships.c.id)
    b = text("SELECT s.turn FROM scene_state s")
    return a, b, placement, profile_view, scene_view
'''


def test_each_direct_projection_read_is_reported(tmp_path: Path) -> None:
    """#137's rule, carried by #141: a keyed reader reads the relationship, placement and scene
    projections only through ``KeyedLookup.profile``/``scene``."""
    root = _tree(
        tmp_path,
        {
            READSET_MODULE: 'KEYED_READERS = ("epistrel.core.retrieval.pipeline",)\n',
            "src/epistrel/core/retrieval/pipeline.py": PROJECTION_VIOLATIONS,
        },
    )
    findings = scan(root)
    lines = sorted(int(f.split(":")[1]) for f in findings)
    assert lines == [4, 5, 9, 10], findings


def test_a_clean_reader_passes_and_prose_is_ignored(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            READSET_MODULE: 'KEYED_READERS = ["epistrel.core.verify.rules"]\n',
            "src/epistrel/core/verify/rules.py": CLEAN,
        },
    )
    assert scan(root) == []


def test_a_missing_or_exempt_listed_module_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, {READSET_MODULE: "KEYED_READERS = ()\n"})
    findings = scan(root, readers=["epistrel.core.verify.rules", "epistrel.core.readset"])
    assert findings == [
        "src/epistrel/core/verify/rules.py: listed in KEYED_READERS but does not exist",
        f"{READSET_MODULE}: listed in KEYED_READERS but exempt ({EXEMPT[READSET_MODULE]})",
    ]


def test_module_paths() -> None:
    assert path_of("epistrel.core.verify.rules") == "src/epistrel/core/verify/rules.py"
    assert path_of("tests.readset_support") == "tests/readset_support.py"
