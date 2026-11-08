"""Helpers for the image job tests (#256): an Engine over the shared database with its own
media root, the image configuration, job inserts that skip the enqueue checks, and reads of
the store and staging."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.ids import new_ulid
from epistrel.core.images import store as image_store
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.jobs import ImageJobRequest, PreparedImageJob
from epistrel.testing import crash, faults
from epistrel.testing.images import PngImageGenerator
from tests.auth_support import CREDENTIAL_A, auth_settings, bearer
from tests.image_support import IMAGE_TABLES_CLEANUP, registered, story_origin
from tests.model_support import (
    CONFIG_KEY,
    admin_client,
    config_key,
    put,
    put_key,
    restored_model_config,
)

PRINCIPAL = "p-images"
CTX = RequestContext(principal=PRINCIPAL)
HEADERS = {**bearer(), "X-Epistrel-Principal": PRINCIPAL}
PROMPT = "a lighthouse at dusk"
MINORS = "minors_no_sexual_content"


def image_settings_for(pg_url: str, tmp_path: Path) -> Settings:
    """Settings with the stub model adapter, a config key and a media root of the test's own."""
    return auth_settings(
        pg_url, model_adapter="stub", config_key=config_key(), media_root=tmp_path / "media"
    )


async def clean(engine: AsyncEngine) -> None:
    """Take back every image job row and ``image_job`` jobs row (``IMAGE_TABLES_CLEANUP``)."""
    async with engine.begin() as conn:
        for statement in IMAGE_TABLES_CLEANUP:
            await conn.execute(text(statement))


@asynccontextmanager
async def image_engine(settings: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    """An Engine on ``engine`` with a clean model configuration, restored afterwards along with
    the image job tables."""
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        epistrel.prepare_media()
        try:
            yield epistrel
        finally:
            await epistrel.image_waiters.aclose()
            await epistrel.models.aclose()
            await clean(engine)


async def configure(lib: Engine, settings: Settings, adapter: str, *, check: bool = True) -> None:
    """The ``image_generation`` port naming ``adapter`` (with a model and key) and, with
    ``check``, an ``image_check`` model."""
    async with admin_client(settings, lib) as client:
        await put(client, "image_generation", {"model": "img-1", "params": {"adapter": adapter}})
        await put_key(client, "image_generation", "sk-image-test-0001")
        if check:
            await put(client, "image_check", {"model": "stub-vision"})


def prepared(
    story_id: str | None = None,
    *,
    kind: ImageJobKind = ImageJobKind.STORY,
    origin: dict[str, Any] | None = None,
    **request: Any,
) -> PreparedImageJob:
    """A job that passed the enqueue checks (``insert`` writes it as is)."""
    return PreparedImageJob(
        kind,
        {},
        origin or story_origin(story_id or new_ulid()),
        ImageJobRequest(prompt=PROMPT, **request),
        PRINCIPAL,
    )


async def insert(lib: Engine, job: PreparedImageJob | None = None) -> str:
    async with lib.db.begin() as conn:
        return (await image_jobs.insert_image_job(conn, job or prepared(), None)).job_id


async def counts(engine: AsyncEngine, story_id: str | None = None) -> dict[str, int]:
    """``media_assets`` rows, and the story's events when given."""
    async with engine.connect() as conn:
        found = {
            "media_assets": int(await conn.scalar(text("SELECT count(*) FROM media_assets")) or 0)
        }
        if story_id is not None:
            found["events"] = int(
                await conn.scalar(
                    text("SELECT count(*) FROM events WHERE story_id = :s"), {"s": story_id}
                )
                or 0
            )
    return found


def blobs(store: Any) -> set[str]:
    """Every content-addressed blob in ``store``."""
    return set(store.list_checksums())


def staged(store: Any, job_id: str) -> list[Path]:
    """The files in the job's private staging."""
    root = store.image_staging_dir(job_id)
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def backdate(path: Path, seconds: float) -> None:
    """Make ``path`` and everything under it look ``seconds`` old."""
    moment = time.time() - seconds
    for item in [path, *path.rglob("*")]:
        os.utime(item, (moment, moment))


async def configure_child_adapter(lib: Engine, settings: Settings) -> None:
    """The child's PNG adapter configured in the shared database (registered here too, as
    the write checks the name)."""
    with registered(faults.IMAGE_ADAPTER, PngImageGenerator()):  # type: ignore[arg-type]
        await configure(lib, settings, faults.IMAGE_ADAPTER, check=False)


async def crashed_child(pg_url: str, point: str, job_id: str) -> faults.EngineProcess:
    """A child engine (images on) killed at ``point`` while it runs ``job_id``."""
    child = faults.EngineProcess(
        pg_url,
        CREDENTIAL_A,
        crash=faults.crash_at(point),
        images=True,
        config_key=CONFIG_KEY,
        mode="all",
    )
    await child.start()
    assert await child.wait_exit(60) == crash.EXIT_STATUS, f"no crash at {point} ({job_id})"
    return child


async def end_by_recovery(engine: AsyncEngine, job_id: str) -> None:
    """End the job as a recovery that committed its ending and died before its after-commit
    delete would (the case the staging sweep exists for): ``failed``, its ``jobs`` row too."""
    async with engine.begin() as conn:
        await image_store.set_status(
            conn, job_id, ImageJobStatus.FAILED, status_reason="interrupted"
        )
        await conn.execute(
            text(
                "UPDATE jobs SET status = 'failed', lease_until = NULL "
                "WHERE kind = 'image_job' AND payload->>'image_job_id' = :j"
            ),
            {"j": job_id},
        )


__all__ = [
    "CTX",
    "HEADERS",
    "MINORS",
    "PRINCIPAL",
    "PROMPT",
    "backdate",
    "blobs",
    "clean",
    "configure",
    "configure_child_adapter",
    "counts",
    "crashed_child",
    "end_by_recovery",
    "image_engine",
    "image_settings_for",
    "insert",
    "prepared",
    "staged",
]
