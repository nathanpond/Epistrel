"""C-22's M8 parts: image guardrails in the job pipeline (FR-IMG-3; #256).

- Fail closed: with an image-output policy applying (the seeded minors policy) and no
  ``image_check`` model, a generated image is deleted from staging and the job ends ``failed``
  naming ``image_check_unconfigured`` and the policy; generation routes refuse such a request
  at enqueue (``require_image_checker``).
- The bounded retry budget (FR-GUARD-6): only an attempt with no passing output is retried,
  ``image_retry_budget`` times, with the requested seed reused and an unseeded request seeded
  afresh each attempt.

#257 adds ``test_c_22__m8_prompt`` (the prompt check before any generation); #258 adds
``test_c_22__m8_output`` (the output check on generated and uploaded images); #259 adds
``test_c_22__m8_image_prompt_policy`` (a built catalog prompt refused by a rating policy); #263
adds ``test_c_22__m8_story_no_checker`` (a story image request refused before any prompt-model
call). Every C-22 M8 part lives in this file (the traceability gate takes one file per test ID and
milestone)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.errors import GuardrailViolation
from epistrel.core.ids import new_ulid
from epistrel.core.images import store as image_store
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.core.stories import StorySpec, create_story_from
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.engine import Engine
from epistrel.orchestrator.images import errors as adapter_errors
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.registry import ImageCheckUnconfigured, ImageNotConfigured
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.policies.check import MINOR_INVOLVED
from epistrel.orchestrator.policies.schemas import PolicyFields
from epistrel.testing.images import (
    PASS,
    ScriptedChecks,
    TestImageTarget,
    installed_checks,
    registered_target,
)
from tests import catalog_image_support as cs
from tests import image_prompt_support as ip
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
from tests.image_safety_support import (
    ADULTS,
    WREN,
    jobs_of,
    prompt_checked,
    recognise_sexual,
    subject,
    whisper_story,
)
from tests.image_safety_support import MINORS as MINORS_CAST
from tests.image_safety_support import origin as safety_origin
from tests.image_support import (
    Fail,
    Outputs,
    StubImageChecker,
    StubImageGenerator,
    catalog_origin,
    registered,
)
from tests.media_support import WRITE, files, metadata, png, upload, uploaded, video
from tests.model_support import admin_client, put, put_key
from tests.policy_support import OPERATOR, unique

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


async def _rating_with_image_policy(lib: Engine, engine: AsyncEngine, word: str) -> tuple[str, str]:
    """A new rating whose own policy is a word list for image prompts and outputs, written
    through the admin path (FR-RATE-1; #257: rating policies may target images)."""
    rating = await create_rating(
        engine, RatingFields(name=unique("image-rating")), principal="p-operator"
    )
    body = PolicyFields.model_validate(
        {
            "scope": "rating",
            "scope_id": rating.rating.id,
            "name": unique("no-wyverns"),
            "rule": {"type": "wordlist", "words": [word]},
            "applies_to": ["image_prompt", "image_output"],
            "message": "No wyverns in pictures.",
        }
    )
    made = (await lib.policies_create(OPERATOR, body)).policy
    current = rating.rating
    await update_rating(
        engine,
        current.id,
        RatingReplacement(
            name=current.name,
            description=current.description,
            display_order=current.display_order,
            prompt_guidance=current.prompt_guidance,
            image_guidance=current.image_guidance,
            policy_ids=[made.id],
            inherits_from=[],
        ),
        principal="p-operator",
    )
    return current.id, made.id


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3; FR-GUARD-8,11")
async def test_c_22__m8_prompt(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """Every image prompt is checked before any generation (FR-IMG-3), by true ages.

    - The seeded minors policy applies to all four targets (migration ``0133``).
    - A prompt the builder wrote depicting Wren (16) in sexual content is refused with the
      minors policy's error before any adapter call (the stub generator is never called) and
      no job is created; a consumer's prompt naming Wren with an empty subject list is checked
      the same way.
    - A rating policy for ``image_prompt`` (accepted by the admin API) is enforced
      deterministically, and a refusal lists every violated policy.
    - An uncertain minors check follows ``on_uncertain`` (violation); a guardrail model that
      cannot answer is 503 ``model_unavailable`` with no job.

    Complements asserted: a non-sexual prompt of Wren passes and creates its job; the rating's
    word in a prompt of a story at another rating passes."""
    async with engine.connect() as conn:
        seeded = await conn.scalar(
            text("SELECT applies_to FROM policies WHERE id = :p"), {"p": MINORS}
        )
    assert seeded == ["text_input", "text_output", "image_prompt", "image_output"]
    stub = StubImageGenerator(script=[])
    guard = lib.models.stub
    story, ids = await whisper_story(engine)
    rt = lib.image_runtime()

    async def enqueue(story_id: str, prompt: str, **request: Any) -> str:
        found = await image_jobs.enqueue_image_job(
            rt,
            ImageJobKind.STORY,
            {},
            await safety_origin(engine, story_id),
            {"prompt": prompt, **request},
            None,
            PRINCIPAL,
        )
        return found.job_id

    with registered(ADAPTER, stub), prompt_checked():
        await configure(lib, image_settings, ADAPTER)
        guard.reset()
        recognise_sexual(guard, "undresses")
        built = "Wren, a boy of sixteen, undresses by the stove."
        requests: list[dict[str, Any]] = [
            {"depicted_subjects": [subject(ids[WREN], "a boy of sixteen")]},
            {"depicted_subjects": []},
        ]
        for request in requests:
            with pytest.raises(GuardrailViolation) as refused:
                await enqueue(story, built, **request)
            assert [v["policy_id"] for v in refused.value.details["violations"]] == [MINORS]
            assert "a minor is involved or present" in refused.value.message
        assert stub.requests == []
        assert await jobs_of(engine, story) == 0
        assert await enqueue(story, "Wren, a boy of sixteen, reads by the stove.")
        assert await jobs_of(engine, story) == 1

        # Uncertain follows on_uncertain; a model that cannot answer is 503 and no job.
        guard.reset()
        guard.script("guardrail_check", ["not a verdict"] * 20)
        with pytest.raises(GuardrailViolation):
            await enqueue(story, "Wren by the window.")
        guard.reset()
        guard.fault("guardrail_check", "http_500", 50)
        with pytest.raises(ModelUnavailable) as down:
            await enqueue(story, "Wren by the window.")
        assert (down.value.code, down.value.http_status) == ("model_unavailable", 503)
        assert await jobs_of(engine, story) == 1
        guard.reset()

        # A rating policy for image prompts: enforced deterministically, every violation listed.
        word = unique("wyvern").replace("-", "")
        rating, policy_id = await _rating_with_image_policy(lib, engine, word)
        rated = (
            await create_story_from(
                engine,
                StorySpec(
                    name="Rated",
                    content_rating_id=rating,
                    characters=[*ADULTS, MINORS_CAST[1]],
                ),
                principal="p-author",
            )
        ).story_id
        recognise_sexual(guard, "undresses")
        with pytest.raises(GuardrailViolation) as both:
            await enqueue(rated, f"A {word} watches as Wren undresses.")
        assert {v["policy_id"] for v in both.value.details["violations"]} == {MINORS, policy_id}
        with pytest.raises(GuardrailViolation) as one:
            await enqueue(rated, f"A {word} over the hills.")
        assert [v["policy_id"] for v in one.value.details["violations"]] == [policy_id]
        assert await enqueue(story, f"A {word} over the hills.")
    assert stub.requests == []


async def _audited(engine: AsyncEngine, kind: str) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT principal, policy_ids, details FROM audit_log WHERE kind = :k "
                        "ORDER BY at, id"
                    ),
                    {"k": kind},
                )
            ).all()
        )


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3")
async def test_c_22__m8_output(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """The output check (#258), through the pipeline's default checks and the stub vision
    checker.

    - A stub checker flags every generated image for the minors policy: each output is
      discarded, generation retries within ``image_retry_budget`` (1 + 2 adapter calls), and the
      job fails ``guardrail_violation`` naming the policy, leaving no media row, no blob and an
      ``image_output_rejected`` audit row per output.
    - An upload with no checker configured is stored ``unchecked``. With a checker, a violating
      upload is 422 ``guardrail_violation`` (FR-GUARD-10's envelope, "a minor is involved or
      present") and no row or blob is created, while a clean upload is stored ``passed``. A
      video's poster is checked the same way, and a refused poster refuses the whole video.

    Complements asserted: with the checker answering ``pass`` the job promotes one asset that is
    ``passed`` with the checker's model and endpoint and the minors policy's result; the
    refused upload wrote an ``image_upload_rejected`` audit row with no image hash."""
    stub = StubImageGenerator(script=[Outputs()] * 4)
    vision = StubImageChecker(lib.models.stub)
    story = (await whisper_story(engine, minors=False))[0]
    async with admin_client(image_settings, lib) as client:
        unchecked = await uploaded(client, caption="a quiet harbour")
        assert (unchecked["source"], unchecked["check_status"], unchecked["check"]) == (
            "uploaded",
            "unchecked",
            None,
        )
        with registered(ADAPTER, stub):
            await configure(lib, image_settings, ADAPTER)
            lib.models.stub.reset()
            vision.answer({MINORS: "violation"})
            before, stored = await counts(engine), blobs(lib.media)
            failing = await insert(lib, prepared(story))
            assert await _run(lib, failing) == {
                "image_job": "failed",
                "reason": "guardrail_violation",
            }
            assert len(stub.requests) == 3
            view = await lib.get_image_job(CTX, failing)
            assert (view.status, view.policy_ids, view.result_ids) == ("failed", (MINORS,), ())
            assert await counts(engine) == before
            assert blobs(lib.media) == stored
            assert staged(lib.media, failing) == []
            rejected = [
                r
                for r in await _audited(engine, "image_output_rejected")
                if r.details["job_id"] == failing
            ]
            assert len(rejected) == 3
            assert all(r.policy_ids == [MINORS] for r in rejected)
            assert len(vision.calls) == 3

            lib.models.stub.reset()
            vision.answer({MINORS: "pass"})
            passing = await insert(lib, prepared(story))
            assert (await _run(lib, passing))["image_job"] == "promoted"
            (asset_id,) = (await lib.get_image_job(CTX, passing)).result_ids
            asset = (await lib.get_media(CTX, asset_id)).model_dump(mode="json")
            assert (asset["source"], asset["check_status"]) == ("generated", "passed")
            assert asset["check"]["model"] == "stub-vision"
            assert asset["check"]["endpoint"]
            assert [(p["policy_id"], p["result"]) for p in asset["check"]["policies"]] == [
                (MINORS, "pass")
            ]

        lib.models.stub.reset()
        vision.answer({MINORS: "violation"}, when="QUILLSHOT")
        vision.answer({MINORS: "pass"})
        before, stored = await counts(engine), blobs(lib.media)
        audited = len(await _audited(engine, "image_upload_rejected"))
        refused = await upload(client, png(), metadata(caption="QUILLSHOT at the harbour"))
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "guardrail_violation"
        assert [v["policy_id"] for v in error["details"]["violations"]] == [MINORS]
        assert MINOR_INVOLVED in error["message"]
        assert await counts(engine) == before
        assert blobs(lib.media) == stored
        (row,) = (await _audited(engine, "image_upload_rejected"))[audited:]
        assert row.policy_ids == [MINORS]
        assert row.details["entry_point"] == "media_upload"
        assert not ({"sha256", "checksum"} & set(row.details))

        clean = await uploaded(client, caption="a quiet harbour at dawn")
        assert (clean["source"], clean["check_status"]) == ("uploaded", "passed")
        assert clean["check"]["model"] == "stub-vision"
        assert [p["policy_id"] for p in clean["check"]["policies"]] == [MINORS]

        # A video's poster is checked as an image upload is; a refused poster refuses the video.
        lib.models.stub.reset()
        vision.answer({MINORS: "violation"}, when="QUILLPOSTER")
        vision.answer({MINORS: "pass"})

        async def with_poster(caption: str) -> Any:
            return await client.post(
                "/media",
                files={
                    **files(
                        video(), metadata(kind="video", caption=caption), content_type="video/mp4"
                    ),
                    "poster_file": ("poster.png", png(), "image/png"),
                },
                headers=WRITE,
            )

        before, stored = await counts(engine), blobs(lib.media)
        refused_video = await with_poster("QUILLPOSTER at the pier")
        assert refused_video.status_code == 422, refused_video.text
        assert refused_video.json()["error"]["code"] == "guardrail_violation"
        assert await counts(engine) == before
        assert blobs(lib.media) == stored
        kept = await with_poster("the pier at noon")
        assert kept.status_code == 201, kept.text
        clip = kept.json()
        assert clip["check_status"] == "unchecked", "the image check judges images only"
        poster = (await client.get(f"/media/{clip['poster_media_id']}", headers=WRITE)).json()
        assert (poster["source"], poster["check_status"]) == ("uploaded", "passed")


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3")
async def test_c_22__m8_image_prompt_policy(image_settings: Settings, engine: AsyncEngine) -> None:
    """#259: a built catalog prompt goes through the same prompt check. A character draft
    under a rating whose own policy forbids a word in image prompts, whose built prompt holds
    that word: ``POST /image-prompts`` answers 422 ``guardrail_violation`` naming the policy,
    the body holds no prompt, an ``image_prompt_rejected`` audit row records it, and no job is
    created.

    Complement: the same draft, with a prompt that does not hold the word, answers 200 with
    the prompt."""
    word = unique("wyvern").replace("-", "")
    async with ip.prompt_app(image_settings, engine) as (client, lib):
        stub = lib.models.stub
        rating, policy_id = await _rating_with_image_policy(lib, engine, word)
        seen, hidden = await ip.references(client)
        body = ip.character_body(seen, hidden)
        body["metadata"]["content_rating_id"] = rating
        created = await ip.create_character(client, body)
        before = await ip.image_rows(engine)

        ip.answer(stub, f"a tall woman feeding a {word} on a pier")
        refused = await ip.build(client, "character", created["id"], "primary", draft=True)
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "guardrail_violation"
        assert [v["policy_id"] for v in error["details"]["violations"]] == [policy_id]
        assert word not in refused.text
        assert "prompt" not in refused.json()
        async with engine.connect() as conn:
            audited = (
                await conn.execute(
                    text(
                        "SELECT details FROM audit_log WHERE kind = 'image_prompt_rejected' "
                        "AND details->>'target_id' = :id"
                    ),
                    {"id": created["id"]},
                )
            ).all()
        assert len(audited) == 1
        assert audited[0].details["entry_point"] == "image_prompt"
        assert await ip.image_rows(engine) == before

        ip.answer(stub, "a tall woman on a pier")
        passed = await ip.built(client, "character", created["id"], "primary", draft=True)
        assert passed["prompt"].startswith("a tall woman on a pier")
        assert await ip.image_rows(engine) == before


# -- catalog candidates (#260) ------------------------------------------------------------------


async def _attempt_rows(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as conn:
        jobs = int(await conn.scalar(text("SELECT count(*) FROM image_jobs")) or 0)
        held = int(await conn.scalar(text("SELECT count(*) FROM image_reservations")) or 0)
    return jobs, held


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-3,7")
async def test_c_22__m8_candidate_no_checker(image_settings: Settings, engine: AsyncEngine) -> None:
    """A catalog image request with the image adapter configured and ``image_check`` unset,
    while the seeded minors policy applies to image outputs, is 409
    ``image_check_unconfigured`` naming it, with no ``image_reservations`` or ``image_jobs``
    row and no adapter call. Without an adapter it is 409 ``not_configured`` and makes no job
    either.

    Complement asserted: with the checker set, the same request is 202."""
    async with cs.candidate_app(image_settings, engine, adapter=False) as app:
        [created] = await cs.characters(app, 1)
        before = await _attempt_rows(engine)
        missing = await cs.generate(app, created["id"], n=2)
        assert missing.status_code == 409, missing.text
        assert missing.json()["error"]["code"] == "not_configured"
        assert await _attempt_rows(engine) == before

        async with admin_client(image_settings, app.lib) as admin:
            await put(admin, "image_generation", {"model": "img-1", "params": {"adapter": ADAPTER}})
            await put_key(admin, "image_generation", "sk-image-test-0001")
        unchecked = await cs.generate(app, created["id"], n=2)
        assert unchecked.status_code == 409, unchecked.text
        error = unchecked.json()["error"]
        assert error["code"] == "image_check_unconfigured"
        assert MINORS in error["details"]["policy_ids"]
        assert await _attempt_rows(engine) == before
        assert app.generator.requests == []

        async with admin_client(image_settings, app.lib) as admin:
            await put(admin, "image_check", {"model": "stub-vision"})
        accepted = await cs.generate(app, created["id"], n=2)
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers["location"] == f"/image-jobs/{accepted.json()['job_id']}"
        assert (await _attempt_rows(engine))[0] == before[0] + 1


@pytest.mark.integration
@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-3")
async def test_c_22__m8_story_no_checker(image_settings: Settings, engine: AsyncEngine) -> None:
    """A story image request with the image adapter configured and ``image_check`` unset (the
    seeded minors policy applies) is 409 ``image_check_unconfigured`` before anything else: no
    ``image_reservations`` row, no ``image_jobs`` row, no ``image_prompt`` call and no adapter
    call; a consumer prompt is refused the same way.

    Complement asserted: with an ``image_check`` model set, the same request is 202."""
    from tests import story_image_generation_support as sg
    from tests.image_job_support import configure as configure_images

    async with sg.generation_app(image_settings, engine) as app:
        scene = await sg.whisper(app, engine, check=False)
        story = scene.story_id
        body: dict[str, Any] = {
            "position": {"turn": 1, "beat": 3},
            "audience_viewpoint": sg.character(scene.ids["Bobby"]),
        }
        extras: list[dict[str, Any]] = [
            {},
            {"prompt": "A lamplit parlor.", "depicted_subjects": []},
        ]
        for extra in extras:
            refused = await sg.request(app, story, **body, **extra)
            assert refused.status_code == 409, refused.text
            assert refused.json()["error"]["code"] == "image_check_unconfigured"
            assert MINORS in refused.json()["error"]["details"]["policy_ids"]
        assert await sg.counts(engine, story) == {"image_jobs": 0, "image_reservations": 0}
        assert app.prompt_calls() == []
        assert app.generator.requests == []

        await configure_images(app.lib, image_settings, sg.ADAPTER, check=True)
        accepted = await sg.request(app, story, **body)
        assert accepted.status_code == 202, accepted.text
        assert len(app.prompt_calls()) == 1


@pytest.mark.integration
@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-3")
async def test_c_22__m8_auto_no_checker(image_settings: Settings, engine: AsyncEngine) -> None:
    """#265 (FR-IMG-3,6): a Lite story under ``illustration_mode = auto`` with the image adapter
    configured and ``image_check`` unset (the seeded minors policy applies). A reply marking a
    point commits and lists its job, which is always queued in the turn's commit. Run, the job
    ends ``failed`` with ``status_reason: image_check_unconfigured`` as its first step: no
    ``image_reservations`` row, no ``image_prompt`` call and no adapter call.

    Complement asserted: with an ``image_check`` model set, the next turn's job promotes."""
    from tests import auto_illustration_support as ai
    from tests import story_image_generation_support as sg
    from tests import story_image_support as si
    from tests.image_job_support import configure as configure_images

    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await si.scene(app.client, 0)
        await app.configure(check=False)
        bobby = ids[si.BOBBY]
        await ai.set_auto(app.client, story)

        played = await ai.lite_turn(app, story, bobby)
        assert played.status_code == 200, played.text
        [pending] = played.json()["pending_images"]
        ended = await sg.run(app, pending["job_id"])
        assert ended["image_job"] == "failed", ended
        view = await ai.job_view(app, pending["job_id"])
        assert (view["status"], view["status_reason"]) == ("failed", "image_check_unconfigured")
        assert MINORS in (view["policy_ids"] or [])
        assert await ai.reservations(engine, story) == 0
        assert app.prompt_calls() == []
        assert app.generator.requests == []

        await configure_images(app.lib, image_settings, sg.ADAPTER, check=True)
        again = await ai.lite_turn(app, story, bobby)
        [pending] = again.json()["pending_images"]
        done = await sg.run(app, pending["job_id"])
        assert done["image_job"] == "promoted", done
        assert len(app.generator.requests) == 1
