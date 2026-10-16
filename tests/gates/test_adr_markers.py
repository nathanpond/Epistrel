"""The ADR log's inline markers follow its header (docs/05; #405).

A clause a later ADR makes wrong carries *[superseded by ADR-…]* (or a variant the log
uses: *[superseded in part by ADR-…]*, *[catalog part superseded by ADR-…]*); only a narrowed
**Consequences** line carries *[revised per ADR-…]*. The index by ADR shows an ADR with a
superseded clause as **Partly superseded** (#405, #415).
"""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

ADR_LOG = REPO_ROOT / "docs" / "05-adr-log.md"
REVISED = re.compile(r"\*\[revised per ADR-\d", re.IGNORECASE)
SUPERSEDED = re.compile(r"\*\[[^\]]*?superseded (?:in part )?by ADR-\d", re.IGNORECASE)
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
    """The ADRs whose own text carries a supersession marker (any form: ``SUPERSEDED``)."""
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


def misindexed(log: str) -> dict[str, str | None]:
    """The ADRs with a superseded clause the index by ADR does not show as superseded, with
    the standing it shows instead."""
    index = standings(log)
    return {
        adr: index.get(adr)
        for adr in superseded_adrs(log)
        if index.get(adr) not in {"Partly superseded", "Superseded"}
    }


def test_revised_per_marks_only_consequences_lines() -> None:
    assert misplaced_revisions(ADR_LOG.read_text("utf-8")) == []


def test_every_adr_with_a_superseded_clause_is_indexed_as_partly_superseded() -> None:
    log = ADR_LOG.read_text("utf-8")
    assert len(standings(log)) > 150  # the index was read
    assert misindexed(log) == {}


def test_the_checks_flag_what_they_are_for() -> None:
    """Complements on a synthetic log: a Decision line marked *[revised per]* (in any case) is
    flagged, a Consequences line is not, and the header's own wording (no ADR number) is not;
    an ADR with a superseded clause, in the plain form or a variant, indexed Amended is
    misindexed, and one indexed Partly superseded is not."""
    log = "\n".join(
        [
            "Narrowed lines are marked *[revised per ADR-…]*.",
            "### ADR-001 — One",
            "**Decision:** Do X. *[revised per ADR-002: X only on Mondays]*",
            "**Consequences:** Y. *[revised per ADR-002: Y on Mondays]*",
            "### ADR-002 — Two",
            "**Decision:** Do Z. *[superseded by ADR-003: Z twice]*",
            "### ADR-003 — Three",
            "**Decision:** Do W. *[Timing superseded in part by ADR-004: W later]*",
            "**Decision:** Do V. *[Revised per ADR-004: V less]*",
            "### ADR-004 — Four",
            "**Decision:** Do U. *[catalog part superseded by ADR-005: U for stories]*",
            "| 002 | Two | Amended | 003 |",
            "| 003 | Three | Amended | 004 |",
            "| 004 | Four | Partly superseded | 005 |",
        ]
    )
    assert misplaced_revisions(log) == [3, 9]
    assert superseded_adrs(log) == {"002", "003", "004"}
    assert misindexed(log) == {"002": "Amended", "003": "Amended"}
