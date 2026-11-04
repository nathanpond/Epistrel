"""Preset certification (NFR-QUAL-11, FR-PROF-15,16; #235): ``epistrel evals certify``
certifies Standard, Full and Max in one command, the records it writes make a deployment on the
same model report ``certified``, ``INDEX.md``'s Presets table follows the records and the preset
constants, and no workflow runs a real model.

No test here carries a part marker: C-18's M7 part (``tests/integration/test_c_18_m7.py``) is
the §15 row these requirements map to, and a part lives in one file.

The stub run here is the CI path's (``ci.yml``'s ``evals`` job runs the command on the stub per
preset); this test runs its own, narrowed to the ``smoke`` suite, on a scratch database.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from epistrel.core.profiles.certification import (
    SEEDS_DIR,
    check_report,
    committed_records,
    parse_record,
)
from epistrel.core.seeds.profiles import (
    FULL_PRESET_ID,
    LITE_PRESET_ID,
    MAX_PRESET_ID,
    STANDARD_PRESET_ID,
)
from epistrel.evals import cli
from epistrel.evals import presets as preset_constants
from epistrel.evals.certify_cli import UsageError, parse_presets
from epistrel.evals.index import REFERENCE_MODEL_ID
from epistrel.evals.report import INDEX_NAME, index_text, write_index
from epistrel.tools.generated import REPO_ROOT
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.profile_support import PROFILES, UNCERTIFIED
from tests.respond_support import body as respond_body
from tests.respond_support import new_story, played, respond_client

WRITE = {**bearer(), "X-Epistrel-Principal": "p-certifier"}
READ = bearer()
IDS = {
    "lite": LITE_PRESET_ID,
    "standard": STANDARD_PRESET_ID,
    "full": FULL_PRESET_ID,
    "max": MAX_PRESET_ID,
}
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPTS = REPO_ROOT / "scripts"


def test_presets_parse_in_canonical_order() -> None:
    """``--presets`` runs Lite, Standard, Full, Max in that order whatever was typed; an
    unknown or repeated name is a usage error naming the valid ones."""
    assert parse_presets("max,standard,full") == ["standard", "full", "max"]
    assert parse_presets("lite") == ["lite"]
    for bad in ("full,huge", "full,full", ""):
        with pytest.raises(UsageError, match="lite, standard, full, max"):
            parse_presets(bad)


def test_certify_usage_errors(tmp_path: Path) -> None:
    """A real-model certification without ``--embedding-model`` exits 2 before anything runs,
    as does a typo'd preset."""
    base = [
        "certify",
        "--base-url",
        "http://127.0.0.1:9/v1",
        "--model",
        "m",
        "--out",
        str(tmp_path),
    ]
    assert cli.main([*base, "--presets", "full"]) == 2
    assert cli.main([*base, "--presets", "fulll", "--embedding-model", "e"]) == 2
    assert not any(tmp_path.iterdir())


def _certify(url: str, out: Path) -> int:
    return cli.main(
        [
            "certify",
            "--presets",
            "max,standard,full",
            "--adapter",
            "stub",
            "--suite",
            "smoke",
            "--seeds",
            "2",
            "--sample",
            "10",
            "--database-url",
            url,
            "--out",
            str(out),
        ]
    )


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json()
    return found


async def _status(client: httpx.AsyncClient, preset: str) -> dict[str, Any]:
    """The quality status a turn of a new story on ``preset`` reports."""
    story, ids = await new_story(client)
    await ok(
        await client.put(
            f"/stories/{story}/engine-profile",
            json={"profile_id": IDS[preset], "ignore_warnings": True},
            headers=WRITE,
        )
    )
    turn = await played(client, story, respond_body(next(iter(ids.values())), "Hello there."))
    found: dict[str, Any] = turn["quality_status"]
    return found


@pytest.mark.integration
async def test_certify_presets__stub(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stub run certifies the three presets: a report and a record each (in canonical
    order), and a scratch ``INDEX.md`` listing them certified. Imported into a deployment on the
    same (stub) model, each preset's turn reports ``certified`` with its record's id.

    Complements: Lite reports ``uncertified``; Full's escalation gate edited, Full reports
    ``uncertified`` while Standard stays certified, and with Full's constant edited the same way
    ``index --check`` is red until the index is regenerated; a different model id makes
    Standard ``uncertified``."""
    out = tmp_path / "certify"
    with migrated_database(pg_url, "evals_certify_run") as url:
        assert await asyncio.to_thread(_certify, url, out) == 0
    records = sorted((out / "records").glob("*.json"))
    reports = sorted((out / "reports").glob("*.md"))
    assert [p.name.split("-")[0] for p in records] == ["full", "max", "standard"]
    assert sorted(p.name.split("-")[0] for p in reports if p.name != INDEX_NAME) == [
        "full",
        "max",
        "standard",
    ]
    documents = {p.name.split("-")[0]: json.loads(p.read_text()) for p in records}
    for preset, document in documents.items():
        record = parse_record(document)
        assert (record.preset, record.model_tier) == (preset, "other")
        check_report(record, (out / "records" / str(record.report_path)).resolve().read_text())
        assert record.judge_calibrated
    index = (out / "reports" / INDEX_NAME).read_text()
    for preset in ("Standard", "Full", "Max"):
        (row,) = [line for line in index.splitlines() if line.startswith(f"| {preset} |")]
        assert documents[preset.lower()]["certification_id"] in row
    (lite,) = [line for line in index.splitlines() if line.startswith("| Lite |")]
    assert "uncertified" in lite
    assert cli.index_check(out / "reports", out / "records") == 0

    with migrated_database(pg_url, "evals_certify_deploy") as url:
        settings = auth_settings(url, model_adapter="stub")
        from epistrel.core.db import make_engine

        db = make_engine(settings)
        try:
            async with respond_client(settings, db) as (client, _):
                for document in documents.values():
                    await ok(
                        await client.post(
                            "/admin/certifications/import", json=document, headers=WRITE
                        )
                    )
                for preset, document in documents.items():
                    status = await _status(client, preset)
                    assert (status["status"], status["certification_id"]) == (
                        "certified",
                        document["certification_id"],
                    ), preset
                assert await _status(client, "lite") == UNCERTIFIED

                # Full's escalation gate edited: Full uncertified, Standard still certified.
                full = await ok(await client.get(f"{PROFILES}/{FULL_PRESET_ID}", headers=READ))
                edited = json.loads(json.dumps(full["settings"]))
                edited.setdefault("verification", {})["entailment_gate"] = 0.9
                await ok(
                    await client.put(
                        f"{PROFILES}/{FULL_PRESET_ID}",
                        json={"name": full["name"], "settings": edited, "ignore_warnings": True},
                        headers=WRITE,
                    )
                )
                assert await _status(client, "full") == UNCERTIFIED
                assert (await _status(client, "standard"))["status"] == "certified"

                # Another model id: no record names it.
                await ok(
                    await client.put(
                        "/admin/model-config/default",
                        json={"model": "stub-other"},
                        headers={**bearer(), "X-Epistrel-Principal": "p-operator"},
                    )
                )
                assert await _status(client, "standard") == UNCERTIFIED
        finally:
            await db.dispose()

    # The same edit to Full's constant: its row turns uncertified and --check is red.
    original = preset_constants.PRESETS["full"]
    changed = dict(original.settings)
    changed["verification"] = {**dict(original.settings["verification"]), "entailment_gate": 0.9}
    monkeypatch.setitem(
        preset_constants.PRESETS,
        "full",
        preset_constants.Preset(
            original.name, original.label, original.profile_id, changed, original.version
        ),
    )
    assert cli.index_check(out / "reports", out / "records") == 1
    text, _ = index_text(out / "reports", out / "records")
    (full_row,) = [line for line in text.splitlines() if line.startswith("| Full |")]
    assert (
        f"uncertified (preset changed since certification {documents['full']['certification_id']})"
        in full_row
    )
    write_index(out / "reports", out / "records")
    assert cli.index_check(out / "reports", out / "records") == 0


@pytest.mark.xfail(not committed_records(), reason="awaiting owner run (#235's Demo)", strict=False)
def test_committed_certifications() -> None:
    """Every committed record validates and matches its committed report's SHA-256 and, for a
    preset with partitions, ran the zero-leak floors on at least 300 cases a seed. Standard,
    Full and Max are each certified on at least one model, the same one (#235 as replanned
    2026-11-04: certifications are kept per model, and the M7 evidence may be a hosted model's;
    ``model_tier`` says whether it was the local reference model)."""
    found = committed_records()
    assert found, "no committed certification records yet"
    by_model: dict[str, set[str]] = {}
    for path in found:
        record = parse_record(json.loads(path.read_text("utf-8")))
        report = record.report_md
        if record.report_path is not None and (SEEDS_DIR / record.report_path).is_file():
            report = (SEEDS_DIR / record.report_path).read_text("utf-8")
        check_report(record, report)
        if record.model_tier == "reference":
            assert record.models["generation"]["id"] == REFERENCE_MODEL_ID, path.name
        zero = [
            f
            for f in (*record.floors_passed, *record.floors_failed)
            if f.metric_id.startswith("NFR-QUAL-1.")
        ]
        assert zero, f"{path.name}: NFR-QUAL-1 did not run"
        assert all(f.n >= 300 for f in zero), path.name
        model = str(record.models["generation"]["id"])
        by_model.setdefault(model, set()).add(str(record.preset))
    assert any({"standard", "full", "max"} <= presets for presets in by_model.values()), by_model


_EVALS_CALL = re.compile(r"epistrel[. ]evals\b(?P<rest>[^\n]*)")


def _sources() -> list[Path]:
    found = sorted(WORKFLOWS.glob("*.y*ml"))
    if SCRIPTS.is_dir():
        found += sorted(p for p in SCRIPTS.rglob("*") if p.is_file())
    return found


def test_no_real_model_in_ci() -> None:
    """No workflow or script runs ``epistrel.evals`` with ``--base-url`` (a real model; #134's
    policy: CI runs the stub only), and no workflow on a ``schedule:`` trigger runs the
    evals."""
    offending: list[str] = []
    for path in _sources():
        text = path.read_text("utf-8", errors="replace")
        offending += [
            f"{path.name}: {m.group(0).strip()}"
            for m in _EVALS_CALL.finditer(text)
            if "--base-url" in m.group("rest")
        ]
        if path.parent == WORKFLOWS and re.search(r"^\s*schedule:", text, re.M):
            assert not _EVALS_CALL.search(text), f"{path.name} runs the evals on a schedule"
    assert offending == []
    assert any("certify" in p.read_text("utf-8") for p in WORKFLOWS.glob("*.y*ml"))
