"""The ADR log's inline markers follow its header (docs/05; #405).

A clause a later ADR makes wrong carries *[superseded by ADR-…]* (or a variant the log
uses: *[superseded in part by ADR-…]*, *[catalog part superseded by ADR-…]*); only a narrowed
**Consequences** line carries *[revised per ADR-…]*. The index by ADR shows an ADR with a
superseded clause as **Partly superseded** (#405, #415). An **Amended by** entry that says a
clause is superseded is matched by a marker citing that entry's ADR in the body (#420).
"""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

ADR_LOG = REPO_ROOT / "docs" / "05-adr-log.md"
REVISED = re.compile(r"\*\[revised per ADR-\d", re.IGNORECASE)
SUPERSEDED = re.compile(r"\*\[[^\]]*?superseded (?:in part )?by ADR-\d", re.IGNORECASE)
HEADING = re.compile(r"^### ADR-(\d{3}) ")
AMENDED_BY = "**Amended by:**"
# One Amended-by entry per later ADR: "ADR-077 — …" or "ADR-070/072 — …".
ENTRY = re.compile(r"ADR-(\d{3})(?:/(\d{3}))? —")
# The ADRs a marker cites: everything between "superseded … by" and the replacement's colon.
CITED = re.compile(r"\*\[[^\]]*?superseded (?:in part )?by (ADR-\d{3}[^:\]]*)", re.IGNORECASE)
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


def _adrs(log: str) -> dict[str, list[str]]:
    """Each ADR's lines, heading included."""
    adrs: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in log.splitlines():
        if heading := HEADING.match(line):
            current = adrs.setdefault(heading.group(1), [])
        if current is not None:
            current.append(line)
    return adrs


def amended_superseding(line: str) -> list[set[str]]:
    """The Amended-by entries that say a clause is superseded, each as the ADRs it names."""
    starts = list(ENTRY.finditer(line))
    return [
        {n for n in start.groups() if n}
        for start, end in zip(starts, [*starts[1:], None], strict=True)
        if "superseded" in line[start.end() : end.start() if end else None].lower()
    ]


def unmarked_supersessions(log: str) -> dict[str, list[str]]:
    """The ADRs whose **Amended by** line says a clause is superseded by ADR-NNN while no inline
    marker in the rest of the ADR cites ADR-NNN, with those ADR numbers."""
    found: dict[str, list[str]] = {}
    for adr, lines in _adrs(log).items():
        body = [line for line in lines if not line.startswith(AMENDED_BY)]
        cited = {
            n
            for line in body
            for m in CITED.finditer(line)
            for n in re.findall(r"ADR-(\d{3})", m.group(1))
        }
        for line in lines:
            if line.startswith(AMENDED_BY):
                for entry in amended_superseding(line):
                    if not entry & cited:
                        found.setdefault(adr, []).extend(sorted(entry))
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


def test_every_amended_by_supersession_has_an_inline_marker() -> None:
    """#420: ADR-001, 017, 023 and 033 said a clause was superseded only on their Amended-by
    line; ADR-009, 022, 048 and 061 already carried the marker."""
    log = ADR_LOG.read_text("utf-8")
    assert {"009", "022", "048", "061"} <= set(_adrs(log))  # the ADRs were read
    assert unmarked_supersessions(log) == {}


def test_the_amended_by_check_flags_what_it_is_for() -> None:
    """Complements on a synthetic log: an Amended-by entry that says "Superseded" (any case) with
    no marker citing its ADR is flagged, even when another entry's ADR is cited or a marker cites
    a different ADR; a marker citing the entry's ADR in any variant (or one of a joint entry's
    ADRs, or alongside another ADR) clears it; an entry that supersedes nothing needs no marker;
    and a marker-shaped quote on the Amended-by line itself does not count."""
    log = "\n".join(
        [
            "### ADR-001 — One",
            "**Amended by:** ADR-002 — X is Superseded by Y; ADR-003 — Z is narrowed",
            "**Decision:** Do X. *[superseded by ADR-009: something else]*",
            "### ADR-004 — Four",
            "**Amended by:** ADR-005 — the W clause is superseded; ADR-006/007 — V is superseded",
            "**Decision:** Do W *[W part superseded in part by ADR-005: W later]*; do V "
            "*[superseded by ADR-001 and ADR-007: V less]*.",
            "### ADR-008 — Eight",
            "**Amended by:** ADR-010 — U is superseded *[superseded by ADR-010: quoted]*",
            "**Decision:** Do U.",
            "### ADR-011 — Eleven",
            "**Amended by:** ADR-012 — T is superseded; ADR-013 — S is superseded",
            "**Decision:** Do T *[superseded by ADR-012: T twice]*; "
            "do S *[superseded by ADR-014: S]*.",
        ]
    )
    assert amended_superseding(log.splitlines()[4]) == [{"005"}, {"006", "007"}]
    assert unmarked_supersessions(log) == {"001": ["002"], "008": ["010"], "011": ["013"]}


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
