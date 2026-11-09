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
import hashlib
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
from epistrel.orchestrator.images import attempts
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
      once no record naming them is ``held``: a settled or released record whose counter a
      ``held`` record also names is kept, so the reconciler stays clean (#796);
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

            assert await limits.retain(sp.db, now=noon(8)) == {
                "records": 0,
                "counters": 0,
                "costs": 0,
            }
            # 2026-11-07 closed a day ago: its settled record goes, then its counter.
            # #269: the returned attempt's cost row goes with its day too.
            assert await limits.retain(sp.db, now=noon(9)) == {
                "records": 1,
                "counters": 1,
                "costs": 1,
            }
            assert set(await counters(sp.db)) == {nov_8}
            assert {r["job_id"] for r in await all_records(sp.db)} == {nxt, doomed}
            # 2026-11-08: a settled sibling of the held record, on the same counter (#796).
            with at(early.astimezone(UTC)):
                sibling = await sp.insert(_story())
                async with sp.db.begin() as conn:
                    (settling,) = await limits.records(conn, sibling)
                    await limits.settle(conn, settling, 1)
            await assert_clean(sp.db)
            # While `nxt` is held, no record naming its counter goes: neither the released one
            # nor the settled one, whose `used` the kept counter still carries.
            removed = await limits.retain(sp.db, now=noon(10))
            await assert_clean(sp.db)
            assert removed == {"records": 0, "counters": 0, "costs": 0}
            assert {r["job_id"] for r in await all_records(sp.db)} == {nxt, doomed, sibling}
            assert set(await counters(sp.db)) == {nov_8}
            # Complement: once the held record settles, the day's records and counter all go.
            async with sp.db.begin() as conn:
                (holding,) = await limits.records(conn, nxt)
                await limits.settle(conn, holding, 1)
            await assert_clean(sp.db)
            assert await limits.retain(sp.db, now=noon(10)) == {
                "records": 3,
                "counters": 1,
                "costs": 0,
            }
            assert await all_records(sp.db) == []
            assert await counters(sp.db) == {}
            await assert_clean(sp.db)
            await image_jobs.cancel_job(sp.lib.image_runtime(), nxt)

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


# -- late returns after lease expiry (#267) ---------------------------------------------------

ASYNC_ADAPTER = "stub_async"
PLAIN_ASYNC_ADAPTER = "stub_async_plain"
"""An ``async`` stub that does not confirm completion: a stall in ``collect`` is a known
request (#267), never an ambiguous send (#268)."""
GRACE_S = 60
"""``image_attempt_grace_s``'s default: a stub attempt's horizon is ``started_at + 120 + 60``."""


async def _sent(stub: StubImageGenerator, count: int) -> None:
    """Until the stub has received ``count`` requests (a stalled run is blocked in its call)."""
    for _ in range(500):
        if len(stub.requests) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"the stub never received request {count}")


async def _expire_lease(sp: Space, job_id: str) -> list[str]:
    """The job's ``jobs`` row as a claim whose lease lapsed, then #70's reaper."""
    async with sp.db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET status = 'running', lease_until = now() - interval '1 second', "
                "attempt = attempt + 1 WHERE kind = 'image_job' "
                "AND payload->>'image_job_id' = :j"
            ),
            {"j": job_id},
        )
    return await queue.reap(sp.db)


async def _one(sp: Space, sql: str, **params: Any) -> Any:
    async with sp.db.connect() as conn:
        return (await conn.execute(text(sql), params)).one()


async def _token(sp: Space, job_id: str) -> str:
    return str((await _one(sp, "SELECT attempt_token FROM image_jobs WHERE id = :j", j=job_id))[0])


async def _past_horizon(sp: Space, job_id: str, token: str) -> None:
    """The attempt's horizon (and its timer, and the parked ``jobs`` row) moved into the past,
    as if the clock had run past it."""
    async with sp.db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE image_reservations SET horizon_at = now() - interval '1 second' "
                "WHERE job_id = :j AND attempt_token = :t"
            ),
            {"j": job_id, "t": token},
        )
        await conn.execute(
            text("UPDATE timers SET due_at = now() - interval '1 second' WHERE target = :t"),
            {"t": f"{job_id}:{token}"},
        )


async def _record(sp: Space, job_id: str, token: str) -> limits.ReservationRecord:
    async with sp.db.connect() as conn:
        found = await limits.read_record(conn, job_id, token)
    assert found is not None
    return found


async def _outcome(sp: Space, job_id: str, token: str) -> tuple[str | None, Any]:
    row = await _one(
        sp,
        "SELECT outcome, applied_parameters->'cost' AS cost FROM image_reservations "
        "WHERE job_id = :j AND attempt_token = :t",
        j=job_id,
        t=token,
    )
    return row.outcome, row.cost


def _counts(record: limits.ReservationRecord) -> tuple[Any, ...]:
    return (
        record.state.value,
        record.requested,
        record.used,
        record.overage,
        record.extra_outputs,
    )


async def _stalled_start(
    sp: Space, stub: StubImageGenerator, job_id: str
) -> tuple[asyncio.Task[dict[str, Any]], str]:
    """The job run until its provider call stalls, then its lease lost: the stalled run and its
    (now fenced) attempt token."""
    sent = len(stub.requests)
    task = asyncio.create_task(sp.run(job_id))
    await _sent(stub, sent + 1)
    token = await _token(sp, job_id)
    wanted = "accepted" if stub.capabilities().submission == "async" else "sending"
    for _ in range(500):
        if (await _record(sp, job_id, token)).send_state.value == wanted:
            break
        await asyncio.sleep(0.01)
    assert await _expire_lease(sp, job_id)
    assert await _token(sp, job_id) != token
    return task, token


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-1,11")
async def test_i_26__m8_late_returns(pg_url: str, tmp_path: Path) -> None:
    """I-26's "Late returns after lease expiry" clauses, with the stub's stall and status
    scripts and backdated horizons:

    - the reaper advances the attempt token and parks the ``jobs`` row at the horizon
      (``started_at + request_timeout_s + image_attempt_grace_s``), with the
      ``image_attempt_horizon`` timer scheduled; the stalled token's promotion and counter
      changes are refused (``FencedToken``), and so is settling a record that is not its own;
    - before the horizon no retry starts (``waiting_for_horizon``, one provider call);
    - non-confirming (an ``async`` stub stalled in ``collect``, a known request: #268 makes a
      ``sync`` stall an ambiguous send, ``test_i_26__m8_ambiguous``): the record is released at
      the horizon with cost ``unknown``; a retry starts then, and the old call's late result is
      settled as overage (``late_after_horizon``) without touching the retry's record or
      candidates;
    - a stalled call returning before its horizon settles its own record (``late_discarded``:
      ``requested = 2``, one returned → ``used = 1``, ``overage = 0``, 2 off ``reserved``),
      nothing promoted, its bytes deleted; settling it again changes nothing
      (``late_duplicate``);
    - a call that never returns is released at the horizon with cost ``unknown``; returning
      after it with 3 outputs pushes the deployment counter past its limit by those 3, and new
      reservations are refused (the job ends ``cap_reached``, a request 409);
    - confirming: a cancel answering ``accepted`` while status says ``running`` keeps the
      retry waiting (``image_status_poll`` timer); ``cancelled`` settles the old record with no
      outputs and lets the retry start; ``succeeded`` with 2 outputs for ``n = 2`` is adopted
      (``released → settled``, ``used = overage = 2``, the counter's ``used`` and ``overage`` up
      by exactly 2 with ``reserved`` unchanged), checked and promoted under the current token
      with no second call, and the fenced worker's duplicate return is ``late_duplicate`` with
      no counter change; 3 outputs for ``n = 2`` → two checked, ``used = overage = 3``,
      ``extra_outputs = 1``, and a status with no cost is filled in once by the duplicate
      return; ``failed`` → retried; ``unknown`` is not definitive; ``running`` past
      ``image_confirm_wait_max_s`` falls back (``confirm_fallback``, ``abandoned_fallback``),
      the retry runs while the old call still reports ``running``, and its later output is
      ``late_after_horizon``, never adopted;
    - the reconciler reports no drift after every step.

    Complement asserted: the current token passes ``require_token``, a run before the horizon
    sends nothing, and a confirmed ``running`` never retries."""
    from epistrel.core.images import store as image_store
    from epistrel.core.images.errors import FencedToken
    from epistrel.orchestrator.images import attempts
    from tests.image_support import STUB_COST, Stall, StatusSeq

    async with space(pg_url, tmp_path, "i26_late") as sp:
        stub = StubImageGenerator(script=[])
        plain = StubImageGenerator(submission="async", script=[])
        confirming = StubImageGenerator(submission="async", confirms_completion=True, script=[])
        catalog = CatalogTarget()
        with (
            registered(ADAPTER, stub),
            registered(PLAIN_ASYNC_ADAPTER, plain),
            registered(ASYNC_ADAPTER, confirming),
            registered_target(),
            registered_target(catalog, kind=ImageJobKind.CATALOG),
            installed_checks(ScriptedChecks(fail_closed=False)) as checks,
        ):
            await sp.configure()
            day = today()

            # -- non-confirming: fenced, parked, released at the horizon, retried, overage --
            await configure_adapter(sp, PLAIN_ASYNC_ADAPTER)
            gate = asyncio.Event()
            plain.script.extend([Stall(gate, Outputs(2)), Outputs(2)])
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            task, old = await _stalled_start(sp, plain, job_id)
            sent = await _one(
                sp,
                "SELECT started_at, horizon_at, send_state FROM image_reservations "
                "WHERE job_id = :j AND attempt_token = :t",
                j=job_id,
                t=old,
            )
            assert sent.send_state == "accepted"
            assert sent.horizon_at == sent.started_at + timedelta(seconds=120 + GRACE_S)
            parked = await _one(
                sp, "SELECT due_at FROM jobs WHERE payload->>'image_job_id' = :j", j=job_id
            )
            assert parked.due_at == sent.horizon_at
            timer = await _one(
                sp,
                "SELECT due_at FROM timers WHERE kind = :k AND target = :t",
                k=attempts.HORIZON_TIMER,
                t=f"{job_id}:{old}",
            )
            assert timer.due_at == sent.horizon_at
            current = await _token(sp, job_id)
            async with sp.db.begin() as conn:
                with pytest.raises(FencedToken):
                    await image_store.require_token(conn, job_id, old)
                await image_store.require_token(conn, job_id, current)  # complement
                job = await image_store.read_job(conn, job_id)
                assert job is not None
                with pytest.raises(FencedToken):
                    await limits.reserve(conn, job, old, 1)
            stranger = image_store.new_token()
            assert await attempts.settle_attempt(sp.db, job_id, stranger, 2, cost=STUB_COST) is None
            view = await sp.lib.get_image_job(CTX, job_id)
            assert view.waiting_for_horizon is True
            assert [(a.attempt_token, a.outcome, a.used) for a in view.attempts] == [
                (old, None, None)
            ]
            await assert_clean(sp.db)

            waited = await sp.run(job_id)
            assert waited["waiting_for_horizon"] is True
            assert waited[image_jobs.REQUEUE_IN] > 0
            assert len(plain.requests) == 1, "no retry before the horizon"
            await assert_clean(sp.db)

            before = await deployment(sp.db, day)
            await _past_horizon(sp, job_id, old)
            assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "released"
            assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "skipped"
            assert _counts(await _record(sp, job_id, old)) == ("released", 2, 0, 0, 0)
            assert await _outcome(sp, job_id, old) == ("released_at_horizon", "unknown")
            assert (await deployment(sp.db, day))["reserved"] == before["reserved"] - 2
            await assert_clean(sp.db)

            ran = await sp.run(job_id)
            assert ran["image_job"] == "candidates_ready", ran
            assert len(plain.requests) == 2
            retry = await _token(sp, job_id)
            assert _counts(await _record(sp, job_id, retry)) == ("settled", 2, 2, 0, 0)
            view = await sp.lib.get_image_job(CTX, job_id)
            kept = list(view.result_ids)
            assert len(kept) == 2
            assert view.waiting_for_horizon is False
            mid = await deployment(sp.db, day)
            gate.set()
            late = await asyncio.wait_for(task, 30)
            assert late == {"image_job": "fenced", "late": "late_after_horizon"}, late
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 2, 2, 0)
            after = await deployment(sp.db, day)
            assert (after["used"], after["overage"], after["reserved"]) == (
                mid["used"] + 2,
                mid["overage"] + 2,
                mid["reserved"],
            )
            assert _counts(await _record(sp, job_id, retry)) == ("settled", 2, 2, 0, 0)
            view = await sp.lib.get_image_job(CTX, job_id)
            assert (view.status, list(view.result_ids)) == ("candidates_ready", kept)
            assert [(a.attempt_token, a.outcome, a.used, a.overage) for a in view.attempts] == [
                (old, "late_after_horizon", 2, 2),
                (retry, "promoted", 2, 0),
            ]
            assert staged(sp.lib.media, job_id) == []
            await assert_clean(sp.db)

            # -- a return before the horizon: late_discarded; twice: late_duplicate --
            await configure_adapter(sp, ADAPTER)
            gate = asyncio.Event()
            stub.script.append(Stall(gate, Outputs(1)))
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            task, old = await _stalled_start(sp, stub, job_id)
            before = await deployment(sp.db, day)
            gate.set()
            late = await asyncio.wait_for(task, 30)
            assert late == {"image_job": "fenced", "late": "late_discarded"}, late
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 1, 0, 0)
            assert (await _outcome(sp, job_id, old))[0] == "late_discarded"
            after = await deployment(sp.db, day)
            assert (after["used"], after["reserved"]) == (
                before["used"] + 1,
                before["reserved"] - 2,
            )
            assert (await sp.lib.get_image_job(CTX, job_id)).result_ids == ()
            assert staged(sp.lib.media, job_id) == []
            again = await attempts.settle_attempt(sp.db, job_id, old, 1, cost=STUB_COST)
            assert again is not None
            assert again.value == "late_duplicate"
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 1, 0, 0)
            assert await deployment(sp.db, day) == after
            await assert_clean(sp.db)
            await image_jobs.cancel_job(sp.lib.image_runtime(), job_id)
            await assert_clean(sp.db)

            # -- never returns: released at the horizon, cost unknown; then 3 after it --
            used = (await deployment(sp.db, day))["used"]
            await sp.set(image_limit_deployment_day=used + 1)
            await configure_adapter(sp, PLAIN_ASYNC_ADAPTER)
            gate = asyncio.Event()
            plain.script.append(Stall(gate, Outputs(3)))
            job_id = await sp.insert(_story())
            task, old = await _stalled_start(sp, plain, job_id)
            await _past_horizon(sp, job_id, old)
            assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "released"
            assert _counts(await _record(sp, job_id, old)) == ("released", 1, 0, 0, 0)
            assert await _outcome(sp, job_id, old) == ("released_at_horizon", "unknown")
            await assert_clean(sp.db)
            gate.set()
            late = await asyncio.wait_for(task, 30)
            assert late["late"] == "late_after_horizon", late
            assert _counts(await _record(sp, job_id, old)) == ("settled", 1, 3, 3, 2)
            totals = await deployment(sp.db, day)
            assert totals["used"] == used + 3  # past the limit of used + 1 by those outputs
            ended = await sp.run(job_id)
            assert ended == {"image_job": "cap_reached", "reason": "image_limit_reached"}, ended
            with pytest.raises(ImageLimitReached):
                await sp.insert(_story())
            await assert_clean(sp.db)
            await sp.set(image_limit_deployment_day=200)

            # -- confirming: accepted cancel + running waits; cancelled lets the retry go --
            await configure_adapter(sp, ASYNC_ADAPTER)
            gate = asyncio.Event()
            confirming.script.extend(
                [Stall(gate, StatusSeq(["running", "cancelled"], Outputs(2))), Outputs(2)]
            )
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            task, old = await _stalled_start(sp, confirming, job_id)
            sent = await _record(sp, job_id, old)
            assert sent.send_state.value == "accepted"
            await _past_horizon(sp, job_id, old)
            polled = await sp.run(job_id)
            assert polled["waiting_on_confirmation"] is True, polled
            assert confirming.cancelled == ["stub-1"]
            assert len(confirming.requests) == 1, "an accepted cancel and running: no retry"
            row = await _one(
                sp,
                "SELECT cancel_result, last_status FROM image_reservations "
                "WHERE job_id = :j AND attempt_token = :t",
                j=job_id,
                t=old,
            )
            assert (row.cancel_result, row.last_status) == ("accepted", "running")
            view = await sp.lib.get_image_job(CTX, job_id)
            horizon = (await _record(sp, job_id, old)).horizon_at
            assert horizon is not None
            assert view.confirm_deadline_at == horizon + timedelta(seconds=1800)
            poll = await _one(
                sp,
                "SELECT count(*) AS n FROM timers WHERE kind = :k AND target = :j",
                k=attempts.STATUS_POLL,
                j=job_id,
            )
            assert poll.n == 1
            await assert_clean(sp.db)
            ran = await sp.run(job_id)
            assert ran["image_job"] == "candidates_ready", ran
            assert confirming.cancelled == ["stub-1"], "cancel is called once"
            assert len(confirming.requests) == 2
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 0, 0, 0)
            assert (await _outcome(sp, job_id, old))[0] == "cancelled"
            task.cancel()
            await assert_clean(sp.db)

            # -- succeeded with 2 for n = 2: adopted, no retry; the duplicate changes nothing --
            gate = asyncio.Event()
            confirming.script.append(Stall(gate, StatusSeq(["succeeded"], Outputs(2))))
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            sent_before = len(confirming.requests)
            task, old = await _stalled_start(sp, confirming, job_id)
            await _past_horizon(sp, job_id, old)
            before = await deployment(sp.db, day)
            ran = await sp.run(job_id)
            assert ran["image_job"] == "candidates_ready", ran
            assert len(confirming.requests) == sent_before + 1, "adopted without retrying"
            record = await _record(sp, job_id, old)
            assert _counts(record) == ("settled", 2, 2, 2, 0)
            assert await _outcome(sp, job_id, old) == (
                "adopted",
                {"amount": "0.04", "currency": "USD"},
            )
            after = await deployment(sp.db, day)
            # Released at the horizon (2 off reserved), then adopted: used and overage + 2.
            assert (after["used"], after["overage"], after["reserved"]) == (
                before["used"] + 2,
                before["overage"] + 2,
                before["reserved"] - 2,
            )
            adopted = list((await sp.lib.get_image_job(CTX, job_id)).result_ids)
            assert len(adopted) == 2
            assert [c for c in checks.checked if c[0] == job_id] == [(job_id, 0), (job_id, 1)]
            gate.set()
            late = await asyncio.wait_for(task, 30)
            assert late == {"image_job": "fenced", "late": "late_duplicate"}, late
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 2, 2, 0)
            assert await deployment(sp.db, day) == after
            assert list((await sp.lib.get_image_job(CTX, job_id)).result_ids) == adopted
            assert staged(sp.lib.media, job_id) == []
            await assert_clean(sp.db)

            # -- 3 for n = 2, no cost in the status: filled in once by the duplicate --
            gate = asyncio.Event()
            confirming.script.append(
                Stall(gate, StatusSeq(["succeeded"], Outputs(3), status_cost=False))
            )
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            task, old = await _stalled_start(sp, confirming, job_id)
            await _past_horizon(sp, job_id, old)
            ran = await sp.run(job_id)
            assert ran["image_job"] == "candidates_ready", ran
            assert [c for c in checks.checked if c[0] == job_id] == [(job_id, 0), (job_id, 1)]
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 3, 3, 1)
            assert await _outcome(sp, job_id, old) == ("adopted", "unknown")
            gate.set()
            assert (await asyncio.wait_for(task, 30))["late"] == "late_duplicate"
            filled = {"amount": "0.04", "currency": "USD"}
            assert await _outcome(sp, job_id, old) == ("adopted", filled)
            from decimal import Decimal

            from epistrel.orchestrator.images.port import Cost

            await attempts.settle_attempt(sp.db, job_id, old, 3, cost=Cost(Decimal("9"), "USD"))
            assert await _outcome(sp, job_id, old) == ("adopted", filled), "filled in once"
            await assert_clean(sp.db)

            # -- failed: settled with no outputs, then retried --
            gate = asyncio.Event()
            confirming.script.extend([Stall(gate, StatusSeq(["failed"], Outputs(2))), Outputs(2)])
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            task, old = await _stalled_start(sp, confirming, job_id)
            sent_before = len(confirming.requests)
            await _past_horizon(sp, job_id, old)
            ran = await sp.run(job_id)
            assert ran["image_job"] == "candidates_ready", ran
            assert len(confirming.requests) == sent_before + 1
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 0, 0, 0)
            assert (await _outcome(sp, job_id, old))[0] == "failed"
            task.cancel()
            await assert_clean(sp.db)

            # -- unknown is not definitive; running past the wait falls back, overlap allowed --
            gate = asyncio.Event()
            confirming.script.extend(
                [Stall(gate, StatusSeq(["unknown", "running"], Outputs(2))), Outputs(2)]
            )
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            task, old = await _stalled_start(sp, confirming, job_id)
            sent_before = len(confirming.requests)
            await _past_horizon(sp, job_id, old)
            assert (await sp.run(job_id))["waiting_on_confirmation"] is True
            assert (await sp.run(job_id))["waiting_on_confirmation"] is True
            assert len(confirming.requests) == sent_before, "unknown and running never retry"
            async with sp.db.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE image_jobs SET confirm_deadline_at = now() - interval '1 second' "
                        "WHERE id = :j"
                    ),
                    {"j": job_id},
                )
            ran = await sp.run(job_id)
            assert ran["image_job"] == "candidates_ready", ran
            assert len(confirming.requests) == sent_before + 1
            assert (await confirming.status("stub-" + str(sent_before))).state == "running"
            assert (await _outcome(sp, job_id, old))[0] == "abandoned_fallback"
            retry = await _token(sp, job_id)
            kept = list((await sp.lib.get_image_job(CTX, job_id)).result_ids)
            gate.set()
            late = await asyncio.wait_for(task, 30)
            assert late == {"image_job": "fenced", "late": "late_after_horizon"}, late
            assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 2, 2, 0)
            assert _counts(await _record(sp, job_id, retry)) == ("settled", 2, 2, 0, 0)
            view = await sp.lib.get_image_job(CTX, job_id)
            assert (view.confirm_fallback, list(view.result_ids)) == (True, kept)
            await assert_clean(sp.db)

        # The settings: defaults, bounds, and the poll never above the wait (either side).
        found = (await sp.client.get("/admin/settings", headers=_READ)).json()
        assert {k: found[k] for k in _ATTEMPT_DEFAULTS} == _ATTEMPT_DEFAULTS
        for name, bad in (
            ("image_attempt_grace_s", -1),
            ("image_attempt_grace_s", 3_601),
            ("image_status_poll_s", 4),
            ("image_status_poll_s", 601),
            ("image_confirm_wait_max_s", 59),
            ("image_confirm_wait_max_s", 86_401),
        ):
            response = await sp.client.put("/admin/settings", json={name: bad}, headers=_WRITE)
            assert response.status_code == 422, (name, bad, response.text)
        poll = {"image_status_poll_s": 100}
        assert (
            await sp.client.put("/admin/settings", json=poll, headers=_WRITE)
        ).status_code == 200
        for body in (
            {"image_confirm_wait_max_s": 60},
            {"image_status_poll_s": 600, "image_confirm_wait_max_s": 300},
        ):
            response = await sp.client.put("/admin/settings", json=body, headers=_WRITE)
            assert response.status_code == 422, (body, response.text)
            assert response.json()["error"]["code"] == "validation_error"
        response = await sp.client.put(
            "/admin/settings",
            json={"image_status_poll_s": 60, "image_confirm_wait_max_s": 60},
            headers=_WRITE,
        )
        assert response.status_code == 200, response.text


_ATTEMPT_DEFAULTS = {
    "image_attempt_grace_s": 60,
    "image_status_poll_s": 15,
    "image_confirm_wait_max_s": 1800,
}


async def configure_adapter(sp: Space, adapter: str) -> None:
    """The space's ``image_generation`` port switched to ``adapter``."""
    from tests.image_job_support import configure

    await configure(sp.lib, sp.settings, adapter)


# -- ambiguous sends (#268) -------------------------------------------------------------------

AMBIGUOUS_GROUPS = ("crash_recovery", "presumed_then_actual", "submission_modes")
IDEMPOTENT_ADAPTER = "stub_idempotent"


async def _killed(sp: Space, point: str, job_id: str) -> None:
    """A child engine (images on) on the space's database, killed at ``point`` while it runs
    ``job_id``, then the lost claim reaped as #70's recovery pass would."""
    from tests.image_job_support import crashed_child

    child = await crashed_child(sp.settings.database_url.get_secret_value(), point, job_id)
    await child.__aexit__(None, None, None)
    assert await _expire_lease(sp, job_id)


async def _attempt_row(sp: Space, job_id: str, token: str) -> Any:
    return await _one(
        sp,
        "SELECT send_state, idempotency_key, resend_of, request_snapshot, "
        "capabilities_snapshot, outcome, outputs_returned, applied_parameters->'cost' AS cost, "
        "requested, counter_keys, basis, state, used, overage, extra_outputs "
        "FROM image_reservations WHERE job_id = :j AND attempt_token = :t",
        j=job_id,
        t=token,
    )


async def _jobs_row(sp: Space, job_id: str) -> Any:
    return await _one(
        sp,
        "SELECT failures, due_at, due_at <= now() AS due FROM jobs "
        "WHERE kind = 'image_job' AND payload->>'image_job_id' = :j",
        j=job_id,
    )


async def _ambiguous_sends(sp: Space) -> int:
    row = await _one(
        sp, "SELECT count(*) AS n FROM image_reservations WHERE send_state = 'ambiguous'"
    )
    return int(row.n)


async def _assets_of(sp: Space, job_id: str) -> list[Any]:
    async with sp.db.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, source_attempt_token, output_index FROM media_assets "
                "WHERE source_job_id = :j ORDER BY output_index"
            ),
            {"j": job_id},
        )
        return list(rows)


def _metric(outcome: str) -> float:
    from prometheus_client import REGISTRY

    found = REGISTRY.get_sample_value("epistrel_image_attempt_outcomes_total", {"outcome": outcome})
    return 0.0 if found is None else found


def _cost(cost: Any) -> dict[str, str]:
    return {"amount": str(cost.amount), "currency": cost.currency}


async def _crash_recovery(sp: Space) -> None:
    """Child engines killed mid-send, then the reaper and an in-process run (the stubs are
    registered here under the child's adapter names)."""
    from sqlalchemy.exc import IntegrityError

    from epistrel.core.images import store as image_store
    from epistrel.core.tables.media import MediaState
    from epistrel.testing import faults
    from tests.image_support import Stall, png

    idempotent = StubImageGenerator(idempotency_keys=True, script=[])
    plain = StubImageGenerator(script=[])
    acked = StubImageGenerator(submission="async", script=[])
    deduplicating = StubImageGenerator(idempotency_keys=True, script=[])
    with (
        registered(faults.IMAGE_ADAPTER_IDEMPOTENT, idempotent),
        registered(faults.IMAGE_ADAPTER, plain),
        registered(faults.IMAGE_ADAPTER_ASYNC, acked),
        registered(IDEMPOTENT_ADAPTER, deduplicating),
        registered_target(),
        registered_target(CatalogTarget(), kind=ImageJobKind.CATALOG),
        installed_checks(ScriptedChecks(fail_closed=False)),
    ):
        day = today()

        # -- killed after `sending` with idempotency_keys: re-sent at once, counted once --
        await configure_adapter(sp, faults.IMAGE_ADAPTER_IDEMPOTENT)
        idempotent.script.append(Outputs(1))
        job_id = await sp.insert(_story())
        old = await _token(sp, job_id)
        await _killed(sp, "images.after_sending_commit", job_id)
        sent = await _attempt_row(sp, job_id, old)
        key = image_store.idempotency_key_of(job_id, old)
        assert key == hashlib.sha256(f"{job_id}:{old}".encode()).hexdigest()
        assert (sent.send_state, sent.idempotency_key, sent.outcome) == ("sending", key, None)
        assert sent.capabilities_snapshot == {"submission": "sync", "idempotency_keys": True}
        assert sent.request_snapshot["n"] == 1
        assert {"prompt", "negative_prompt"}.isdisjoint(sent.request_snapshot), "no story text"
        parked = await _jobs_row(sp, job_id)
        assert (parked.due, parked.failures) == (True, 1), "due at once; one failed attempt"
        view = await sp.lib.get_image_job(CTX, job_id)
        assert view.waiting_for_horizon is False
        before = await deployment(sp.db, day)
        ran = await sp.run(job_id)
        assert ran["image_job"] == "promoted", ran
        new = await _token(sp, job_id)
        assert new != old
        (received,) = idempotent.requests
        assert received.idempotency_key == key, "the stalled attempt's key, sent again"
        assert received.request.prompt == "a lighthouse at dusk"
        resend = await _attempt_row(sp, job_id, new)
        assert (str(resend.resend_of), resend.requested, resend.counter_keys) == (old, 0, [])
        assert resend.idempotency_key == key
        assert resend.outcome == "promoted"
        original = await _attempt_row(sp, job_id, old)
        assert (original.state, original.used, original.basis, original.outcome) == (
            "settled",
            1,
            "actual",
            "resent",
        )
        assert original.send_state != "ambiguous", "an idempotent re-send is no ambiguous send"
        after = await deployment(sp.db, day)
        assert (after["used"], after["reserved"]) == (before["used"] + 1, before["reserved"] - 1)
        (asset,) = await _assets_of(sp, job_id)
        assert (str(asset.source_attempt_token), asset.output_index) == (new, 0)
        view = await sp.lib.get_image_job(CTX, job_id)
        assert [(a.attempt_token, a.send_state, a.basis, a.resend_of) for a in view.attempts] == [
            (old, "returned", "actual", None),
            (new, "returned", "actual", old),
        ]
        assert await _ambiguous_sends(sp) == 0
        await assert_clean(sp.db)

        # No output is promoted twice: the source triple is unique.
        data = png(16, 16)
        with pytest.raises(IntegrityError):
            async with sp.db.begin() as conn:
                await image_store.insert_generated_asset(
                    conn,
                    checksum=hashlib.sha256(data).hexdigest(),
                    mime_type="image/png",
                    size=len(data),
                    width=16,
                    height=16,
                    owner="p-images",
                    content_rating_version_id=(
                        await conn.execute(
                            text(
                                "SELECT content_rating_version_id FROM media_assets WHERE id = :a"
                            ),
                            {"a": asset.id},
                        )
                    ).scalar_one(),
                    generation_metadata={},
                    state=MediaState.LIVE,
                    created_by="p-images",
                    source_job_id=job_id,
                    source_attempt_token=new,
                    output_index=0,
                )

        # -- the provider deduplicates: a stalled call and its re-send yield one result --
        await configure_adapter(sp, IDEMPOTENT_ADAPTER)
        gate = asyncio.Event()
        deduplicating.script.append(Stall(gate, Outputs(1)))
        job_id = await sp.insert(_story())
        before = await deployment(sp.db, day)
        task, old = await _stalled_start(sp, deduplicating, job_id)
        assert (await _attempt_row(sp, job_id, old)).send_state == "sending"
        rerun = asyncio.create_task(sp.run(job_id))
        await _sent(deduplicating, 2)
        gate.set()
        late = await asyncio.wait_for(task, 30)
        ran = await asyncio.wait_for(rerun, 30)
        assert ran["image_job"] == "promoted", ran
        assert late["image_job"] == "fenced", late
        assert deduplicating.deduplicated == [image_store.idempotency_key_of(job_id, old)]
        original = await _attempt_row(sp, job_id, old)
        assert (original.state, original.used, original.overage) == ("settled", 1, 0)
        assert original.outcome in ("resent", "late_discarded")
        after = await deployment(sp.db, day)
        assert after["used"] == before["used"] + 1, "the original result, counted once"
        assert len(await _assets_of(sp, job_id)) == 1
        await assert_clean(sp.db)

        # -- killed after `sending` without it: ambiguous, presumed at the horizon, retried --
        await configure_adapter(sp, faults.IMAGE_ADAPTER)
        plain.script.append(Outputs(1))
        job_id = await sp.insert(_story())
        old = await _token(sp, job_id)
        ambiguous = await _ambiguous_sends(sp)
        await _killed(sp, "images.after_sending_commit", job_id)
        record = await _attempt_row(sp, job_id, old)
        assert (record.send_state, record.state) == ("ambiguous", "held")
        assert await _ambiguous_sends(sp) == ambiguous + 1, "the deployment's ambiguous sends + 1"
        view = await sp.lib.get_image_job(CTX, job_id)
        assert view.waiting_for_horizon is True
        parked = await _jobs_row(sp, job_id)
        assert (parked.due, parked.failures) == (False, 1)
        waited = await sp.run(job_id)
        assert waited["waiting_for_horizon"] is True
        assert plain.requests == [], "no retry before the horizon"
        await assert_clean(sp.db)
        before = await deployment(sp.db, day)
        await _past_horizon(sp, job_id, old)
        assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "presumed"
        assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "skipped"
        record = await _attempt_row(sp, job_id, old)
        assert (
            record.state,
            record.requested,
            record.used,
            record.basis,
            record.cost,
            record.outcome,
            record.send_state,
        ) == ("settled", 1, 1, "presumed", "unknown", "presumed", "ambiguous")
        after = await deployment(sp.db, day)
        assert (after["used"], after["reserved"]) == (before["used"] + 1, before["reserved"] - 1)
        await assert_clean(sp.db)
        ran = await sp.run(job_id)
        assert ran["image_job"] == "promoted", ran
        retry = await _token(sp, job_id)
        assert len(plain.requests) == 1
        assert _counts(await _record(sp, job_id, retry)) == ("settled", 1, 1, 0, 0)
        assert [str(a.source_attempt_token) for a in await _assets_of(sp, job_id)] == [retry]
        await assert_clean(sp.db)

        # -- an async submit killed before its acknowledgement commits: ambiguous too --
        await configure_adapter(sp, faults.IMAGE_ADAPTER_ASYNC)
        acked.script.append(Outputs(1))
        job_id = await sp.insert(_story())
        old = await _token(sp, job_id)
        await _killed(sp, "images.async.before_ack_commit", job_id)
        record = await _attempt_row(sp, job_id, old)
        assert (record.send_state, record.state) == ("ambiguous", "held")
        assert record.capabilities_snapshot["submission"] == "async"
        await _past_horizon(sp, job_id, old)
        assert (await sp.run(job_id))["image_job"] == "promoted"
        assert (await _attempt_row(sp, job_id, old)).basis == "presumed"
        await assert_clean(sp.db)

        # -- killed before `sending`: a retry with no presumption and no ambiguity --
        await configure_adapter(sp, faults.IMAGE_ADAPTER)
        plain.script.append(Outputs(1))
        job_id = await sp.insert(_story())
        old = await _token(sp, job_id)
        ambiguous = await _ambiguous_sends(sp)
        assert await _expire_lease(sp, job_id)
        assert (await _attempt_row(sp, job_id, old)).send_state == "prepared"
        assert (await _jobs_row(sp, job_id)).failures == 0
        ran = await sp.run(job_id)
        assert ran["image_job"] == "promoted", ran
        record = await _attempt_row(sp, job_id, old)
        assert (record.state, record.basis, record.outcome, record.used) == (
            "released",
            "actual",
            "discarded",
            0,
        )
        assert await _ambiguous_sends(sp) == ambiguous
        await assert_clean(sp.db)


async def _presumed_then_actual(sp: Space) -> None:
    """A ``sync`` stub without ``idempotency_keys`` whose call stalls past the lease: the
    in-process run is the stalled worker, the reaper fences it, and its late delivery is the
    actual result."""
    from epistrel.core.images import limits as image_limits
    from tests.image_support import STUB_COST, Stall

    stub = StubImageGenerator(script=[])
    catalog = CatalogTarget()
    with (
        registered(ADAPTER, stub),
        registered_target(),
        registered_target(catalog, kind=ImageJobKind.CATALOG),
        installed_checks(ScriptedChecks(fail_closed=False)),
    ):
        await sp.configure()
        day = today()

        # -- requested 2, presumed at the horizon, retried; then 1 arrives: corrected once --
        gate = asyncio.Event()
        stub.script.extend([Stall(gate, Outputs(1)), Outputs(2)])
        job_id = await sp.insert(_catalog(new_ulid(), 2))
        task, old = await _stalled_start(sp, stub, job_id)
        record = await _attempt_row(sp, job_id, old)
        assert (record.send_state, record.state) == ("ambiguous", "held")
        assert (await sp.run(job_id))["waiting_for_horizon"] is True
        before = await deployment(sp.db, day)
        await _past_horizon(sp, job_id, old)
        assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "presumed"
        assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 2, 0, 0)
        assert (await _attempt_row(sp, job_id, old)).cost == "unknown"
        presumed = await deployment(sp.db, day)
        assert (presumed["used"], presumed["reserved"]) == (
            before["used"] + 2,
            before["reserved"] - 2,
        )
        await assert_clean(sp.db)
        ran = await sp.run(job_id)
        assert ran["image_job"] == "candidates_ready", ran
        retry = await _token(sp, job_id)
        kept = list((await sp.lib.get_image_job(CTX, job_id)).result_ids)
        assert _counts(await _record(sp, job_id, retry)) == ("settled", 2, 2, 0, 0)
        mid = await deployment(sp.db, day)
        late_before = _metric("late_after_presumed")
        gate.set()
        late = await asyncio.wait_for(task, 30)
        assert late == {"image_job": "fenced", "late": "late_after_presumed"}, late
        assert _metric("late_after_presumed") == late_before + 1
        record = await _attempt_row(sp, job_id, old)
        assert (record.basis, record.used, record.overage, record.extra_outputs) == (
            "corrected",
            1,
            0,
            0,
        )
        assert (record.cost, record.outcome, record.outputs_returned) == (
            _cost(STUB_COST),
            "late_after_presumed",
            1,
        )
        after = await deployment(sp.db, day)
        assert (after["used"], after["overage"], after["extra_outputs"], after["reserved"]) == (
            mid["used"] - 1,
            mid["overage"],
            mid["extra_outputs"],
            mid["reserved"],
        )
        assert _counts(await _record(sp, job_id, retry)) == ("settled", 2, 2, 0, 0)
        view = await sp.lib.get_image_job(CTX, job_id)
        assert (view.status, list(view.result_ids)) == ("candidates_ready", kept)
        assert staged(sp.lib.media, job_id) == []
        await assert_clean(sp.db)
        again = await attempts.settle_attempt(sp.db, job_id, old, 1, cost=STUB_COST)
        assert again is not None
        assert again.value == "late_duplicate"
        assert await deployment(sp.db, day) == after
        assert (await _attempt_row(sp, job_id, old)).basis == "corrected"
        await assert_clean(sp.db)

        # -- 3 for requested 2: used 3, overage 1, extra_outputs 1 --
        gate = asyncio.Event()
        stub.script.append(Stall(gate, Outputs(3)))
        job_id = await sp.insert(_catalog(new_ulid(), 2))
        task, old = await _stalled_start(sp, stub, job_id)
        await _past_horizon(sp, job_id, old)
        assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "presumed"
        await image_jobs.cancel_job(sp.lib.image_runtime(), job_id)
        presumed = await deployment(sp.db, day)
        gate.set()
        assert (await asyncio.wait_for(task, 30))["late"] == "late_after_presumed"
        assert _counts(await _record(sp, job_id, old)) == ("settled", 2, 3, 1, 1)
        after = await deployment(sp.db, day)
        assert (after["used"], after["overage"], after["extra_outputs"], after["reserved"]) == (
            presumed["used"] + 1,
            presumed["overage"] + 1,
            presumed["extra_outputs"] + 1,
            presumed["reserved"],
        )
        await assert_clean(sp.db)

        # -- the presumed usage ends the retry `cap_reached`; it stays ended after the correction --
        used = (await deployment(sp.db, day))["used"]
        await sp.set(image_limit_deployment_day=used + 2)
        gate = asyncio.Event()
        stub.script.append(Stall(gate, Outputs(1)))
        job_id = await sp.insert(_catalog(new_ulid(), 2))
        task, old = await _stalled_start(sp, stub, job_id)
        await _past_horizon(sp, job_id, old)
        ended = await sp.run(job_id)
        assert ended == {"image_job": "cap_reached", "reason": "image_limit_reached"}, ended
        assert (await _attempt_row(sp, job_id, old)).basis == "presumed"
        gate.set()
        assert (await asyncio.wait_for(task, 30))["late"] == "late_after_presumed"
        assert (await _attempt_row(sp, job_id, old)).used == 1
        assert (await sp.lib.get_image_job(CTX, job_id)).status == "cap_reached"
        await assert_clean(sp.db)
        await sp.set(image_limit_deployment_day=200)

        # -- an ambiguous attempt that delivers before its horizon is never presumed --
        gate = asyncio.Event()
        stub.script.append(Stall(gate, Outputs(1)))
        job_id = await sp.insert(_catalog(new_ulid(), 2))
        task, old = await _stalled_start(sp, stub, job_id)
        assert (await _attempt_row(sp, job_id, old)).send_state == "ambiguous"
        gate.set()
        late = await asyncio.wait_for(task, 30)
        assert late == {"image_job": "fenced", "late": "late_discarded"}, late
        record = await _attempt_row(sp, job_id, old)
        assert (record.state, record.used, record.basis, record.send_state) == (
            "settled",
            1,
            "actual",
            "returned",
        )
        await _past_horizon(sp, job_id, old)
        assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "skipped"
        assert (await _attempt_row(sp, job_id, old)).basis == "actual", "never presumed"
        await image_jobs.cancel_job(sp.lib.image_runtime(), job_id)
        await assert_clean(sp.db)

        # -- cancelled, or its target lost, while ambiguous and before its horizon: the record
        # stays held until the horizon and is presumed there, never released (#799) --
        for ending in (ImageJobStatus.CANCELLED, ImageJobStatus.TARGET_GONE):
            gate = asyncio.Event()
            stub.script.append(Stall(gate, Outputs(1)))
            job_id = await sp.insert(_catalog(new_ulid(), 2))
            task, old = await _stalled_start(sp, stub, job_id)
            record = await _attempt_row(sp, job_id, old)
            assert (record.send_state, record.state) == ("ambiguous", "held")
            runtime = sp.lib.image_runtime()
            if ending is ImageJobStatus.CANCELLED:
                await image_jobs.cancel_job(runtime, job_id)
            else:
                assert await image_jobs.end_job(runtime, job_id, ending)
            assert (await sp.lib.get_image_job(CTX, job_id)).status == ending.value
            record = await _attempt_row(sp, job_id, old)
            assert (record.send_state, record.state) == ("ambiguous", "held"), ending
            await assert_clean(sp.db)
            await _past_horizon(sp, job_id, old)
            assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "presumed"
            record = await _attempt_row(sp, job_id, old)
            assert (record.state, record.basis, record.used, record.send_state) == (
                "settled",
                "presumed",
                2,
                "ambiguous",
            ), ending
            gate.set()
            assert (await asyncio.wait_for(task, 30))["late"] == "late_after_presumed"
            await assert_clean(sp.db)

        # -- after retention purged its records: late_unaccounted, no counter changes --
        gate = asyncio.Event()
        stub.script.append(Stall(gate, Outputs(1)))
        job_id = await sp.insert(_catalog(new_ulid(), 2))
        task, old = await _stalled_start(sp, stub, job_id)
        await _past_horizon(sp, job_id, old)
        assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "presumed"
        await image_jobs.cancel_job(sp.lib.image_runtime(), job_id)
        purged = await image_limits.retain(sp.db, now=datetime.now(UTC) + timedelta(days=400))
        assert purged["records"] >= 1
        assert await records(sp.db, job_id) == []
        kept_counters = await counters(sp.db)
        unaccounted = _metric("late_unaccounted")
        gate.set()
        late = await asyncio.wait_for(task, 30)
        assert late == {"image_job": "fenced", "late": "late_unaccounted"}, late
        assert _metric("late_unaccounted") == unaccounted + 1
        assert await counters(sp.db) == kept_counters
        # #269: its cost is recorded on a row of its own, on its recorded_at day.
        cost = await _one(
            sp,
            "SELECT cost_status, cost, accounting, outputs_billed, entry_id IS NOT NULL AS e "
            "FROM image_costs WHERE job_id = :j AND attempt_token = :t",
            j=job_id,
            t=old,
        )
        assert tuple(cost) == ("known", STUB_COST.amount, "late_unaccounted", 1, True)
        await assert_clean(sp.db)


async def _submission_modes(sp: Space) -> None:
    """``async`` stubs stalled in ``collect``: a known request, recovered through ``status``
    when the stub confirms completion and held to its horizon when it does not; never an
    ambiguous send."""
    from tests.image_support import Stall, StatusSeq

    plain = StubImageGenerator(submission="async", script=[])
    confirming = StubImageGenerator(submission="async", confirms_completion=True, script=[])
    with (
        registered(ADAPTER, StubImageGenerator(script=[])),
        registered(PLAIN_ASYNC_ADAPTER, plain),
        registered(ASYNC_ADAPTER, confirming),
        registered_target(),
        installed_checks(ScriptedChecks(fail_closed=False)),
    ):
        await sp.configure()

        # -- non-confirming: held to its horizon, released there, then the retry --
        await configure_adapter(sp, PLAIN_ASYNC_ADAPTER)
        gate = asyncio.Event()
        plain.script.extend([Stall(gate, Outputs(1)), Outputs(1)])
        job_id = await sp.insert(_story())
        task, old = await _stalled_start(sp, plain, job_id)
        record = await _attempt_row(sp, job_id, old)
        assert (record.send_state, record.state) == ("accepted", "held")
        assert record.capabilities_snapshot == {"submission": "async", "idempotency_keys": False}
        assert (await _jobs_row(sp, job_id)).failures == 0, "a known request is no failure"
        assert (await sp.run(job_id))["waiting_for_horizon"] is True
        await _past_horizon(sp, job_id, old)
        assert await attempts.horizon_reached(sp.db, f"{job_id}:{old}") == "released"
        record = await _attempt_row(sp, job_id, old)
        assert (record.send_state, record.basis, record.outcome) == (
            "accepted",
            "actual",
            "released_at_horizon",
        )
        assert (await sp.run(job_id))["image_job"] == "promoted"
        task.cancel()
        assert await _ambiguous_sends(sp) == 0
        await assert_clean(sp.db)

        # -- confirming: recovered through status --
        await configure_adapter(sp, ASYNC_ADAPTER)
        gate = asyncio.Event()
        confirming.script.append(Stall(gate, StatusSeq(["succeeded"], Outputs(1))))
        job_id = await sp.insert(_story())
        task, old = await _stalled_start(sp, confirming, job_id)
        assert (await _attempt_row(sp, job_id, old)).send_state == "accepted"
        await _past_horizon(sp, job_id, old)
        assert (await sp.run(job_id))["image_job"] == "promoted"
        record = await _attempt_row(sp, job_id, old)
        assert (record.outcome, record.basis) == ("adopted", "actual")
        task.cancel()
        assert await _ambiguous_sends(sp) == 0
        await assert_clean(sp.db)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11; NFR-REL-5")
@pytest.mark.parametrize("group", AMBIGUOUS_GROUPS)
async def test_i_26__m8_ambiguous(pg_url: str, tmp_path: Path, group: str) -> None:
    """I-26's "Ambiguous sends", "Presumed, then the actual result" and "Submission modes"
    clauses (FR-IMG-11 part C; NFR-REL-5's recovery of attempts left in ``sending``), each group
    on a scratch space of its own:

    - ``crash_recovery``: a child engine killed at ``images.after_sending_commit`` with an
      adapter declaring ``idempotency_keys`` → the attempt (its key ``sha256(job:token)``, its
      request and capabilities snapshots committed before ``submit``) is re-sent at once under
      a new token with the same key, on a row with ``resend_of``, ``requested = 0`` and no
      ``counter_keys``; its output counts once on the original record (``resent``) and is
      promoted under the new token; a deduplicating stub answers a stalled call and its
      re-send with one result, counted once; the media source triple refuses a second
      promotion. Without the capability → ``ambiguous``, held to its horizon, ``presumed``
      there (``used = requested``, cost ``unknown``), then retried; the deployment's
      ambiguous sends + 1. An async ``submit`` killed at ``images.async.before_ack_commit`` is
      ambiguous too. A worker lost before ``sending`` retries with no presumption.
    - ``presumed_then_actual``: a sync stall with ``requested = 2`` is ambiguous, presumed at
      its horizon, retried; its late single output corrects the record once (``corrected``,
      ``used = 1``, the counter's ``used`` down 1, ``reserved`` unchanged,
      ``late_after_presumed``), nothing promoted and the retry undisturbed; again →
      ``late_duplicate``; 3 for 2 → ``used = 3``, ``overage = extra_outputs = 1``; a job the
      presumption ended ``cap_reached`` stays ended; a delivery before the horizon is
      ``late_discarded`` and never presumed; a job cancelled or ended ``target_gone`` while its
      attempt is ambiguous keeps the record ``held`` until the horizon, where it is presumed,
      not released (#799); after retention → ``late_unaccounted``.
    - ``submission_modes``: an async stall in ``collect`` is ``accepted`` (a known request):
      held to its horizon and released when the stub does not confirm, adopted through
      ``status`` when it does; never ambiguous.

    The reconciler reports no drift after every step. Complement asserted: an idempotent
    re-send is no ambiguous send, a known request counts no failure, and a pre-horizon
    delivery is never presumed."""
    async with space(pg_url, tmp_path, f"i26_amb_{group[:8]}") as sp:
        await {
            "crash_recovery": _crash_recovery,
            "presumed_then_actual": _presumed_then_actual,
            "submission_modes": _submission_modes,
        }[group](sp)
