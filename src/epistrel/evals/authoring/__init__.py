"""#157's set authoring: the labeled sets of the NFR-QUAL suites, generated deterministically
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
    claim_detection,
    conformance,
    consolidation,
    entailment,
    importance,
    retrieval,
    salience,
    structured_output,
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
    }
)
"""Each generated set's path under ``evals/sets`` and the function that authors it."""


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
    """The generated sets whose committed file differs from their module's output."""
    found = []
    for relative, generate in GENERATORS.items():
        path = sets_dir / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != render(generate()):
            found.append(f"{relative} differs from what its authoring module generates")
    return found


__all__ = ["GENERATORS", "render", "stale", "write"]
