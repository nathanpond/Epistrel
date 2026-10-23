"""check-perception-authority (#161; FR-TURN-10): only the routing modules read the scene and the
location topology directly. The repository passes, and the scan catches each kind of read outside
the allowed modules, and nothing that merely mentions them in prose."""

from __future__ import annotations

from pathlib import Path

from tools.gates.__main__ import main as gates_main
from tools.gates.perception import ALLOWED, main, scan

READS_THE_SCENE = '''"""Reads FROM placement in prose only here."""
from epistrel.core.tables import placement, scene_state
from epistrel.core.propositions.vocabulary import ADJACENT_TO
from epistrel.core.propositions import vocabulary


def reads(conn):
    return placement.c.entity_id, scene_state.c.turn, ADJACENT_TO, vocabulary.ON_CHANNEL
'''

CLEAN = '''"""Asks the routing module (the scene table and ADJACENT_TO in prose are fine)."""
from epistrel.core.epistemic.perception import co_located_set


def present(scene):
    return co_located_set(scene)
'''


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_the_repository_has_one_perception_decision() -> None:
    assert scan() == []
    assert main([]) == 0
    assert gates_main(["check-perception-authority"]) == 0


def test_a_second_reader_of_the_scene_is_found(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "src/epistrel/orchestrator/elsewhere.py": READS_THE_SCENE,
            "src/epistrel/orchestrator/clean.py": CLEAN,
            "src/epistrel/core/epistemic/perception.py": READS_THE_SCENE,
        },
    )
    found = scan(root)
    assert [f.split(": ", 1)[1].split(";")[0] for f in found] == [
        "imports the placement, scene_state table",
        "imports ADJACENT_TO",
        "names ON_CHANNEL",
        "placement.c",
        "scene_state.c",
    ]
    assert all(f.startswith("src/epistrel/orchestrator/elsewhere.py:") for f in found)
    assert "src/epistrel/core/epistemic/perception.py" in ALLOWED
