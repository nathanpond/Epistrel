from __future__ import annotations

import pytest
from tools.gates.spec import SpecError, expand_ids, id_key


def test_simple_range() -> None:
    assert expand_ids("FR-STORE-10..12") == ["FR-STORE-10", "FR-STORE-11", "FR-STORE-12"]


def test_list_with_range() -> None:
    assert expand_ids("FR-CONC-1,2,4..6") == [
        "FR-CONC-1",
        "FR-CONC-2",
        "FR-CONC-4",
        "FR-CONC-5",
        "FR-CONC-6",
    ]


def test_nfr_list_with_range_and_spaces() -> None:
    assert len(expand_ids(" NFR-PERF-3, 4, 6..8 ")) == 5


def test_several_groups_and_bold() -> None:
    assert expand_ids("**FR-A-1; NFR-B-2..3**") == ["FR-A-1", "NFR-B-2", "NFR-B-3"]


def test_none_row_contributes_nothing() -> None:
    assert expand_ids("None — not yet planned") == []
    assert expand_ids("FR-A-x") == []  # no ID in the cell at all: treated as a None row


@pytest.mark.parametrize("cell", ["FR-A-5..3", "FR-A-3..3", "FR-A-1..", "FR-A-1..2..3", "FR-A-1,x"])
def test_malformed_ranges_raise(cell: str) -> None:
    with pytest.raises(SpecError):
        expand_ids(cell)


def test_natural_order() -> None:
    ids = ["FR-STORE-10", "FR-STORE-2", "FR-CONC-1", "NFR-PERF-1"]
    assert sorted(ids, key=id_key) == ["FR-CONC-1", "FR-STORE-2", "FR-STORE-10", "NFR-PERF-1"]
