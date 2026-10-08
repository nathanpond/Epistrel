"""check-traceability: docs/04 §15 (requirement → tests) agrees with docs/03 §22 and with the code.

§15 stays the plan of record. The check reports disagreements; `--write` regenerates only the
Tests column of rows at or before `current_milestone` from the parts found in code, never
dropping a planned test ID, and regenerates the two footer lines.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.gates.spec import Spec, id_key, milestone_label, milestone_number

SECTION_RE = re.compile(r"^## 15\. ")
HEADER = "| Requirement | Priority | Milestone | Tests |"
EMPTY_CELL = "—"
ENTRY_RE = re.compile(
    r"^(?P<id>[A-Z][A-Z0-9-]*)\s*\((?P<ms>M\d+)(?:,\s*interim until (?P<until>M\d+))?\)$"
)
NOT_GATED_PREFIX = "**Not release-gated**"
PER_MILESTONE_PREFIX = "**M requirements first gated per milestone:**"
FAMILIES = ["U", "I", "C", None, "S", "E", "Q", "R", "P", "X"]  # None = property tests


class PlanError(Exception):
    """§15 does not have the shape the checker relies on (structural, exit 2)."""


@dataclass(frozen=True)
class Entry:
    test_id: str
    milestone: int
    interim_until: int | None = None

    def render(self) -> str:
        inner = milestone_label(self.milestone)
        if self.interim_until is not None:
            inner += f", interim until {milestone_label(self.interim_until)}"
        return f"{self.test_id} ({inner})"

    @property
    def key(self) -> tuple[str, int]:
        return (self.test_id, self.milestone)


@dataclass
class Row:
    requirement: str
    priority: str
    milestone: int
    entries: list[Entry]
    lineno: int

    def render(self) -> str:
        cell = ", ".join(e.render() for e in self.entries) or EMPTY_CELL
        return (
            f"| {self.requirement} | {self.priority} | {milestone_label(self.milestone)} | {cell} |"
        )


@dataclass
class Plan:
    lines: list[str]
    rows: dict[str, Row] = field(default_factory=dict)
    not_gated_lineno: int | None = None
    per_milestone_lineno: int | None = None


def entry_sort_key(entry: Entry) -> tuple[int, int, str, int]:
    family, _, rest = entry.test_id.partition("-")
    if family in FAMILIES and rest:
        idx = FAMILIES.index(family)
        number = int(rest) if rest.isdigit() else 0
        return (idx, number, rest if not rest.isdigit() else "", entry.milestone)
    return (FAMILIES.index(None), 0, entry.test_id, entry.milestone)


def _parse_entry(text: str, where: str) -> Entry:
    match = ENTRY_RE.match(text.strip())
    if match is None:
        raise PlanError(f"{where}: malformed Tests entry {text.strip()!r}")
    until = match.group("until")
    return Entry(
        match.group("id"),
        milestone_number(match.group("ms")),
        None if until is None else milestone_number(until),
    )


def parse_plan(text: str) -> Plan:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if SECTION_RE.match(line)]
    if len(starts) != 1:
        raise PlanError(f"expected one '## 15.' heading in docs/04, found {len(starts)}")
    headers = [i for i in range(starts[0], len(lines)) if lines[i].strip() == HEADER]
    if len(headers) != 1:
        raise PlanError(f"expected one §15 table header, found {len(headers)}")
    plan = Plan(lines)
    i = headers[0] + 2
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        if len(cells) != 4:
            raise PlanError(f"docs/04:{i + 1}: §15 row does not have four cells")
        req, prio, ms, tests = cells
        if req in plan.rows:
            raise PlanError(f"docs/04:{i + 1}: {req} appears twice in §15")
        if prio not in ("M", "S", "C"):
            raise PlanError(f"docs/04:{i + 1}: priority must be M, S or C, got {prio!r}")
        entries = (
            []
            if tests in (EMPTY_CELL, "", "-")
            else [_parse_entry(e, f"docs/04:{i + 1}") for e in tests.split(",")]
        )
        plan.rows[req] = Row(req, prio, milestone_number(ms), entries, i)
        i += 1
    if not plan.rows:
        raise PlanError("§15 table has no rows")
    for j in range(i, min(i + 12, len(lines))):
        if lines[j].startswith(NOT_GATED_PREFIX):
            plan.not_gated_lineno = j
        elif lines[j].startswith(PER_MILESTONE_PREFIX):
            plan.per_milestone_lineno = j
    if plan.not_gated_lineno is None or plan.per_milestone_lineno is None:
        raise PlanError(
            "§15 footer lines (Not release-gated / M requirements first gated) not found"
        )
    return plan


def render_footers(plan: Plan) -> tuple[str, str]:
    not_gated = [r for r in plan.rows.values() if r.priority in ("S", "C") and not r.entries]
    listing = (
        "none"
        if not not_gated
        else ", ".join(
            f"{r.requirement} ({r.priority})"
            for r in sorted(not_gated, key=lambda r: id_key(r.requirement))
        )
    )
    line1 = f"{NOT_GATED_PREFIX} (S or C requirements with no named test): {listing}."
    counts: dict[int, int] = defaultdict(int)
    for r in plan.rows.values():
        if r.priority == "M":
            counts[r.milestone] += 1
    line2 = (
        f"{PER_MILESTONE_PREFIX} "
        + " · ".join(f"{milestone_label(m)} {counts[m]}" for m in sorted(counts))
        + "."
    )
    return line1, line2


@dataclass(frozen=True)
class CodePart:
    node_id: str
    test_id: str | None
    milestone: int
    requirements: tuple[str, ...]
    interim_until: int | None
    file: str


def code_parts(parts: list[dict[str, Any]]) -> list[CodePart]:
    out: list[CodePart] = []
    for p in parts:
        if p.get("origin") != "marker":
            continue
        until = p.get("interim_until")
        out.append(
            CodePart(
                node_id=str(p["node_id"]),
                test_id=p.get("test_id"),
                milestone=milestone_number(str(p["milestone"])),
                requirements=tuple(str(r) for r in p.get("requirements", [])),
                interim_until=None if until is None else milestone_number(str(until)),
                file=str(p.get("file", "")),
            )
        )
    return out


def reconcile(plan: Plan, spec: Spec, parts: list[CodePart], current: int) -> list[str]:
    findings: list[str] = []
    assigned = spec.assigned_milestones()
    priorities = spec.priorities()

    for req in sorted(set(assigned) - set(plan.rows), key=id_key):
        findings.append(f"missing row: {req} is assigned in §22 but has no §15 row")
    for req in sorted(set(plan.rows) - set(assigned), key=id_key):
        findings.append(f"extra row: {req} is in §15 but not assigned in §22")
    for req in sorted(set(plan.rows) & set(assigned), key=id_key):
        row = plan.rows[req]
        if row.milestone != assigned[req]:
            theirs, ours = milestone_label(row.milestone), milestone_label(assigned[req])
            findings.append(f"milestone mismatch {req}: §15 says {theirs}, §22 says {ours}")
        if req in priorities and row.priority != priorities[req]:
            findings.append(
                f"priority mismatch {req}: §15 says {row.priority}, docs/03 says {priorities[req]}"
            )

    by_key: dict[tuple[str, int], set[str]] = defaultdict(set)
    for part in parts:
        if part.test_id is None:
            findings.append(
                f"unmappable part {part.node_id}: name does not map to a docs/04 test ID"
            )
            continue
        by_key[(part.test_id, part.milestone)].add(part.file)
    for (test_id, ms), files in sorted(by_key.items()):
        if len(files) > 1:
            findings.append(
                f"duplicate part {test_id} ({milestone_label(ms)}): {', '.join(sorted(files))}"
            )

    for part in parts:
        if part.test_id is None:
            continue
        for req in part.requirements:
            row = plan.rows.get(req)
            if row is None:
                continue  # already a missing-row finding (or an undefined ID, which #38 reports)
            match = next((e for e in row.entries if e.key == (part.test_id, part.milestone)), None)
            label = milestone_label(part.milestone)
            if match is None:
                fn = part.node_id.rsplit("::", 1)[-1]
                findings.append(f"missing from §15: {req} ← {fn} ({label})")
                continue
            if match.interim_until != part.interim_until:
                findings.append(
                    f"interim mismatch {req} {part.test_id} ({label}): §15 says "
                    f"{match.interim_until and milestone_label(match.interim_until)}, code says "
                    f"{part.interim_until and milestone_label(part.interim_until)}"
                )
            until = part.interim_until if part.interim_until is not None else match.interim_until
            if until is not None and until <= current:
                findings.append(
                    f"retired part still present: {req} {part.test_id} ({label}, interim until "
                    f"{milestone_label(until)})"
                )

    for req in sorted(plan.rows, key=id_key):
        row = plan.rows[req]
        for entry in row.entries:
            if entry.milestone > current:
                continue
            present = any(
                p.test_id == entry.test_id
                and p.milestone == entry.milestone
                and req in p.requirements
                for p in parts
            )
            if not present:
                findings.append(f"planned but absent: {req} {entry.render()}")
        if row.priority == "M" and row.milestone <= current and not row.entries:
            findings.append(f"no tests: {req} (M, {milestone_label(row.milestone)})")
    return findings


def write_plan(plan: Plan, parts: list[CodePart], current: int) -> list[str]:
    """New file lines: Tests cells of rows ≤ current merged with code; footers regenerated."""
    lines = list(plan.lines)
    by_req: dict[str, list[CodePart]] = defaultdict(list)
    for part in parts:
        if part.test_id is not None:
            for req in part.requirements:
                by_req[req].append(part)
    for req, row in plan.rows.items():
        if row.milestone > current:
            continue
        merged: list[Entry] = []
        code = {(p.test_id, p.milestone): p for p in by_req.get(req, [])}
        for entry in row.entries:
            part = code.get(entry.key)
            merged.append(
                Entry(entry.test_id, entry.milestone, part.interim_until) if part else entry
            )
        existing = {e.key for e in merged}
        additions = sorted(
            (
                Entry(p.test_id or "", p.milestone, p.interim_until)
                for key, p in code.items()
                if key not in existing
            ),
            key=entry_sort_key,
        )
        new_row = Row(req, row.priority, row.milestone, merged + additions, row.lineno)
        lines[row.lineno] = new_row.render()
        plan.rows[req] = new_row
    line1, line2 = render_footers(plan)
    if plan.not_gated_lineno is not None:
        lines[plan.not_gated_lineno] = line1
    if plan.per_milestone_lineno is not None:
        lines[plan.per_milestone_lineno] = line2
    return lines


def write_text(lines: list[str], original: str) -> str:
    return "\n".join(lines) + ("\n" if original.endswith("\n") else "")


def run(
    plan_path: Path, spec: Spec, parts: list[CodePart], current: int, *, write: bool
) -> tuple[list[str], str]:
    """Run the check (optionally writing first). Returns (findings, summary)."""
    original = plan_path.read_text(encoding="utf-8")
    plan = parse_plan(original)
    if write:
        pre = [
            f for f in reconcile(plan, spec, parts, current) if f.startswith("milestone mismatch")
        ]
        if pre:
            raise PlanError(
                "refusing to --write while §15 and §22 disagree on a milestone: " + pre[0]
            )
        new_text = write_text(write_plan(plan, parts, current), original)
        if new_text != original:
            plan_path.write_text(new_text, encoding="utf-8")
        plan = parse_plan(new_text)
    findings = reconcile(plan, spec, parts, current)
    summary = (
        f"{len(plan.rows)} §15 rows, {len(parts)} code parts, "
        f"{sum(1 for r in plan.rows.values() if r.milestone <= current)} rows at ≤ "
        f"{milestone_label(current)}; §15 and §22 agree on every milestone"
        if not any(f.startswith("milestone mismatch") for f in findings)
        else f"{len(plan.rows)} §15 rows, {len(parts)} code parts"
    )
    return findings, summary
