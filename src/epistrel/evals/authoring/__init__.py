"""#157's set authoring (and #158's judge calibration pairs and intervention scenarios, and
#179's zero-leak and viewpoint sets, and #201's time-estimation set): the
labeled sets of the NFR-QUAL suites, generated deterministically
from hand-written templates (no model), one module per set.

``python -m epistrel.evals author`` writes every generated set under ``evals/sets/``;
``--check-sets`` (CI's lint job) fails when a committed set differs from what its module
generates, so a set changes only through its reviewed authoring module. The smoke and E-02 sets
(#134, #155) are hand-written and not generated here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from epistrel.evals.authoring import (
    asymmetry,
    claim_detection,
    conformance,
    consolidation,
    entailment,
    entailment_viewpoint,
    importance,
    judge_calibration,
    retrieval,
    salience,
    scenarios,
    structured_output,
    think_leak,
    time_estimation,
)

GENERATORS: Mapping[str, Callable[[], list[dict[str, Any]]]] = MappingProxyType(
    {
        "entailment/cases.jsonl": entailment.cases,
        "consolidation/cases.jsonl": consolidation.cases,
        "importance/cases.jsonl": importance.cases,
        "salience/cases.jsonl": salience.cases,
        "generation_conformance/cases.jsonl": conformance.cases,
        "structured_output/cases.jsonl": structured_output.cases,
        "claim_detection/cases.jsonl": claim_detection.cases,
        "retrieval/recall.jsonl": retrieval.recall_cases,
        "retrieval/resolution.jsonl": retrieval.resolution_cases,
        "retrieval/abstract.jsonl": retrieval.abstract_cases,
        "judge_calibration/cases.jsonl": judge_calibration.cases,
        "scenarios/cases.jsonl": scenarios.cases,
        "asymmetry/cases.jsonl": asymmetry.cases,
        "think_leak/cases.jsonl": think_leak.cases,
        "entailment_viewpoint/cases.jsonl": entailment_viewpoint.cases,
        "time_estimation/cases.jsonl": time_estimation.cases,
    }
)
"""Each generated set's path under ``evals/sets`` and the function that authors it."""
E11_MANIFEST = "e11/cases.jsonl"
"""E-11's manifest (#270): written by ``author e11`` (``authoring.e11``) from a local image
model, never regenerated here."""


def render(cases: list[dict[str, Any]]) -> str:
    """JSON lines, one case each, keys in authoring order."""
    return "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases)


def write(sets_dir: Path) -> list[Path]:
    """Write every generated set; the paths written."""
    written = []
    for relative, generate in GENERATORS.items():
        path = sets_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(generate()), encoding="utf-8")
        written.append(path)
    return written


def stale(sets_dir: Path) -> list[str]:
    """The generated sets whose committed file differs from their module's output. E-11's set
    (#270) is exempt: its images come from a model and are never committed, so it cannot be
    regenerated here; instead every line of its manifest, when there is one, must validate
    (the schema and the safety rule, ``evals.e11_safety``)."""
    found = []
    for relative, generate in GENERATORS.items():
        path = sets_dir / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != render(generate()):
            found.append(f"{relative} differs from what its authoring module generates")
    return found + e11_manifest_problems(sets_dir)


def e11_manifest_problems(sets_dir: Path) -> list[str]:
    """Why E-11's committed manifest does not validate (nothing when it is absent)."""
    from epistrel.evals import e11_safety

    path = sets_dir / E11_MANIFEST
    if not path.is_file():
        return []
    try:
        e11_safety.parse_lines(path)
    except e11_safety.SafetyRefusal as exc:
        return [f"{E11_MANIFEST}: {problem}" for problem in exc.problems]
    return []


__all__ = ["E11_MANIFEST", "GENERATORS", "e11_manifest_problems", "render", "stale", "write"]
