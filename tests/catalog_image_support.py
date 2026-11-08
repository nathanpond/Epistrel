"""Helpers for the catalog image candidate tests (#260): the REST app with the stub model
adapter, a stub image adapter and scripted checks, a character ready to request images for, the
candidate routes, and row reads.

Each test's outputs get a size of their own (``Outputs(size=…)``), so no two tests share a
checksum on the shared database: whether bytes are kept is then each test's own business."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.testing.images import ScriptedChecks, installed_checks
from tests import image_prompt_support as ip
from tests.catalog_support import WRITE
from tests.image_job_support import configure
from tests.image_support import Outputs, StubImageGenerator, registered

ADAPTER = "stub_sync"
CHARACTERS = "/catalog/characters"
SCENARIOS = "/catalog/scenarios"


@dataclass
class CandidateApp:
    client: httpx.AsyncClient
    lib: Engine
    generator: StubImageGenerator
    checks: ScriptedChecks
    settings: Settings
    adapter: bool = True
    checker: bool = True
    configured: bool = False

    async def configure(self) -> None:
        """Configure the image adapter and checker, once: after the reference uploads, which an
        ``image_check`` model would check too."""
        if self.adapter and not self.configured:
            await configure(self.lib, self.settings, ADAPTER, check=self.checker)
        self.configured = True

    def script(self, *steps: Outputs) -> None:
        self.generator.script.extend(steps)


@asynccontextmanager
async def candidate_app(
    settings: Settings, engine: AsyncEngine, *, adapter: bool = True, checker: bool = True
) -> AsyncIterator[CandidateApp]:
    """``ip.prompt_app`` with the stub image adapter (``adapter``) and an ``image_check`` model
    (``checker``) configured by ``CandidateApp.configure`` (``character`` calls it), and scripted
    checks that pass every prompt and output."""
    generator = StubImageGenerator()
    async with ip.prompt_app(settings, engine) as (client, lib):
        with (
            registered(ADAPTER, generator),
            installed_checks(ScriptedChecks(fail_closed=False)) as checks,
        ):
            yield CandidateApp(client, lib, generator, checks, settings, adapter, checker)


async def characters(app: CandidateApp, count: int) -> list[dict[str, Any]]:
    """``count`` complete characters (34, Everyone), each with a visual identity and a styled
    companion, made before the adapter and checker are configured."""
    assert not app.configured, "make every character before configuring"
    made = []
    for _ in range(count):
        seen, hidden = await ip.references(app.client)
        created = await ip.create_character(app.client, ip.character_body(seen, hidden))
        await ip.style_companion(app.client, created["id"])
        made.append(created)
    await app.configure()
    return made


async def character(app: CandidateApp) -> dict[str, Any]:
    """One such character (``characters``)."""
    [created] = await characters(app, 1)
    return created


def base(kind: str) -> str:
    return CHARACTERS if kind == "character" else SCENARIOS


async def generate(
    app: CandidateApp, entry_id: str, *, kind: str = "character", **body: Any
) -> httpx.Response:
    return await app.client.post(
        f"{base(kind)}/{entry_id}/images/generate",
        json={"slot": "primary", **body},
        headers=WRITE,
    )


async def run(app: CandidateApp, job_id: str) -> dict[str, Any]:
    """Run the job's handler directly (no worker claims another test's job)."""
    return await image_jobs.run_image_job(app.lib.image_runtime(), job_id, 0)


async def generated(
    app: CandidateApp, entry_id: str, outputs: Outputs, *, kind: str = "character", **body: Any
) -> tuple[str, list[str]]:
    """Request, run and read a job: ``(job_id, candidate_ids)``."""
    app.script(outputs)
    accepted = await generate(app, entry_id, kind=kind, n=outputs.n, **body)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    done = await run(app, job_id)
    assert done["image_job"] == "candidates_ready", done
    job = await app.client.get(f"/image-jobs/{job_id}", headers=WRITE)
    assert job.status_code == 200, job.text
    return job_id, list(job.json()["candidate_ids"])


async def listed(
    app: CandidateApp, entry_id: str, *, kind: str = "character", **params: Any
) -> dict[str, Any]:
    response = await app.client.get(
        f"{base(kind)}/{entry_id}/images/candidates", params=params, headers=WRITE
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def candidate_path(entry_id: str, candidate_id: str, *, kind: str = "character") -> str:
    return f"{base(kind)}/{entry_id}/images/candidates/{candidate_id}"


async def draft(app: CandidateApp, character_id: str) -> dict[str, Any]:
    response = await app.client.get(
        f"{CHARACTERS}/{character_id}",
        params={"draft": "true", "projection": "full"},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def save(
    app: CandidateApp, character_id: str, revision: int, **groups: Any
) -> httpx.Response:
    return await app.client.patch(
        f"{CHARACTERS}/{character_id}", json={"draft_revision": revision, **groups}, headers=WRITE
    )


async def candidate_rows(engine: AsyncEngine, candidate_ids: list[str]) -> list[Any]:
    """The candidates' rows joined to their assets (``asset_id``, ``state``, ``checksum``,
    ``owner_principal``, ``expires_at``)."""
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT c.id, c.asset_id, c.expires_at, c.slot, m.state, m.checksum, "
                        "m.owner_principal FROM catalog.image_candidates c "
                        "JOIN media_assets m ON m.id = c.asset_id "
                        "WHERE c.id = ANY(CAST(:ids AS text[])) ORDER BY c.id"
                    ),
                    {"ids": candidate_ids},
                )
            ).all()
        )


async def asset(engine: AsyncEngine, asset_id: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id, state, checksum, owner_principal FROM media_assets WHERE id = :i"),
                {"i": asset_id},
            )
        ).one_or_none()


async def expire_at(engine: AsyncEngine, candidate_ids: list[str], offset: str) -> None:
    """Set the candidates' ``expires_at`` to now plus ``offset`` (an SQL interval)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE catalog.image_candidates SET expires_at = now() + CAST(:o AS interval) "
                "WHERE id = ANY(CAST(:ids AS text[]))"
            ),
            {"o": offset, "ids": candidate_ids},
        )


async def image_job_count(engine: AsyncEngine) -> dict[str, int]:
    return await ip.image_rows(engine)


__all__ = [
    "ADAPTER",
    "CHARACTERS",
    "SCENARIOS",
    "CandidateApp",
    "asset",
    "candidate_app",
    "candidate_path",
    "candidate_rows",
    "character",
    "characters",
    "draft",
    "expire_at",
    "generate",
    "generated",
    "image_job_count",
    "listed",
    "run",
    "save",
]
