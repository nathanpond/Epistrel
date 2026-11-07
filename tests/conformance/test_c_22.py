"""C-22's M8 parts: image guardrails in the job pipeline (FR-IMG-3; #256).

- Fail closed: with an image-output policy applying (the seeded minors policy) and no
  ``image_check`` model, a generated image is deleted from staging and the job ends ``failed``
  naming ``image_check_unconfigured`` and the policy; generation routes refuse such a request
  at enqueue (``require_image_checker``).
- The bounded retry budget (FR-GUARD-6): only an attempt with no passing output is retried,
  ``image_retry_budget`` times, with the requested seed reused and an unseeded request seeded
  afresh each attempt.

#258 adds ``test_c_22__m8_output`` here. Every C-22 M8 part lives in this file (the
traceability gate takes one file per test ID and milestone)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ids import new_ulid
from epistrel.core.images import store as image_store
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.engine import Engine
from epistrel.orchestrator.images import errors as adapter_errors
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.registry import ImageCheckUnconfigured, ImageNotConfigured
from epistrel.testing.images import (
    PASS,
    ScriptedChecks,
    TestImageTarget,
    installed_checks,
    registered_target,
)
from tests.image_job_support import (
    CTX,
    MINORS,
    PRINCIPAL,
    PROMPT,
    blobs,
    configure,
    counts,
    image_engine,
    image_settings_for,
    insert,
    prepared,
    staged,
)
from tests.image_support import Fail, Outputs, StubImageGenerator, catalog_origin, registered

ADAPTER = "stub_sync"


@pytest.fixture
def image_settings(pg_url: str, tmp_path: Path) -> Settings:
    return image_settings_for(pg_url, tmp_path)


@pytest.fixture
async def lib(image_settings: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    async with image_engine(image_settings, engine) as epistrel:
        yield epistrel


@pytest.fixture
def target() -> Iterator[TestImageTarget]:
    with registered_target() as found:
        yield found


class CatalogTarget(TestImageTarget):
    """The test target as a catalog job's: its outputs are candidates."""

    success_status = ImageJobStatus.CANDIDATES_READY

    async def result_ids(self, conn: Any, job: Any) -> tuple[str, list[str]]:
        return "candidate_ids", list(job.asset_ids)


async def _run(lib: Engine, job_id: str, failures: int = 0) -> dict[str, Any]:
    return await image_jobs.run_image_job(lib.image_runtime(), job_id, failures)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3")
async def test_c_22__m8_fail_closed(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """With the seeded minors policy on and no ``image_check`` model, a job that generated an
    image (inserted past the enqueue refusal: the in-job backstop) ends ``failed`` with
    ``status_reason: image_check_unconfigured`` and the minors policy among its ``policy_ids``:
    no media row, no blob outside ``tmp/images/`` (staging is emptied too) and no history
    entry, and an ``image_job_ended`` audit row names the policy.

    Complement asserted: with the checker set, the same job promotes."""
    stub = StubImageGenerator(script=[Outputs(), Outputs()])
    story_id = new_ulid()
    with registered(ADAPTER, stub), installed_checks(ScriptedChecks()):
        await configure(lib, image_settings, ADAPTER, check=False)
        before, stored = await counts(engine, story_id), blobs(lib.media)
        job_id = await insert(lib, prepared(story_id))
        result = await _run(lib, job_id)
        assert result == {"image_job": "failed", "reason": "image_check_unconfigured"}
        view = await lib.get_image_job(CTX, job_id)
        assert (view.status, view.status_reason) == ("failed", "image_check_unconfigured")
        assert view.policy_ids is not None
        assert MINORS in view.policy_ids
        assert view.result_ids == ()
        assert len(stub.requests) == 1
        assert await counts(engine, story_id) == before
        assert blobs(lib.media) == stored
        assert staged(lib.media, job_id) == []
        async with engine.connect() as conn:
            audited = (
                await conn.execute(
                    text(
                        "SELECT policy_ids, details FROM audit_log "
                        "WHERE kind = 'image_job_ended' AND details->>'job_id' = :j"
                    ),
                    {"j": job_id},
                )
            ).one()
        assert MINORS in audited.policy_ids
        assert audited.details["reason"] == "image_check_unconfigured"

        await configure(lib, image_settings, ADAPTER, check=True)
        promoted = await insert(lib, prepared(story_id))
        assert (await _run(lib, promoted))["image_job"] == "promoted"
        assert len((await lib.get_image_job(CTX, promoted)).result_ids) == 1


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3")
async def test_c_22__m8_require_checker(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """``require_image_checker``: with an adapter configured and ``image_check`` unset it raises
    ``ImageCheckUnconfigured`` naming the minors policy, and an enqueue is refused with 409
    ``image_check_unconfigured`` before any job row exists; with no adapter, ``not_configured``
    wins (it is checked first).

    Complement asserted: with the checker set it returns, and the enqueue writes its job."""
    rt = lib.image_runtime()
    request = {"prompt": PROMPT}
    with pytest.raises(ImageNotConfigured):
        await image_jobs.require_image_checker(lib.models, lib.db)
    stub = StubImageGenerator(script=[])
    with registered(ADAPTER, stub), installed_checks(ScriptedChecks()):
        await configure(lib, image_settings, ADAPTER, check=False)
        with pytest.raises(ImageCheckUnconfigured) as unset:
            await image_jobs.require_image_checker(lib.models, lib.db)
        assert MINORS in unset.value.policy_ids
        with pytest.raises(image_jobs.ImageCheckRequired) as refused:
            await image_jobs.enqueue_image_job(
                rt, ImageJobKind.STORY, {}, prepared().origin, request, None, PRINCIPAL
            )
        assert (refused.value.code, refused.value.http_status) == ("image_check_unconfigured", 409)
        assert MINORS in refused.value.details["policy_ids"]
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM image_jobs")) == 0

        await configure(lib, image_settings, ADAPTER, check=True)
        await image_jobs.require_image_checker(lib.models, lib.db)
        queued = await image_jobs.enqueue_image_job(
            rt, ImageJobKind.STORY, {}, prepared().origin, request, None, PRINCIPAL
        )
        assert (await lib.get_image_job(CTX, queued.job_id)).status == "queued"
        assert stub.requests == []


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3")
async def test_c_22__m8_retry_budget(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """A checker failing every output makes exactly ``1 + image_retry_budget`` (3) adapter
    calls, then the job fails ``guardrail_violation`` naming the policy; one failing once
    promotes on the second attempt. A requested seed is reused on the retry; an unseeded
    request gets a fresh engine-minted seed per attempt, recorded as applied. A catalog job of
    four with one passing output ends ``candidates_ready`` with it and makes no retry. Provider
    errors use no retry budget: a retryable one requeues the job (and fails it
    ``provider_unavailable`` on the last try), a non-retryable one ends it naming its code.

    Complements asserted: the passing retry promoted exactly one asset; the catalog job's
    adapter was called once."""
    violation = [MINORS]
    script: list[Any] = [Outputs()] * 3 + [Outputs()] * 2 + [Outputs()] * 2 + [Outputs(n=4)]
    stub = StubImageGenerator(script=script)
    with registered(ADAPTER, stub):
        await configure(lib, image_settings, ADAPTER)
        with installed_checks(ScriptedChecks(violation)):
            exhausted = await insert(lib)
            assert (await _run(lib, exhausted))["reason"] == "guardrail_violation"
        assert len(stub.requests) == 3
        view = await lib.get_image_job(CTX, exhausted)
        assert (view.status, view.status_reason, view.policy_ids) == (
            "failed",
            "guardrail_violation",
            (MINORS,),
        )
        assert view.attempts == 3

        with installed_checks(ScriptedChecks([MINORS, PASS])):
            seeded = await insert(lib, prepared(seed=7))
            assert (await _run(lib, seeded))["image_job"] == "promoted"
        assert [r.request.seed for r in stub.requests[3:5]] == [7, 7]
        assert len((await lib.get_image_job(CTX, seeded)).result_ids) == 1

        with installed_checks(ScriptedChecks([MINORS, PASS])):
            unseeded = await insert(lib)
            assert (await _run(lib, unseeded))["image_job"] == "promoted"
        first, second = (r.request.seed for r in stub.requests[5:7])
        assert first is not None
        assert second is not None
        assert first != second
        async with engine.connect() as conn:
            applied = list(
                await conn.scalars(
                    text(
                        "SELECT applied_parameters FROM image_reservations WHERE job_id = :j "
                        "ORDER BY created_at"
                    ),
                    {"j": unseeded},
                )
            )
        assert [(a["seed"], a["seed_source"]) for a in applied] == [
            (first, "engine"),
            (second, "engine"),
        ]

        with registered_target(CatalogTarget(), ImageJobKind.CATALOG):
            calls = len(stub.requests)
            with installed_checks(ScriptedChecks([MINORS, PASS, MINORS, MINORS])):
                catalog = await insert(
                    lib,
                    prepared(kind=ImageJobKind.CATALOG, origin=catalog_origin(new_ulid()), n=4),
                )
                assert (await _run(lib, catalog))["image_job"] == "candidates_ready"
            assert len(stub.requests) == calls + 1
            view = await lib.get_image_job(CTX, catalog)
            assert (view.status, view.result_field, len(view.result_ids)) == (
                "candidates_ready",
                "candidate_ids",
                1,
            )

    retrying = StubImageGenerator(
        script=[
            Fail(adapter_errors.ImageProviderUnavailable(status=503)),
            Fail(adapter_errors.ImageProviderUnavailable(status=503)),
            Fail(adapter_errors.ImageAdapterAuth(status=401)),
        ]
    )
    with registered(ADAPTER, retrying), installed_checks(ScriptedChecks()):
        await configure(lib, image_settings, ADAPTER)
        unavailable = await insert(lib)
        with pytest.raises(image_jobs.ImageJobRetry):
            await _run(lib, unavailable, failures=0)
        assert (await lib.get_image_job(CTX, unavailable)).status == "queued"
        last = await _run(lib, unavailable, failures=lib.settings.job_max_attempts - 1)
        assert last["image_job"] == "failed"
        view = await lib.get_image_job(CTX, unavailable)
        assert (view.status_reason, view.attempts, view.policy_ids) == (
            "provider_unavailable",
            2,
            None,
        )
        refused = await insert(lib)
        assert (await _run(lib, refused))["error"] == "image_adapter_auth"
        assert (await lib.get_image_job(CTX, refused)).status_reason == "adapter_auth"
        async with engine.connect() as conn:
            reservation = await image_store.reservations(conn, refused)
        assert [r.outcome.value if r.outcome else None for r in reservation] == ["failed"]
