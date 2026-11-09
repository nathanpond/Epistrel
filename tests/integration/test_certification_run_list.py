"""``GET /certification-runs`` (FR-PROF-17; #776, ADR-183): the certification runs, requested
and canary, newest first, each with a canary's top-level ``outcome``.

The canaries are driven by the scheduler hook (the recurring canary task enqueued and run), on
the stub adapter, as C-18 M7's stale test drives them: one that holds every floor, then one the
stub is scripted to miss. The runs share one scratch database (a certified Full would change
what every other test's Full story reports).
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text

from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest
from epistrel.core.seeds.profiles import FULL_PRESET_ID, LITE_PRESET_ID
from epistrel.evals.suites.smoke import SmokeSuite
from epistrel.orchestrator.models.registry import GENERATION
from tests.integration.test_c_18_m7 import (
    CANARY_ON,
    READ,
    RUN,
    WRITE,
    WRONG_WORD,
    _canary,
    _certify,
    _on_full,
    ok,
    scratch,
)

pytestmark = pytest.mark.integration

RUNS = "/certification-runs"
OTHER_FINGERPRINT = "sha256:" + "0" * 64
NO_SUCH = "01J00000000000000000000000"


async def _list(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    return await ok(await client.get(RUNS, params=params, headers=READ))


async def _ids(client: httpx.AsyncClient, **params: Any) -> list[str]:
    return [item["id"] for item in (await _list(client, **params))["items"]]


async def _refused_at(client: httpx.AsyncClient, params: dict[str, Any]) -> str:
    response = await client.get(RUNS, params=params, headers=READ)
    assert response.status_code == 422, (params, response.text)
    error = response.json()["error"]
    assert error["code"] == "validation_error", error
    field: str = error["details"]["field"]
    return field


async def test_certification_runs__list(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the canary on and Full certified and in use, two canary intervals (held, then
    missed) list newest first with their outcomes; a missed canary's id is the stale
    certification's ``invalidated_by_run_id``. ``kind`` is exclusive both ways, filters AND,
    a repeated ``kind``/``status`` matches any value, ``outcome`` is null on a certification run
    and on an unfinished run (even with a value planted in the column), paging walks every run
    once and ends with a null ``next_cursor``, a run outside the request's filters is never
    listed, and an invalid filter is 422 ``validation_error`` at its field."""
    async with scratch(pg_url, "epistrel_certification_run_list") as (client, lib, db):
        await ok(
            await client.put(
                "/admin/model-config/generation", json={"model": "stub-model"}, headers=WRITE
            )
        )
        story, _ = await _on_full(client)
        other, _ = await _on_full(client)

        first = await _certify(client, db)
        (requested,) = await _ids(client)
        fingerprint = (await ok(await client.get(f"{RUNS}/{requested}", headers=READ)))[
            "fingerprint"
        ]
        await ok(await client.put("/admin/settings", json=CANARY_ON, headers=WRITE))

        held = await _canary(db)
        assert held["outcome"] == "held"
        with monkeypatch.context() as patched:
            patched.setattr(
                SmokeSuite, "stub_answers", lambda self, case: {GENERATION.name: WRONG_WORD}
            )
            missed = await _canary(db)
        assert missed["outcome"] == "missed"
        await ok(
            await client.put(
                "/admin/settings", json={"certification_canary_enabled": False}, headers=WRITE
            )
        )

        # A story's run, queued and never played: unfinished.
        queued = (
            await ok(
                await client.post(
                    f"/stories/{story}/certification-runs",
                    json={**RUN, "confirm": True},
                    headers=WRITE,
                ),
                202,
            )
        )["run_id"]
        newest_first = [queued, missed["id"], held["id"], requested]
        assert newest_first == sorted(newest_first, reverse=True)

        # Two canary intervals, newest first, each with its outcome.
        canaries = await _list(client, fingerprint=fingerprint, kind="canary")
        assert [(i["id"], i["kind"], i["status"], i["outcome"]) for i in canaries["items"]] == [
            (missed["id"], "canary", "completed", "missed"),
            (held["id"], "canary", "completed", "held"),
        ]
        assert canaries["next_cursor"] is None

        # The list serves the single read's body.
        everything = await _list(client)
        assert [i["id"] for i in everything["items"]] == newest_first
        for item in everything["items"]:
            assert item == await ok(await client.get(f"{RUNS}/{item['id']}", headers=READ))

        # A missed canary's id is the stale certification's invalidated_by_run_id.
        record = await ok(await client.get(f"/certifications/{first}", headers=READ))
        assert (record["status"], record["invalidated_by_run_id"]) == ("stale", missed["id"])

        # kind is exclusive both ways; repeated, it matches either.
        assert await _ids(client, kind="certification") == [queued, requested]
        assert await _ids(client, kind="canary") == [missed["id"], held["id"]]
        assert await _ids(client, kind=["canary", "certification"]) == newest_first

        # status, repeated; and filters AND together.
        assert await _ids(client, status="queued") == [queued]
        assert await _ids(client, status=["queued", "completed"]) == newest_first
        assert await _ids(client, status="running") == []
        assert await _ids(client, kind="certification", status="completed") == [requested]
        assert await _ids(client, kind="canary", status="queued") == []
        assert await _ids(client, fingerprint=fingerprint, status="failed") == []
        assert await _ids(client, story_id=story) == [queued]
        assert await _ids(client, story_id=story, kind="canary") == []
        assert await _ids(client, profile_id=FULL_PRESET_ID) == newest_first
        assert await _ids(client, profile_id=FULL_PRESET_ID, fingerprint=fingerprint) == (
            newest_first
        )

        # A run outside the request's scope is never listed.
        assert await _ids(client, fingerprint=OTHER_FINGERPRINT) == []
        assert await _ids(client, profile_id=LITE_PRESET_ID) == []
        assert await _ids(client, story_id=other) == []
        assert await _ids(client, story_id=NO_SUCH) == []
        assert (await client.get(RUNS)).status_code == 401
        assert (
            await client.get(RUNS, headers={"Authorization": "Bearer not-a-credential"})
        ).status_code == 401

        # outcome: only a completed canary's. A value planted on a certification run and on an
        # unfinished run is never served.
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE certification_runs SET outcome = 'held' WHERE id IN (:a, :b)"),
                {"a": requested, "b": queued},
            )
        for run in (requested, queued):
            single = await ok(await client.get(f"{RUNS}/{run}", headers=READ))
            assert ("outcome", None) in single.items(), single
        by_id = {i["id"]: i["outcome"] for i in (await _list(client))["items"]}
        assert by_id == {
            queued: None,
            missed["id"]: "missed",
            held["id"]: "held",
            requested: None,
        }

        # Paging: one at a time walks every run once; past the last page next_cursor is null.
        walked: list[str] = []
        cursor: str | None = None
        while True:
            page = await _list(client, limit=1, **({"cursor": cursor} if cursor else {}))
            walked += [i["id"] for i in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert cursor == walked[-1]
        assert walked == newest_first
        exact = await _list(client, limit=len(newest_first))
        assert ([i["id"] for i in exact["items"]], exact["next_cursor"]) == (newest_first, None)
        short = await _list(client, limit=2)
        assert ([i["id"] for i in short["items"]], short["next_cursor"]) == (
            newest_first[:2],
            newest_first[1],
        )
        assert await _ids(client, cursor=requested) == []

        # An invalid filter is 422 validation_error at its field.
        for params, field in (
            ({"kind": "recertify"}, "query.kind"),
            ({"kind": ["canary", "bogus"]}, "query.kind"),
            ({"status": "done"}, "query.status"),
            ({"fingerprint": "sha256:abc"}, "query.fingerprint"),
            ({"profile_id": "full"}, "query.profile_id"),
            ({"story_id": "not-a-ulid"}, "query.story_id"),
            ({"cursor": "nope"}, "query.cursor"),
            ({"limit": 0}, "query.limit"),
            ({"limit": 201}, "query.limit"),
        ):
            assert (await _refused_at(client, params)).startswith(field), params
        assert len((await _list(client, limit=200))["items"]) == len(newest_first)

        # The library refuses an unknown kind or status at the same field.
        with pytest.raises(InvalidRequest) as refused:
            await lib.list_certification_runs(RequestContext(), kind=["recertify"])
        assert refused.value.details["field"] == "query.kind"
        with pytest.raises(InvalidRequest) as refused:
            await lib.list_certification_runs(RequestContext(), status=["done"])
        assert refused.value.details["field"] == "query.status"
