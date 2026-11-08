"""C-18 M7 (FR-PROF-15,16,17, NFR-QUAL-11): certification records and the quality status they
produce (#233). #234's stale clause, the canary
off by default, and the reported version follow.

The certification comes from a real ``certification_run`` job on the stub adapter (the tests'
"reference" model), not a hand-built row, so the run path is covered. It runs on a scratch
database of its own: a certified Full would change what every other test's Full story reports.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from pydantic import BaseModel

from epistrel.core.db import make_engine
from epistrel.core.ids import new_ulid
from epistrel.core.profiles import certification, schema
from epistrel.core.profiles.certification import HarnessOutcome, RunEstimate
from epistrel.core.profiles.guarantees import MINORS_IMAGE_CHECK_LINE as ATTESTATION_RULE
from epistrel.core.profiles.schema import Quality, Tag
from epistrel.core.seeds.profiles import FULL_PRESET_ID
from epistrel.core.settings_schema import (
    CERTIFICATION_CANARY_TASK,
    CERTIFICATION_RECERTIFY_TASK,
    ConfigClass,
)
from epistrel.engine import Engine
from epistrel.evals.certify import install
from epistrel.evals.suites.smoke import INSTRUCTION_FOLLOWED, SmokeSuite
from epistrel.orchestrator.jobs import queue, timers
from epistrel.orchestrator.jobs.recurring import NoPayload
from epistrel.orchestrator.models import reported_version
from epistrel.orchestrator.models.registry import GENERATION
from epistrel.tools.generated import REPO_ROOT
from epistrel.tools.guarantees import QUALITY_BOUNDARY_END, QUALITY_BOUNDARY_START, TABLE_PATH
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.event_support import rows, scalar
from tests.offboarding_support import run_jobs
from tests.profile_support import PROFILES, UNCERTIFIED
from tests.respond_support import body as respond_body
from tests.respond_support import new_story, played, respond_client
from tests.wall_clock_support import moving

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-certifier"}
READ = bearer()
RUN = {"suites": ["smoke"], "seeds": 2, "sample": 10}
FULL = f"{PROFILES}/{FULL_PRESET_ID}"


@asynccontextmanager
async def scratch(pg_url: str, name: str) -> AsyncIterator[tuple[httpx.AsyncClient, Engine, Any]]:
    """The REST app on a fresh database with the eval harness installed over its Engine."""
    with migrated_database(pg_url, name) as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            async with respond_client(settings, db) as (client, lib):
                install(lib)
                try:
                    yield client, lib, db
                finally:
                    certification.install_harness(None)
        finally:
            await db.dispose()


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json()
    return found


async def _on_full(client: httpx.AsyncClient) -> tuple[str, str]:
    story, ids = await new_story(client)
    await ok(
        await client.put(
            f"/stories/{story}/engine-profile",
            json={"profile_id": FULL_PRESET_ID, "ignore_warnings": True},
            headers=WRITE,
        )
    )
    return story, next(iter(ids.values()))


async def _status(client: httpx.AsyncClient, story: str, actor: str) -> dict[str, Any]:
    """The quality status a turn reports, checked against the effective-config read."""
    turn = await played(client, story, respond_body(actor, "Where does the road lead?"))
    read = await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))
    assert read["quality_status"] == turn["quality_status"]
    assert read["fingerprint"] == turn["fingerprint"]
    found: dict[str, Any] = turn["quality_status"]
    return found


def _record(**fields: Any) -> dict[str, Any]:
    report = "# A hand run\n\nsmoke passed.\n"
    return {
        "schema_version": 1,
        "certification_id": new_ulid(),
        "fingerprint": "sha256:" + "b" * 64,
        "floors_passed": [
            {
                "metric_id": "smoke.turn_completed",
                "rule": "rate",
                "n": 10,
                "seeds": 2,
                "verdict": "pass",
                "floor": 0.95,
                "mean": 1.0,
                "sd": 0.0,
            }
        ],
        "seed_count": 2,
        "report_sha256": hashlib.sha256(report.encode()).hexdigest(),
        "run_at": "2026-11-01T09:00:00+00:00",
        "report_md": report,
        **fields,
    }


@pytest.mark.part("M7", "FR-PROF-15,16,17; NFR-QUAL-11")
async def test_c_18__m7(pg_url: str) -> None:
    """Certify Full on the stub: the run's estimate writes nothing; confirmed, a
    ``certification_run`` job records a certification and leaves the profile's versions as they
    were. Then the respond, effective-config and impact surfaces say ``certified`` with the id and
    floors, and the profile read shows its ``floors_met``. A ``turn_concurrency`` change and a
    story override of the quality-irrelevant ``pins.max_pinned`` keep the fingerprint and the
    status. A story override of ``verification.entailment_gate`` warns ``certification_lost``
    (nothing applied); confirmed, that story is ``uncertified`` while its sibling stays
    ``certified``. Swapping Full's ``extraction_backstop`` model warns ``certification_lost``
    and changes nothing; with ``ignore_warnings`` the stories report ``uncertified`` (never
    ``stale``). Complements: a profile PUT carrying ``floors_met`` is 422; a record whose report
    does not match its hash is 422 ``invalid_certification_record`` with nothing stored; a record
    imported twice is one row."""
    async with scratch(pg_url, "epistrel_c18_m7") as (client, _, db):
        first, first_actor = await _on_full(client)
        second, second_actor = await _on_full(client)
        assert await _status(client, first, first_actor) == UNCERTIFIED

        versions = await rows(
            db, "SELECT profile_id, version, settings FROM engine_profile_versions"
        )
        estimate = await ok(
            await client.post(f"{FULL}/certification-runs", json=RUN, headers=WRITE)
        )
        assert estimate["suites"] == ["smoke"]
        assert estimate["estimate"]["model_calls"] == 10 * 2
        assert "run_id" not in estimate
        assert await scalar(db, "SELECT count(*) FROM certification_runs") == 0

        queued = await ok(
            await client.post(
                f"{FULL}/certification-runs", json={**RUN, "confirm": True}, headers=WRITE
            ),
            202,
        )
        assert queued["fingerprint"] == estimate["fingerprint"]
        busy = await client.post(
            f"{FULL}/certification-runs", json={**RUN, "confirm": True}, headers=WRITE
        )
        assert busy.status_code == 409, busy.text
        assert busy.json()["error"]["code"] == "certification_run_in_progress"
        assert await run_jobs(db) >= 1
        run = await ok(await client.get(f"/certification-runs/{queued['run_id']}", headers=READ))
        assert run["status"] == "completed", run
        certification_id = run["certification_id"]
        assert certification_id is not None
        floors = [f["metric_id"] for f in run["report"]["floors"]["passed"]]
        assert "smoke.turn_completed" in floors
        assert (
            await rows(db, "SELECT profile_id, version, settings FROM engine_profile_versions")
            == versions
        )
        record = await ok(await client.get(f"/certifications/{certification_id}", headers=READ))
        assert (record["fingerprint"], record["recorded_via"]) == (queued["fingerprint"], "job")
        assert record["status"] == "valid"

        certified = {
            "status": "certified",
            "certification_id": certification_id,
            "floors_passed": floors,
            "intervention_types_covered": [],
            "intervention_types_insufficient": [],
        }
        assert await _status(client, first, first_actor) == certified
        assert await _status(client, second, second_actor) == certified
        preview = await ok(
            await client.post(
                f"/stories/{first}/impact-preview",
                json={"overrides": {"pins": {"max_pinned": 7}}},
                headers=WRITE,
            )
        )
        assert preview["quality_status_after"] == certified
        assert preview["warnings"] == []
        profile = await ok(await client.get(FULL, headers=READ))
        assert profile["quality"] == {
            "fingerprint": queued["fingerprint"],
            "status": "certified",
            "floors_met": floors,
        }

        # Quality-irrelevant changes keep the fingerprint and the status.
        await ok(
            await client.patch(
                f"/stories/{first}/settings", json={"turn_concurrency": "queue"}, headers=WRITE
            )
        )
        await ok(
            await client.patch(
                f"/stories/{first}/profile-overrides",
                json={"settings": {"pins": {"max_pinned": 7}}},
                headers=WRITE,
            )
        )
        assert await _status(client, first, first_actor) == certified

        # A story override of a quality-relevant gate: that story only.
        gate = {"settings": {"verification": {"entailment_gate": 0.97}}}
        held = await ok(
            await client.patch(f"/stories/{second}/profile-overrides", json=gate, headers=WRITE)
        )
        assert held["applied"] is False
        assert "certification_lost" in held["impact"]["warnings"]
        assert held["impact"]["certifications_lost"] == [
            {
                "story_id": second,
                "from": "certified",
                "to": "uncertified",
                "certification_id": certification_id,
            }
        ]
        await ok(
            await client.patch(
                f"/stories/{second}/profile-overrides",
                json={**gate, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        assert await _status(client, second, second_actor) == UNCERTIFIED
        assert await _status(client, first, first_actor) == certified

        # The extraction model swapped on Full: a warning, nothing changed; confirmed, uncertified.
        current = await ok(await client.get(FULL, headers=READ))
        swapped = json.loads(json.dumps(current["settings"]))
        swapped.setdefault("models", {}).setdefault("purposes", {})["extraction_backstop"] = {
            "model": "another-model"
        }
        edit = {"name": current["name"], "settings": swapped}
        report = await ok(await client.put(FULL, json=edit, headers=WRITE))
        assert report["applied"] is False
        assert "certification_lost" in report["impact"]["warnings"]
        assert {e["story_id"] for e in report["impact"]["certifications_lost"]} == {first}
        assert (
            await rows(db, "SELECT profile_id, version, settings FROM engine_profile_versions")
            == versions
        )
        await ok(await client.put(FULL, json={**edit, "ignore_warnings": True}, headers=WRITE))
        assert await _status(client, first, first_actor) == UNCERTIFIED

        # Complements: no label without a certification; a tampered or repeated record.
        labelled = await client.put(FULL, json={**edit, "floors_met": ["x"]}, headers=WRITE)
        assert labelled.status_code == 422, labelled.text
        assert labelled.json()["error"]["code"] == "validation_error"
        before = await scalar(db, "SELECT count(*) FROM profile_certifications")
        tampered = await client.post(
            "/admin/certifications/import",
            json=_record(report_sha256="0" * 64),
            headers=WRITE,
        )
        assert tampered.status_code == 422, tampered.text
        assert tampered.json()["error"]["code"] == "invalid_certification_record"
        assert await scalar(db, "SELECT count(*) FROM profile_certifications") == before
        hand = _record()
        once = await ok(await client.post("/admin/certifications/import", json=hand, headers=WRITE))
        twice = await ok(
            await client.post("/admin/certifications/import", json=hand, headers=WRITE)
        )
        assert (once["stored"], twice["stored"]) == (True, False)
        assert await scalar(db, "SELECT count(*) FROM profile_certifications") == before + 1


class _OneUntagged(BaseModel):
    """A fixture schema: one setting tagged in full, one with no quality tag."""

    tagged: Annotated[int, Tag.behavioral, ConfigClass.prospective, Quality.relevant] = 1
    loose: Annotated[int, Tag.behavioral, ConfigClass.prospective] = 2


@pytest.mark.part("M7", "FR-PROF-16")
def test_c_18__m7_quality_tags() -> None:
    """Every setting of the M7 profile schema (#227's two dials included) is tagged
    quality-relevant or not, by #106's build-time check; the check reports a fixture schema's
    leaf that lacks the tag (complement)."""
    assert schema.missing_tags() == {}
    tags = schema.setting_tags()
    for path in ("verification.shape_tightening", "commitment.salience_bias"):
        assert tags[path].quality in (Quality.relevant, Quality.irrelevant), path
    assert schema.missing_tags(_OneUntagged) == {"loose": ("Quality",)}


@pytest.mark.part("M7", "FR-PROF-16")
def test_c_18__m7_no_floor_label() -> None:
    """``docs/guarantees.md`` names each model-dependent step's metric but never states a floor
    as met: outside its boundary prose no line says ``certified`` or ``meets``; the boundary
    prose itself is there, and a document with the claim outside it is caught (complement).
    #270 (ADR-182): one M8 row states the rule by which the minors policy is certified
    (``ATTESTATION_RULE``), not a floor met, so that phrase is set aside."""

    def claims(document: str) -> list[str]:
        outside = re.sub(
            rf"{re.escape(QUALITY_BOUNDARY_START)}.*?{re.escape(QUALITY_BOUNDARY_END)}",
            "",
            document,
            flags=re.DOTALL,
        ).replace(ATTESTATION_RULE, "")
        return [
            line for line in outside.splitlines() if re.search(r"\b(certified|meets)\b", line, re.I)
        ]

    document = TABLE_PATH.read_text(encoding="utf-8")
    assert QUALITY_BOUNDARY_START in document
    assert claims(document) == []
    assert claims(document + "\nThe Full preset meets every floor.\n") != []
    assert claims(document + "\nThe Lite preset is certified for every floor.\n") != []


# -- #234: the canary, stale, and the reported version -------------------------------------------

CANARY_ON = {
    "certification_canary_enabled": True,
    "certification_canary_seeds": 2,
    "certification_canary_sample_size": 10,
}
WRONG_WORD = 'Maren nods. "Of course."'


async def _canary(db: Any) -> dict[str, Any]:
    """Fire the canary now and run what it queues; the canary run it recorded."""
    async with db.begin() as conn:
        await queue.enqueue(conn, CERTIFICATION_CANARY_TASK, NoPayload())
    while await run_jobs(db):
        pass
    (run,) = await rows(
        db,
        "SELECT id, outcome, status, checks_certification_id, report FROM certification_runs "
        "WHERE kind = 'canary' ORDER BY created_at DESC, id DESC LIMIT 1",
    )
    return dict(
        zip(("id", "outcome", "status", "checks_certification_id", "report"), run, strict=True)
    )


async def _certify(client: httpx.AsyncClient, db: Any) -> str:
    queued = await ok(
        await client.post(
            f"{FULL}/certification-runs", json={**RUN, "confirm": True}, headers=WRITE
        ),
        202,
    )
    while await run_jobs(db):
        pass
    run = await ok(await client.get(f"/certification-runs/{queued['run_id']}", headers=READ))
    assert run["certification_id"] is not None, run
    return str(run["certification_id"])


@pytest.mark.part("M7", "FR-PROF-16,17")
async def test_c_18__m7_stale(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the canary on, a canary on which the stub holds every floor leaves Full ``certified``
    (``held``); on one seed it is ``inconclusive`` (no measured miss) and leaves it ``valid``;
    with the stub scripted below the smoke instruction floor, Full becomes ``stale``
    (``invalidated_reason: canary``, the floor that missed) on the respond, effective-config and
    impact surfaces. A quality-relevant override moves a story to a never-certified fingerprint:
    ``uncertified``, never ``stale``. A new certification run restores ``certified`` with a new
    id; the stale record is kept. A reported model version change makes the next turn
    ``uncertified`` (a new fingerprint) and leaves the certification ``valid``."""
    reported_version.forget()
    async with scratch(pg_url, "epistrel_c18_m7_stale") as (client, lib, db):
        # A named generation model, so its reported version has an identity to attach to.
        await ok(
            await client.put(
                "/admin/model-config/generation", json={"model": "stub-model"}, headers=WRITE
            )
        )
        story, actor = await _on_full(client)
        other, other_actor = await _on_full(client)
        first = await _certify(client, db)
        assert (await _status(client, story, actor))["certification_id"] == first
        await ok(await client.put("/admin/settings", json=CANARY_ON, headers=WRITE))

        held = await _canary(db)
        assert (held["outcome"], held["checks_certification_id"]) == ("held", first)
        assert (await _status(client, story, actor))["status"] == "certified"

        await ok(
            await client.put(
                "/admin/settings", json={"certification_canary_seeds": 1}, headers=WRITE
            )
        )
        assert (await _canary(db))["outcome"] == "inconclusive"
        assert (await _status(client, story, actor))["status"] == "certified"
        await ok(await client.put("/admin/settings", json=CANARY_ON, headers=WRITE))

        with monkeypatch.context() as patched:
            patched.setattr(
                SmokeSuite, "stub_answers", lambda self, case: {GENERATION.name: WRONG_WORD}
            )
            missed = await _canary(db)
        assert missed["outcome"] == "missed"
        assert missed["report"]["missed_floors"] == [INSTRUCTION_FOLLOWED]
        stale = {
            "status": "stale",
            "certification_id": first,
            "invalidated_reason": "canary",
            "failed_floors": [INSTRUCTION_FOLLOWED],
        }
        assert await _status(client, story, actor) == stale
        preview = await ok(
            await client.post(
                f"/stories/{story}/impact-preview",
                json={"overrides": {"pins": {"max_pinned": 7}}},
                headers=WRITE,
            )
        )
        assert preview["quality_status_after"] == stale
        record = await ok(await client.get(f"/certifications/{first}", headers=READ))
        assert (record["status"], record["invalidated_reason"]) == ("stale", "canary")
        assert record["invalidated_by_run_id"] == missed["id"]

        # A quality-relevant change: a fingerprint never certified, so uncertified.
        gate = {"settings": {"verification": {"entailment_gate": 0.97}}, "ignore_warnings": True}
        await ok(
            await client.patch(f"/stories/{other}/profile-overrides", json=gate, headers=WRITE)
        )
        assert await _status(client, other, other_actor) == UNCERTIFIED

        # Recovery: a new certification, the stale one kept.
        second = await _certify(client, db)
        assert second != first
        restored = await _status(client, story, actor)
        assert (restored["status"], restored["certification_id"]) == ("certified", second)
        assert (await ok(await client.get(f"/certifications/{first}", headers=READ)))[
            "status"
        ] == "stale"

        # A reported version change: a new fingerprint (uncertified), the record untouched.
        before = (await ok(await client.get(f"/stories/{story}/effective-config", headers=READ)))[
            "fingerprint"
        ]
        lib.models.stub.reported_version = "stub-2026-11"
        await played(client, story, respond_body(actor, "And beyond the hills?"))
        after = await _status(client, story, actor)
        assert after == UNCERTIFIED
        changed = await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))
        assert changed["fingerprint"] != before
        assert (await ok(await client.get(f"/certifications/{second}", headers=READ)))[
            "status"
        ] == "valid"
        versions = await rows(db, "SELECT model_id, reported_version FROM model_reported_versions")
        assert ("stub-model", "stub-2026-11") in {tuple(r) for r in versions}
    reported_version.forget()


class _Recorder:
    """A harness that records every call it is asked to make (none, while the canary is off)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def suites(self) -> list[str]:
        self.calls.append("suites")
        return ["smoke"]

    def applicable(self, settings: Any) -> list[str]:
        self.calls.append("applicable")
        return ["smoke"]

    def estimate(self, suites: Any, seeds: int, sample: int | None) -> RunEstimate:
        self.calls.append("estimate")
        return RunEstimate(model_calls=0, tokens=0, duration_s=0)

    def committed_runs(self) -> list[dict[str, Any]]:
        return []

    def suites_of(self, metric_ids: Any) -> list[str]:
        self.calls.append("suites_of")
        return ["smoke"]

    async def run(self, request: Any) -> HarnessOutcome:
        self.calls.append("run")
        return HarnessOutcome(summary={}, report_md="")


async def test_canary__off_by_default(pg_url: str) -> None:
    """FR-PROF-17, the owner's decision 3: on a fresh deployment with a reference certification
    in use, the canary and re-certification tasks fire every due time over 30 simulated days and
    queue nothing: no canary run, no harness call, no model call. No workflow under ``.github``
    turns the canary on."""
    with migrated_database(pg_url, "epistrel_canary_off") as url:
        db = make_engine(auth_settings(url, model_adapter="stub"))
        recorder = _Recorder()
        certification.install_harness(recorder)
        try:
            async with db.begin() as conn:
                await certification.store_record(
                    conn,
                    certification.CertificationRecord.model_validate(
                        {
                            **{k: v for k, v in _record().items() if k != "report_md"},
                            "profile_id": FULL_PRESET_ID,
                            "profile_version": 1,
                            "model_tier": "reference",
                        }
                    ),
                    via="import",
                )
            await timers.reconcile(db)
            with moving() as moved:
                fired: list[str] = []
                for _ in range(30):
                    moved.advance(86_400)
                    fired += (await timers.fire_due(db)).recurring
                    while await run_jobs(db):
                        pass
            assert fired.count(CERTIFICATION_CANARY_TASK) >= 4  # weekly, over 30 days
            assert CERTIFICATION_RECERTIFY_TASK in fired
            assert await scalar(db, "SELECT count(*) FROM certification_runs") == 0
            assert recorder.calls == []
            ran = await rows(
                db,
                "SELECT name, last_status FROM recurring_tasks WHERE name LIKE 'certification_%'",
            )
            assert {r[1] for r in ran} == {"ok"}
        finally:
            certification.install_harness(None)
            await db.dispose()
    workflows = REPO_ROOT / ".github"
    for path in workflows.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "certification_canary" not in text, path
            assert "EPISTREL_CERTIFICATION_CANARY" not in text, path


async def test_certifications__invalidate(pg_url: str) -> None:
    """``POST /admin/certifications/{id}/invalidate``: declared drift marks a valid
    certification ``stale`` (``drift``), and the record is kept. Complements: an unknown id is
    404 ``certification_not_found``; a stale one is 409 ``certification_not_valid``; a missing
    note is 422."""
    async with scratch(pg_url, "epistrel_c18_invalidate") as (client, _, _db):
        hand = _record()
        await ok(await client.post("/admin/certifications/import", json=hand, headers=WRITE))
        url = f"/admin/certifications/{hand['certification_id']}/invalidate"
        bare = await client.post(url, json={"reason": "drift"}, headers=WRITE)
        assert bare.status_code == 422, bare.text
        drift = {"reason": "drift", "note": "the provider confirmed a silent update"}
        done = await ok(await client.post(url, json=drift, headers=WRITE))
        assert (done["status"], done["invalidated_reason"]) == ("stale", "drift")
        record = await ok(
            await client.get(f"/certifications/{hand['certification_id']}", headers=READ)
        )
        assert (record["status"], record["invalidated_reason"]) == ("stale", "drift")
        again = await client.post(url, json=drift, headers=WRITE)
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "certification_not_valid"
        unknown = await client.post(
            f"/admin/certifications/{new_ulid()}/invalidate", json=drift, headers=WRITE
        )
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["error"]["code"] == "certification_not_found"
