"""The Lite history rewrite's guarantee rows (#245's C-14 complement; FR-VERIFY-15; #700).

Both Lite rewrite purposes, ``lite_rewrite_parse`` and ``lite_rewrite_reach``, are delivered and
have a model-dependent, unmeasured row outside stories, which the published table
(``docs/guarantees.md``) and the model guide (``docs/models.md``) list. Complement: removing
either row fails the guarantee check (a delivered purpose with no model-dependent row).
"""

from __future__ import annotations

import pytest

from epistrel.core.profiles.guarantees import GUARANTEE, GUARANTEES, UNMEASURED, Kind
from epistrel.orchestrator.models import registry
from epistrel.tools import guarantees as tool
from epistrel.tools.generated import REPO_ROOT

ROWS = ("lite_rewrite_parse", "lite_rewrite_reach")


@pytest.mark.parametrize("row_id", ROWS)
def test_lite_rewrite_rows(row_id: str) -> None:
    row = GUARANTEE[row_id]
    assert (row.kind, row.metric, row.purposes) == (Kind.model_dependent, UNMEASURED, (row_id,))
    assert row.in_story is False
    assert row_id in registry.DELIVERED
    assert tool.problems() == []
    table = (REPO_ROOT / "docs" / "guarantees.md").read_text(encoding="utf-8")
    assert table == tool.render()
    assert f"`{row_id}`" in table
    models = (REPO_ROOT / "docs" / "models.md").read_text(encoding="utf-8")
    assert f"### `{row_id}`" in models

    without = [r for r in GUARANTEES if row_id not in r.purposes]
    assert f"{row_id}: a delivered model purpose with no model-dependent row" in tool.problems(
        without
    )
