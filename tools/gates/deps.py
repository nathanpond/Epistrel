"""check-deps: the runtime dependency closure in uv.lock contains nothing on the deny-lists.

Invariant 1 — Epistrel runs no model of its own — denies inference runtimes and model-weight
packages; invariant 5 — no external broker, cache, or scheduler — denies their client libraries.
Dev groups are not checked: dev tooling is free to use anything.
"""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIST_TO_INVARIANT = {"model_runtimes": 1, "infra": 5}


class DepsError(Exception):
    """The lock or the deny configuration cannot be interpreted (structural, exit 2)."""


def normalize(name: str) -> str:
    """PEP 503 normalisation: case-insensitive, runs of `-_.` collapse to `-`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_lock(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DepsError(f"cannot read {path}: {exc}") from exc
    if data.get("version") != 1:
        raise DepsError(f"{path}: expected uv.lock version = 1, got {data.get('version')!r}")
    if not isinstance(data.get("package"), list):
        raise DepsError(f"{path}: no [[package]] entries")
    return data


def _index(lock: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    """Merge same-name entries (forks) and find the editable project root."""
    packages: dict[str, dict[str, Any]] = {}
    root: str | None = None
    for entry in lock["package"]:
        name = normalize(str(entry.get("name", "")))
        if not name:
            raise DepsError("a [[package]] entry has no name")
        merged = packages.setdefault(name, {"dependencies": [], "optional-dependencies": {}})
        merged["dependencies"].extend(entry.get("dependencies", []))
        for extra, deps in entry.get("optional-dependencies", {}).items():
            merged["optional-dependencies"].setdefault(extra, []).extend(deps)
        if isinstance(entry.get("source"), dict) and "editable" in entry["source"]:
            if root is not None and root != name:
                raise DepsError(f"two editable packages in the lock: {root}, {name}")
            root = name
    if root is None:
        raise DepsError("no editable project entry (source = { editable = ... }) in the lock")
    return packages, root


def closure(lock: dict[str, Any]) -> dict[str, set[str]]:
    """Runtime closure as package → immediate parents; the project's direct deps have parent ROOT.

    Follows `dependencies` and the requested extras' `optional-dependencies`; ignores markers
    (conservative union); excludes every dependency group. Reused by the license check (#42).
    """
    packages, root = _index(lock)
    parents: dict[str, set[str]] = defaultdict(set)
    seen: set[tuple[str, frozenset[str]]] = set()
    queue: deque[tuple[str, frozenset[str], str]] = deque()
    for dep in packages[root]["dependencies"]:
        queue.append((normalize(dep["name"]), frozenset(dep.get("extra", [])), root))
    while queue:
        name, extras, parent = queue.popleft()
        parents[name].add(parent)
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        if name not in packages:
            raise DepsError(f"{name} is required by {parent} but has no [[package]] entry")
        pkg = packages[name]
        deps = list(pkg["dependencies"])
        for extra in sorted(extras):
            if extra not in pkg["optional-dependencies"]:
                raise DepsError(f"{name} has no extra {extra!r} (required by {parent})")
            deps.extend(pkg["optional-dependencies"][extra])
        for dep in deps:
            queue.append((normalize(dep["name"]), frozenset(dep.get("extra", [])), name))
    return dict(parents)


def load_deny(pyproject: Path) -> dict[str, list[str]]:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DepsError(f"cannot read {pyproject}: {exc}") from exc
    deny = data.get("tool", {}).get("epistrel", {}).get("gates", {}).get("deny")
    if not isinstance(deny, dict):
        raise DepsError(f"{pyproject}: missing [tool.epistrel.gates.deny]")
    unknown = set(deny) - set(LIST_TO_INVARIANT)
    if unknown:
        raise DepsError(f"{pyproject}: unknown deny lists {sorted(unknown)}")
    out: dict[str, list[str]] = {}
    for key in LIST_TO_INVARIANT:
        value = deny.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise DepsError(f"{pyproject}: deny.{key} must be a list of package names")
        out[key] = [normalize(v) for v in value]
    if not any(out.values()):
        raise DepsError(f"{pyproject}: every deny list is empty — the gate would be vacuous")
    return out


@dataclass(frozen=True)
class DepsReport:
    findings: list[str]
    runtime_packages: int

    @property
    def ok(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        return f"{self.runtime_packages} runtime packages, {len(self.findings)} denied"


def _direct_roots(name: str, parents: dict[str, set[str]]) -> list[str]:
    """Direct project dependencies through which `name` is reached; "(direct)" if it is one."""
    roots: set[str] = set()
    stack, visited = [name], set()
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for parent in parents.get(current, ()):
            if parent not in parents:  # the project root itself is never in the closure
                roots.add("(direct)" if current == name else current)
            else:
                stack.append(parent)
    return sorted(roots)


def check_deps(lock: dict[str, Any], deny: dict[str, list[str]]) -> DepsReport:
    parents = closure(lock)
    findings: list[str] = []
    for list_name, invariant in LIST_TO_INVARIANT.items():
        for pkg in deny.get(list_name, []):
            if pkg in parents:
                via = ", ".join(_direct_roots(pkg, parents))
                findings.append(f"DENIED {pkg} [{list_name}, invariant {invariant}] via {via}")
    return DepsReport(sorted(findings, key=lambda f: f.split()[1]), runtime_packages=len(parents))
