"""check-parts: milestone part markers on tests, validated against docs/03 and `current_milestone`.

Tests are read by AST, never imported, so the check needs no Docker and no fixtures.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from tools.gates.spec import (
    ID_PATTERN,
    Spec,
    SpecError,
    expand_ids,
    id_key,
    milestone_label,
    milestone_number,
)

FIXTURES_DIR = Path("tests") / "gates" / "fixtures"
FILE_PATTERNS = ("test_*.py", "*_test.py")
DOCSTRING_ID_RE = re.compile(rf"{ID_PATTERN}(?:\.\.\d+)?")
TEST_ID_FAMILIES = frozenset("uicseqrpx")


Candidate = tuple[int, list[str], str | None, Literal["marker", "docstring"]]


class PartsError(Exception):
    """A test file the checker cannot interpret (structural, exit 2)."""


@dataclass(frozen=True)
class Part:
    node_id: str
    test_id: str | None
    milestone: int
    requirements: list[str]
    interim_until: int | None
    source: str
    file: str
    line: int
    origin: Literal["marker", "docstring"]
    milestone_label: str = ""
    interim_until_label: str | None = None

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["milestone"] = milestone_label(self.milestone)
        data["interim_until"] = (
            None if self.interim_until is None else milestone_label(self.interim_until)
        )
        del data["milestone_label"], data["interim_until_label"]
        return data


@dataclass(frozen=True)
class PartsReport:
    findings: list[str]
    parts: int
    gated: int
    current_milestone: int

    @property
    def ok(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        return (
            f"{self.parts} parts, {self.gated} requirements gated at ≤ "
            f"{milestone_label(self.current_milestone)}"
        )


def derive_test_id(name: str) -> str | None:
    """`test_i_18__pins` → `I-18`; `test_outbox` → `OUTBOX`; non-`test_` names → None."""
    if not name.startswith("test_"):
        return None
    rest = name.removeprefix("test_").split("__", 1)[0]
    if not rest or not re.fullmatch(r"[a-z0-9_]+", rest):
        return None
    head, _, tail = rest.partition("_")
    if head in TEST_ID_FAMILIES and tail:
        return f"{head.upper()}-{tail.upper().replace('_', '-')}"
    return rest.upper().replace("_", "-")


# --- AST collection -------------------------------------------------------------------------


def _is_part_marker(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "part":
        return False
    owner = node.func.value
    if isinstance(owner, ast.Name):
        return owner.id == "mark"
    return (
        isinstance(owner, ast.Attribute)
        and owner.attr == "mark"
        and isinstance(owner.value, ast.Name)
    )


def _mentions_part(node: ast.expr) -> bool:
    return any(_is_part_marker(sub) for sub in ast.walk(node) if isinstance(sub, ast.Call))


def _literal(node: ast.expr, where: str) -> str | None:
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str)):
        return node.value
    raise PartsError(f"{where}: part() arguments must be string literals")


def _marker_args(call: ast.Call, where: str) -> tuple[str, str, str | None]:
    positional = [_literal(a, where) for a in call.args]
    keywords = {k.arg: _literal(k.value, where) for k in call.keywords}
    if len(positional) > 2 or None in positional[:2]:
        raise PartsError(f"{where}: part() takes (milestone, requirements, *, interim_until=None)")
    extra = set(keywords) - {"milestone", "requirements", "interim_until"}
    if extra or None in keywords:
        raise PartsError(f"{where}: unexpected part() keyword(s) {sorted(str(k) for k in extra)}")
    milestone = positional[0] if len(positional) > 0 else keywords.get("milestone")
    requirements = positional[1] if len(positional) > 1 else keywords.get("requirements")
    if milestone is None or requirements is None:
        raise PartsError(f"{where}: part() needs both milestone and requirements")
    return milestone, requirements, keywords.get("interim_until")


def _source(lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    end = node.end_lineno or node.lineno
    return "\n".join(lines[start - 1 : end])


def _tests_in(
    tree: ast.Module, where: str
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    found: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
            if value is not None and _mentions_part(value):
                raise PartsError(f"{where}: module-level pytestmark part markers are not supported")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            found.append((node, node.name))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            if any(_mentions_part(d) for d in node.decorator_list):
                raise PartsError(
                    f"{where}::{node.name}: class-level part markers are not supported"
                )
            for item in node.body:
                if isinstance(
                    item, ast.FunctionDef | ast.AsyncFunctionDef
                ) and item.name.startswith("test"):
                    found.append((item, f"{node.name}::{item.name}"))
    return found


def _parse_milestone(label: str, where: str) -> int:
    try:
        return milestone_number(label)
    except SpecError as exc:
        raise PartsError(f"{where}: {exc}") from exc


def _parse_requirements(text: str, where: str) -> list[str]:
    try:
        ids = expand_ids(text)
    except SpecError as exc:
        raise PartsError(f"{where}: {exc}") from exc
    if not ids:
        raise PartsError(f"{where}: part() names no requirement")
    return ids


def _interim_problem(interim: int, milestone: int, rows: set[int]) -> str | None:
    if interim not in rows:
        return "is not a §22 milestone"
    if interim <= milestone:
        return f"is not later than {milestone_label(milestone)}"
    return None


def _docstring_ids(doc: str) -> list[str]:
    ids: list[str] = []
    for token in DOCSTRING_ID_RE.findall(doc):
        prefix, _, numbers = token.rpartition("-")
        if ".." in numbers:
            lo, hi = numbers.split("..")
            if int(lo) >= int(hi):
                raise SpecError(f"range {token!r} must ascend")
            ids.extend(f"{prefix}-{n}" for n in range(int(lo), int(hi) + 1))
        else:
            ids.append(token)
    return ids


def collect_parts(tests_root: Path, repo_root: Path, spec: Spec) -> tuple[list[Part], list[str]]:
    """Walk `tests_root`; return (parts, findings about individual parts), sorted by (file, line).

    Findings here are about a part's own shape against the spec (bad milestone, undefined or
    later requirement, bad interim_until). Coverage findings are computed by `check_parts`.
    """
    tests_root = tests_root.resolve()
    repo_root = repo_root.resolve()
    fixtures = (repo_root / FIXTURES_DIR).resolve()
    files = sorted({p for pat in FILE_PATTERNS for p in tests_root.rglob(pat)})
    assigned = spec.assigned_milestones()
    defined = set(spec.priorities())
    rows = set(spec.assignment)

    parts: list[Part] = []
    findings: list[str] = []
    for path in files:
        # tests/gates/fixtures/ holds deliberately broken trees; skip it unless walking inside it.
        if (
            fixtures in path.parents
            and fixtures not in tests_root.parents
            and fixtures != tests_root
        ):
            continue
        rel = path.relative_to(repo_root).as_posix() if repo_root in path.parents else str(path)
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            raise PartsError(f"{rel}: syntax error: {exc.msg} (line {exc.lineno})") from exc
        lines = text.splitlines()
        for node, name in _tests_in(tree, rel):
            node_id = f"{rel}::{name}"
            markers = [d for d in node.decorator_list if _is_part_marker(d)]
            for d in node.decorator_list:
                if d not in markers and _mentions_part(d):
                    raise PartsError(f"{node_id}: part() must be a direct decorator")
            candidates: list[Candidate] = []
            if markers:
                seen: set[int] = set()
                for call in markers:
                    if not isinstance(
                        call, ast.Call
                    ):  # pragma: no cover - _is_part_marker guarantees it
                        continue
                    ms_label, req_text, interim_label = _marker_args(call, node_id)
                    milestone = _parse_milestone(ms_label, node_id)
                    if milestone in seen:
                        raise PartsError(f"{node_id}: two part markers for {ms_label}")
                    seen.add(milestone)
                    interim = (
                        None if interim_label is None else _parse_milestone(interim_label, node_id)
                    )
                    candidates.append(
                        (milestone, _parse_requirements(req_text, node_id), interim_label, "marker")
                    )
                    if interim is not None:
                        problem = _interim_problem(interim, milestone, rows)
                        if problem:
                            findings.append(
                                f"bad interim_until {node_id}: {interim_label} {problem}"
                            )
            else:
                doc = ast.get_docstring(node) or ""
                try:
                    ids = _docstring_ids(doc)
                except SpecError as exc:
                    raise PartsError(f"{node_id}: {exc}") from exc
                if not ids:
                    continue
                gating = [assigned[i] for i in ids if i in assigned]
                for req_id in ids:
                    if req_id not in defined:
                        findings.append(f"undefined {node_id}: {req_id}")
                    elif req_id not in assigned:
                        findings.append(f"unassigned {node_id}: {req_id}")
                if not gating:
                    continue
                candidates.append((max(gating), ids, None, "docstring"))

            for milestone, ids, interim_label, origin in candidates:
                if milestone not in rows:
                    label = milestone_label(milestone)
                    findings.append(f"bad milestone {node_id}: {label} is not a §22 milestone")
                for req_id in ids:
                    if origin == "marker":
                        if req_id not in defined:
                            findings.append(f"undefined {node_id}: {req_id}")
                            continue
                        if req_id not in assigned:
                            findings.append(f"unassigned {node_id}: {req_id}")
                            continue
                    if req_id in assigned and assigned[req_id] > milestone:
                        theirs, ours = milestone_label(assigned[req_id]), milestone_label(milestone)
                        findings.append(
                            f"later requirement {node_id}: {req_id} is {theirs}, part is {ours}"
                        )
                interim_number = (
                    None if interim_label is None else _parse_milestone(interim_label, node_id)
                )
                parts.append(
                    Part(
                        node_id=node_id,
                        test_id=derive_test_id(name.rsplit("::", 1)[-1]),
                        milestone=milestone,
                        requirements=ids,
                        interim_until=interim_number,
                        source=_source(lines, node),
                        file=rel,
                        line=min([node.lineno, *(d.lineno for d in node.decorator_list)]),
                        origin=origin,
                    )
                )
    parts.sort(key=lambda p: (p.file, p.line, p.milestone))
    return parts, findings


# --- Coverage ---------------------------------------------------------------------------------


def check_parts(
    parts: list[Part], part_findings: list[str], spec: Spec, current_milestone: int
) -> PartsReport:
    priorities = spec.priorities()
    assigned = spec.assigned_milestones()
    gated = sorted(
        (
            req_id
            for req_id, ms in assigned.items()
            if ms <= current_milestone and priorities.get(req_id) == "M"
        ),
        key=id_key,
    )
    covered = {
        req_id for p in parts if p.milestone <= current_milestone for req_id in p.requirements
    }
    findings = list(part_findings)
    findings.extend(
        f"uncovered {req_id} (M, {milestone_label(assigned[req_id])})"
        for req_id in gated
        if req_id not in covered
    )
    return PartsReport(
        findings, parts=len(parts), gated=len(gated), current_milestone=current_milestone
    )


def write_json(parts: list[Part], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([p.to_json() for p in parts], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
