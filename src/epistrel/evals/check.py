"""``python -m epistrel.evals --check`` (#134): the suites, the guarantee table and the
committed reports agree. It needs no labeled set, model or database (it runs in CI's lint job).

1. **Every suite can gate as declared**: each metric passes ``gate.metric_problems`` (a rate or
   zero-tolerance metric without a floor fails here, as does an informational one with a floor),
   metric ids are unique, and every ``GUARANTEES`` row (#132) a suite declares exists, is
   model-dependent, and has its metric measured by that suite.
2. **Every model-dependent guarantee row's metric is measured** (one-directional; rows whose
   metric is ``unmeasured`` are skipped): some suite reports a metric under that NFR-QUAL id
   (``nfr_qual_<n>.…``), or the metric is listed in ``PENDING`` with the story or milestone that
   ships its suite. A pending metric that a suite already measures fails too, so the list only
   shrinks.
3. ``docs/evals/INDEX.md`` is exactly what the committed reports regenerate, and every report
   carries its summary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

from epistrel.core.profiles.guarantees import GUARANTEE, GUARANTEES, UNMEASURED, Kind
from epistrel.evals.gate import metric_problems
from epistrel.evals.report import INDEX_NAME, index_text
from epistrel.evals.suites import SUITES, Suite
from epistrel.tools.generated import REPO_ROOT

REPORTS_DIR = REPO_ROOT / "docs" / "evals"

PENDING: Mapping[str, str] = MappingProxyType(
    {
        "NFR-QUAL-14": "#158",
        "NFR-QUAL-9": "M6 (time estimation is delivered there)",
    }
)
"""Model-dependent guarantee metrics whose suite a later story ships, with that story: the
prose floor's is #158's, time estimation's M6's (#157's suites measure NFR-QUAL-2..7,10). Each
story removes its entries when its suites register."""


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def measured(suites: Iterable[Suite]) -> set[str]:
    """The NFR-QUAL requirements some suite reports a metric under."""
    return {m.requirement for s in suites for m in s.metrics if m.requirement is not None}


def suite_problems(suite: Suite) -> list[str]:
    found = [f"suite {suite.name}: {p}" for m in suite.metrics for p in metric_problems(m)]
    ids = [m.id for m in suite.metrics]
    found += [f"suite {suite.name}: metric {i} twice" for i in sorted(set(ids)) if ids.count(i) > 1]
    if not suite.metrics:
        found.append(f"suite {suite.name}: no metrics")
    own = measured([suite])
    for row_id in suite.guarantees:
        row = GUARANTEE.get(row_id)
        if row is None:
            found.append(f"suite {suite.name}: no guarantee row {row_id!r}")
        elif row.kind is not Kind.model_dependent:
            found.append(f"suite {suite.name}: guarantee row {row_id} is not model-dependent")
        elif row.metric not in own:
            found.append(
                f"suite {suite.name}: declares guarantee row {row_id} but reports no "
                f"{row.metric} metric"
            )
    return found


def problems(
    suites: Iterable[Suite] | None = None,
    *,
    pending: Mapping[str, str] = PENDING,
    reports_dir: Path = REPORTS_DIR,
) -> list[str]:
    """Why the evals and the guarantee table disagree (empty when they agree)."""
    chosen = list(SUITES.values()) if suites is None else list(suites)
    found = [p for suite in chosen for p in suite_problems(suite)]
    names = [s.name for s in chosen]
    found += [f"suite {n}: registered twice" for n in sorted(set(names)) if names.count(n) > 1]
    covered = measured(chosen)
    for row in GUARANTEES:
        if row.kind is not Kind.model_dependent or row.metric in (None, UNMEASURED):
            continue
        if row.metric in covered or row.metric in pending:
            continue
        found.append(
            f"guarantee row {row.id}: its metric {row.metric} is measured by no eval suite"
        )
    found += [
        f"{req} is measured now: remove it from PENDING ({story})"
        for req, story in sorted(pending.items())
        if req in covered
    ]
    expected, unreadable = index_text(reports_dir)
    found += [f"{_shown(reports_dir / n)}: no embedded report summary" for n in unreadable]
    index = reports_dir / INDEX_NAME
    if not index.is_file() or index.read_text(encoding="utf-8") != expected:
        found.append(f"{_shown(index)} is stale: run `uv run python -m epistrel.evals index`")
    return found


__all__ = ["PENDING", "REPORTS_DIR", "measured", "problems", "suite_problems"]
