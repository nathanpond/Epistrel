"""C-14 (M6) (docs/04): time estimation joins the guarantee table with its metric (FR-VERIFY-15,
NFR-QUAL-9; #201).

- ``test_c_14__m6``: the ``time_estimation`` row is a model-dependent step measured by
  NFR-QUAL-9, on the clock at ``on``, naming the ``time_estimation`` purpose; it says M6 until
  #203 delivers the ``clock`` module, and nothing once it is delivered. The published table
  (``docs/guarantees.md``) and the model guide (``docs/models.md``) list it. E-09 (``e09``)
  declares the row and measures NFR-QUAL-9 (``nfr_qual_9.within_range``, rate, ≥ 85 %), so
  ``evals --check`` passes with nothing pending, and ``docs/evals/README.md`` names the suite and
  its LM Studio command on port 1235. Complement: removing the row fails the guarantee check (a
  delivered purpose with no row) and the evals check (the suite declares a row that does not
  exist).
"""

from __future__ import annotations

import pytest

from epistrel.core.profiles import schema
from epistrel.core.profiles.guarantees import GUARANTEE, GUARANTEES, Kind
from epistrel.evals import SUITES, Direction, Rule
from epistrel.evals import check as evals_check
from epistrel.orchestrator.models import registry
from epistrel.tools import guarantees as tool
from epistrel.tools.generated import REPO_ROOT

ROW = "time_estimation"
METRIC = "nfr_qual_9.within_range"
LIVE_RUN = (
    "uv run epistrel evals run --suite e09 --new-run --base-url http://localhost:1235/v1 \\\n"
    "    --model cydonia-24b-v4.3 --seeds 3"
)


@pytest.mark.part("M6", "FR-VERIFY-15; NFR-QUAL-9")
def test_c_14__m6(monkeypatch: pytest.MonkeyPatch) -> None:
    """Time estimation's row, its metric, its suite and its documents agree; without the row
    both checks fail."""
    row = GUARANTEE[ROW]
    assert (row.kind, row.metric, row.purposes) == (Kind.model_dependent, "NFR-QUAL-9", (ROW,))
    assert [need.module for need in row.requires] == ["clock"]
    assert ROW in registry.DELIVERED
    # The row says M6 until #203 delivers the clock, and nothing after (``tool.problems``).
    assert row.delivered is schema.is_delivered("clock", schema.ON)
    assert row.delivered_in == (None if row.delivered else "M6")
    assert tool.problems() == []

    table = (REPO_ROOT / "docs" / "guarantees.md").read_text(encoding="utf-8")
    assert table == tool.render()
    (line,) = [
        line for line in table.splitlines() if f"| `{ROW}` | model-dependent | NFR-QUAL-9 |" in line
    ]
    assert f"`{ROW}`" in line.rsplit("|", 2)[-2], line
    models = (REPO_ROOT / "docs" / "models.md").read_text(encoding="utf-8")
    assert f"### `{ROW}`" in models

    suite = SUITES["e09"]
    assert ROW in suite.guarantees
    (metric,) = [m for m in suite.metrics if m.id == METRIC]
    assert (metric.rule, metric.floor, metric.direction, metric.requirement) == (
        Rule.rate,
        0.85,
        Direction.at_least,
        "NFR-QUAL-9",
    )
    assert "NFR-QUAL-9" not in evals_check.PENDING
    assert evals_check.problems() == []
    readme = (REPO_ROOT / "docs" / "evals" / "README.md").read_text(encoding="utf-8")
    assert "| `e09` |" in readme
    assert LIVE_RUN in readme

    # Complement: without the row (and #714's Lite row for the same purpose), the table cannot
    # be published and the suite's row is gone.
    without = [r for r in GUARANTEES if ROW not in r.purposes]
    assert f"{ROW}: a delivered model purpose with no model-dependent row" in tool.problems(without)
    monkeypatch.setattr(evals_check, "GUARANTEE", {r.id: r for r in without})
    monkeypatch.setattr(evals_check, "GUARANTEES", tuple(without))
    assert f"suite e09: no guarantee row {ROW!r}" in evals_check.problems()
