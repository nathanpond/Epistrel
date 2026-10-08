"""Every GitHub Actions workflow is pinned, least-privilege, and downloads nothing floating."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import REPO_ROOT

WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
PINNED = re.compile(r"@v\d+(\.\d+){0,2}$")  # a major tag, or an exact release where no major exists
LOCAL = ("./", "docker://")


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def _scalars(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [s for v in node.values() for s in _scalars(v)]
    if isinstance(node, list):
        return [s for v in node for s in _scalars(v)]
    return [node]


def _uses(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [str(node["uses"])] if "uses" in node else []
        return found + [u for v in node.values() for u in _uses(v)]
    if isinstance(node, list):
        return [u for v in node for u in _uses(v)]
    return []


def test_there_are_workflows() -> None:
    assert WORKFLOWS, "no workflow files under .github/workflows/"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_uses_is_pinned_to_a_major(path: Path) -> None:
    unpinned = [u for u in _uses(_load(path)) if not u.startswith(LOCAL) and not PINNED.search(u)]
    assert unpinned == [], f"{path.name}: pin at a version tag (@vN or @vN.N.N): {unpinned}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_top_level_permissions_declared(path: Path) -> None:
    workflow = _load(path)
    assert "permissions" in workflow, f"{path.name}: declare top-level permissions:"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_nothing_floats_to_latest(path: Path) -> None:
    floating = [s for s in _scalars(_load(path)) if isinstance(s, str) and s.strip() == "latest"]
    assert floating == [], f"{path.name}: a value is `latest`; pin an exact release"


def _workflow(name: str) -> dict[str, Any]:
    return _load(REPO_ROOT / ".github" / "workflows" / name)


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    """The `on:` block — PyYAML parses the bare key `on` as the boolean True."""
    triggers: dict[str, Any] = workflow.get("on", workflow.get(True))
    return triggers


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return steps


def test_publish_edge_never_cancels_and_reuses_the_gate() -> None:
    wf = _workflow("publish-edge.yml")
    assert wf["concurrency"] == {"group": "publish-edge", "cancel-in-progress": False}
    assert wf["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert "ref" in _on(wf)["workflow_dispatch"]["inputs"]
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "read", "packages": "write"}


def test_release_triggers_only_on_v_tags_and_withholds_latest_by_flavor() -> None:
    wf = _workflow("release.yml")
    assert _on(wf) == {"push": {"tags": ["v*"]}}
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"
    meta = next(s for s in _steps(wf, "publish") if "metadata-action" in str(s.get("uses")))
    assert "latest=auto" in meta["with"]["flavor"]
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "write", "packages": "write"}
    assert "permissions" not in wf["jobs"]["validate"] or "write" not in str(
        wf["jobs"]["validate"]["permissions"]
    )


def test_rollback_is_dispatch_only_with_the_right_inputs() -> None:
    wf = _workflow("rollback.yml")
    inputs = _on(wf)["workflow_dispatch"]["inputs"]
    assert list(_on(wf)) == ["workflow_dispatch"]
    assert inputs["good_version"]["required"] is True
    assert inputs["bad_version"]["required"] is False
    assert wf["concurrency"] == {"group": "release-latest", "cancel-in-progress": False}
    assert wf["jobs"]["rollback"]["if"] == "github.ref == 'refs/heads/main'"
