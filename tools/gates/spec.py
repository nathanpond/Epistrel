"""Parse docs/03: requirement definitions and the §22 Assignment table."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ID_PATTERN = r"(?:FR|NFR)-[A-Z]+-\d+"
ID_RE = re.compile(rf"^{ID_PATTERN}$")
DEFINITION_RE = re.compile(rf"^- \*\*(?P<id>{ID_PATTERN}) \((?P<priority>[MSC])\)\.\*\*")
SECTION_22_RE = re.compile(r"^## 22\. Delivery milestones")
ASSIGNMENT_HEADER = "| Milestone | Requirements first gated at this milestone |"
MILESTONE_RE = re.compile(r"^M(?P<n>\d+)$")
PREFIX_RE = re.compile(r"^(?P<prefix>(?:FR|NFR)-[A-Z]+)-(?P<rest>.+)$")
RANGE_RE = re.compile(r"^(?P<lo>\d+)\.\.(?P<hi>\d+)$")


class SpecError(Exception):
    """The document does not have the shape the checker relies on (a structural problem)."""


@dataclass(frozen=True)
class Definition:
    id: str
    priority: str
    line: int


@dataclass(frozen=True)
class Spec:
    definitions: list[Definition]
    assignment: dict[int, list[str]] = field(default_factory=dict)
    """Milestone number → requirement IDs in table order (duplicates kept)."""

    def priorities(self) -> dict[str, str]:
        """ID → priority letter (first definition wins; `redefined` is #37's finding)."""
        out: dict[str, str] = {}
        for d in self.definitions:
            out.setdefault(d.id, d.priority)
        return out

    def assigned_milestones(self) -> dict[str, int]:
        """ID → milestone number (first row wins; `duplicate` is #37's finding)."""
        out: dict[str, int] = {}
        for milestone, ids in sorted(self.assignment.items()):
            for req_id in ids:
                out.setdefault(req_id, milestone)
        return out


def milestone_number(label: str) -> int:
    """`M2` → 2; anything else is a SpecError."""
    match = MILESTONE_RE.match(label.strip())
    if match is None:
        raise SpecError(f"milestone must look like M<n>, got {label!r}")
    return int(match.group("n"))


def milestone_label(number: int) -> str:
    """2 → `M2`."""
    return f"M{number}"


def id_key(req_id: str) -> tuple[str, int]:
    """Natural order: alphabetical by prefix, numeric within."""
    prefix, _, number = req_id.rpartition("-")
    return prefix, int(number)


def expand_ids(cell: str) -> list[str]:
    """Expand `FR-STORE-10..12; FR-CONC-1,2,4..6` into individual IDs, in cell order.

    A cell that contains no ID (e.g. `None — not yet planned`) expands to nothing.
    """
    text = cell.replace("**", "").strip()
    if not re.search(ID_PATTERN, text):
        return []
    ids: list[str] = []
    for group in filter(None, (g.strip() for g in text.split(";"))):
        match = PREFIX_RE.match(group)
        if match is None:
            raise SpecError(f"malformed requirement group: {group!r}")
        prefix, rest = match.group("prefix"), match.group("rest")
        for part in (p.strip() for p in rest.split(",")):
            if part.isdigit():
                ids.append(f"{prefix}-{int(part)}")
                continue
            rng = RANGE_RE.match(part)
            if rng is None:
                raise SpecError(f"malformed range {part!r} in {group!r}")
            lo, hi = int(rng.group("lo")), int(rng.group("hi"))
            if lo >= hi:
                raise SpecError(f"range {part!r} in {group!r} must ascend")
            ids.extend(f"{prefix}-{n}" for n in range(lo, hi + 1))
    return ids


def parse_definitions(lines: Iterable[str]) -> list[Definition]:
    found: list[Definition] = []
    for lineno, line in enumerate(lines, start=1):
        match = DEFINITION_RE.match(line)
        if match:
            found.append(Definition(match.group("id"), match.group("priority"), lineno))
    return found


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_assignment(lines: list[str]) -> dict[int, list[str]]:
    """Find the §22 Assignment table; return milestone → IDs in table order, duplicates kept."""
    starts = [i for i, line in enumerate(lines) if SECTION_22_RE.match(line)]
    if len(starts) != 1:
        raise SpecError(
            f"expected exactly one '## 22. Delivery milestones' heading, found {len(starts)}"
        )
    headers = [i for i in range(starts[0], len(lines)) if lines[i].strip() == ASSIGNMENT_HEADER]
    if len(headers) != 1:
        raise SpecError(
            f"expected exactly one assignment table header under §22, found {len(headers)}"
        )
    table: dict[int, list[str]] = {}
    i = headers[0] + 1
    if i >= len(lines) or not re.match(r"^\|\s*-+\s*\|\s*-+\s*\|\s*$", lines[i].strip()):
        raise SpecError("assignment table header is not followed by a separator row")
    for line in lines[i + 1 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = _split_row(line)
        if len(cells) != 2:
            raise SpecError(f"assignment row does not have two cells: {line.strip()!r}")
        ms = MILESTONE_RE.match(cells[0].replace("**", "").strip())
        if ms is None:
            raise SpecError(f"assignment row milestone must match M<n>: {cells[0]!r}")
        number = int(ms.group("n"))
        if number in table:
            raise SpecError(f"milestone M{number} appears in two assignment rows")
        table[number] = expand_ids(cells[1])
    if not table:
        raise SpecError("assignment table has no rows")
    return table


def load_spec(path: Path) -> Spec:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc
    return Spec(definitions=parse_definitions(lines), assignment=parse_assignment(lines))
