"""C-14 (M5) (docs/04): no document claims leakage is impossible (NFR-PRIV-4, NFR-QUAL-1; #179).

- ``test_c_14__m5``: no user-facing document (#132's set: the README, the generated documents
  under ``docs/`` but the design documents 01 to 05, the OpenAPI descriptions and summaries;
  plus every document under ``docs/evals/``, the eval reports among them) describes a clean run
  as "leak-free" or claims leakage is impossible, in any of the forbidden phrasings
  (``FORBIDDEN``, case-insensitive). NFR-QUAL-1's metrics (``nfr_qual_1.asymmetry``,
  ``nfr_qual_1.think_leak``) are zero-tolerance metrics, and their report rows carry #134's
  zero-rule phrasing: "0 observed in n cases, one-sided 95% upper bound ≈ x%".

- ``test_c_14__guarantee_rows`` (#181): the guarantee-table rows these metrics feed.
  ``paraphrase_detection`` is model-dependent with metric NFR-QUAL-1, and the two zero-leak
  suites declare it; structural knowledge separation and audience filtering are deterministic,
  provided under ``Structured`` and not under Lite, and the published table says so.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from epistrel.core.profiles.guarantees import GUARANTEE, Kind
from epistrel.core.seeds import profiles as seeds
from epistrel.evals import SUITES, Rule, SeedTally, Verdict, judge
from epistrel.evals.report import (
    Host,
    MetricResult,
    Report,
    SuiteResult,
    render_markdown,
)
from epistrel.tools import guarantees as guarantee_tool
from epistrel.tools.generated import REPO_ROOT
from tests.docs.test_c_14 import user_facing

FORBIDDEN = (
    "leak-free",
    "leak free",
    "cannot leak",
    "can't leak",
    "impossible to leak",
    "zero leakage",
    "leak-proof",
    "leakproof",
    "no leakage",
    "zero leaks",
    "never leaks",
)
"""How a document would claim leakage is impossible (C-14 M5); documents are reworded to avoid
them, never allow-listed."""
PATTERN = re.compile("|".join(re.escape(p).replace(r"\ ", r"[\s-]+") for p in FORBIDDEN), re.I)
METRICS = {"nfr_qual_1.asymmetry": "q04", "nfr_qual_1.think_leak": "priv_think_leak"}
ZERO_PHRASE = re.compile(r"0 observed in \d+ cases, one-sided 95% upper bound ≈ \d+\.\d+%")


def leak_claims(text: str) -> list[str]:
    """Where ``text`` claims leakage is impossible (a forbidden phrase, any case)."""
    return [m.group(0) for m in PATTERN.finditer(text)]


def documents() -> dict[str, str]:
    """#132's user-facing set and every document under ``docs/evals/``."""
    docs = user_facing()
    for path in sorted((REPO_ROOT / "docs" / "evals").rglob("*.md")):
        docs[str(path.relative_to(REPO_ROOT))] = path.read_text(encoding="utf-8")
    return docs


def _report(metric: MetricResult) -> Report:
    suite = SuiteResult(
        name="q04", title="Q-04", ci_gate=True, cases=300, seeds=[1, 2], metrics=[metric],
        verdict=metric.verdict,
    )  # fmt: skip
    return Report(
        run_id="c14",
        started_at=datetime(2026, 10, 23, tzinfo=UTC).isoformat(),
        model="stub",
        fingerprint="sha256:" + "0" * 64,
        fingerprint_profile="Structured (M4) v1",
        adapter="stub",
        engine_sha="0" * 7,
        host=Host(kind="ci"),
        seed_base=1,
        seeds=[1, 2],
        suites=[suite],
    )


@pytest.mark.part("M5", "NFR-PRIV-4; NFR-QUAL-1")
def test_c_14__m5() -> None:
    """No user-facing document or eval document claims leakage is impossible or calls a clean
    run leak-free; NFR-QUAL-1's two metrics are zero-tolerance metrics of the zero-leak suites,
    and a clean report row of either reads "0 observed in n cases" with its upper bound (every
    committed report row of them too). The check finds a planted claim in any casing and
    spacing (its complement), and a row with a violation gives no bound."""
    docs = documents()
    assert {"README.md", "docs/guarantees.md", "docs/evals/INDEX.md", "openapi"} <= set(docs)
    assert {name: leak_claims(text) for name, text in docs.items() if leak_claims(text)} == {}
    assert leak_claims("A clean run is Leak-Free.") == ["Leak-Free"]
    assert leak_claims("The engine cannot  leak a secret; there is zero\nleakage.") != []
    assert leak_claims("leak free, leak-proof, no leakage, zero leaks") != []
    assert leak_claims("0 observed in 300 cases, one-sided 95% upper bound ≈ 0.99%") == []

    for metric_id, suite_name in METRICS.items():
        declared = next(m for m in SUITES[suite_name].metrics if m.id == metric_id)
        assert (declared.rule, declared.floor, declared.requirement) == (
            Rule.zero,
            0.0,
            "NFR-QUAL-1",
        )
        clean = MetricResult.of(
            declared, judge(declared, [SeedTally(1, good=300), SeedTally(2, good=300)])
        )
        assert clean.verdict is Verdict.passed
        markdown = render_markdown(_report(clean))
        (row,) = [line for line in markdown.splitlines() if line.startswith(f"| `{metric_id}`")]
        assert ZERO_PHRASE.search(row), row
        assert leak_claims(markdown) == []
        leaked = MetricResult.of(
            declared, judge(declared, [SeedTally(1, good=300), SeedTally(2, good=299, bad=1)])
        )
        (row,) = [
            line
            for line in render_markdown(_report(leaked)).splitlines()
            if line.startswith(f"| `{metric_id}`")
        ]
        assert "upper bound" not in row

    for name, text in docs.items():
        if not name.startswith("docs/evals/"):
            continue
        for line in text.splitlines():
            if any(line.startswith(f"| `{m}`") for m in METRICS) and "| **fail**" not in line:
                assert ZERO_PHRASE.search(line), (name, line)


@pytest.mark.part("M5", "NFR-PRIV-4; NFR-QUAL-1")
def test_c_14__guarantee_rows() -> None:
    """NFR-QUAL-1's metrics feed the ``paraphrase_detection`` row: model-dependent, measured by
    NFR-QUAL-1, held under partitions, and declared by both zero-leak suites (q04 and the
    private-thought suite), so their reports name the row. NFR-PRIV-4's structural isolation is
    ``structural_knowledge_separation``, deterministic. Under the seeded ``Structured`` (version
    2) both it and audience filtering are provided, and under Lite neither is, which the
    published table states in prose; no milestone is named for any of them any more."""
    row = GUARANTEE["paraphrase_detection"]
    assert (row.kind, row.metric, row.delivered_in) == (Kind.model_dependent, "NFR-QUAL-1", None)
    for suite_name in METRICS.values():
        assert "paraphrase_detection" in SUITES[suite_name].guarantees, suite_name
    for row_id in ("structural_knowledge_separation", "audience_filtering"):
        assert GUARANTEE[row_id].kind is Kind.deterministic, row_id
        assert GUARANTEE[row_id].delivered_in is None, row_id
    lite, structured = guarantee_tool.SEEDED
    assert structured.version == seeds.STRUCTURED_VERSION
    for row_id in ("structural_knowledge_separation", "audience_filtering", "paraphrase_detection"):
        assert GUARANTEE[row_id].provided(structured.effective()), row_id
        assert not GUARANTEE[row_id].provided(lite.effective()), row_id
    table = (REPO_ROOT / "docs" / "guarantees.md").read_text(encoding="utf-8")
    assert table == guarantee_tool.render()
    lite_part = table.split(f"### {lite.name}\n", 1)[1].split("\n### ", 1)[0]
    assert "structural knowledge separation" in lite_part.split("**Not provided under", 1)[1]
    assert "(M5)" not in table
    (step,) = [
        line
        for line in table.split("## Notes", 1)[0].splitlines()
        if line.startswith("| paraphrase/indirect-disclosure detection |")
    ]
    assert "| model-dependent | NFR-QUAL-1 |" in step
