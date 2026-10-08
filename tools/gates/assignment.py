"""check-assignment: every defined requirement is assigned to exactly one milestone, and back."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from tools.gates.spec import Spec, id_key


@dataclass(frozen=True)
class AssignmentReport:
    findings: list[str]
    defined: int
    assigned_unique: int

    @property
    def ok(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        return f"defined {self.defined}, assigned {self.assigned_unique} (unique)"


def check_assignment(spec: Spec) -> AssignmentReport:
    defined_counts = Counter(d.id for d in spec.definitions)
    defined_ids = set(defined_counts)

    where: dict[str, list[int]] = defaultdict(list)
    for milestone, ids in spec.assignment.items():
        for req_id in ids:
            where[req_id].append(milestone)
    assigned_ids = set(where)

    problems: dict[str, str] = {}
    for req_id, count in defined_counts.items():
        if count > 1:
            problems[f"redefined {req_id}"] = req_id
    for req_id in defined_ids - assigned_ids:
        problems[f"unassigned {req_id}"] = req_id
    for req_id, milestones in where.items():
        if len(milestones) > 1:
            listed = ", ".join(f"M{m}" for m in sorted(milestones))
            problems[f"duplicate {req_id} ({listed})"] = req_id
    for req_id in assigned_ids - defined_ids:
        listed = ", ".join(f"M{m}" for m in sorted(set(where[req_id])))
        problems[f"undefined {req_id} ({listed})"] = req_id

    findings = sorted(problems, key=lambda line: (id_key(problems[line]), line))
    return AssignmentReport(findings, defined=len(defined_ids), assigned_unique=len(assigned_ids))
