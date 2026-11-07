"""I-26's M8 parts: atomic image reservations against the deployment and catalog-entry daily
limits (FR-IMG-11 as amended by #262).

Each test runs on a scratch database of its own (``tests.image_limits_support.space``). The
limits, the time zone and the counters are deployment-wide, so they never reach the shared
database or another test's limits. Jobs run through the handler's entry point
(``run_image_job``) with the test target, scripted checks and the scripted image adapter, as in
I-25. Concurrent requests hold their own connections and are released together by an
``asyncio.Barrier``.

#267 (late returns), #268 (ambiguous sends) and #269 (cost) add their I-26 parts here. Every
I-26 M8 part lives in this file (the traceability gate takes one file per test ID and
milestone)."""

from __future__ import annotations

import asyncio
import random
from collections import Counter as Tally
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from epistrel import cli
from epistrel.api.routes import health
from epistrel.core import bundles, stories
from epistrel.core.errors import ImageLimitReached, ImagesDisabled, StoryBusy
from epistrel.core.ids import new_ulid
from epistrel.core.images import limits
from epistrel.core.settings_schema import (
    IMAGE_ACCOUNTING_RETENTION_TASK,
    IMAGE_COUNTER_RECONCILE_TASK,
)
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus, ReservationState
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images import reconcile as image_reconcile
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.recurring import registered_tasks
from epistrel.testing.images import PASS, ScriptedChecks, installed_checks, registered_target
from tests.auth_support import bearer
from tests.event_support import new_story
from tests.image_job_support import CTX, MINORS, prepared, staged
from tests.image_limits_support import (
    ADAPTER,
    CatalogTarget,
    Space,
    all_records,
    assert_clean,
    at,
    counters,
    deployment,
    records,
    space,
    today,
)
from tests.image_support import Outputs, StubImageGenerator, catalog_origin, registered
from tests.offboarding_support import run_jobs
from tests.story_image_support import exported

NEW_YORK = "America/New_York"


def _story(story_id: str | None = None) -> image_jobs.PreparedImageJob:
    return prepared(story_id or new_ulid())


def _catalog(entry_id: str, n: int) -> image_jobs.PreparedImageJob:
    return prepared(kind=ImageJobKind.CATALOG, origin=catalog_origin(entry_id), n=n)


async def _concurrently(
    sp: Space, jobs: list[image_jobs.PreparedImageJob]
) -> list[str | BaseException]:
    """Every job enqueued at once, each in its own transaction on its own connection (all
    released together by a barrier): the job id, or the refusal."""
    barrier = asyncio.Barrier(len(jobs))

    async def one(job: image_jobs.PreparedImageJob) -> str:
        async with sp.db.connect() as conn:
            await barrier.wait()
            async with conn.begin():
                return (await image_jobs.insert_image_job(conn, job, None)).job_id

    return list(await asyncio.gather(*(one(j) for j in jobs), return_exceptions=True))


class _RacingChecks(ScriptedChecks):
    """Scripted checks whose first two checks wait for each other, so two jobs reach their
    retries together."""

    def __init__(self) -> None:
        super().__init__([MINORS, MINORS, PASS], fail_closed=False)
        self.barrier = asyncio.Barrier(2)
        self.calls = 0

    async def check_output(self, runtime: Any, job: Any, output: Any) -> Any:
        self.calls += 1
        if self.calls <= 2:
            await asyncio.wait_for(self.barrier.wait(), 30)
        return await super().check_output(runtime, job, output)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11")
async def test_i_26__m8_reservations(pg_url: str, tmp_path: Path) -> None:
    """Concurrent requests never jointly reserve past a limit:

    - 20 concurrent story requests against a deployment-day limit of 1 → exactly one accepted,
      19 refused 409 ``image_limit_reached`` naming the deployment scope, with no job written;
    - 10 concurrent catalog requests for 4 candidates against a deployment-day allowance of 10
      → two accepted (8 reserved, never more than 10), the rest refused whole (every record
      reserves 4);
    - two jobs racing their retries for the last unit → one retries and promotes, the other ends
      ``cap_reached`` (``status_detail {scope, period}``) with its staging gone;
    - a provider returning fewer outputs than reserved releases the difference;
    - 3 outputs for ``n = 1`` → one checked, two discarded unchecked and unpromoted, ``used = 3``,
      ``overage = 2``, ``extra_outputs = 2``;
    - the reconciler reports no drift after every step.

    Complement asserted: under the limit every request is admitted, and a counter's ``reserved``
    returns to 0 once its jobs end."""
    async with space(pg_url, tmp_path, "i26_res") as sp:
        stub = StubImageGenerator(script=[])
        with (
            registered(ADAPTER, stub),
            registered_target(),
            registered_target(CatalogTarget(), kind=ImageJobKind.CATALOG),
        ):
            await sp.configure()
            day = today()

            # 20 story requests, a limit of 1.
            await sp.set(image_limit_deployment_day=1)
            results = await _concurrently(sp, [_story() for _ in range(20)])
            accepted = [r for r in results if isinstance(r, str)]
            refused = [r for r in results if isinstance(r, ImageLimitReached)]
            assert (len(accepted), len(refused)) == (1, 19), results
            assert refused[0].http_status == 409
            assert refused[0].details["scope"] == "deployment"
            assert refused[0].details["limit"] == 1
            assert refused[0].details["requested"] == 1
            assert refused[0].details["used"] + refused[0].details["reserved"] == 1
            async with sp.db.connect() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM image_jobs")) == 1
            assert (await deployment(sp.db, day))["reserved"] == 1
            await assert_clean(sp.db)
            await image_jobs.cancel_job(sp.lib.image_runtime(), accepted[0])
            assert (await deployment(sp.db, day))["reserved"] == 0
            await assert_clean(sp.db)

            # 10 catalog requests for 4, an allowance of 10.
            await sp.set(image_limit_deployment_day=10)
            results = await _concurrently(sp, [_catalog(new_ulid(), 4) for _ in range(10)])
            accepted = [r for r in results if isinstance(r, str)]
            refused = [r for r in results if isinstance(r, ImageLimitReached)]
            assert (len(accepted), len(refused)) == (2, 8), results
            assert all(r.details["requested"] == 4 for r in refused)
            held = await deployment(sp.db, day)
            assert held["reserved"] == 8
            assert held["reserved"] + held["used"] <= 10
            assert {r["requested"] for r in await all_records(sp.db)} == {1, 4}
            await assert_clean(sp.db)
            for job_id in accepted:
                await image_jobs.cancel_job(sp.lib.image_runtime(), job_id)
            await assert_clean(sp.db)

            # Fewer outputs than reserved: 2 of 4.
            await sp.set(image_limit_deployment_day=200)
            with installed_checks(ScriptedChecks(fail_closed=False)):
                entry = new_ulid()
                job_id = await sp.insert(_catalog(entry, 4))
                stub.script.append(Outputs(2))
                assert (await sp.run(job_id))["image_job"] == "candidates_ready"
            (record,) = await records(sp.db, job_id)
            assert (record.requested, record.used, record.overage, record.extra_outputs) == (
                4,
                2,
                0,
                0,
            )
            assert record.state is ReservationState.SETTLED
            entry_row = (await counters(sp.db))[("entry", f"character:{entry}", day)]
            assert (entry_row["reserved"], entry_row["used"]) == (0, 2)
            await assert_clean(sp.db)

            # Three outputs for n = 1.
            with installed_checks(ScriptedChecks(fail_closed=False)) as checks:
                job_id = await sp.insert(_story())
                stub.script.append(Outputs(3))
                assert (await sp.run(job_id))["image_job"] == "promoted"
                assert [c for c in checks.checked if c[0] == job_id] == [(job_id, 0)]
            (record,) = await records(sp.db, job_id)
            assert (record.requested, record.used, record.overage, record.extra_outputs) == (
                1,
                3,
                2,
                2,
            )
            assert staged(sp.lib.media, job_id) == []
            view = await sp.lib.get_image_job(CTX, job_id)
            assert len(view.result_ids) == 1
            await assert_clean(sp.db)

            # Two jobs race their retries for the last unit.
            used = (await deployment(sp.db, day))["used"]
            await sp.set(image_limit_deployment_day=used + 3)
            first, second = await sp.insert(_story()), await sp.insert(_story())
            stub.script.extend([Outputs(), Outputs(), Outputs()])
            with installed_checks(_RacingChecks()):
                ran = await asyncio.wait_for(asyncio.gather(sp.run(first), sp.run(second)), 60)
            assert sorted(r["image_job"] for r in ran) == ["cap_reached", "promoted"], ran
            capped = first if ran[0]["image_job"] == "cap_reached" else second
            view = await sp.lib.get_image_job(CTX, capped)
            assert (view.status, view.status_reason) == ("cap_reached", "image_limit_reached")
            assert view.status_detail == {"scope": "deployment", "period": day}
            assert staged(sp.lib.media, capped) == []
            totals = await deployment(sp.db, day)
            assert (totals["used"], totals["reserved"]) == (used + 3, 0)
            await assert_clean(sp.db)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11")
async def test_i_26__m8_stress(pg_url: str, tmp_path: Path) -> None:
    """A mixed-scope stress run: 200 jobs over 3 stories and 2 catalog entries at concurrency
    32. Each one reserves at enqueue and then settles, releases or ends. It finishes within 60 s
    with no deadlock (SQLSTATE 40P01), and no counter's ``used + reserved`` passes its limit.
    The reconciler then finds no drift.

    Complement asserted: some requests were refused (the limits bound), and some were admitted
    at each scope."""
    async with space(pg_url, tmp_path, "i26_stress") as sp:
        await sp.set(image_limit_deployment_day=150, image_limit_entry_day=40)
        stories_ = [new_ulid() for _ in range(3)]
        entries = [new_ulid() for _ in range(2)]
        gate = asyncio.Semaphore(32)
        rng = random.Random(262)  # noqa: S311 - a reproducible plan, not a secret
        plan = [(i, rng.random()) for i in range(200)]
        outcome: Tally[str] = Tally()
        runtime = sp.lib.image_runtime()

        async def one(i: int, roll: float) -> None:
            job = _story(stories_[i % 3]) if i % 5 < 3 else _catalog(entries[i % 2], 2)
            async with gate:
                try:
                    job_id = await sp.insert(job)
                except ImageLimitReached:
                    outcome["refused"] += 1
                    return
                outcome["catalog" if i % 5 >= 3 else "story"] += 1
                if roll < 0.5:
                    async with sp.db.begin() as conn:
                        (record,) = await limits.records(conn, job_id)
                        await limits.settle(conn, record, record.requested)
                elif roll < 0.75:
                    async with sp.db.begin() as conn:
                        await limits.release_all(conn, job_id)
                else:
                    await image_jobs.end_job(
                        runtime, job_id, ImageJobStatus.FAILED, reason="stress"
                    )

        try:
            await asyncio.wait_for(asyncio.gather(*(one(i, r) for i, r in plan)), 60)
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            pytest.fail(f"stress run failed with SQLSTATE {sqlstate}")
        assert outcome["refused"] > 0
        assert outcome["story"] > 0
        assert outcome["catalog"] > 0
        for (scope, _key, _period), row in (await counters(sp.db)).items():
            bound = 150 if scope == "deployment" else 40
            assert row["used"] + row["reserved"] <= bound, (scope, row)
        await assert_clean(sp.db)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11")
async def test_i_26__m8_day_boundary(pg_url: str, tmp_path: Path) -> None:
    """Days are calendar days in ``deployment_time_zone`` (America/New_York):

    - a reservation at 23:59 local counts toward that day and one at 00:01 toward the next;
    - a full day resets at local midnight, and the refusal's ``resets_at`` is that midnight;
    - a job reserved at 23:59 whose provider returns at 00:01 charges the earlier day; the new
      day's counter is untouched;
    - regenerating the target turn (its origin changed) changes no counter key;
    - deleting the story while its attempt is held is refused while the job generates. Once
      the job is only queued, the deletion keeps the reservation records with ``story_id``
      nulled and the attempt released, the counter rows stay, and the reconciler agrees;
    - the record's immutable columns refuse an update (``image_reservation_immutable``);
    - retention removes closed-day rows only after ``image_accounting_retention_days`` and only
      once no record naming them is ``held``;
    - a story export has no ``image_reservations`` member.

    Complement asserted: the same reservation at 00:01 lands on the next day, and the next day
    admits again."""
    tz = ZoneInfo(NEW_YORK)
    late = datetime(2026, 11, 7, 23, 59, tzinfo=tz)
    later = datetime(2026, 11, 7, 23, 59, 30, tzinfo=tz)
    early = datetime(2026, 11, 8, 0, 1, tzinfo=tz)
    async with space(pg_url, tmp_path, "i26_day") as sp:
        stub = StubImageGenerator(script=[Outputs()])
        with (
            registered(ADAPTER, stub),
            registered_target(),
            installed_checks(ScriptedChecks(fail_closed=False)),
        ):
            await sp.configure()
            await sp.set(deployment_time_zone=NEW_YORK, image_limit_deployment_day=1)
            story = await new_story(sp.db)
            with at(late.astimezone(UTC)):
                held = await sp.insert(_story(story))
            with at(later.astimezone(UTC)), pytest.raises(ImageLimitReached) as full:
                await sp.insert(_story())
            assert full.value.details["period"] == "2026-11-07"
            assert full.value.details["resets_at"] == "2026-11-08T00:00:00-05:00"
            (record,) = await records(sp.db, held)
            assert [k.as_json() for k in record.counter_keys] == [
                {"scope": "deployment", "scope_key": "deployment", "period": "2026-11-07"}
            ]
            with at(early.astimezone(UTC)):
                nxt = await sp.insert(_story())
            (record,) = await records(sp.db, nxt)
            assert record.counter_keys[0].period == date(2026, 11, 8)
            # A DST gap: the first instant of the next date that exists.
            santiago = ZoneInfo("America/Santiago")
            gap = limits.resets_at(date(2026, 9, 5), santiago)
            assert gap.date() == date(2026, 9, 6)
            instant = gap.astimezone(UTC) - timedelta(seconds=1)
            assert instant.astimezone(santiago).date() == date(2026, 9, 5)

            # The story's job, reserved at 23:59, regenerated, returning at 00:01.
            async with sp.db.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE image_jobs SET origin = origin || '{\"variant\": 2}'::jsonb "
                        "WHERE id = :j"
                    ),
                    {"j": held},
                )
            with at(early.astimezone(UTC)):
                assert (await sp.run(held))["image_job"] == "promoted"
            (record,) = await records(sp.db, held)
            assert record.counter_keys[0].period == date(2026, 11, 7)
            found = await counters(sp.db)
            assert found[("deployment", "deployment", "2026-11-07")]["used"] == 1
            assert found[("deployment", "deployment", "2026-11-08")] == {
                "limit_value": 1,
                "reserved": 1,
                "used": 0,
                "overage": 0,
                "extra_outputs": 0,
            }
            await assert_clean(sp.db)

            # Deleting the story while its attempt is held.
            await sp.set(image_limit_deployment_day=10)
            with at(early.astimezone(UTC)):
                doomed = await sp.insert(_story(story))
            async with sp.db.begin() as conn:
                await conn.execute(
                    text("UPDATE image_jobs SET status = 'generating' WHERE id = :j"),
                    {"j": doomed},
                )
            with pytest.raises(StoryBusy) as busy:
                await stories.delete_story(sp.db, story, principal="p-images")
            assert busy.value.details["reason"] == "job_running"
            async with sp.db.begin() as conn:
                await conn.execute(
                    text("UPDATE image_jobs SET status = 'queued' WHERE id = :j"), {"j": doomed}
                )
            before = await counters(sp.db)
            await stories.delete_story(sp.db, story, principal="p-images")
            kept = [r for r in await all_records(sp.db) if r["job_id"] in (held, doomed)]
            assert len(kept) == 2
            assert all(r["story_id"] is None for r in kept)
            assert {r["job_id"]: r["state"] for r in kept} == {
                held: "settled",
                doomed: "released",
            }
            after = await counters(sp.db)
            assert set(after) == set(before)
            assert after[("deployment", "deployment", "2026-11-08")]["reserved"] == (
                before[("deployment", "deployment", "2026-11-08")]["reserved"] - 1
            )
            await assert_clean(sp.db)

            # The immutable columns.
            for statement in (
                "UPDATE image_reservations SET requested = requested + 1 WHERE job_id = :j",
                "UPDATE image_reservations SET counter_keys = '[]'::jsonb WHERE job_id = :j",
                "UPDATE image_reservations SET story_id = :j WHERE job_id = :j",
            ):
                with pytest.raises(DBAPIError, match="image_reservation_immutable"):
                    async with sp.db.begin() as conn:
                        await conn.execute(text(statement), {"j": nxt})

            # Retention: one day kept after the close, and never while a record is held.
            await sp.set(image_accounting_retention_days=1)
            nov_8 = ("deployment", "deployment", "2026-11-08")

            def noon(day: int) -> datetime:
                return datetime(2026, 11, day, 12, 0, tzinfo=tz).astimezone(UTC)

            assert await limits.retain(sp.db, now=noon(8)) == {"records": 0, "counters": 0}
            # 2026-11-07 closed a day ago: its settled record goes, then its counter.
            assert await limits.retain(sp.db, now=noon(9)) == {"records": 1, "counters": 1}
            assert set(await counters(sp.db)) == {nov_8}
            assert {r["job_id"] for r in await all_records(sp.db)} == {nxt, doomed}
            # 2026-11-08: the released record goes, the held one and its counter stay.
            assert await limits.retain(sp.db, now=noon(10)) == {"records": 1, "counters": 0}
            assert [r["job_id"] for r in await all_records(sp.db)] == [nxt]
            assert set(await counters(sp.db)) == {nov_8}
            await image_jobs.cancel_job(sp.lib.image_runtime(), nxt)
            assert await limits.retain(sp.db, now=noon(10)) == {"records": 1, "counters": 1}
            assert await counters(sp.db) == {}

            # A story export carries no deployment accounting.
            await sp.set(image_limit_deployment_day=10, deployment_time_zone="UTC")
            exported_story = await new_story(sp.db)
            await sp.insert(_story(exported_story))
            assert bundles.classify("image_reservations") == "NEVER"
            members = bundles.read_archive(
                await exported(sp.client, sp.db, exported_story, tmp_path / "bundle")
            )
            assert members
            assert not [name for name, _ in members if "image_reservations" in name]
            assert not [name for name, _ in members if "image_limit_counters" in name]


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11")
async def test_i_26__m8_defaults(pg_url: str, tmp_path: Path) -> None:
    """The deployment settings:

    - the defaults are ``image_limit_deployment_day`` 200, ``image_limit_entry_day`` 16,
      ``catalog_candidates_n`` 4, ``deployment_time_zone`` UTC and
      ``image_accounting_retention_days`` 90, all prospective;
    - a limit of 0 refuses at enqueue with 422 ``images_disabled {scope}``, even when another
      scope is also full;
    - negative, non-integer, ``null`` (missing) and ``"unlimited"`` limits are 422, and so are
      the bounds (``catalog_candidates_n`` above a non-zero entry limit, from either side; the
      retention days);
    - an unknown time zone is 422 ``invalid_time_zone``, and an alias is accepted.

    Complement asserted: 0 for the entry limit leaves story jobs admitted, and 0 is always
    savable whatever ``catalog_candidates_n`` is."""
    async with space(pg_url, tmp_path, "i26_def") as sp:
        shown = (await sp.client.get("/admin/settings", headers=_READ)).json()
        assert {k: shown[k] for k in _DEFAULTS} == _DEFAULTS
        from epistrel.core.settings_schema import DeploymentSettings, setting_class

        for name in _DEFAULTS:
            assert setting_class(DeploymentSettings, name).value == "prospective"

        stub = StubImageGenerator(script=[])
        with registered(ADAPTER, stub), registered_target():
            await sp.configure()
            # The entry is full and the deployment is off: images_disabled wins.
            entry = new_ulid()
            await sp.set(image_limit_entry_day=4)
            await sp.insert(_catalog(entry, 4))
            await sp.set(image_limit_deployment_day=0)
            with pytest.raises(ImagesDisabled) as off:
                await sp.insert(_catalog(entry, 4))
            assert (off.value.http_status, off.value.details) == (422, {"scope": "deployment"})
            await sp.set(image_limit_deployment_day=200, image_limit_entry_day=0)
            with pytest.raises(ImagesDisabled) as off:
                await sp.insert(_catalog(entry, 1))
            assert off.value.details == {"scope": "entry"}
            await sp.insert(_story())  # a story job charges no entry
            await assert_clean(sp.db)

        for bad in (-1, 1.5, None, "unlimited", 100_001):
            for name in ("image_limit_deployment_day", "image_limit_entry_day"):
                response = await sp.client.put("/admin/settings", json={name: bad}, headers=_WRITE)
                assert response.status_code == 422, (name, bad, response.text)
                assert response.json()["error"]["code"] == "validation_error"
        for name, bad in (
            ("image_accounting_retention_days", 0),
            ("image_accounting_retention_days", 3_651),
            ("catalog_candidates_n", 11),
        ):
            response = await sp.client.put("/admin/settings", json={name: bad}, headers=_WRITE)
            assert response.status_code == 422, (name, bad, response.text)
        # The cross-check, from both sides; 0 always saves.
        assert (
            await sp.client.put(
                "/admin/settings", json={"image_limit_entry_day": 3}, headers=_WRITE
            )
        ).status_code == 422
        assert (
            await sp.client.put(
                "/admin/settings",
                json={"image_limit_entry_day": 5, "catalog_candidates_n": 6},
                headers=_WRITE,
            )
        ).status_code == 422
        assert (
            await sp.client.put(
                "/admin/settings", json={"image_limit_entry_day": 0}, headers=_WRITE
            )
        ).status_code == 200
        assert (
            await sp.client.put(
                "/admin/settings", json={"catalog_candidates_n": 10}, headers=_WRITE
            )
        ).status_code == 200
        response = await sp.client.put(
            "/admin/settings", json={"deployment_time_zone": "Mars/Olympus"}, headers=_WRITE
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_time_zone"
        response = await sp.client.put(
            "/admin/settings", json={"deployment_time_zone": "US/Eastern"}, headers=_WRITE
        )
        assert response.status_code == 200, response.text
        assert response.json()["deployment_time_zone"] == "US/Eastern"


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11")
async def test_i_26__m8_reconciler_drift(
    pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reconciler reports drift and never hides or silently fixes it:

    - a counter corrupted directly is reported (``drift``, the metric
      ``epistrel_image_counter_drift_total{scope}`` up by one, ``/health``
      ``checks.image_counters: drift`` with the status unchanged), and the counter keeps its
      corrupted value;
    - a ``held`` record whose job ended is reported as ``stuck_held``;
    - ``epistrel images reconcile`` exits 1 and changes nothing. ``--fix`` releases the stuck
      record through ``release``, rewrites the counter and writes an
      ``image_counters_reconciled`` audit row with a count per scope, and leaves
      ``last_record_at`` alone. The next run is clean and clears ``/health``.

    Complement asserted: before the corruption the reconcile is clean and ``/health`` reads
    ``ok``. Both daily tasks are registered."""
    from epistrel.observability.metrics import IMAGE_COUNTER_DRIFT

    tasks = registered_tasks()
    for name in (IMAGE_COUNTER_RECONCILE_TASK, IMAGE_ACCOUNTING_RETENTION_TASK):
        assert tasks[name].interval == timedelta(hours=24)

    async def reconcile_run() -> str | None:
        async with sp.db.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE kind = 'image_job'"))
            await queue.enqueue(conn, IMAGE_COUNTER_RECONCILE_TASK, {})
        assert await run_jobs(sp.db) == 1
        checked = await health.check(sp.db)
        assert checked.status != "unavailable"
        return checked.checks.image_counters

    def drifted(scope: str) -> float:
        return float(IMAGE_COUNTER_DRIFT.labels(scope=scope)._value.get())

    async with space(pg_url, tmp_path, "i26_drift") as sp:
        day = today()
        job_id = await sp.insert(_story())
        ended = await sp.insert(_story())
        assert (await health.check(sp.db)).checks.image_counters is None
        assert await reconcile_run() == "ok"

        async with sp.db.begin() as conn:
            await conn.execute(text("UPDATE image_limit_counters SET used = used + 5"))
            await conn.execute(
                text("UPDATE image_jobs SET status = 'failed' WHERE id = :j"), {"j": ended}
            )
            stamped = await conn.scalar(text("SELECT last_record_at FROM image_limit_counters"))
        before = (drifted("deployment"), drifted(limits.STUCK_HELD))
        report = await image_reconcile.reconcile(sp.db)
        assert not report.clean
        assert [d.key.scope_key for d in report.drift] == ["deployment"]
        assert report.drift[0].expected["used"] == 0
        assert report.drift[0].stored is not None
        assert report.drift[0].stored["used"] == 5
        assert [s.job_id for s in report.stuck] == [ended]
        assert (drifted("deployment"), drifted(limits.STUCK_HELD)) == (
            before[0] + 1,
            before[1] + 1,
        )
        assert (await deployment(sp.db, day))["used"] == 5  # unchanged: never fixed silently
        assert await reconcile_run() == "drift"
        assert (await health.check(sp.db)).status in ("ok", "degraded")

        with _cli_settings(sp):
            assert await asyncio.to_thread(cli.main, ["images", "reconcile"]) == 1
            assert (await deployment(sp.db, day))["used"] == 5
            assert await asyncio.to_thread(cli.main, ["images", "reconcile", "--fix"]) == 0
        capsys.readouterr()
        fixed = await deployment(sp.db, day)
        assert (fixed["used"], fixed["reserved"]) == (0, 1)
        (stuck,) = await records(sp.db, ended)
        assert stuck.state is ReservationState.RELEASED
        async with sp.db.connect() as conn:
            audited = (
                await conn.execute(
                    text(
                        "SELECT principal, details FROM audit_log "
                        "WHERE kind = 'image_counters_reconciled'"
                    )
                )
            ).one()
            assert (
                await conn.scalar(text("SELECT last_record_at FROM image_limit_counters"))
                == stamped
            )
        assert audited.principal == image_reconcile.FIX_ACTOR
        assert audited.details["counts"] == {"stuck_held": 1, "deployment": 1, "entry": 0}
        await assert_clean(sp.db)
        assert await reconcile_run() == "ok"
        assert job_id


_READ = bearer()
_WRITE = {**bearer(), "X-Epistrel-Principal": "op-limits"}
_DEFAULTS = {
    "image_limit_deployment_day": 200,
    "image_limit_entry_day": 16,
    "catalog_candidates_n": 4,
    "deployment_time_zone": "UTC",
    "image_accounting_retention_days": 90,
}


class _cli_settings:
    """``epistrel images reconcile`` against the space's database (its settings, not the
    environment's)."""

    def __init__(self, sp: Space) -> None:
        self.sp = sp
        self._load = cli._load

    def __enter__(self) -> None:
        cli._load = lambda: self.sp.settings

    def __exit__(self, *exc: object) -> None:
        cli._load = self._load
