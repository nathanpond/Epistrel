"""Image usage and cost reports beyond C-24 and I-23's parts (FR-IMG-11; #269): the story
rollup after the story's deletion, the report ranges and retention, the entry report and its
limit, the one fill-in, and the cost values' rules. Each test runs on a scratch database
(``tests.image_limits_support.space``): the reports are deployment-wide."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from epistrel.core import clock, stories
from epistrel.core.bundles import NEVER
from epistrel.core.images import costs, limits
from epistrel.core.stories import STORY_ID_EXCEPTIONS
from epistrel.core.tables.images import CostStatus, ImageJobKind
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.port import Cost
from epistrel.testing.images import ScriptedChecks, installed_checks, registered_target
from tests import catalog_support
from tests.auth_support import bearer
from tests.event_support import new_story
from tests.image_job_support import prepared
from tests.image_limits_support import CatalogTarget, Space, space
from tests.image_support import Outputs, StubImageGenerator, catalog_origin, registered

ADAPTER = "stub_sync"
USD = Cost(Decimal("0.04"), "USD")


def _today() -> date:
    return clock.now().astimezone(UTC).date()


async def _usage(sp: Space, path: str, **params: Any) -> Any:
    return await sp.client.get(path, params=params, headers=bearer())


async def _story_rows(sp: Space, job_id: str) -> list[Any]:
    async with sp.db.connect() as conn:
        return list(
            await conn.execute(
                text("SELECT story_id, cost FROM image_costs WHERE job_id = :j"), {"j": job_id}
            )
        )


async def test_usage_after_story_delete(pg_url: str, tmp_path: Path) -> None:
    """A story's deletion keeps its cost rows with ``story_id`` nulled (owner decision 13):
    ``GET /admin/image-usage`` still counts its costs, and ``GET /stories/{id}/image-usage``
    answers 404. ``image_costs`` is a ``STORY_ID_EXCEPTIONS`` table and never exported.

    Complement asserted: before the deletion the story's report answers 200 with the cost."""
    assert "image_costs" in STORY_ID_EXCEPTIONS
    assert "image_costs" in NEVER
    async with space(pg_url, tmp_path, "usage_delete") as sp:
        story = await new_story(sp.db)
        stub = StubImageGenerator(script=[Outputs(cost=USD)])
        with (
            registered(ADAPTER, stub),
            registered_target(),
            installed_checks(ScriptedChecks(fail_closed=False)),
        ):
            await sp.configure()
            job = await sp.insert(prepared(story))
            assert (await sp.run(job))["image_job"] == "promoted"
        before = await _usage(sp, f"/stories/{story}/image-usage")
        assert before.status_code == 200, before.text
        assert before.json()["all_time"]["costs"][0]["known_total"] == "0.04000000"

        await stories.delete_story(sp.db, story, principal="p-images")
        assert [tuple(r) for r in await _story_rows(sp, job)] == [(None, Decimal("0.04"))]
        gone = await _usage(sp, f"/stories/{story}/image-usage")
        assert gone.status_code == 404, gone.text
        assert gone.json()["error"]["code"] == "not_found"
        today = _today().isoformat()
        admin = (await _usage(sp, "/admin/image-usage", **{"from": today, "to": today})).json()
        assert admin["totals"]["costs"] == [
            {"currency": "USD", "known_total": "0.04000000", "known_attempts": 1}
        ]
        assert admin["totals"]["outputs_used"] == 1


async def test_usage_ranges(pg_url: str, tmp_path: Path) -> None:
    """The admin report's range: by default the last 30 days through today, every day listed
    with zeros; ``from`` after ``to`` and a range over 366 days are 422 ``invalid_range``; days
    the accounting retention purged are left out (``retained_from``).

    Complement asserted: a 366-day range is accepted."""
    async with space(pg_url, tmp_path, "usage_range") as sp:
        await sp.set(deployment_time_zone="UTC")
        today = _today()
        default = await _usage(sp, "/admin/image-usage")
        assert default.status_code == 200, default.text
        body = default.json()
        assert (body["from"], body["to"]) == (
            (today - timedelta(days=29)).isoformat(),
            today.isoformat(),
        )
        assert len(body["days"]) == 30
        assert body["days"][0] == {
            "date": body["from"],
            "outputs_used": 0,
            "overage": 0,
            "extra_outputs": 0,
            "reserved": 0,
            "ambiguous_sends": 0,
            "cost_unknown_attempts": 0,
            "cost_zero_attempts": 0,
            "costs": [],
        }
        for params in (
            {"from": today.isoformat(), "to": (today - timedelta(days=1)).isoformat()},
            {"from": (today - timedelta(days=366)).isoformat(), "to": today.isoformat()},
        ):
            refused = await _usage(sp, "/admin/image-usage", **params)
            assert refused.status_code == 422, (params, refused.text)
            assert refused.json()["error"]["code"] == "invalid_range"
        year = {"from": (today - timedelta(days=365)).isoformat(), "to": today.isoformat()}
        assert (await _usage(sp, "/admin/image-usage", **year)).status_code == 200

        await sp.set(image_accounting_retention_days=3)
        kept = (
            await _usage(
                sp,
                "/admin/image-usage",
                **{"from": (today - timedelta(days=10)).isoformat(), "to": today.isoformat()},
            )
        ).json()
        assert kept["retained_from"] == (today - timedelta(days=3)).isoformat()
        assert kept["days"][0]["date"] == kept["retained_from"]
        assert len(kept["days"]) == 4


async def test_cost_retention(pg_url: str, tmp_path: Path) -> None:
    """The accounting retention purges cost rows by ``period_date`` (no state condition), with
    the records.

    Complement asserted: a retention run inside the window keeps the row."""
    async with space(pg_url, tmp_path, "usage_retain") as sp:
        story = await new_story(sp.db)
        with (
            registered(ADAPTER, StubImageGenerator(script=[Outputs(cost=USD)])),
            registered_target(),
            installed_checks(ScriptedChecks(fail_closed=False)),
        ):
            await sp.configure()
            job = await sp.insert(prepared(story))
            assert (await sp.run(job))["image_job"] == "promoted"
        assert (await limits.retain(sp.db))["costs"] == 0
        assert len(await _story_rows(sp, job)) == 1
        purged = await limits.retain(sp.db, now=datetime.now(UTC) + timedelta(days=400))
        assert purged["costs"] == 1
        assert await _story_rows(sp, job) == []


async def test_entry_usage(pg_url: str, tmp_path: Path) -> None:
    """``GET /catalog/characters/{id}/image-usage``: the entry's candidate jobs' usage and
    cost, all-time and by day, and today's entry limit; an unknown entry, and a character's id
    read as a scenario, are 404.

    Complement asserted: another entry's usage is not included."""
    async with space(pg_url, tmp_path, "usage_entry") as sp:
        created = await catalog_support.create(sp.client)
        other = await catalog_support.create(sp.client, catalog_support.character("Tamsin Oake"))
        stub = StubImageGenerator(script=[Outputs(n=2, cost=USD), Outputs(cost=USD)])
        with (
            registered(ADAPTER, stub),
            registered_target(CatalogTarget(), kind=ImageJobKind.CATALOG),
            installed_checks(ScriptedChecks(fail_closed=False)),
        ):
            await sp.configure()
            await sp.set(image_limit_entry_day=10)
            for entry, n in ((created["id"], 2), (other["id"], 1)):
                job = await sp.insert(
                    prepared(kind=ImageJobKind.CATALOG, origin=catalog_origin(entry), n=n)
                )
                assert (await sp.run(job))["image_job"] == "candidates_ready"
        found = await _usage(sp, f"/catalog/characters/{created['id']}/image-usage")
        assert found.status_code == 200, found.text
        body = found.json()
        assert (body["entry_kind"], body["entry_id"]) == ("character", created["id"])
        assert body["all_time"]["outputs_used"] == 2
        assert body["all_time"]["costs"] == [
            {"currency": "USD", "known_total": "0.04000000", "known_attempts": 1}
        ]
        assert body["limits"] == {
            "image_limit_entry_day": 10,
            "today_used": 2,
            "today_reserved": 0,
            "today_remaining": 8,
        }
        assert [d["outputs_used"] for d in body["days"]] == [2]
        for path in (
            "/catalog/characters/01JZZZZZZZZZZZZZZZZZZZZZZZ/image-usage",
            f"/catalog/scenarios/{created['id']}/image-usage",
        ):
            missing = await _usage(sp, path)
            assert missing.status_code == 404, (path, missing.text)


async def test_cost_fill_in_once(pg_url: str, tmp_path: Path) -> None:
    """An ``unknown`` cost is filled in once, to known (or zero), keeping its ``period_date``;
    after that no return changes it.

    Complement asserted: the first known report changes the row, the second does not."""
    async with space(pg_url, tmp_path, "usage_fill") as sp:
        story = await new_story(sp.db)
        with registered(ADAPTER, StubImageGenerator(script=[])), registered_target():
            await sp.configure()
            job = await sp.insert(prepared(story))
        async with sp.db.connect() as conn:
            token = str(
                await conn.scalar(
                    text("SELECT attempt_token FROM image_jobs WHERE id = :j"), {"j": job}
                )
            )
        async with sp.db.begin() as conn:
            record = await limits.read_record(conn, job, token)
            assert record is not None
            assert await costs.record_attempt(conn, record, costs.UNKNOWN, None) == "recorded"
            assert await costs.record_attempt(conn, record, costs.UNKNOWN, 2) == "unchanged"
            assert (
                await costs.record_attempt(conn, record, costs.known("0.04", "USD"), 2) == "filled"
            )
            assert (
                await costs.record_attempt(conn, record, costs.known("9", "USD"), 2) == "unchanged"
            )
            assert await costs.record_attempt(conn, record, costs.ZERO, 0) == "unchanged"
            row = (
                await conn.execute(
                    text(
                        "SELECT cost, cost_status, outputs_billed, filled_at IS NOT NULL AS f, "
                        "period_date FROM image_costs WHERE job_id = :j"
                    ),
                    {"j": job},
                )
            ).one()
        assert tuple(row) == (Decimal("0.04"), "known", 2, True, record.counter_keys[0].period)
        await image_jobs.cancel_job(sp.lib.image_runtime(), job)


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("0.123456785", "usd", costs.CostEntry(CostStatus.KNOWN, Decimal("0.12345678"), "USD")),
        ("0.123456775", "USD", costs.CostEntry(CostStatus.KNOWN, Decimal("0.12345678"), "USD")),
        (Decimal(0), "EUR", costs.CostEntry(CostStatus.KNOWN, Decimal("0E-8"), "EUR")),
        ("-1", "USD", costs.UNKNOWN),
        ("nan", "USD", costs.UNKNOWN),
        ("1e12", "USD", costs.UNKNOWN),
        ("0.5", "dollars", costs.UNKNOWN),
        ("0.5", None, costs.UNKNOWN),
    ],
)
def test_cost_values(amount: Any, currency: Any, expected: costs.CostEntry) -> None:
    """Adapter amounts are rounded half-even to 8 decimals and currencies upper-cased; anything
    unusable is ``unknown``, never 0. A refusal before generation is ``zero``; any other error,
    and an ambiguous one, ``unknown``."""
    assert costs.known(amount, currency) == expected
    assert costs.of_error(402, ambiguous=False) == costs.ZERO
    assert costs.of_error(500, ambiguous=False) == costs.UNKNOWN
    assert costs.of_error(429, ambiguous=True) == costs.UNKNOWN
    assert costs.money(Decimal("0E-8")) == "0.00000000"
