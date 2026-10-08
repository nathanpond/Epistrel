from __future__ import annotations

from pathlib import Path

import pytest
from tools.gates.config import ConfigError, load_config

from tests.conftest import REPO_ROOT


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body)
    return path


def test_real_pyproject_has_current_milestone() -> None:
    assert load_config(REPO_ROOT / "pyproject.toml").current_milestone >= 0


def test_reads_value_and_subtables(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            '[tool.epistrel.gates]\ncurrent_milestone = 3\n[tool.epistrel.gates.deny]\nx = ["a"]\n',
        )
    )
    assert cfg.current_milestone == 3
    assert cfg.subtables == {"deny": {"x": ["a"]}}


@pytest.mark.parametrize(
    "body",
    [
        "[tool.other]\nx = 1\n",
        "[tool.epistrel.gates]\n",
        "[tool.epistrel.gates]\ncurrent_milestone = -1\n",
        '[tool.epistrel.gates]\ncurrent_milestone = "2"\n',
        "[tool.epistrel.gates]\ncurrent_milestone = true\n",
        "[tool.epistrel.gates]\ncurrent_milestone = 1\nbogus = 2\n",
    ],
)
def test_bad_sections_raise(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))
