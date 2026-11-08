"""C-14 (M8) (docs/04): the image check joins the guarantee table with its metric
(FR-VERIFY-15, NFR-QUAL-12; #270, ADR-182).

- ``test_c_14__m8_image_check``: the ``image_check`` row is a model-dependent step measured by
  NFR-QUAL-12 (E-11's per-policy false-negative and false-positive rates), naming the
  ``image_check`` purpose, outside stories, and its note states the minors line "certified only
  by external attestation; otherwise an uncertified backstop". The published table
  (``docs/guarantees.md``) is what the rows render and carries that line. E-11 (``e11``) declares
  the row and reports NFR-QUAL-12, so ``evals --check`` passes with nothing pending. Complement:
  without the row the guarantee check fails (a delivered purpose with no row) and so does the
  evals check (the suite declares a row that does not exist); with the row unmeasured, E-11's
  declaration fails the evals check.
"""

from __future__ import annotations

import dataclasses

import pytest

from epistrel.core.profiles.guarantees import (
    GUARANTEE,
    GUARANTEES,
    MINORS_IMAGE_CHECK_LINE,
    UNMEASURED,
    Kind,
)
from epistrel.evals import SUITES
from epistrel.evals import check as evals_check
from epistrel.orchestrator.models import registry
from epistrel.tools import guarantees as tool
from epistrel.tools.generated import REPO_ROOT

ROW = "image_check"
MINORS_LINE = "certified only by external attestation; otherwise an uncertified backstop"


@pytest.mark.part("M8", "FR-VERIFY-15; NFR-QUAL-12")
def test_c_14__m8_image_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The image check's row, its metric, its suite and the published table agree; without the
    row, or with it unmeasured, the checks fail."""
    row = GUARANTEE[ROW]
    assert (row.kind, row.metric, row.purposes, row.in_story) == (
        Kind.model_dependent,
        "NFR-QUAL-12",
        (ROW,),
        False,
    )
    assert ROW in registry.DELIVERED
    assert MINORS_IMAGE_CHECK_LINE == MINORS_LINE
    assert row.note is not None
    assert MINORS_LINE in row.note
    assert tool.problems() == []

    table = (REPO_ROOT / "docs" / "guarantees.md").read_text(encoding="utf-8")
    assert table == tool.render()
    (line,) = [
        line
        for line in table.splitlines()
        if f"| `{ROW}` | model-dependent | NFR-QUAL-12 |" in line
    ]
    assert f"`{ROW}`" in line
    assert MINORS_LINE in table

    suite = SUITES["e11"]
    assert ROW in suite.guarantees
    assert {m.requirement for m in suite.metrics} == {"NFR-QUAL-12"}
    assert "NFR-QUAL-12" not in evals_check.PENDING
    assert evals_check.problems() == []

    without = [r for r in GUARANTEES if r.id != ROW]
    assert f"{ROW}: a delivered model purpose with no model-dependent row" in tool.problems(without)
    monkeypatch.setattr(evals_check, "GUARANTEE", {r.id: r for r in without})
    monkeypatch.setattr(evals_check, "GUARANTEES", tuple(without))
    assert f"suite e11: no guarantee row {ROW!r}" in evals_check.problems()

    unmeasured = [
        dataclasses.replace(r, metric=UNMEASURED) if r.id == ROW else r for r in GUARANTEES
    ]
    monkeypatch.setattr(evals_check, "GUARANTEE", {r.id: r for r in unmeasured})
    monkeypatch.setattr(evals_check, "GUARANTEES", tuple(unmeasured))
    assert (
        f"suite e11: declares guarantee row {ROW} but reports no {UNMEASURED} metric"
        in evals_check.problems()
    )
