"""The committed model guide is exactly what the purpose registry generates (FR-MODEL-13, #54).

C-04's guide part (``tests/conformance/test_c_04.py::test_c_04__guide``) covers the floor and
data-flow completeness rules; this file covers the generator and its ``--check`` / ``--write``
modes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated

import pytest

from epistrel.orchestrator.models.registry import (
    DELIVERED,
    ContentCategory,
    GenerationInputs,
    Purpose,
)
from epistrel.tools import generated, model_guide

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "models.md"


class WiderGenerationInputs(GenerationInputs):
    """``generation``'s inputs with one more declared field: a registry change."""

    scene_note: Annotated[str, ContentCategory.CUSTOM_INSTRUCTIONS] = ""


def changed_registry() -> dict[str, Purpose]:
    purposes = dict(DELIVERED)
    purposes["generation"] = replace(DELIVERED["generation"], input_schema=WiderGenerationInputs)
    return purposes


def test_committed_guide_is_generated() -> None:
    """``docs/models.md`` is byte-for-byte what the registry generates, and ``--check`` passes.

    Complement asserted: the guide names every delivered purpose and every reserved one."""
    text = GUIDE.read_text(encoding="utf-8")
    assert text == model_guide.render()
    assert model_guide.main(["--check"]) == 0
    for name in DELIVERED:
        assert f"### `{name}`" in text
    assert "`image_check`" in text
    assert text.startswith("<!-- Generated from src/epistrel/orchestrator/models/registry.py")


def test_registry_change_fails_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A purpose that gains an input field changes the guide, so ``--check`` exits 1 with a diff
    naming the field; ``--write`` regenerates it and ``--check`` then passes."""
    copy = tmp_path / "models.md"
    copy.write_text(GUIDE.read_text(encoding="utf-8"), encoding="utf-8")
    original = model_guide.render
    monkeypatch.setattr(model_guide, "render", lambda: original(changed_registry()))

    assert model_guide.main(["--check", "--path", str(copy)]) == 1
    err = capsys.readouterr().err
    assert "+| `scene_note` | `custom_instructions` |" in err
    assert "--write" in err

    assert model_guide.main(["--write", "--path", str(copy)]) == 0
    assert model_guide.main(["--check", "--path", str(copy)]) == 0
    assert "scene_note" in copy.read_text(encoding="utf-8")


def test_missing_guide_fails_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert model_guide.main(["--check", "--path", str(tmp_path / "absent.md")]) == 1
    assert "is missing" in capsys.readouterr().err


def test_check_and_write_are_required_and_exclusive() -> None:
    with pytest.raises(SystemExit):
        model_guide.main([])
    with pytest.raises(SystemExit):
        model_guide.main(["--check", "--write"])


def test_long_diff_is_truncated(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stale = tmp_path / "doc.md"
    stale.write_text("".join(f"old {i}\n" for i in range(300)), encoding="utf-8")
    assert generated.check(stale, "new\n", regenerate="regen --write") == 1
    assert "more diff lines" in capsys.readouterr().err


def test_category_without_description_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptions = dict(model_guide.CATEGORY_DESCRIPTIONS)
    del descriptions[ContentCategory.CHECKED_TEXT]
    monkeypatch.setattr(model_guide, "CATEGORY_DESCRIPTIONS", descriptions)
    with pytest.raises(model_guide.GuideError, match="checked_text"):
        model_guide.render()
    assert model_guide.main(["--check"]) == 1


def test_write_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "models.md"
    assert model_guide.main(["--write", "--path", str(target)]) == 0
    first = target.read_bytes()
    assert model_guide.main(["--write", "--path", str(target)]) == 0
    assert target.read_bytes() == first
