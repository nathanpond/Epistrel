"""I-25's M8 parts: image staging and the image job core (FR-IMG-3,4; #256).

The job pipeline runs here with the test target (``epistrel.testing.images.TestImageTarget``,
registered for ``story``), scripted checks (``ScriptedChecks``) and the scripted image adapter
(``tests.image_support.StubImageGenerator``), so it is proven before any real target or check
exists. A job is run by calling the handler's entry point (``run_image_job``) directly, so no
worker claims another test's job; the process-crash parts start a child engine
(``testing.faults.EngineProcess``) that registers the same seams (``images=True``).

Every I-25 M8 part lives in this file (the traceability gate takes one file per test ID and
milestone)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import engine as engine_module
from epistrel.api import errors as api_errors
from epistrel.api.deps import ContextDep, EngineDep
from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind
from epistrel.core.errors import MediaNotFound
from epistrel.core.events.operation import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.images import store as image_store
from epistrel.core.media import sweep as media_sweep
from epistrel.core.media.store import LocalFilesystemStore
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.jobs import (
    IMAGE_JOB_CODEC,
    ImageJobQueued,
    ImageJobRequest,
    PreparedImageJob,
)
from epistrel.orchestrator.images.targets.story import AGE_PREDICATE_ID
from epistrel.testing.images import (
    PASS,
    ScriptedChecks,
    installed_checks,
    registered_target,
)
from tests import catalog_image_support as cs
from tests import erasure_support as e
from tests import image_prompt_support as ip
from tests import story_image_generation_support as sg
from tests import story_image_support as si
from tests import story_revalidation_support as rv
from tests.auth_support import auth_settings
from tests.catalog_support import PRINCIPAL as CATALOG_PRINCIPAL
from tests.image_job_support import (
    CTX,
    HEADERS,
    PRINCIPAL,
    PROMPT,
    backdate,
    blobs,
    clean,
    configure,
    configure_child_adapter,
    counts,
    crashed_child,
    end_by_recovery,
    image_engine,
    image_settings_for,
    insert,
    prepared,
    staged,
)
from tests.image_support import (
    Outputs,
    StubImageGenerator,
    registered,
    story_origin,
)
from tests.media_support import OPERATOR as MEDIA_OPERATOR
from tests.media_support import set_media
from tests.model_support import admin_client
from tests.projection_support import swap, truncate, untruncate
from tests.respond_support import body, new_story, played, respond_client, until
from tests.story_support import WRITE


@pytest.fixture
def image_settings(pg_url: str, tmp_path: Path) -> Settings:
    return image_settings_for(pg_url, tmp_path)


@pytest.fixture
async def lib(image_settings: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    async with image_engine(image_settings, engine) as epistrel:
        yield epistrel


@pytest.fixture
def target() -> Iterator[Any]:
    with registered_target() as found:
        yield found


# -- staging ----------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3,4")
async def test_i_25__m8_staging(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: Any
) -> None:
    """While a job checks its output (the checker stalled), the bytes are in private staging
    only: no media row, no blob in the content-addressed store, no history entry, and no
    media read reaches them; released, the job promotes one asset (with its generation
    metadata) and its staging is gone. A checker rejection leaves the store and staging as
    they were: the job fails and each rejected output is audited without the image.

    Complements asserted: the promoted asset and its blob exist only after the release, and the
    rejected job wrote no media row."""
    stub = StubImageGenerator(script=[Outputs(), Outputs(), Outputs(), Outputs()])
    rt = lib.image_runtime()
    story_id = new_ulid()
    with registered("stub_sync", stub):
        await configure(lib, image_settings, "stub_sync")
        release = asyncio.Event()
        with installed_checks(ScriptedChecks(stall=release)) as checks:
            before, stored = await counts(engine, story_id), blobs(lib.media)
            job_id = await insert(lib, prepared(story_id, seed=11))
            run = asyncio.create_task(image_jobs.run_image_job(rt, job_id, 0))
            await asyncio.wait_for(checks.stalled.wait(), 10)
            view = await lib.get_image_job(CTX, job_id)
            assert (view.status, view.result_ids) == ("checking", ())
            assert await counts(engine, story_id) == before
            assert blobs(lib.media) == stored
            files = staged(lib.media, job_id)
            assert [f.name for f in files] == ["0"]
            assert str(files[0]).startswith(str(lib.media.root / "tmp" / "images"))
            with pytest.raises(ValueError, match="SHA-256"):
                lib.media.open(str(files[0].relative_to(lib.media.root)))
            with pytest.raises(MediaNotFound):
                await lib.get_media(CTX, job_id)
            release.set()
            assert (await run)["image_job"] == "promoted"
        view = await lib.get_image_job(CTX, job_id)
        assert view.status == "promoted"
        assert view.status_reason is None
        (asset_id,) = view.result_ids
        asset = await lib.get_media(CTX, asset_id)
        assert asset.source == "generated"
        assert asset.kind == "image"
        metadata = asset.generation_metadata or {}
        assert {
            "adapter",
            "model",
            "endpoint",
            "prompt",
            "negative_prompt",
            "requested_parameters",
            "applied_parameters",
            "reference_image_ids",
            "usage",
            "cost",
        } <= set(metadata)
        assert (metadata["adapter"], metadata["model"], metadata["prompt"]) == (
            "stub_sync",
            "img-1",
            PROMPT,
        )
        assert metadata["requested_parameters"]["seed"] == 11
        assert metadata["applied_parameters"]["seed"] == 11
        assert metadata["cost"] == {"amount": "0.04", "currency": "USD"}
        assert asset.checksum in blobs(lib.media)
        assert staged(lib.media, job_id) == []
        assert (await counts(engine, story_id))["media_assets"] == before["media_assets"] + 1

        # A rejection: every attempt's output violates; the budget runs out (1 + 2 attempts).
        with installed_checks(ScriptedChecks(["minors_no_sexual_content"])):
            before, stored = await counts(engine), blobs(lib.media)
            rejected = await insert(lib)
            assert (await image_jobs.run_image_job(rt, rejected, 0))["image_job"] == "failed"
        assert await counts(engine) == before
        assert blobs(lib.media) == stored
        assert staged(lib.media, rejected) == []
        async with engine.connect() as conn:
            audited = (
                await conn.execute(
                    text(
                        "SELECT policy_ids, details FROM audit_log "
                        "WHERE kind = 'image_output_rejected' AND details->>'job_id' = :j"
                    ),
                    {"j": rejected},
                )
            ).all()
        assert len(audited) == 3
        for row in audited:
            assert row.policy_ids == ["minors_no_sexual_content"]
            assert set(row.details) == {"job_id", "attempt_token", "output", "outcome"}
            assert PROMPT not in str(row.details)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3")
async def test_i_25__m8_crash_sweeps(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, pg_url: str
) -> None:
    """A worker killed after it staged an output (``images.after_stage``) leaves the staging
    object. The staging sweep skips it while the job is not terminal, however old; once the
    job has ended it keeps it at ``staging_ttl - 1 s`` and deletes it past ``staging_ttl``. The
    media sweep never touches it, however old: it removes a stale staged upload beside it and
    leaves ``tmp/images/<job>/<token>/`` to the staging sweep (#799). A
    worker killed with the promote transaction written and not committed
    (``images.promote.before_commit``) leaves a content-addressed blob no asset names: the media
    sweep's orphan step keeps it for its hour and deletes it after.

    Complements asserted: no asset row exists for the rolled-back promotion, and each sweep
    deleted only what it was meant to."""
    ttl = 3600
    await configure_child_adapter(lib, image_settings)
    staged_job = await insert(lib)
    child = await crashed_child(pg_url, "images.after_stage", staged_job)
    store = LocalFilesystemStore(Path(child.media_root))
    job_dir = store.image_staging_dir(staged_job)
    assert [p.name for p in staged(store, staged_job)] == ["0"]
    backdate(job_dir, ttl + 600)
    kept = await image_jobs.sweep_staging(lib.db, store, ttl_s=ttl)
    assert kept["removed"] == 0
    assert kept["skipped_running"] >= 1
    assert job_dir.exists()
    # Only the image staging sweep owns ``tmp/images/``: the media sweep's staging step removes
    # a stale staged upload beside it and leaves the stale ``<job>/<token>/`` tree alone.
    outputs = staged(store, staged_job)
    stale_upload = store.staging_path(new_ulid())
    stale_upload.write_bytes(b"crashed upload")
    backdate(stale_upload, ttl + 600)
    backdate(store.image_staging, ttl + 600)
    swept = await media_sweep.sweep(lib.db, store)
    assert swept["staged_removed"] >= 1
    assert not stale_upload.exists()
    assert staged(store, staged_job) == outputs
    assert all(p.parent.parent == job_dir for p in outputs)
    await end_by_recovery(engine, staged_job)
    backdate(job_dir, ttl - 1)
    assert (await image_jobs.sweep_staging(lib.db, store, ttl_s=ttl))["removed"] == 0
    assert job_dir.exists()
    backdate(job_dir, ttl + 1)
    assert (await image_jobs.sweep_staging(lib.db, store, ttl_s=ttl))["removed"] == 1
    assert not job_dir.exists()
    assert (await image_jobs.sweep_staging(lib.db, store, ttl_s=ttl))["removed"] == 0
    await child.__aexit__(None, None, None)

    promoted_job = await insert(lib)
    child = await crashed_child(pg_url, "images.promote.before_commit", promoted_job)
    try:
        store = LocalFilesystemStore(Path(child.media_root))
        view = await lib.get_image_job(CTX, promoted_job)
        assert view.status == "checking"
        assert view.result_ids == ()
        (orphan,) = blobs(store)
        # The child's own media sweep runs against the shared database too, so the rolled-back
        # promotion is asserted by the orphan's checksum, not by a row count.
        async with engine.connect() as conn:
            named = await conn.scalar(
                text("SELECT count(*) FROM media_assets WHERE checksum = :c"), {"c": orphan}
            )
        assert named == 0
        await media_sweep.sweep(lib.db, store)
        assert blobs(store) == {orphan}
        os.utime(store.path(orphan), (time.time() - 2 * 3600,) * 2)
        swept = await media_sweep.sweep(lib.db, store)
        assert swept["blobs_deleted"] == 1
        assert blobs(store) == set()
    finally:
        await child.__aexit__(None, None, None)


# -- idempotency ------------------------------------------------------------------------------

TEST_OPERATION = "test_image_job"


def _enqueue_app(lib: Engine, stub_story: str) -> FastAPI:
    """A generation route as #260/#263 will write one: #61's key around ``enqueue_image_job``,
    202 with ``Location`` and the replay header."""
    app = FastAPI()
    api_errors.install(app)
    app.state.epistrel = lib

    @app.post("/test-image-jobs")
    async def create(ctx: ContextDep, engine: EngineDep) -> JSONResponse:
        async def run(c: RequestContext) -> ImageJobQueued:
            return await image_jobs.enqueue_image_job(
                engine.image_runtime(),
                ImageJobKind.STORY,
                {},
                story_origin(stub_story),
                {"prompt": PROMPT},
                c.idempotency_key,
                PRINCIPAL,
            )

        queued = await engine._keyed(
            ctx, TEST_OPERATION, run, story_id=stub_story, material={"prompt": PROMPT}
        )
        headers = {"Location": queued.location, "Idempotency-Key": ctx.idempotency_key or ""}
        if queued.replayed:
            headers["Idempotent-Replay"] = "true"
        return JSONResponse(
            {"job_id": queued.job_id, "status": queued.status}, status_code=202, headers=headers
        )

    return app


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4")
async def test_i_25__m8_idempotent(
    lib: Engine,
    image_settings: Settings,
    engine: AsyncEngine,
    target: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generation request returns 202 ``{job_id, status: queued}`` with ``Location:
    /image-jobs/{id}``; the same ``Idempotency-Key`` again answers the same job with #61's
    stored status and ``Idempotent-Replay: true``, and running the jobs calls the adapter once.

    Complement asserted: a different key is a second job."""
    monkeypatch.setitem(engine_module.CODECS, TEST_OPERATION, IMAGE_JOB_CODEC)
    stub = StubImageGenerator(script=[Outputs(), Outputs()])
    story_id = new_ulid()
    with registered("stub_sync", stub), installed_checks(ScriptedChecks()):
        await configure(lib, image_settings, "stub_sync")
        transport = httpx.ASGITransport(app=_enqueue_app(lib, story_id))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            key = {"Idempotency-Key": "image-key-1", "X-Epistrel-Principal": PRINCIPAL}
            first = await client.post("/test-image-jobs", headers=key)
            again = await client.post("/test-image-jobs", headers=key)
            other = await client.post(
                "/test-image-jobs",
                headers={**key, "Idempotency-Key": "image-key-2"},
            )
        assert first.status_code == again.status_code == 202, first.text
        job_id = first.json()["job_id"]
        assert first.json() == {"job_id": job_id, "status": "queued"}
        assert first.headers["Location"] == f"/image-jobs/{job_id}"
        assert "Idempotent-Replay" not in first.headers
        assert again.json() == first.json()
        assert again.headers["Idempotent-Replay"] == "true"
        assert again.headers["Location"] == first.headers["Location"]
        assert other.json()["job_id"] != job_id
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT idempotency_key FROM image_jobs WHERE id = :j"), {"j": job_id}
                )
                == "image-key-1"
            )
            jobs_rows = await conn.scalar(
                text(
                    "SELECT count(*) FROM jobs WHERE kind = 'image_job' "
                    "AND payload->>'image_job_id' = :j"
                ),
                {"j": job_id},
            )
        assert jobs_rows == 1
        result = await image_jobs.run_image_job(lib.image_runtime(), job_id, 0)
        assert result["image_job"] == "promoted"
        assert len(stub.requests) == 1


# -- read, long poll and cancel ---------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4")
async def test_i_25__m8_long_poll(
    lib: Engine,
    image_settings: Settings,
    engine: AsyncEngine,
    target: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?wait=5&since_status=queued`` answers when the job leaves ``queued``: on the
    notification, and with notifications suppressed within 1.5 s of the change (the one-second
    fallback); ``?wait=1`` on an unchanged job answers after about a second with the same
    status. ``wait`` outside 1..30 and an unknown ``since_status`` are 422, an unknown job 404,
    and the 201st waiter in a process 503 ``too_many_waiters``.

    Complement asserted: without ``wait`` the read answers at once."""
    loop = asyncio.get_running_loop()
    job_id = await insert(lib)

    async def change(status: ImageJobStatus, after_s: float) -> float:
        await asyncio.sleep(after_s)
        async with engine.begin() as conn:
            await image_store.set_status(conn, job_id, status)
        return loop.time()

    started = loop.time()
    waited = asyncio.create_task(lib.get_image_job(CTX, job_id, wait=5, since_status="queued"))
    changed_at = await change(ImageJobStatus.GENERATING, 0.3)
    view = await waited
    assert view.status == "generating"
    assert loop.time() - changed_at < 1.5
    assert loop.time() - started < 3

    async def silent(conn: Any, job: str, status: ImageJobStatus) -> None:
        return None

    monkeypatch.setattr(image_store, "notify", silent)
    lib.image_waiters.listen = False
    waited = asyncio.create_task(lib.get_image_job(CTX, job_id, wait=5, since_status="generating"))
    changed_at = await change(ImageJobStatus.CHECKING, 0.4)
    view = await waited
    assert view.status == "checking"
    assert loop.time() - changed_at < 1.5

    started = loop.time()
    view = await lib.get_image_job(CTX, job_id, wait=1)
    took = loop.time() - started
    assert view.status == "checking"
    assert 0.9 <= took < 2.0
    started = loop.time()
    assert (await lib.get_image_job(CTX, job_id)).status == "checking"
    assert loop.time() - started < 0.5

    async with admin_client(image_settings, lib) as client:
        path = f"/image-jobs/{job_id}"
        ok = await client.get(path, params={"wait": 1}, headers=HEADERS)
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "checking"
        assert ok.json()["asset_ids"] == []
        assert "candidate_ids" not in ok.json()
        for params in ({"wait": 0}, {"wait": 31}, {"since_status": "done"}):
            refused = await client.get(path, params=params, headers=HEADERS)
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["code"] == "validation_error"
        missing = await client.get(f"/image-jobs/{new_ulid()}", headers=HEADERS)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "image_job_not_found"

    monkeypatch.setattr(image_jobs, "MAX_WAITERS", 1)
    holder = asyncio.create_task(lib.get_image_job(CTX, job_id, wait=2))
    await asyncio.sleep(0.2)
    with pytest.raises(image_jobs.TooManyWaiters):
        await lib.get_image_job(CTX, job_id, wait=1)
    await holder


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4")
async def test_i_25__m8_cancel(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: Any
) -> None:
    """``DELETE /image-jobs/{id}`` cancels a queued job (its ``jobs`` row finished) and a running
    one: the running worker, released after the cancel, is fenced and promotes nothing, and
    the staged bytes are gone. A finished job answers unchanged, and an unknown one is 404.

    Complement asserted: the cancelled running job's adapter call happened (the bytes were
    staged before the cancel)."""
    stub = StubImageGenerator(script=[Outputs()])
    rt = lib.image_runtime()
    with registered("stub_sync", stub):
        await configure(lib, image_settings, "stub_sync")
        queued = await insert(lib)
        async with admin_client(image_settings, lib) as client:
            cancelled = await client.delete(f"/image-jobs/{queued}", headers=HEADERS)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["status_reason"] is None
            again = await client.delete(f"/image-jobs/{queued}", headers=HEADERS)
            assert again.json() == cancelled.json()
            missing = await client.delete(f"/image-jobs/{new_ulid()}", headers=HEADERS)
            assert missing.status_code == 404
        async with engine.connect() as conn:
            row = await conn.scalar(
                text(
                    "SELECT status FROM jobs WHERE kind = 'image_job' "
                    "AND payload->>'image_job_id' = :j"
                ),
                {"j": queued},
            )
        assert row == "done"
        assert (await image_jobs.run_image_job(rt, queued, 0))["image_job"] == "cancelled"

        release = asyncio.Event()
        with installed_checks(ScriptedChecks(stall=release)) as checks:
            before = await counts(engine)
            running = await insert(lib)
            task = asyncio.create_task(image_jobs.run_image_job(rt, running, 0))
            await asyncio.wait_for(checks.stalled.wait(), 10)
            assert staged(lib.media, running)
            view = await lib.cancel_image_job(CTX, running)
            assert view.status == "cancelled"
            assert staged(lib.media, running) == []
            release.set()
            assert (await task)["image_job"] == "fenced"
        assert len(stub.requests) == 1
        assert await counts(engine) == before
        assert (await lib.get_image_job(CTX, running)).status == "cancelled"


# -- outside the turn ------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4")
async def test_i_25__m8_outside_turn_budget(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A respond whose turn queues an image job (a test-only hook inserting a job with the
    test target inside the turn's commit) completes with #69's call counts and budget exactly
    those of the same turn without it: the job is queued, not run, and no image adapter is
    called inside the turn.

    Complement asserted: the hooked turn did queue its job, in its own operation."""
    stub = StubImageGenerator(script=[])
    queued: list[str] = []
    original = Operation.commit

    async def hooked(self: Operation, *args: Any, **kwargs: Any) -> None:
        if self.kind is OperationKind.TURN and self.story_id is not None:
            story_id, operation_id = self.story_id, self.operation_id

            async def step(conn: Any) -> None:
                job = PreparedImageJob(
                    ImageJobKind.STORY,
                    {},
                    story_origin(story_id, operation_id=operation_id),
                    ImageJobRequest(prompt=PROMPT),
                    PRINCIPAL,
                )
                queued.append((await image_jobs.insert_image_job(conn, job, None)).job_id)

            self.add_commit_step(step)
        await original(self, *args, **kwargs)

    try:
        with registered("stub_sync", stub), registered_target():
            settings = auth_settings(pg_url, model_adapter="stub")
            async with respond_client(settings, engine) as (client, _):
                plain_story, cast = await new_story(client)
                plain = await played(client, plain_story, body(cast["Wren"]))
                monkeypatch.setattr(Operation, "commit", hooked)
                hooked_story, cast = await new_story(client)
                turned = await played(client, hooked_story, body(cast["Wren"]))
                monkeypatch.setattr(Operation, "commit", original)
        assert turned["call_counts"] == plain["call_counts"]
        assert turned["budget"] == plain["budget"]
        assert len(queued) == 1
        assert stub.requests == []
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT status, story_id, origin FROM image_jobs WHERE id = :j"),
                    {"j": queued[0]},
                )
            ).one()
        assert (row.status, row.story_id) == ("queued", hooked_story)
        assert row.origin["operation_id"] is not None
    finally:
        await clean(engine)


async def test_image_worker_concurrency_caps_claims(
    monkeypatch: pytest.MonkeyPatch, dummy_dsn: str
) -> None:
    """A worker process claims an ``image_job`` only while fewer than
    ``image_worker_concurrency`` of them run in it (``kind_caps``), and with 0 never; other
    kinds are claimed as before."""
    from epistrel.orchestrator.jobs import queue
    from epistrel.orchestrator.jobs.worker import IMAGE_JOB_KIND, Worker, WorkerConfig

    asked: list[list[str]] = []

    async def claim(db: Any, worker_id: str, *, lease_s: float, exclude_kinds: Any = ()) -> None:
        asked.append(list(exclude_kinds))

    monkeypatch.setattr(queue, "claim", claim)
    worker = Worker(cast(AsyncEngine, None), WorkerConfig(kind_caps={IMAGE_JOB_KIND: 1}))
    assert await worker.run_one("w") is False
    worker._running[IMAGE_JOB_KIND] = 1
    assert await worker.run_one("w") is False
    assert asked == [[], [IMAGE_JOB_KIND]]
    assert worker._running[IMAGE_JOB_KIND] == 1
    off = auth_settings(dummy_dsn, image_worker_concurrency=0)
    assert WorkerConfig.from_settings(off).kind_caps == {IMAGE_JOB_KIND: 0}
    assert image_jobs.IMAGE_JOB_KIND == IMAGE_JOB_KIND


# -- catalog candidates (#260) ------------------------------------------------------------------


def _ids(page: dict[str, Any]) -> list[str]:
    return [item["candidate_id"] for item in page["items"]]


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-7; FR-IMG-4; FR-CAT-3; FR-MEDIA-2")
async def test_i_25__m8_candidates(image_settings: Settings, engine: AsyncEngine) -> None:
    """A request for four candidates of a character's primary image yields four checked
    candidates and attaches none: the job ends ``candidates_ready`` with their ids, and each has
    a ``candidate``-state asset owned by the entry's owner. They are listed and previewed
    through the candidate routes only (content with ``Range`` and ``private, no-store``, and a
    signed URL that serves without the credential), and are absent from ``GET /media``, the
    media reads, the card and full projections. The staging sweep and the general media sweep
    (run over seven days) leave them in place. Attaching one with a stale revision is 409
    ``draft_stale``; after a tagline edit, with the current revision it attaches (the asset
    ``live``, the card's primary its media id, the revision advanced, the candidate gone).
    After an appearance edit another is listed ``stale`` and refused 409 ``candidate_stale``;
    restored, it attaches after a publish. An unknown candidate is 422 ``candidate_not_found``.

    Complements asserted: before attaching, the draft has no primary image and a credentialed
    media read of a candidate is 404; the unattached sibling is still listed after an attach."""
    async with cs.candidate_app(image_settings, engine) as app:
        client = app.client
        created = await cs.character(app)
        character_id, revision = created["id"], created["draft_revision"]
        job_id, ids = await cs.generated(app, character_id, Outputs(n=4, size=(71, 53)))
        assert len(ids) == 4
        rows = await cs.candidate_rows(engine, ids)
        assert {r.state for r in rows} == {"candidate"}
        assert len({r.checksum for r in rows}) == 4
        assert {r.owner_principal for r in rows} == {CATALOG_PRINCIPAL}
        assets = [r.asset_id for r in rows]
        assert (await cs.draft(app, character_id))["images"].get("primary") is None

        page = await cs.listed(app, character_id, job_id=job_id)
        assert _ids(page) == ids
        assert {i["stale"] for i in page["items"]} == {False}
        assert page["items"][0]["media"]["check_status"] == "passed"
        assert _ids(await cs.listed(app, character_id, job_id=new_ulid())) == []
        first = await cs.listed(app, character_id, limit=3)
        rest = await cs.listed(app, character_id, cursor=first["next_cursor"])
        assert _ids(first) + _ids(rest) == ids

        path = cs.candidate_path(character_id, ids[0])
        content = await client.get(f"{path}/content", headers=HEADERS)
        assert content.status_code == 200
        assert content.headers["cache-control"] == "private, no-store"
        ranged = await client.get(f"{path}/content", headers={**HEADERS, "Range": "bytes=0-3"})
        assert ranged.status_code == 206
        assert ranged.content == content.content[:4]
        for media_path in (f"/media/{assets[0]}", f"/media/{assets[0]}/content"):
            assert (await client.get(media_path, headers=HEADERS)).status_code == 404
        assert (await client.get(f"/media/{assets[0]}/url", headers=HEADERS)).status_code == 404
        listing = await client.get("/media", params={"limit": 200}, headers=HEADERS)
        assert not {a["id"] for a in listing.json()["items"]} & set(assets)
        state = await client.get("/media", params={"state": "candidate"}, headers=HEADERS)
        assert state.status_code == 422

        await set_media(client, public_base_url="https://epistrel.example")
        try:
            url = await client.get(f"{path}/url", headers=HEADERS)
            assert url.status_code == 200, url.text
            signed = url.json()["url"].removeprefix("https://epistrel.example")
            assert signed.startswith(f"/media/{assets[0]}/content?")
            anonymous = await client.get(signed)
            assert anonymous.status_code == 200
            assert anonymous.content == content.content
        finally:
            await set_media(client, public_base_url=None)

        # The staging sweep and the general sweep, every day for seven days, leave them alone.
        await image_jobs.sweep_staging(engine, app.lib.media, ttl_s=0)
        for day in range(1, 8):
            await media_sweep.sweep(engine, app.lib.media, now=clock.now() + timedelta(days=day))
        assert _ids(await cs.listed(app, character_id)) == ids
        assert {r.checksum for r in await cs.candidate_rows(engine, ids)} <= blobs(app.lib.media)

        stale = await cs.save(
            app,
            character_id,
            revision - 1 if revision > 1 else revision + 5,
            public_card={"images": {"primary": {"candidate_id": ids[0]}}},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "draft_stale"
        tagline = await cs.save(
            app, character_id, revision, public_card={"public_description": "New."}
        )
        assert tagline.status_code == 200, tagline.text
        revision = tagline.json()["draft_revision"]
        attached = await cs.save(
            app,
            character_id,
            revision,
            public_card={"images": {"primary": {"candidate_id": ids[0]}}},
        )
        assert attached.status_code == 200, attached.text
        assert attached.json()["draft_revision"] == revision + 1
        revision = attached.json()["draft_revision"]
        assert (await cs.asset(engine, assets[0])).state == "live"
        assert (await cs.draft(app, character_id))["images"]["primary"]["media_id"] == assets[0]
        assert (await client.get(f"/media/{assets[0]}", headers=HEADERS)).status_code == 200
        assert _ids(await cs.listed(app, character_id)) == ids[1:]
        assert (await client.get(f"{path}/content", headers=HEADERS)).status_code == 404

        appearance = created["definition"]["appearance"]
        changed = await cs.save(
            app, character_id, revision, definition={"appearance": appearance + " Freckles."}
        )
        revision = changed.json()["draft_revision"]
        assert {i["stale"] for i in (await cs.listed(app, character_id))["items"]} == {True}
        refused = await cs.save(
            app,
            character_id,
            revision,
            public_card={"images": {"gallery": [{"candidate_id": ids[1]}]}},
        )
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["details"]["reason"]) == ("candidate_stale", "stale")
        assert error["details"]["candidate_ids"] == [ids[1]]
        restored = await cs.save(app, character_id, revision, definition={"appearance": appearance})
        revision = restored.json()["draft_revision"]
        published = await ip.publish(client, character_id, revision)
        revision = published["draft_revision"]
        later = await cs.save(
            app,
            character_id,
            revision,
            public_card={"images": {"gallery": [{"candidate_id": ids[1]}]}},
        )
        assert later.status_code == 200, later.text
        assert (await cs.asset(engine, assets[1])).state == "live"
        unknown = await cs.save(
            app,
            character_id,
            later.json()["draft_revision"],
            public_card={"images": {"primary": {"candidate_id": new_ulid()}}},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "candidate_not_found"
        assert _ids(await cs.listed(app, character_id)) == ids[2:]


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-7; FR-MEDIA-2")
async def test_i_25__m8_candidate_expiry(image_settings: Settings, engine: AsyncEngine) -> None:
    """A candidate expires ``candidate_ttl`` (seven days by default) after promotion, fixed
    then: the expiry deletes its row and its asset row, and its bytes unless another asset has
    the same checksum. A candidate sharing its bytes with an attached image loses its rows and
    keeps the bytes. A ``candidate_ttl`` change applies to later candidates only, and must be
    whole hours.

    Complement asserted: a minute before ``expires_at`` the expiry leaves both in place."""
    from epistrel.orchestrator.jobs.handlers.image_candidate_expiry import expire

    async with cs.candidate_app(image_settings, engine) as app:
        created = await cs.character(app)
        character_id, revision = created["id"], created["draft_revision"]
        _, first = await cs.generated(app, character_id, Outputs(n=2, size=(73, 51)))
        rows = await cs.candidate_rows(engine, first)
        for row in rows:
            left = row.expires_at - clock.now()
            assert timedelta(days=7) - timedelta(minutes=5) < left <= timedelta(days=7)
        attached = await cs.save(
            app,
            character_id,
            revision,
            public_card={"images": {"primary": {"candidate_id": first[0]}}},
        )
        assert attached.status_code == 200, attached.text
        _, second = await cs.generated(app, character_id, Outputs(n=1, size=(73, 51)))
        [twin] = await cs.candidate_rows(engine, second)
        assert twin.checksum == rows[0].checksum
        waiting = [first[1], second[0]]

        await cs.expire_at(engine, waiting, "1 minute")
        assert (await expire(engine, app.lib.media))["expired"] == 0
        assert _ids(await cs.listed(app, character_id)) == waiting

        await cs.expire_at(engine, waiting, "-1 minute")
        assert (await expire(engine, app.lib.media))["expired"] == 2
        assert await cs.candidate_rows(engine, waiting) == []
        assert await cs.asset(engine, rows[1].asset_id) is None
        assert await cs.asset(engine, twin.asset_id) is None
        stored = blobs(app.lib.media)
        assert rows[1].checksum not in stored
        assert rows[0].checksum in stored
        assert (await cs.asset(engine, rows[0].asset_id)).state == "live"
        assert (await expire(engine, app.lib.media))["expired"] == 0

        async with admin_client(image_settings, app.lib) as admin:
            refused = await admin.put(
                "/admin/settings", json={"candidate_ttl": 5_400}, headers=MEDIA_OPERATOR
            )
            assert refused.status_code == 422, refused.text
            await set_media(admin, candidate_ttl=3_600)
        try:
            _, third = await cs.generated(app, character_id, Outputs(n=1, size=(73, 52)))
            [row] = await cs.candidate_rows(engine, third)
            assert row.expires_at - clock.now() <= timedelta(hours=1)
        finally:
            async with admin_client(image_settings, app.lib) as admin:
                await set_media(admin, candidate_ttl=7 * 24 * 3_600)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4,7")
async def test_i_25__m8_candidate_target_gone(
    image_settings: Settings, engine: AsyncEngine
) -> None:
    """Deleting the entry while its job is queued ends the job ``target_gone`` with no provider
    call and nothing promoted; deleting a character deletes its candidates and its companion
    scenario's (a cover's), rows, assets and bytes. A slot the kind's images cannot have and an
    ``n`` outside 1 to 10 are 422 before any job.

    Complement asserted: before the delete the cover candidates are listed and their bytes
    stored."""
    async with cs.candidate_app(image_settings, engine) as app:
        client = app.client
        doomed, created = await cs.characters(app, 2)
        app.script(Outputs(n=1, size=(75, 49)))
        accepted = await cs.generate(app, doomed["id"], n=1)
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        deleted = await client.delete(f"{cs.CHARACTERS}/{doomed['id']}", headers=HEADERS)
        assert deleted.status_code == 204, deleted.text
        assert (await cs.run(app, job_id))["image_job"] == "target_gone"
        assert app.generator.requests == []
        assert app.generator.script  # the scripted output was never asked for
        app.generator.script.clear()
        job = (await client.get(f"/image-jobs/{job_id}", headers=HEADERS)).json()
        assert (job["status"], job["candidate_ids"]) == ("target_gone", [])
        assert staged(app.lib.media, job_id) == []

        companion = (
            created.get("companion_id") or (await cs.draft(app, created["id"]))["companion_id"]
        )
        for body in (
            {"slot": "cover"},
            {"slot": "primary", "n": 11},
            {"slot": "primary", "n": 2.0},
        ):
            refused = await client.post(
                f"{cs.CHARACTERS}/{created['id']}/images/generate", json=body, headers=HEADERS
            )
            assert refused.status_code == 422, body
        wrong = await cs.generate(app, companion, kind="scenario", slot="primary")
        assert wrong.status_code == 422
        _, covers = await cs.generated(
            app, companion, Outputs(n=2, size=(75, 48)), kind="scenario", slot="cover"
        )
        assert _ids(await cs.listed(app, companion, kind="scenario")) == covers
        rows = await cs.candidate_rows(engine, covers)
        assert {r.checksum for r in rows} <= blobs(app.lib.media)
        gone = await client.delete(f"{cs.CHARACTERS}/{created['id']}", headers=HEADERS)
        assert gone.status_code == 204, gone.text
        assert await cs.candidate_rows(engine, covers) == []
        for row in rows:
            assert await cs.asset(engine, row.asset_id) is None
            assert row.checksum not in blobs(app.lib.media)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-7")
async def test_i_25__m8_candidate_discard(image_settings: Settings, engine: AsyncEngine) -> None:
    """``DELETE …/images/candidates/{cid}`` discards a candidate: 204, its candidate and asset
    rows gone and its bytes deleted. A second discard, an unknown candidate and an unknown
    entry are 204 with no change. A candidate sharing its bytes with another asset leaves the
    bytes, and discarding an attached candidate never touches its live asset.

    Complement asserted: the undiscarded sibling is still listed and attachable."""
    async with cs.candidate_app(image_settings, engine) as app:
        client = app.client
        created = await cs.character(app)
        character_id, revision = created["id"], created["draft_revision"]
        _, ids = await cs.generated(app, character_id, Outputs(n=3, size=(77, 47)))
        rows = await cs.candidate_rows(engine, ids)
        path = cs.candidate_path(character_id, ids[0])
        for _ in range(2):
            gone = await client.delete(path, headers=HEADERS)
            assert gone.status_code == 204, gone.text
            assert gone.content == b""
        assert await cs.candidate_rows(engine, [ids[0]]) == []
        assert await cs.asset(engine, rows[0].asset_id) is None
        assert rows[0].checksum not in blobs(app.lib.media)
        for other in (
            cs.candidate_path(character_id, new_ulid()),
            cs.candidate_path(new_ulid(), ids[1]),
        ):
            assert (await client.delete(other, headers=HEADERS)).status_code == 204
        assert _ids(await cs.listed(app, character_id)) == ids[1:]

        attached = await cs.save(
            app,
            character_id,
            revision,
            public_card={"images": {"primary": {"candidate_id": ids[1]}}},
        )
        assert attached.status_code == 200, attached.text
        revision = attached.json()["draft_revision"]
        assert (
            await client.delete(cs.candidate_path(character_id, ids[1]), headers=HEADERS)
        ).status_code == 204
        assert (await cs.asset(engine, rows[1].asset_id)).state == "live"

        _, twins = await cs.generated(app, character_id, Outputs(n=2, size=(77, 47)))
        twin_rows = await cs.candidate_rows(engine, twins)
        assert twin_rows[1].checksum == rows[1].checksum
        assert (
            await client.delete(cs.candidate_path(character_id, twins[1]), headers=HEADERS)
        ).status_code == 204
        assert rows[1].checksum in blobs(app.lib.media)

        sibling = await cs.save(
            app,
            character_id,
            revision,
            public_card={"images": {"gallery": [{"candidate_id": ids[2]}]}},
        )
        assert sibling.status_code == 200, sibling.text


# -- #264: story revalidation (FR-IMG-4; FR-ADMIN-9) ------------------------------------------


async def _promoted(app: sg.GenerationApp, story: str, job_id: str) -> list[dict[str, Any]]:
    found = [r for r in await sg.story_image_rows(app.lib.db, story) if r["job_id"] == job_id]
    return found


async def _ended_stale(
    app: sg.GenerationApp, story: str, job_id: str, reason: str, **detail: Any
) -> dict[str, Any]:
    """The job ended ``stale`` with ``reason`` (and ``detail``): nothing attached, its staging
    deleted, its reservation released or settled, an audit row naming the reason; the read
    answers the reason and its detail."""
    row = await rv.job(app.lib.db, job_id)
    assert (row["status"], row["status_reason"]) == ("stale", reason), row
    for key, value in detail.items():
        assert (row["status_detail"] or {}).get(key) == value, row["status_detail"]
    assert await _promoted(app, story, job_id) == []
    assert rv.staging(app, job_id) == []
    assert "held" not in await rv.reservations(app.lib.db, job_id)
    audit = await rv.audit_reason(app.lib.db, job_id)
    assert audit is not None, job_id
    assert audit["reason"] == reason, audit
    read = await app.client.get(f"/image-jobs/{job_id}", headers=HEADERS)
    assert read.status_code == 200, read.text
    assert read.json()["status_reason"] == reason
    assert read.json()["asset_ids"] == []
    return row


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4; FR-ADMIN-9")
async def test_i_25__m8_story_races(
    image_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """While a story image is being made (its provider call stalled), the story changes under
    it; at promotion the job revalidates inside the attaching transaction and attaches nothing:

    - its turn truncated → ``stale``, ``target_inactive``, ``cause: truncated``; swapped away
      → ``cause: swapped``; regenerated → ``cause: regenerated``, and no job is queued again
      for the new variant (#265 queues its own);
    - an amendment to a fact about a depicted character (the story predicate ``hair_colour``)
      → ``stale``, ``amended``, naming the amendment;
    - a tightening check started (the story paused under a stricter rating): the job waits,
      ``checking`` with ``waiting_on: pause``, its staged bytes and reservation kept; once the
      check ends it evaluates the new rating's policy, and is attached when the image passes
      it, or ends ``stale`` with ``policy_changed`` naming it when it fails;
    - a depicted subject erased → ``cancelled``, ``erased``, by the erasure itself (#266), its
      prompt emptied;
    - the story gone → ``target_gone`` (a story's deletion is refused while its image is being
      made, 423 ``story_busy``, and removes its jobs with it: the job is pointed at a story that
      no longer exists).

    Every ending deletes the staged bytes, holds no reservation, attaches nothing and writes an
    ``image_job_ended`` audit row with the reason.

    Complement asserted for every race: the same job with no race promotes; an amendment to a
    fact about an undepicted character, and an ``age`` amendment about the depicted one, leave
    the image valid."""
    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await rv.lite_scene(app)
        # The complement: no race, the image is attached at its position.
        quiet = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        assert (await quiet.finished())["image_job"] == "promoted"
        assert len(await _promoted(app, story, quiet.job_id)) == 1

        # Truncated while it is being made.
        cut = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        assert await truncate(app.lib.db, story, 1) is not None
        assert (await cut.finished())["image_job"] == "stale"
        await _ended_stale(app, story, cut.job_id, "target_inactive", cause="truncated")

        # Swapped away: turn 2 written again (variant 2), then the old variant swapped back.
        await si.turn(app.client, story, ids)
        swapped = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        assert (await rv.job(engine, swapped.job_id))["origin"]["variant"] == 2
        assert await swap(app.lib.db, story) is not None
        assert (await swapped.finished())["image_job"] == "stale"
        await _ended_stale(app, story, swapped.job_id, "target_inactive", cause="swapped")

        # Regenerated: a newer variant is active at the turn, and nothing queues itself again.
        regenerated = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        jobs_before = (await sg.counts(engine, story))["image_jobs"]
        again = await app.client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
        assert again.status_code == 200, again.text
        assert (await regenerated.finished())["image_job"] == "stale"
        await _ended_stale(app, story, regenerated.job_id, "target_inactive", cause="regenerated")
        assert (await sg.counts(engine, story))["image_jobs"] == jobs_before

        # The complement after the races: the regenerated turn's own image promotes.
        fresh = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        assert (await fresh.finished())["image_job"] == "promoted"

        # Amendments made while it is being made.
        jenni, marcus = ids["Jenni"], ids["Marcus"]
        hair = await rv.story_predicate(engine, story, "hair_colour")
        for subject, predicate, value, stales in (
            (marcus, hair, "grey", False),  # an undepicted character's fact
            (jenni, AGE_PREDICATE_ID, 31, False),  # the depicted one's true age
            (jenni, hair, "red", True),  # a non-age fact of the depicted one
        ):
            amended = await rv.started(app, story, **rv.consumer(2, [jenni]))
            amendment_id = await rv.amend_fact(engine, story, subject, predicate, value)
            done = await amended.finished()
            if stales:
                assert done["image_job"] == "stale", done
                await _ended_stale(app, story, amended.job_id, "amended", amendment_id=amendment_id)
            else:
                assert done["image_job"] == "promoted", (subject, predicate, done)

        # A tightening check starts while it is being checked: it waits, then the new rating's
        # policy judges it.
        for outcome in ("promoted", "stale"):
            await _tightening(app, engine, ids, outcome)

        # A depicted subject erased while it is being made.
        erased_story, erased_ids = await si.scene(app.client, 1)
        erased = await rv.started(app, erased_story, **rv.consumer(1, [erased_ids["Jenni"]]))
        await e.erase(app.client, "character", erased_ids["Jenni"], erased_story, confirm_all=True)
        # #266: the erasure cancels it in its own transaction (its prompt emptied); the worker
        # is fenced at promotion and attaches nothing.
        assert (await erased.finished())["image_job"] == "fenced"
        row = await rv.job(app.lib.db, erased.job_id)
        assert (row["status"], row["status_reason"], row["prompt"]) == ("cancelled", "erased", None)
        assert await _promoted(app, erased_story, erased.job_id) == []
        assert rv.staging(app, erased.job_id) == []
        assert "held" not in await rv.reservations(app.lib.db, erased.job_id)
        audit = await rv.audit_reason(app.lib.db, erased.job_id)
        assert audit is not None, erased.job_id
        assert audit["reason"] == "erased", audit
        kept = await rv.started(app, erased_story, **rv.consumer(1, [erased_ids["Marcus"]]))
        assert (await kept.finished())["image_job"] == "promoted"

        # The story gone: its deletion waits for the image being made, and would take the job
        # with it, so the job is pointed at a story that no longer exists.
        gone = await rv.started(app, story, **rv.consumer(2, [jenni]))
        busy = await app.client.delete(f"/stories/{story}", headers=WRITE)
        assert busy.status_code == 423, busy.text
        assert busy.json()["error"]["details"]["reason"] == "job_running"
        await rv.point_at_missing_story(engine, gone.job_id)
        assert (await gone.finished())["image_job"] == "target_gone"
        row = await rv.job(engine, gone.job_id)
        assert row["status"] == "target_gone"
        assert rv.staging(app, gone.job_id) == []
        assert (await rv.audit_reason(engine, gone.job_id) or {}).get("outcome") == "target_gone"


async def _tightening(
    app: sg.GenerationApp, engine: AsyncEngine, ids: dict[str, str], outcome: str
) -> None:
    """A story paused by a tightening while its image is checked: the job waits through the
    pause, then is attached (``promoted``) or ends ``stale`` (``policy_changed``) on the new
    rating's policy."""
    story, cast = await si.scene(app.client, 1)
    rating_id, policy = await rv.image_policy_rating(app.lib, engine)
    release = asyncio.Event()
    app.checks.stall, app.checks.stalled = release, asyncio.Event()
    app.checks.script = [PASS, PASS if outcome == "promoted" else policy.id]
    job_id = await sg.queued(app, story, **rv.consumer(1, [cast["Jenni"]]))
    app.generator.script.append(Outputs(size=sg.next_size()))
    task = rv.run_in_background(app, job_id)
    await until(app.checks.stalled.is_set, "the output check")
    await rv.tightened(engine, story, rating_id)
    release.set()
    waited = await asyncio.wait_for(task, 60)
    app.checks.stall = None
    assert (waited["image_job"], waited["waiting_on"], waited["requeue_in"]) == (
        "checking",
        "pause",
        30,
    )
    row = await rv.job(engine, job_id)
    assert row["status"] == "checking"
    assert row["status_detail"]["waiting_on"] == "pause"
    assert row["wait_started_at"] is not None
    assert len(rv.staging(app, job_id)) == 1
    assert await rv.reservations(engine, job_id) == ["settled"]
    read = (await app.client.get(f"/image-jobs/{job_id}", headers=HEADERS)).json()
    assert (read["status"], read["waiting_on"]) == ("checking", "pause")
    assert read["next_check_at"] is not None
    # Still paused: it waits again, with no check (no model call under a pause).
    checked = len(app.checks.checked)
    assert (await rv.rerun(app, job_id))["waiting_on"] == "pause"
    assert len(app.checks.checked) == checked
    assert (await rv.job(engine, job_id))["wait_started_at"] == row["wait_started_at"]
    # The check ends: the new rating's policy is in force, and the image is judged by it.
    await rv.set_status(engine, story, "active")
    done = await rv.rerun(app, job_id)
    assert len(app.checks.checked) == checked + 1
    checked_policies = {p[0] for p in (await rv.job(engine, job_id))["checked_policies"]}
    assert policy.id in checked_policies
    if outcome == "promoted":
        assert done["image_job"] == "promoted", done
        assert len(await _promoted(app, story, job_id)) == 1
        assert (
            "waiting_on"
            not in (await app.client.get(f"/image-jobs/{job_id}", headers=HEADERS)).json()
        )
    else:
        assert done["image_job"] == "stale", done
        row = await _ended_stale(app, story, job_id, "policy_changed", policy_ids=[policy.id])
        assert row["attempts"] == 1, "no regeneration"
    app.checks.script = [PASS]


@pytest.mark.integration
async def test_revalidate_unrelated_amendment(
    image_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """An amendment (#221) correcting a beat of another turn leaves the image of turn 2 valid:
    it promotes.

    Complement asserted: the same correction to a beat of turn 2 itself ends the job ``stale``
    with ``amended``."""
    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await rv.lite_scene(app)
        for turn, stales in ((1, False), (2, True)):
            running = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
            event_id = await rv.reply_event(engine, story, turn)
            amendment_id = await rv.amend_record(engine, story, "events", event_id)
            done = await running.finished()
            if stales:
                assert done["image_job"] == "stale", done
                await _ended_stale(app, story, running.job_id, "amended", amendment_id=amendment_id)
            else:
                assert done["image_job"] == "promoted", done


@pytest.mark.integration
async def test_pause_max_wait(
    image_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A job waiting on its paused story ends ``stale`` with ``paused_too_long`` once it has
    waited longer than ``image_pause_max_wait_s`` (default 24 h) in total, counted from its
    first wait. Each wait, run through the ``image_job`` handler, puts its ``jobs`` row back due
    in ``image_pause_recheck_s`` (``requeue_in``).

    Complement asserted: just inside the bound it still waits, keeping its staged bytes; and a
    story whose image job waits on it can be deleted (the job holds no lease), its job going
    with it."""
    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await rv.lite_scene(app)
        running = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        await rv.set_status(engine, story, "paused")
        assert (await running.finished())["waiting_on"] == "pause"
        started_at = (await rv.job(engine, running.job_id))["wait_started_at"]
        # Through the ``image_job`` handler, a wait is #70's ``requeue_in``, not a failure.
        output = await rv.handled(app, running.job_id)
        assert output.requeue_in == 30
        assert output.result == {"image_job": "checking", "waiting_on": "pause"}
        with clock.frozen_at(started_at + timedelta(seconds=86_400)):
            assert (await rv.rerun(app, running.job_id))["waiting_on"] == "pause"
        assert len(rv.staging(app, running.job_id)) == 1
        with clock.frozen_at(started_at + timedelta(seconds=86_401)):
            assert (await rv.rerun(app, running.job_id))["image_job"] == "stale"
        await _ended_stale(app, story, running.job_id, "paused_too_long")

        waiting_story, cast = await si.scene(app.client, 1)
        waiting = await rv.started(app, waiting_story, **rv.consumer(1, [cast["Jenni"]]))
        await rv.set_status(engine, waiting_story, "paused")
        assert (await waiting.finished())["waiting_on"] == "pause"
        deleted = await app.client.delete(f"/stories/{waiting_story}", headers=WRITE)
        assert deleted.status_code in (200, 204), deleted.text
        assert (await sg.counts(engine, waiting_story))["image_jobs"] == 0


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4")
async def test_i_25__m8_revalidate_point_in_time(
    image_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Revalidation is point in time: a turn truncated and untruncated while its image is being
    made is active at promotion, and the image is attached.

    Complement asserted: truncated alone, it ends ``stale`` with ``target_inactive``
    (``truncated``), and an untruncate afterwards never revives the ended job."""
    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await rv.lite_scene(app, 3)
        back = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        assert await truncate(app.lib.db, story, 1) is not None
        assert await untruncate(app.lib.db, story) is not None
        assert (await back.finished())["image_job"] == "promoted"
        assert len(await _promoted(app, story, back.job_id)) == 1

        cut = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        assert await truncate(app.lib.db, story, 1) is not None
        assert (await cut.finished())["image_job"] == "stale"
        await _ended_stale(app, story, cut.job_id, "target_inactive", cause="truncated")
        assert await untruncate(app.lib.db, story) is not None
        assert (await rv.rerun(app, cut.job_id))["image_job"] == "stale"
        assert await _promoted(app, story, cut.job_id) == []


class _LockSpy(ScriptedChecks):
    """Scripted checks that record, at each output check, whether the story's row could be
    locked by another transaction (``FOR UPDATE NOWAIT``): no check runs under its lock."""

    def __init__(self, engine: AsyncEngine, story_id: str, script: list[str]) -> None:
        super().__init__(script, fail_closed=False)
        self.engine, self.story_id = engine, story_id
        self.unlocked: list[bool] = []
        self.before_check: list[Any] = []

    async def check_output(
        self, runtime: image_jobs.ImageRuntime, job: Any, output: image_jobs.StagedOutput
    ) -> image_jobs.OutputVerdict:
        async with self.engine.connect() as conn:
            try:
                await conn.execute(
                    text("SELECT id FROM stories WHERE id = :s FOR UPDATE NOWAIT"),
                    {"s": self.story_id},
                )
                self.unlocked.append(True)
            except DBAPIError:
                self.unlocked.append(False)
            await conn.rollback()
        for hook in self.before_check:
            await hook()
        self.before_check.clear()
        return await super().check_output(runtime, job, output)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4")
async def test_i_25__m8_missing_policy_outside_lock(
    image_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A policy that comes into force while the image is checked has no result for it at
    promotion: the revalidation answers ``needs_evaluation``, the transaction ends without
    attaching, and the policy is evaluated with no lock on the story's row (the spy's
    ``FOR UPDATE NOWAIT`` succeeds at every check); then the job is attached when the image
    passes it.

    Complement asserted: when the image fails the new policy, the job ends ``stale`` with
    ``policy_changed`` naming it, and is not generated again."""
    async with sg.generation_app(image_settings, engine) as app:
        for outcome in ("promoted", "stale"):
            story, ids = await si.scene(app.client, 1)
            await app.configure()
            rating_id, policy = await rv.image_policy_rating(app.lib, engine)
            spy = _LockSpy(engine, story, [PASS, PASS if outcome == "promoted" else policy.id])
            spy.before_check.append(lambda s=story, r=rating_id: rv.rated(engine, s, r))
            with installed_checks(spy):
                job_id = await sg.queued(app, story, **rv.consumer(1, [ids["Jenni"]]))
                done = await sg.run(app, job_id)
            assert spy.unlocked == [True, True], spy.unlocked
            checked = {p[0] for p in (await rv.job(engine, job_id))["checked_policies"]}
            assert policy.id in checked
            assert done["image_job"] == outcome, done
            if outcome == "stale":
                row = await _ended_stale(
                    app, story, job_id, "policy_changed", policy_ids=[policy.id]
                )
                assert row["attempts"] == 1
            else:
                assert len(await _promoted(app, story, job_id)) == 1


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3,4")
async def test_i_25__m8_wait_exempt_from_staging_sweep(
    image_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A job waiting on its paused story past ``staging_ttl`` keeps its staged bytes through the
    staging sweep (its job is not terminal), and is attached from them once the story resumes.

    Complement asserted: a terminal job's leftover staging prefix of the same age is swept."""
    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await rv.lite_scene(app)
        store = app.lib.image_runtime().media
        leftover = await rv.ended_leftover(app, story, ids)
        running = await rv.started(app, story, **rv.consumer(2, [ids["Jenni"]]))
        await rv.set_status(engine, story, "paused")
        assert (await running.finished())["waiting_on"] == "pause"
        ttl = 600
        backdate(store.image_staging_dir(running.job_id), ttl + 60)
        backdate(store.image_staging_dir(leftover), ttl + 60)
        swept = await image_jobs.sweep_staging(engine, store, ttl_s=ttl)
        assert swept["skipped_running"] >= 1
        assert len(rv.staging(app, running.job_id)) == 1
        assert rv.staging(app, leftover) == []
        await rv.set_status(engine, story, "active")
        assert (await rv.rerun(app, running.job_id))["image_job"] == "promoted"
        assert len(await _promoted(app, story, running.job_id)) == 1
