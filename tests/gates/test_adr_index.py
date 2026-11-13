"""The ADR log's two indexes are generated from the log itself (#889).

``epistrel.tools.adr_index`` derives the index by requirement area and the index by ADR from the
ADR headings, **Status** and **Amended by** lines, inline markers and requirement ids; CI runs
its ``--check`` in the lint job. ``tests/gates/test_adr_markers.py`` checks the markers
themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from epistrel.tools import adr_index
from tests.conftest import REPO_ROOT
from tests.gates.test_adr_markers import superseded_adrs

ADR_LOG = REPO_ROOT / "docs" / "05-adr-log.md"

SPEC = """# Spec

## 1. The respond contract (FR-RESP)

### 1.1 A subsection (FR-SUB)

### 1.2 A subsection with no family

## 2. Other (FR-OTHER)

## 20. Non-functional requirements

### Scale (NFR-SCALE)
"""

LOG = """# Log

The **indexes** below are generated.

## Indexes

### By requirement area

The ADRs that cite each area.

| Area | Spec | ADRs |
|---|---|---|
| FR-RESP | §1 | 999 |

### By ADR

The legend.

| ADR | Title | Standing | Read with |
|---|---|---|---|

---

### ADR-001 — One *[narrowed — ADR-003]*
**Status:** Accepted.
**Amended by:** ADR-003/004 — X is superseded; see also ADR-002.
**Decision:** Do X (FR-RESP-1). *[superseded by ADR-003: Y]*
**Consequences:** Holds at NFR-SCALE-2.

---

### ADR-002 — Two
**Status:** Superseded by ADR-004.
**Amended by:** ADR-004 — FR-OTHER-1 is moot.
**Decision:** Y.

---

### ADR-003 — Three
**Status:** Accepted. Refines FR-OTHER-3.
**Amended by:** ADR-004 — narrower.
**Decision:** Z, under FR-SUB-1,2.
### ADR-004 — Four
**Status:** Accepted.
**Decision:** W.

---

*Trailing note.*
"""

AREA_TABLE = """| Area | Spec | ADRs |
|---|---|---|
| FR-RESP | §1 | 001 |
| FR-SUB | §1.1 | 003 |
| NFR-SCALE | §20 | 001 |
"""

ADR_TABLE = """| ADR | Title | Standing | Read with |
|---|---|---|---|
| 001 | One | Partly superseded | 003, 004, 002 |
| 002 | Two | Superseded | 004 |
| 003 | Three | Amended | 004 |
| 004 | Four | Current | — |
"""


def test_the_committed_indexes_are_generated() -> None:
    """The real log is in sync: ``--check`` passes on docs/05 as committed."""
    assert adr_index.main(["--check"]) == 0


def test_standings_follow_the_marker_rule() -> None:
    """The generator's Partly superseded ADRs are exactly the ADRs ``test_adr_markers`` finds a
    supersession marker in (none of the real log's ADRs has a Superseded status)."""
    log = ADR_LOG.read_text("utf-8")
    adrs = adr_index.parse_adrs(log)
    assert len(adrs) > 190
    assert {a.number for a in adrs if a.standing == "Partly superseded"} == superseded_adrs(log)


def test_render_derives_both_indexes_from_a_fixture_log() -> None:
    """Areas, order and § come from docs/03's headings (an NFR family takes its chapter's §);
    a citation on a Status or Amended-by line does not count, a marker's does not change the
    title, an area no ADR cites has no row, an ADR ends at ``---`` or the next heading, and every
    line outside the two tables is kept."""
    rendered = adr_index.render(LOG, SPEC, curated={})
    assert AREA_TABLE in rendered
    assert ADR_TABLE in rendered
    assert "| 999 |" not in rendered
    outside = [line for line in rendered.splitlines() if not line.startswith("|")]
    assert outside == [line for line in LOG.splitlines() if not line.startswith("|")]
    assert adr_index.render(rendered, SPEC, curated={}) == rendered


def test_curated_links_add_rows_and_must_not_be_derivable() -> None:
    rendered = adr_index.render(LOG, SPEC, curated={"004": ("FR-OTHER",)})
    assert "| FR-OTHER | §2 | 004 |" in rendered
    with pytest.raises(adr_index.AdrIndexError, match="already cites"):
        adr_index.render(LOG, SPEC, curated={"001": ("FR-RESP",)})
    with pytest.raises(adr_index.AdrIndexError, match="does not have"):
        adr_index.render(LOG, SPEC, curated={"005": ("FR-RESP",)})
    with pytest.raises(adr_index.AdrIndexError, match="does not define"):
        adr_index.render(LOG, SPEC, curated={"004": ("FR-NONE",)})


def test_a_cited_family_docs_03_does_not_define_is_an_error() -> None:
    log = LOG.replace("**Decision:** W.", "**Decision:** W (FR-NONE-1).")
    with pytest.raises(adr_index.AdrIndexError, match="FR-NONE"):
        adr_index.render(log, SPEC, curated={})


def test_check_fails_on_drift_and_write_repairs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-edited row (here a wrong standing) fails ``--check`` with a diff naming the
    command; ``--write`` regenerates it, and ``--check`` then passes."""
    monkeypatch.setattr(adr_index, "CURATED", {})
    log, spec = tmp_path / "05.md", tmp_path / "03.md"
    spec.write_text(SPEC, "utf-8")
    args = ["--path", str(log), "--spec", str(spec)]
    log.write_text(adr_index.render(LOG, SPEC, curated={}), "utf-8")
    assert adr_index.main(["--check", *args]) == 0
    log.write_text(
        log.read_text("utf-8").replace("| 003 | Three | Amended |", "| 003 | Three | Current |"),
        "utf-8",
    )
    capsys.readouterr()
    assert adr_index.main(["--check", *args]) == 1
    err = capsys.readouterr().err
    assert "-| 003 | Three | Current | 004 |" in err
    assert "+| 003 | Three | Amended | 004 |" in err
    assert "epistrel.tools.adr_index --write" in err
    assert adr_index.main(["--write", *args]) == 0
    assert adr_index.main(["--check", *args]) == 0


def test_a_log_without_an_index_table_is_an_error() -> None:
    with pytest.raises(adr_index.AdrIndexError, match="By ADR"):
        adr_index.render(LOG.replace("### By ADR", "### Not the index"), SPEC, curated={})
