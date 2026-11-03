"""C-16 M7 (FR-PROF-7,8,12, FR-VERIFY-15, NFR-QUAL-11): every preset gets its own guarantee
table and its own eval report (#227), and a data-producing profile edit reaches stories with
history by upgrade_mode (#229), retiring M3-M6's interim upgrade_unavailable. #231
appends its C-16 M7 parts beside these.

#229's parts run on a database of their own each (the backfill queue is deployment-wide). A
profile is a copy of the Lite preset; its edit switches it to the Full preset's settings (the
structured pipeline, data-producing). Stories play Lite turns by direct commits
(tests/fixtures/long_lite_story), and the backfill job runs step by step
(tests.integration.test_i_19.run_backfill):

- test_c_16__m7_upgrade: the report (queue positions, estimates, ranges) writes nothing; the
  confirmed edit queues one backfill per story with history, each story paused only while its
  own backfill runs, one at a time.
- test_c_16__m7_forward_only: no pause and no backfill; the gap is marked by retrieval and
  verification until a gap fill closes it.
- test_c_16__m7_pinned: POST /engine-profiles/{id}/upgrade-pinned brings pinned stories
  forward, by either mode.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.profiles.guarantees import evaluate_guarantees
from epistrel.core.seeds.profiles import FULL_PRESET_ID, LITE_PRESET_ID
from epistrel.engine import Engine
from epistrel.evals import cli
from epistrel.evals.gate import Metric, Rule, Verdict
from epistrel.evals.presets import PRESETS, needs
from epistrel.evals.report import Report, preset_of, render_index, summary
from epistrel.evals.runner import AdapterConfig, CaseContext, run_fingerprint, run_suites
from epistrel.evals.suites import SUITES, CaseModel, Outcome, Suite
from epistrel.orchestrator.jobs.handlers import backfill as backfill_job
from epistrel.tools import guarantees as guarantee_tool
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.evals.support import stub_engine
from tests.event_support import rows, scalar
from tests.fixtures import long_lite_story as lls
from tests.integration.test_i_19 import run_backfill
from tests.profile_support import PROFILES, REDERIVATION
from tests.respond_support import body as respond_body
from tests.respond_support import new_story, respond_client
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


WRITE = {**bearer(), "X-Epistrel-Principal": "p-c16-m7"}


@dataclass
class Space:
    settings: Settings
    db: AsyncEngine


@pytest.fixture
async def scratch(pg_url: str) -> AsyncIterator[Space]:
    with migrated_database(pg_url, f"epistrel_c16_m7_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            yield Space(settings, db)
        finally:
            await db.dispose()


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json() if response.content else {}
    return found


async def _profile(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    """A copy of the Lite preset, and the Full preset's settings (its data-producing edit)."""
    copied = await ok(
        await client.post(
            f"{PROFILES}/{LITE_PRESET_ID}/copy", json={"name": "Trial"}, headers=WRITE
        ),
        201,
    )
    full = await ok(await client.get(f"{PROFILES}/{FULL_PRESET_ID}", headers=READ))
    return copied["id"], {"name": "Trial", "settings": full["settings"]}


async def _story(
    client: httpx.AsyncClient, db: AsyncEngine, lib: Engine, profile: str, messages: int
) -> tuple[str, str]:
    """A story on ``profile`` with ``messages`` Lite messages of history; its id and player."""
    story, ids = await new_story(client, "Ada", "Bobby", "Cal")
    await ok(
        await client.put(
            f"/stories/{story}/engine-profile",
            json={"profile_id": profile, "ignore_warnings": True},
            headers=WRITE,
        )
    )
    if messages:
        await lls.long_lite_story(db, story, ids["Ada"], messages)
    assert lib.models.stub is not None
    lls.script(lib.models.stub)
    backfill_job.bind(lib.models)
    return story, ids["Ada"]


async def _effective(client: httpx.AsyncClient, story: str) -> dict[str, Any]:
    return await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))


async def _tables(db: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    """The rows a confirmed edit writes: profile versions, stories, backfills, jobs."""
    return {
        name: [tuple(r) for r in await rows(db, query)]
        for name, query in {
            "versions": "SELECT profile_id, version FROM engine_profile_versions ORDER BY 1, 2",
            "stories": "SELECT id, engine_profile_id, engine_profile_version_pin, status, "
            "paused_by, config_revision FROM stories ORDER BY id",
            "backfills": "SELECT id, status FROM story_backfills ORDER BY id",
            "jobs": "SELECT id, kind, status FROM jobs ORDER BY id",
        }.items()
    }


async def _status(db: AsyncEngine, story: str) -> str:
    return str(await scalar(db, "SELECT status FROM stories WHERE id = :s", s=story))


async def _due_now(db: AsyncEngine, story: str) -> None:
    """Make a waiting backfill job due now (it waits ``worker_poll_interval`` between tries)."""
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET due_at = transaction_timestamp() WHERE story_id = :s "
                "AND kind = 'backfill' AND status = 'queued'"
            ),
            {"s": story},
        )


async def _counts(db: AsyncEngine, story: str) -> tuple[int, int]:
    return (
        await scalar(db, "SELECT count(*) FROM events WHERE story_id = :s", s=story),
        await scalar(db, "SELECT count(*) FROM operations WHERE story_id = :s", s=story),
    )


async def _respond(client: httpx.AsyncClient, story: str, actor: str) -> httpx.Response:
    return await client.post(f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE)


@pytest.mark.part("M7", "FR-PROF-7,8")
async def test_c_16__m7_upgrade(scratch: Space) -> None:
    """A data-producing edit to a profile three stories with history and one without follow:
    without ``ignore_warnings`` it writes nothing (no version, story, backfill or job row) and
    reports the three with ``capabilities_gained``, the estimate and range of each one's
    backfill, ``upgrade_mode: backfill`` and queue positions 1-3. Confirmed, the story without
    history follows the new version at once and the three keep the old one, each with a
    ``queued`` backfill and none paused. Claimed one at a time (``backfill_concurrency`` 1), each
    story is paused only while its own backfill runs: a respond to it is 409 ``story_paused``
    and records nothing, while a queued story still plays under its previous fingerprint (a turn
    the claim then includes in its range). Each completes onto the new version, unpinned, before
    the next one starts."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        profile, edit = await _profile(client)
        stories = sorted([await _story(client, scratch.db, lib, profile, 8) for _ in range(3)])
        (first, first_actor), (second, second_actor), (third, _) = stories
        fresh, _ = await _story(client, scratch.db, lib, profile, 0)
        lived = [first, second, third]

        before = await _tables(scratch.db)
        held = await ok(await client.put(f"{PROFILES}/{profile}", json=edit, headers=WRITE))
        assert held["applied"] is False
        assert await _tables(scratch.db) == before
        entries = {e["story_id"]: e for e in held["impact"]["affected_stories"]}
        assert sorted(entries) == sorted([*lived, fresh])
        for position, story in enumerate(lived, start=1):
            entry = entries[story]
            assert entry["upgrade_unavailable"] is False
            assert (entry["upgrade_mode"], entry["queue_position"]) == ("backfill", position)
            assert entry["capabilities_gained"]
            assert entry[REDERIVATION]["needed"] is True
            assert entry[REDERIVATION]["estimate"]["model_calls"] > 0
            assert entry[REDERIVATION]["range"] == {"from_turn": 1, "to_turn": 4}
        assert (entries[fresh]["upgrade_mode"], entries[fresh][REDERIVATION]["needed"]) == (
            None,
            False,
        )

        confirmed = await ok(
            await client.put(
                f"{PROFILES}/{profile}", json={**edit, "ignore_warnings": True}, headers=WRITE
            )
        )
        assert (confirmed["applied"], confirmed["result"]["version"]) == (True, 2)
        queued = confirmed["backfills"]
        assert [(b["story_id"], b["status"], b["queue_position"]) for b in queued] == [
            (story, "queued", n) for n, story in enumerate(lived, start=1)
        ]
        assert (await _effective(client, fresh))["profile"]["version"] == 2
        old = {}
        for story in lived:
            shown = await _effective(client, story)
            assert (shown["profile"]["version"], shown["profile"]["pinned"]) == (1, True)
            assert await _status(scratch.db, story) == "active"
            old[story] = shown["fingerprint"]

        # A queued story plays on its previous version; its claim includes the turn.
        played = await ok(await _respond(client, second, second_actor))
        assert played["fingerprint"] == old[second]

        # The first claims and pauses; the second waits its turn.
        await run_backfill(scratch.db, first, steps=1)
        assert await _status(scratch.db, first) == "paused"
        counts = await _counts(scratch.db, first)
        refused = await _respond(client, first, first_actor)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "story_paused"
        assert await _counts(scratch.db, first) == counts
        await _due_now(scratch.db, second)
        await run_backfill(scratch.db, second, steps=1)
        assert await _status(scratch.db, second) == "active"

        for story in lived:
            await _due_now(scratch.db, story)
            assert await run_backfill(scratch.db, story) > 0
            done = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
            assert done["status"] == "completed", done
            assert await _status(scratch.db, story) == "active"
            shown = await _effective(client, story)
            assert (shown["profile"]["version"], shown["profile"]["pinned"]) == (2, False)
        claimed = await rows(
            scratch.db,
            "SELECT story_id, range_to_turn, started_at, finished_at FROM story_backfills "
            "ORDER BY seq",
        )
        assert [str(r.story_id) for r in claimed] == lived
        assert {str(r.story_id): int(r.range_to_turn) for r in claimed}[second] == 5
        for earlier, later in itertools.pairwise(claimed):
            assert earlier.finished_at <= later.started_at


async def _turn_report(db: AsyncEngine, story: str) -> dict[str, Any]:
    (row,) = await rows(
        db,
        "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND membership = 'active' ORDER BY narrative_turn DESC LIMIT 1",
        s=story,
    )
    found: dict[str, Any] = row.turn_report
    return found


async def _retrieve(client: httpx.AsyncClient, story: str) -> dict[str, Any]:
    return await ok(
        await client.get(
            f"/stories/{story}/retrieve",
            params={"q": "Bobby", "viewpoint": "omniscient"},
            headers=READ,
        )
    )


@pytest.mark.part("M7", "FR-PROF-8")
async def test_c_16__m7_forward_only(scratch: Space) -> None:
    """With ``upgrade_mode: forward_only`` no story pauses and no backfill is written; the
    story's next turn runs under the new configuration (its fingerprint changes). Retrieval over
    the old turns lists the gap (``coverage_gaps``), the turn report's retrieval names it, and the
    respond response carries it. Complement: a story on the same profile created after the edit
    has no gap. A gap fill (``POST /stories/{id}/backfill``) closes it, and the marks are
    gone."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        profile, edit = await _profile(client)
        story, actor = await _story(client, scratch.db, lib, profile, 8)
        before = (await _effective(client, story))["fingerprint"]
        confirmed = await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={**edit, "ignore_warnings": True, "upgradeMode": "forward_only"},
                headers=WRITE,
            )
        )
        (entry,) = confirmed["impact"]["affected_stories"]
        assert (entry["upgrade_mode"], entry["queue_position"]) == ("forward_only", None)
        assert entry["forward_only_gap"]["through_turn"] == 4
        assert entry[REDERIVATION]["needed"] is True
        assert confirmed["backfills"] == []
        assert await scalar(scratch.db, "SELECT count(*) FROM story_backfills") == 0
        assert await _status(scratch.db, story) == "active"
        shown = await _effective(client, story)
        assert (shown["profile"]["version"], shown["profile"]["pinned"]) == (2, False)

        played = await ok(await _respond(client, story, actor))
        assert played["fingerprint"] != before
        gaps = played["coverage_gaps"]
        assert gaps
        assert {(g["from_turn"], g["to_turn"]) for g in gaps} == {(1, 4)}
        assert "pipeline" in {g["capability"] for g in gaps}
        report = await _turn_report(scratch.db, story)
        assert report["retrieval"]["coverage_gaps"]
        for verdict in report.get("verify", {}).get("verdicts", []):
            assert verdict["gap_affected"]["through_turn"] == 4
        found = await _retrieve(client, story)
        assert {(g["from_turn"], g["to_turn"]) for g in found["coverage_gaps"]} == {(1, 4)}

        # Complement: a story created after the edit has no gap.
        later, later_actor = await _story(client, scratch.db, lib, profile, 0)
        fresh = await ok(await _respond(client, later, later_actor))
        assert "coverage_gaps" not in fresh
        assert (await _turn_report(scratch.db, later))["retrieval"]["coverage_gaps"] == []
        assert (await _retrieve(client, later))["coverage_gaps"] == []

        # The gap fill closes it.
        await ok(
            await client.post(
                f"/stories/{story}/backfill", json={"ignore_warnings": True}, headers=WRITE
            ),
            202,
        )
        assert await run_backfill(scratch.db, story) > 0
        assert (await _retrieve(client, story))["coverage_gaps"] == []
        again = await ok(await _respond(client, story, actor))
        assert "coverage_gaps" not in again
        report = await _turn_report(scratch.db, story)
        assert report["retrieval"]["coverage_gaps"] == []
        assert all("gap_affected" not in v for v in report.get("verify", {}).get("verdicts", []))


async def _pinned(
    client: httpx.AsyncClient, db: AsyncEngine, lib: Engine, profile: str
) -> tuple[str, str]:
    """A story with history left on ``profile``'s version 1: its queued backfill cancelled
    (FR-PROF-9: it returns to its previous configuration, pinned)."""
    story, actor = await _story(client, db, lib, profile, 8)
    return story, actor


@pytest.mark.part("M7", "FR-PROF-7,8")
async def test_c_16__m7_pinned(scratch: Space) -> None:
    """Two stories with history are left pinned to version 1 (their queued backfills
    cancelled). ``POST /engine-profiles/{id}/upgrade-pinned`` without ``ignore_warnings`` lists
    both and writes nothing; an unpinned story on the latest version is not listed (complement).
    Confirmed with ``forward_only`` the first's pin is cleared at once with its gap open; with
    ``backfill`` the second is queued and moves to the latest version when its backfill
    completes."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        profile, edit = await _profile(client)
        first, _ = await _pinned(client, scratch.db, lib, profile)
        second, _ = await _pinned(client, scratch.db, lib, profile)
        await ok(
            await client.put(
                f"{PROFILES}/{profile}", json={**edit, "ignore_warnings": True}, headers=WRITE
            )
        )
        for story in (first, second):
            await ok(await client.delete(f"/stories/{story}/backfill", headers=WRITE))
            assert (await _effective(client, story))["profile"]["pinned"] is True
        follower, _ = await _story(client, scratch.db, lib, profile, 0)
        assert (await _effective(client, follower))["profile"]["version"] == 2

        before = await _tables(scratch.db)
        held = await ok(
            await client.post(f"{PROFILES}/{profile}/upgrade-pinned", json={}, headers=WRITE)
        )
        assert held["applied"] is False
        assert await _tables(scratch.db) == before
        listed = {e["story_id"]: e for e in held["impact"]["affected_stories"]}
        assert sorted(listed) == sorted([first, second])
        assert follower not in listed
        assert all(e[REDERIVATION]["needed"] for e in listed.values())

        # Move the second aside (pinned again later) to bring the first forward alone.
        async with scratch.db.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET engine_profile_version_pin = NULL WHERE id = :s"),
                {"s": second},
            )
        forward = await ok(
            await client.post(
                f"{PROFILES}/{profile}/upgrade-pinned",
                json={"ignore_warnings": True, "upgrade_mode": "forward_only"},
                headers=WRITE,
            )
        )
        assert forward["applied"] is True
        assert forward["backfills"] == []
        shown = await _effective(client, first)
        assert (shown["profile"]["version"], shown["profile"]["pinned"]) == (2, False)
        assert (await _retrieve(client, first))["coverage_gaps"]

        async with scratch.db.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET engine_profile_version_pin = 1 WHERE id = :s"),
                {"s": second},
            )
        queued = await ok(
            await client.post(
                f"{PROFILES}/{profile}/upgrade-pinned",
                json={"ignore_warnings": True, "upgrade_mode": "backfill"},
                headers=WRITE,
            )
        )
        assert [b["story_id"] for b in queued["backfills"]] == [second]
        assert (await _effective(client, second))["profile"]["pinned"] is True
        assert await run_backfill(scratch.db, second) > 0
        shown = await _effective(client, second)
        assert (shown["profile"]["version"], shown["profile"]["pinned"]) == (2, False)

        # Nothing pinned is left: an empty report, applied.
        empty = await ok(
            await client.post(f"{PROFILES}/{profile}/upgrade-pinned", json={}, headers=WRITE)
        )
        assert (empty["applied"], empty["impact"]["affected_stories"]) == (True, [])


async def test_upgrade_mode__validation(scratch: Space) -> None:
    """``upgrade_mode`` takes ``backfill`` or ``forward_only``: another value, or the two
    spellings disagreeing, is 422 ``validation_error`` and nothing changes."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile, edit = await _profile(client)
        before = await _tables(scratch.db)
        for body in (
            {**edit, "upgrade_mode": "eventually"},
            {**edit, "upgrade_mode": "backfill", "upgradeMode": "forward_only"},
        ):
            refused = await client.put(f"{PROFILES}/{profile}", json=body, headers=WRITE)
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["code"] == "validation_error"
        same = {**edit, "upgrade_mode": "forward_only", "upgradeMode": "forward_only"}
        assert (
            await client.put(f"{PROFILES}/{profile}", json=same, headers=WRITE)
        ).status_code == 200
        refused = await client.post(
            f"{PROFILES}/{profile}/upgrade-pinned", json={"upgrade_mode": "x"}, headers=WRITE
        )
        assert refused.status_code == 422, refused.text
        assert before["backfills"] == (await _tables(scratch.db))["backfills"]


def test_no_code_sets_upgrade_unavailable() -> None:
    """#229: M3's interim branch is gone: no engine code sets ``upgrade_unavailable`` true (the
    field stays in the schema for API stability, always ``false``)."""
    import epistrel

    root = Path(epistrel.__file__).parent
    found = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        for line in path.read_text().splitlines()
        if "upgrade_unavailable" in line and ("True" in line or "= unavailable" in line)
    ]
    assert found == []
