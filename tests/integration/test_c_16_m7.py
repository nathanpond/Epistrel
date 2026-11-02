"""C-16 M7 (FR-PROF-12, FR-VERIFY-15, NFR-QUAL-11): every preset gets its own guarantee table
and its own eval report (#227). #229 and #231 append their C-16 M7 parts beside this one."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.profiles.guarantees import evaluate_guarantees
from epistrel.evals import cli
from epistrel.evals.gate import Metric, Rule, Verdict
from epistrel.evals.presets import PRESETS, needs
from epistrel.evals.report import Report, preset_of, render_index, summary
from epistrel.evals.runner import AdapterConfig, CaseContext, run_fingerprint, run_suites
from epistrel.evals.suites import SUITES, CaseModel, Outcome, Suite
from epistrel.tools import guarantees as guarantee_tool
from tests.auth_support import auth_settings
from tests.evals.support import stub_engine
from tests.profile_support import PROFILES
from tests.respond_support import respond_client
from tests.story_support import READ

pytestmark = pytest.mark.integration

ORDER = ["Lite", "Standard", "Full", "Max"]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


class NestedSuite(Suite):
    """A suite that needs nested beliefs (tests only): Standard has none."""

    name = "nested_only"
    title = "Nested beliefs (tests only)"
    metrics = (Metric("nested_ok", Rule.informational),)
    requires: ClassVar[tuple[str, ...]] = ("nested_beliefs",)

    def load(self, sets_dir: Path) -> list[CaseModel]:
        del sets_dir
        return [CaseModel(id="nested-001")]

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        raise AssertionError("a not_applicable suite is never executed")


@pytest.mark.part("M7", "FR-PROF-12; FR-VERIFY-15; NFR-QUAL-11")
async def test_c_16__m7_per_preset(
    stub_settings: Settings, engine: AsyncEngine, pg_url: str, tmp_path: Path
) -> None:
    """``docs/guarantees.md`` has exactly the four preset sections, in the order Lite, Standard,
    Full, Max (no ``Structured`` table), and each Standard/Full/Max table equals
    ``evaluate_guarantees`` of the preset over the seeded defaults, as
    ``GET /engine-profiles/{id}/guarantees`` serves it. ``evals run --preset standard`` writes a
    report that names ``standard`` and Standard's fingerprint (the INDEX row too), where E-01
    (entailment fallback, which Standard lacks) is ``not_applicable`` with what it needs.
    Complement: a nested-belief suite under Standard is ``not_applicable`` and never executed
    (no model call), while under Full it would run."""
    rendered = guarantee_tool.render()
    assert rendered == guarantee_tool.TABLE_PATH.read_text(encoding="utf-8")
    per_profile = rendered.split("## Per profile\n", 1)[1]
    sections = [line[4:] for line in per_profile.splitlines() if line.startswith("### ")]
    assert sections == ORDER
    assert [p.name for p in guarantee_tool.SEEDED] == ORDER

    async with respond_client(stub_settings, engine) as (client, _):
        for seeded in guarantee_tool.SEEDED[1:]:
            expected = [
                row.model_dump(mode="json") for row in evaluate_guarantees(seeded.effective())
            ]
            served = (
                await client.get(f"{PROFILES}/{seeded.profile_id}/guarantees", headers=READ)
            ).json()
            assert (served["profile_id"], served["version"]) == (
                seeded.profile_id,
                seeded.version,
            )
            assert served["rows"] == expected, seeded.name
            table = per_profile.split(f"### {seeded.name}\n", 1)[1].split("\n### ", 1)[0]
            for row in expected:
                cell = "provided" if row["provided"] else "not provided"
                if row["provided"] is None:
                    cell = "outside stories"
                assert f"| `{row['id']}` |" in table
                line = next(x for x in table.splitlines() if f"| `{row['id']}` |" in x)
                assert line.endswith(f"| {cell} |") or (
                    cell == "not provided" and "| not provided" in line
                ), (seeded.name, line)

    argv = [
        "run",
        "--suite",
        "e01",
        "--preset",
        "standard",
        "--adapter",
        "stub",
        "--database-url",
        pg_url,
        "--json-dir",
        str(tmp_path / "json"),
        "--markdown",
        str(tmp_path / "standard.md"),
        "--new-run",
    ]
    assert await asyncio.to_thread(cli.main, argv) == 0  # the CLI runs its own loop
    (written,) = (tmp_path / "json").glob("standard-*.json")
    report = Report.from_json(json.loads(written.read_text(encoding="utf-8")))
    assert preset_of(report) == "standard"
    assert report.fingerprint == run_fingerprint("stub", preset="standard")
    assert report.fingerprint != run_fingerprint("stub")
    (e01,) = report.suites
    assert (e01.verdict, e01.cases) == (Verdict.not_applicable, 0)
    assert e01.needs == ["`verification` at `entailment`"]
    markdown = (tmp_path / "standard.md").read_text(encoding="utf-8")
    assert "- **Preset:** `standard`" in markdown
    assert report.fingerprint in markdown
    index = render_index([("standard.md", summary(report))])
    assert "| Preset |" in index
    assert "| standard |" in index
    legacy = summary(report) | {"preset": None}
    assert "| structured |" in render_index([("old.md", legacy)])

    assert needs(PRESETS["standard"], requires=NestedSuite.requires) == ["nested_beliefs"]
    assert needs(PRESETS["full"], requires=NestedSuite.requires) == []
    assert needs(PRESETS["lite"], guarantees=SUITES["e05"].guarantees)[0] == "structured"
    async with stub_engine(pg_url, engine) as lib:
        skipped = await run_suites(
            [NestedSuite()], AdapterConfig(preset="standard"), [1], engine=lib
        )
        calls = [c for c in lib.models.stub.calls if len(c.messages) > 1]
    (nested,) = skipped.suites
    assert (nested.verdict, nested.needs) == (Verdict.not_applicable, ["nested_beliefs"])
    assert calls == []
    assert skipped.verdict is Verdict.not_applicable
