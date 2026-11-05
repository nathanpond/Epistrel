"""#532: the eval runs over REST (``GET /admin/evals/runs``, ``/{run_id}``)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.profiles import certification
from epistrel.evals.certify import install
from tests.auth_support import auth_settings, bearer
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

READ = bearer()
NEWEST = "74606ed2b224-2026-11-01"
EARLIER = "1cf75dec4046-2026-10-31"


async def test_eval_runs(pg_url: str, engine: AsyncEngine) -> None:
    """The committed reports are served newest first and page by their cursor; one reads by its
    id with its date, model, fingerprint, suites and requirements; an unknown id is 404
    ``eval_run_not_found``. A completed certification run's report is served beside them, so a
    certification's run and this route never disagree."""
    run_id = f"certification-{new_ulid()}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO certification_runs (id, fingerprint, profile_id, profile_version, "
                "suites, seeds, status, report, requested_by) VALUES (:i, :f, :p, 1, "
                "ARRAY['smoke'], 2, 'completed', CAST(:r AS jsonb), 'p-test')"
            ),
            {
                "i": new_ulid(),
                "f": "sha256:" + "d" * 64,
                "p": new_ulid(),
                "r": json.dumps(
                    {
                        "run_id": run_id,
                        "date": "2026-11-02",
                        "model": "stub",
                        "suites": {"smoke": "pass"},
                        "seeds": [1, 2],
                        "sample_size": 10,
                    }
                ),
            },
        )
    try:
        async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
            client,
            lib,
        ):
            install(lib)
            first = (
                await client.get("/admin/evals/runs", params={"limit": 2}, headers=READ)
            ).json()
            served: list[str] = [run["run_id"] for run in first["items"]]
            cursor = first["next_cursor"]
            while cursor:
                page = (
                    await client.get(
                        "/admin/evals/runs", params={"limit": 2, "cursor": cursor}, headers=READ
                    )
                ).json()
                served += [run["run_id"] for run in page["items"]]
                cursor = page["next_cursor"]
            assert run_id in served  # not by page: committed reports may be newer (#235)
            assert NEWEST in served
            assert EARLIER in served
            assert served.index(NEWEST) < served.index(EARLIER)
            assert len(served) == len(set(served))
            one: dict[str, Any] = (
                await client.get(f"/admin/evals/runs/{NEWEST}", headers=READ)
            ).json()
            assert (one["run_id"], one["date"], one["source"]) == (
                NEWEST,
                "2026-11-01",
                "committed",
            )
            assert one["model"]
            assert one["suites"]
            ours = (await client.get(f"/admin/evals/runs/{run_id}", headers=READ)).json()
            assert (ours["source"], ours["seeds"], ours["sample_size"]) == (
                "certification",
                [1, 2],
                10,
            )
            missing = await client.get("/admin/evals/runs/no-such-run", headers=READ)
            assert missing.status_code == 404, missing.text
            assert missing.json()["error"]["code"] == "eval_run_not_found"
    finally:
        certification.install_harness(None)  # the run row stays: certification runs are kept
