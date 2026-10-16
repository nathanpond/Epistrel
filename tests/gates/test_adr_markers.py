"""The ADR log's inline markers follow its header (docs/05; #405).

A clause a later ADR makes wrong carries *[superseded by ADR-…]*; only a narrowed
**Consequences** line carries *[revised per ADR-…]*. The index by ADR shows an ADR with a
superseded clause as **Partly superseded**.
"""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

ADR_LOG = REPO_ROOT / "docs" / "05-adr-log.md"
REVISED = re.compile(r"\*\[revised per ADR-\d")
SUPERSEDED = re.compile(r"\*\[superseded by ADR-\d")
HEADING = re.compile(r"^### ADR-(\d{3}) ")
INDEX_ROW = re.compile(r"^\| (\d{3}) \| .* \| (Current|Amended|Partly superseded|Superseded) \|")


def misplaced_revisions(log: str) -> list[int]:
    """The line numbers of *[revised per ADR-…]* markers outside a **Consequences** line."""
    return [
        n
        for n, line in enumerate(log.splitlines(), 1)
        if REVISED.search(line) and not line.startswith("**Consequences:**")
    ]


def superseded_adrs(log: str) -> set[str]:
    """The ADRs whose own text carries a *[superseded by ADR-…]* marker."""
    found: set[str] = set()
    adr: str | None = None
    for line in log.splitlines():
        if heading := HEADING.match(line):
            adr = heading.group(1)
        elif adr is not None and SUPERSEDED.search(line):
            found.add(adr)
    return found


def standings(log: str) -> dict[str, str]:
    """The index by ADR: each ADR's standing."""
    return {m.group(1): m.group(2) for line in log.splitlines() if (m := INDEX_ROW.match(line))}


def test_revised_per_marks_only_consequences_lines() -> None:
    assert misplaced_revisions(ADR_LOG.read_text("utf-8")) == []


def test_every_adr_with_a_superseded_clause_is_indexed_as_partly_superseded() -> None:
    log = ADR_LOG.read_text("utf-8")
    index = standings(log)
    assert len(index) > 150  # the index was read
    wrong = {
        adr: index.get(adr)
        for adr in superseded_adrs(log)
        if index.get(adr) not in {"Partly superseded", "Superseded"}
    }
    assert wrong == {}


def test_the_checks_flag_what_they_are_for() -> None:
    """Complements on a synthetic log: a Decision line marked *[revised per]* is flagged, a
    Consequences line is not, and the header's own wording (no ADR number) is not; an ADR
    with a superseded clause is found and its Amended standing is visible."""
    log = "\n".join(
        [
            "Narrowed lines are marked *[revised per ADR-…]*.",
            "### ADR-001 — One",
            "**Decision:** Do X. *[revised per ADR-002: X only on Mondays]*",
            "**Consequences:** Y. *[revised per ADR-002: Y on Mondays]*",
            "### ADR-002 — Two",
            "**Decision:** Do Z. *[superseded by ADR-003: Z twice]*",
            "| 002 | Two | Amended | 003 |",
        ]
    )
    assert misplaced_revisions(log) == [3]
    assert superseded_adrs(log) == {"002"}
    assert standings(log) == {"002": "Amended"}
