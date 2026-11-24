"""I-30's M9 parts: safety-checked images of a subject outside any story or catalog
(FR-IMG-14; #830).

``POST /subject-images`` runs as a background image job (kind ``subject``) through the catalog
image path's safety checks. These tests run the job's handler directly (``run_image_job``), so no
worker claims another test's job, on the stub image adapter (``StubImageGenerator``), with
#257's real prompt check (``prompt_checked``: the stub guardrail model, scripted where a test
needs a verdict) and scripted output checks (``ScriptedChecks``: an unscripted ``image_check``
model counts as uncertain and blocks, so every output check is scripted). Uploads are made before
the image checker is configured, which an ``image_check`` model would check too.

The offboarding part runs on a scratch database of its own: it runs the job worker, which on the
session database would claim other tests' jobs. Every I-30 M9 part lives in this file (the
traceability gate takes one file per test ID and milestone).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.images.perceived import redact_ages
from epistrel.core.media import sweep as media_sweep
from epistrel.core.ratings import RatingFields, create_rating
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.errors import ImageRequestInvalid
from epistrel.orchestrator.images.jobs import MINORS_POLICY_ID
from epistrel.orchestrator.images.port import AppliedParameters, ImageRequest, SubmitResult
from epistrel.orchestrator.images.targets.subject import SubjectImageRequest, SubjectSpec
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.testing.images import PASS, ScriptedChecks
from tests import image_prompt_support as ip
from tests.conformance.conftest import migrated_database
from tests.image_job_support import configure, image_settings_for
from tests.image_safety_support import (
    assert_no_age,
    figures,
    payload,
    prompt_checked,
    recognise_sexual,
    safety_calls,
)
from tests.image_support import Outputs, StubImageGenerator, registered
from tests.integration.ownership_fixtures import headers
from tests.media_support import png, uploaded
from tests.minors_support import SUBJECTS_HEADING
from tests.model_support import admin_client, put
from tests.offboarding_support import OFFBOARDING, run_jobs
from tests.policy_support import OPERATOR as POLICY_OPERATOR
from tests.policy_support import global_policy, rated_policy_rating, unique, words
from tests.story_support import WRITE, create, story_body

ADAPTER = "stub_sync"
PATH = "/subject-images"
OWNER = "p-subject"
HEADERS = headers(OWNER)
LABEL = "Contestant SEVEN-830"
"""The consumer's label: never in a log line or an audit row."""
DESCRIPTION = "Weathered fisherman with a grey beard and a yellow oilskin coat."
UNSAFE = "posed provocatively in lingerie"
TRUE_AGE = 52
STUB_DIGEST = re.compile(r"\bstub image [0-9a-f]{12}\b")
"""The stub ``image_prompt`` answer's request hash (``ImagePromptResult.stub_example``)."""
MINOR_AGE = 15


def subject(**fields: Any) -> dict[str, Any]:
    return {"description": DESCRIPTION, "true_age": TRUE_AGE, "label": LABEL, **fields}


def body(**fields: Any) -> dict[str, Any]:
    return {"subject": subject(), **fields}


@pytest.fixture
def image_settings(pg_url: str, tmp_path: Path) -> Settings:
    return image_settings_for(pg_url, tmp_path)


@dataclass
class SubjectApp:
    client: httpx.AsyncClient
    lib: Engine
    generator: StubImageGenerator
    checks: ScriptedChecks
    settings: Settings

    @property
    def guard(self) -> StubAdapter:
        stub = self.lib.models.stub
        assert stub is not None
        return stub

    async def configure(self, *, check: bool = True) -> None:
        await configure(self.lib, self.settings, ADAPTER, check=check)

    async def request(
        self, payload: dict[str, Any], *, key: str | None = None, principal: str = OWNER
    ) -> httpx.Response:
        return await self.client.post(PATH, json=payload, headers=headers(principal, key))

    async def run(self, job_id: str) -> dict[str, Any]:
        return await image_jobs.run_image_job(self.lib.image_runtime(), job_id, 0)

    async def job(self, job_id: str) -> dict[str, Any]:
        found = await self.client.get(f"/image-jobs/{job_id}", headers=HEADERS)
        assert found.status_code == 200, found.text
        result: dict[str, Any] = found.json()
        return result

    async def generated(
        self, outputs: Outputs, *, principal: str = OWNER, **fields: Any
    ) -> tuple[str, dict[str, Any]]:
        """Request, run and read a job that completes: ``(job_id, job)``."""
        self.generator.script.append(outputs)
        accepted = await self.request(body(n=outputs.n, **fields), principal=principal)
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        done = await self.run(job_id)
        assert done["image_job"] == "completed", done
        return job_id, await self.job(job_id)

    async def asset(self, asset_id: str) -> dict[str, Any]:
        found = await self.client.get(f"/media/{asset_id}", headers=HEADERS)
        assert found.status_code == 200, found.text
        result: dict[str, Any] = found.json()
        return result


@asynccontextmanager
async def subject_app(
    settings: Settings,
    engine: AsyncEngine,
    checks: ScriptedChecks | None = None,
    generator: StubImageGenerator | None = None,
) -> AsyncIterator[SubjectApp]:
    """The REST app with the stub model adapter, the stub image adapter (``generator``, else a
    plain one), #257's real prompt check and scripted output checks (passing, unless given)."""
    generator = generator or StubImageGenerator()
    async with ip.prompt_app(settings, engine) as (client, lib):
        with registered(ADAPTER, generator), prompt_checked(checks) as scripted:
            yield SubjectApp(client, lib, generator, scripted, settings)


async def subject_jobs(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(text("SELECT count(*) FROM image_jobs WHERE kind = 'subject'")) or 0
        )


async def job_assets(engine: AsyncEngine, job_id: str) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT id, state, origin, source, owner_principal FROM media_assets "
                        "WHERE source_job_id = :j ORDER BY output_index"
                    ),
                    {"j": job_id},
                )
            ).all()
        )


async def asset_state(engine: AsyncEngine, asset_id: str) -> str | None:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT state FROM media_assets WHERE id = :a"), {"a": asset_id}
        )
    return None if found is None else str(found)


def error_of(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    found = response.json()["error"]
    return response.status_code, str(found["code"]), dict(found.get("details") or {})


# -- the request and the job ------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_request(
    image_settings: Settings, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A subject image request answers 202 ``{job_id}`` with ``Location: /image-jobs/{id}``; the
    job completes, and ``GET /image-jobs/{id}`` reports ``kind: subject``, ``completed``, its
    ``asset_ids`` and no ``failures``. The asset is live, owned by the caller, ``origin:
    subject``, ``source: generated``, under the deployment default rating, depicting ``{kind:
    subject, id, label, true_age}``. The image adapter never receives the true age. A repeat
    under the same ``Idempotency-Key`` answers the same job (one job, one provider call); the
    library call does what REST does. The label and description never reach a log line.

    Complement asserted: a different key makes a second job."""
    caplog.set_level(logging.DEBUG)
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        app.guard.reset()
        app.generator.script.append(Outputs(n=1, size=(83, 61)))
        key = f"subject-{secrets.token_hex(4)}"
        accepted = await app.request(body(), key=key)
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        assert accepted.headers["location"] == f"/image-jobs/{job_id}"
        assert accepted.json()["status"] == "queued"
        replay = await app.request(body(), key=key)
        assert replay.status_code == 202, replay.text
        assert replay.json()["job_id"] == job_id
        assert (await app.run(job_id))["image_job"] == "completed"
        job = await app.job(job_id)
        assert (job["kind"], job["status"], job["status_reason"]) == ("subject", "completed", None)
        assert job["failures"] == []
        assert "candidate_ids" not in job
        [asset_id] = job["asset_ids"]
        assert len(app.generator.requests) == 1

        asset = await app.asset(asset_id)
        assert (asset["state"], asset["owner_principal"], asset["origin"], asset["source"]) == (
            "live",
            OWNER,
            "subject",
            "generated",
        )
        assert asset["content_rating_id"] == EVERYONE_ID
        [declared] = asset["depicted_subjects"]
        assert (declared["kind"], declared["label"], declared["true_age"]) == (
            "subject",
            LABEL,
            TRUE_AGE,
        )
        assert asset["check_status"] == "passed"

        sent = app.generator.requests[0].request
        # The stub model's prompt is named by its request's hash, whose hex digits may spell the
        # age by chance (#1012): the hash is not model input, so it is left out of the check.
        prompt = STUB_DIGEST.sub("stub image", sent.prompt)
        assert_no_age({"prompt": prompt, "negative_prompt": sent.negative_prompt}, TRUE_AGE)

        other = await app.request(body(), key=f"subject-{secrets.token_hex(4)}")
        assert other.status_code == 202, other.text
        assert other.json()["job_id"] != job_id

        lib_key = f"subject-lib-{secrets.token_hex(4)}"
        ctx = RequestContext(principal=OWNER, idempotency_key=lib_key)
        request = SubjectImageRequest(subject=SubjectSpec(**subject()), n=1)
        queued = await app.lib.request_subject_images(ctx, request)
        assert queued.location == f"/image-jobs/{queued.job_id}"
        again = await app.lib.request_subject_images(ctx, request)
        assert again.job_id == queued.job_id
        app.generator.script.append(Outputs(n=1, size=(83, 62)))
        assert (await app.run(queued.job_id))["image_job"] == "completed"
        assert len((await app.job(queued.job_id))["asset_ids"]) == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert LABEL not in logged
    assert DESCRIPTION not in logged


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_refusals(image_settings: Settings, engine: AsyncEngine) -> None:
    """Every refusal comes before any job or provider call: 409 ``not_configured`` without an
    image adapter; 422 ``validation_error`` for ``n`` outside 1..4, a true age outside 0..150, a
    blank or over-long description, a label over 120 characters or with a control character, a
    style over 1,000 characters, a seed or size parameter, a missing subject; 422 for an unknown
    ``content_rating`` (at ``body.content_rating``). The library refuses the same bodies.

    Complement asserted: a request at every bound (``n`` 4, true age 0 and 150, a 120-character
    label, a 4,000-character description, a 1,000-character style) is accepted."""
    async with subject_app(image_settings, engine) as app:
        unconfigured = await app.request(body())
        assert error_of(unconfigured)[:2] == (409, "not_configured")
        await app.configure()
        jobs_before = await subject_jobs(engine)
        for payload, field in (
            (body(n=5), "body.n"),
            (body(n=0), "body.n"),
            ({"subject": subject(true_age=151)}, "body.subject.true_age"),
            ({"subject": subject(true_age=-1)}, "body.subject.true_age"),
            ({"subject": subject(description="   ")}, "body.subject.description"),
            ({"subject": subject(description="x" * 4_001)}, "body.subject.description"),
            ({"subject": subject(label="L" * 121)}, "body.subject.label"),
            ({"subject": subject(label="two\nlines")}, "body.subject.label"),
            (body(style="s" * 1_001), "body.style"),
            (body(seed=7), "body.seed"),
            (body(size="1024x1024"), "body.size"),
            ({"n": 1}, "body.subject"),
        ):
            status, code, details = error_of(await app.request(payload))
            assert (status, code) == (422, "validation_error"), payload
            assert str(details.get("field", "")).startswith(field), (payload, details)
        unknown = await app.request(body(content_rating="no-such-rating"))
        status, code, details = error_of(unknown)
        assert (status, code, details["field"]) == (422, "validation_error", "body.content_rating")
        assert await subject_jobs(engine) == jobs_before
        assert app.generator.requests == []
        with pytest.raises(ValidationError):
            SubjectImageRequest(subject=SubjectSpec(**subject()), n=5)

        edge = {
            "subject": subject(description="d" * 4_000, label="L" * 120, true_age=150),
            "style": "s" * 1_000,
            "n": 4,
        }
        assert (await app.request(edge)).status_code == 202
        young = {"subject": subject(true_age=0)}
        assert (await app.request(young)).status_code == 202


class RatioDropper(StubImageGenerator):
    """A stub image adapter whose model cannot apply a ratio: it reports the request's
    parameters as requested and the ratio as dropped, and returns its scripted size."""

    async def submit(self, request: ImageRequest, idempotency_key: str | None) -> SubmitResult:
        result = await super().submit(request, idempotency_key)
        requested = {"n": request.n, "size": request.size, "aspect_ratio": request.aspect_ratio}
        applied = AppliedParameters(
            requested={k: v for k, v in requested.items() if v is not None},
            dropped_parameters=("aspect_ratio",) if request.aspect_ratio else (),
        )
        return replace(result, applied_parameters=applied)


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_aspect_ratio(image_settings: Settings, engine: AsyncEngine) -> None:
    """#1012: a subject request's ``aspect_ratio`` wins over the deployment's ``default_size``:
    the provider call carries ``aspect_ratio: "2:3"`` and no size, and the asset records the
    ratio as requested. The stub model cannot apply it, so the image is still made: its applied
    parameters name ``aspect_ratio`` as dropped, and the asset's ``width``/``height`` are what
    came back. An unlisted ratio (``5:4``) is 422 at ``body.aspect_ratio`` with no job.

    Complement asserted: with no ``aspect_ratio`` the provider call carries the deployment's
    ``default_size`` and no ratio, exactly as before."""
    async with subject_app(image_settings, engine, generator=RatioDropper()) as app:
        await app.configure()
        async with admin_client(image_settings, app.lib) as admin:
            await put(
                admin,
                "image_generation",
                {"model": "img-1", "params": {"adapter": ADAPTER, "default_size": "512x512"}},
            )
        jobs_before = await subject_jobs(engine)
        status, code, details = error_of(await app.request(body(aspect_ratio="5:4")))
        assert (status, code, details["field"]) == (422, "validation_error", "body.aspect_ratio")
        assert await subject_jobs(engine) == jobs_before
        assert app.generator.requests == []

        _, job = await app.generated(Outputs(n=1, size=(83, 61)), aspect_ratio="2:3")
        sent = app.generator.requests[-1].request
        assert (sent.aspect_ratio, sent.size) == ("2:3", None)
        asset = await app.asset(job["asset_ids"][0])
        metadata = asset["generation_metadata"]
        assert metadata["requested_parameters"]["aspect_ratio"] == "2:3"
        assert metadata["requested_parameters"]["size"] is None
        assert metadata["applied_parameters"]["requested"]["aspect_ratio"] == "2:3"
        assert "aspect_ratio" in metadata["applied_parameters"]["dropped_parameters"]
        assert (asset["width"], asset["height"]) == (83, 61)

        await app.generated(Outputs(n=1, size=(83, 62)))
        plain = app.generator.requests[-1].request
        assert (plain.aspect_ratio, plain.size) == (None, "512x512")


# -- the safety path --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_minor_refused(image_settings: Settings, engine: AsyncEngine) -> None:
    """A subject under the minor threshold with a sexualizing description is refused by the
    minors policy before anything is generated: 422 ``guardrail_violation`` naming the policy,
    no job, **zero** generation calls, and one ``image_prompt_rejected`` audit row for the
    subject (``target_kind: subject``) that carries neither its label nor its description. The
    check judged the declared true age: the minors check received the subject at 15.

    Complement asserted: the same description for an adult subject passes the minors policy
    (202), and a minor with an ordinary description passes too."""
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        app.guard.reset()
        recognise_sexual(app.guard, UNSAFE)
        before = await subject_jobs(engine)
        unsafe = f"{DESCRIPTION} {UNSAFE}"
        refused = await app.request({"subject": subject(description=unsafe, true_age=MINOR_AGE)})
        status, code, details = error_of(refused)
        assert (status, code) == (422, "guardrail_violation"), refused.text
        assert [v["policy_id"] for v in details["violations"]] == [MINORS_POLICY_ID]
        assert app.generator.requests == []
        assert await subject_jobs(engine) == before
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT principal, policy_ids, details FROM audit_log "
                        "WHERE kind = 'image_prompt_rejected' AND principal = :p "
                        "ORDER BY at DESC LIMIT 1"
                    ),
                    {"p": OWNER},
                )
            ).one()
        assert row.policy_ids == [MINORS_POLICY_ID]
        assert row.details["target_kind"] == "subject"
        assert LABEL not in json.dumps(row.details)
        assert UNSAFE not in json.dumps(row.details)
        minors_calls = [
            text_of
            for text_of in ("\n".join(m.content for m in c.messages) for c in app.guard.calls)
            if SUBJECTS_HEADING in text_of and UNSAFE in text_of
        ]
        assert minors_calls
        assert all(f"- subject_1: age {MINOR_AGE}" in p for p in minors_calls), minors_calls[0]
        assert all(LABEL not in p for p in minors_calls)

        adult = await app.request({"subject": subject(description=unsafe, true_age=30)})
        assert adult.status_code == 202, adult.text
        ordinary = await app.request({"subject": subject(true_age=MINOR_AGE)})
        assert ordinary.status_code == 202, ordinary.text
        assert app.generator.requests == []


def word_of(prefix: str) -> str:
    """A word no other test's policy lists: the policy matches only this test's requests."""
    return unique(prefix).replace("-", "")


def refused_by(response: httpx.Response) -> list[str]:
    """The policies a 422 ``guardrail_violation`` names."""
    assert response.status_code == 422, response.text
    status, code, details = error_of(response)
    assert (status, code) == (422, "guardrail_violation"), response.text
    return [str(v["policy_id"]) for v in details["violations"]]


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_policy_scopes(image_settings: Settings, engine: AsyncEngine) -> None:
    """A subject image request is judged by the image-prompt policies outside the seeded minors
    policy (#844): a global word-list policy refuses a description holding its word, and a
    rating policy (listed by its rating, as C-22 writes it) refuses one requested at that
    rating. Each refusal is 422 ``guardrail_violation`` naming the policy, before any job or
    generation call. (No principal-scoped policy exists: a policy's scope is global, rating,
    story or character.)

    Complement asserted: the rating policy's word passes under another rating (the deployment
    default), and a description holding neither word passes at the policy's rating."""
    global_word, rating_word = word_of("kraken"), word_of("wyvern")
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        app.guard.reset()
        rating_id, rated = await rated_policy_rating(
            app.lib, engine, words(rating_word), applies_to=["image_prompt"]
        )
        made = await global_policy(app.lib, words(global_word), applies_to=["image_prompt"])
        try:
            before = await subject_jobs(engine)
            by_global = await app.request(
                {"subject": subject(description=f"{DESCRIPTION} Holding a {global_word}.")}
            )
            assert refused_by(by_global) == [made.id]
            by_rating = await app.request(
                {
                    "subject": subject(description=f"{DESCRIPTION} Holding a {rating_word}."),
                    "content_rating": rating_id,
                }
            )
            assert refused_by(by_rating) == [rated.id]
            assert await subject_jobs(engine) == before
            assert app.generator.requests == []

            elsewhere = await app.request(
                {"subject": subject(description=f"{DESCRIPTION} Holding a {rating_word}.")}
            )
            assert elsewhere.status_code == 202, elsewhere.text
            plain = await app.request(body(content_rating=rating_id))
            assert plain.status_code == 202, plain.text
        finally:
            await app.lib.policies_retire(POLICY_OPERATOR, made.id)


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_figures(image_settings: Settings, engine: AsyncEngine) -> None:
    """The figure classification sees the subject request's description (#844): an adult
    subject whose description names an extra figure that the classification answers ``minor``,
    with a sexualizing phrase, is refused 422 ``guardrail_violation`` by the minors policy, and
    the minors check received that figure as a minor figure.

    Complement asserted: the same description without the extra figure passes (202)."""
    extra = "beside a young girl"
    unsafe = f"{DESCRIPTION} {UNSAFE}"
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        app.guard.reset()
        recognise_sexual(app.guard, UNSAFE)
        figures(app.guard, extra, ("a young girl", "minor"))
        before = await subject_jobs(engine)
        refused = await app.request({"subject": subject(description=f"{unsafe} {extra}.")})
        assert refused_by(refused) == [MINORS_POLICY_ID]
        assert await subject_jobs(engine) == before
        minors_checks = [
            payload(c)
            for c in safety_calls(app.guard)
            if SUBJECTS_HEADING in payload(c) and UNSAFE in payload(c)
        ]
        assert any("figure_1: minor figure; a young girl" in p for p in minors_checks)
        assert all(f"- subject_1: age {TRUE_AGE}" in p for p in minors_checks)

        passed = await app.request({"subject": subject(description=unsafe)})
        assert passed.status_code == 202, passed.text
        assert app.generator.requests == []


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_retired_rating(image_settings: Settings, engine: AsyncEngine) -> None:
    """A subject image request naming a retired content rating (retired through ``DELETE
    /admin/content-ratings/{id}``) answers 422 ``validation_error`` with ``{field:
    body.content_rating, reason: retired, rating_id}``, before any job.

    Complement asserted: the same rating, while active, is accepted (202)."""
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        rating = await create_rating(
            engine, RatingFields(name=unique("subject-rating")), principal="p-operator"
        )
        rating_id = rating.rating.id
        active = await app.request(body(content_rating=rating_id))
        assert active.status_code == 202, active.text

        retired = await app.client.delete(
            f"/admin/content-ratings/{rating_id}", headers=headers("p-operator")
        )
        assert retired.status_code == 200, retired.text
        before = await subject_jobs(engine)
        status, code, details = error_of(await app.request(body(content_rating=rating_id)))
        assert (status, code) == (422, "validation_error")
        assert {k: details.get(k) for k in ("field", "reason", "rating_id")} == {
            "field": "body.content_rating",
            "reason": "retired",
            "rating_id": rating_id,
        }
        assert await subject_jobs(engine) == before


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_flagged_outputs(image_settings: Settings, engine: AsyncEngine) -> None:
    """An output the output check flags is never stored and is reported in the job: with
    ``n=2`` and the second flagged, the job completes with one asset and ``failures`` ``[{index:
    1, reason: guardrail_violation}]``; with ``n=3`` and one flagged, exactly 2 assets exist.

    Complement asserted: when every output is flagged on every attempt, the job ends ``failed``
    (``guardrail_violation``) with no asset at all."""
    checks = ScriptedChecks(fail_closed=False)
    async with subject_app(image_settings, engine, checks) as app:
        await app.configure()
        checks.script = [PASS, MINORS_POLICY_ID, PASS]
        job_id, job = await app.generated(Outputs(n=2, size=(91, 57)))
        assert len(job["asset_ids"]) == 1
        assert job["failures"] == [{"index": 1, "reason": "guardrail_violation"}]
        assert [r.state for r in await job_assets(engine, job_id)] == ["live"]

        checks.script = [PASS, MINORS_POLICY_ID, PASS, PASS]
        job_id, job = await app.generated(Outputs(n=3, size=(91, 58)))
        stored = await job_assets(engine, job_id)
        assert len(stored) == 2
        assert sorted(job["asset_ids"]) == sorted(str(r.id) for r in stored)
        assert job["failures"] == [{"index": 1, "reason": "guardrail_violation"}]
        assert {(r.origin, r.owner_principal) for r in stored} == {("subject", OWNER)}

        checks.script = [MINORS_POLICY_ID]
        app.generator.script.extend(Outputs(n=1, size=(91, 59)) for _ in range(12))
        accepted = await app.request(body())
        assert accepted.status_code == 202, accepted.text
        failed_id = accepted.json()["job_id"]
        assert await app.run(failed_id) == {"image_job": "failed", "reason": "guardrail_violation"}
        failed = await app.job(failed_id)
        assert (failed["status"], failed["asset_ids"]) == ("failed", [])
        assert "failures" not in failed
        assert await job_assets(engine, failed_id) == []
        app.generator.script.clear()


# -- a consumer-written prompt (#1017) -------------------------------------------------------

CANARY = "CANARY-1017-QUILLFEATHER"
"""A distinctive word in the consumer prompt: never in a log line."""
CONSUMER_PROMPT = (
    f"Full-length photo of a tall {TRUE_AGE}-year-old fisherman, aged {TRUE_AGE}, in a yellow "
    f"oilskin coat on a rainy quay, head to toe in frame, {CANARY}"
)


async def job_origin(engine: AsyncEngine, job_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT origin FROM image_jobs WHERE id = :j"), {"j": job_id}
        )
    return dict(found)


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_consumer_prompt(
    image_settings: Settings, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """#1017: with ``prompt`` the stub model records **zero** ``image_prompt`` calls and the
    image call's prompt is exactly ``redact_ages(prompt, true_age)``: the written ages are gone
    and nothing is appended (no rating guidance, age band or scene; no negative prompt). The
    ratio is sent. The prompt check judged that prompt with the description beside it. The job's
    ``origin`` keeps the raw prompt; its applied parameters, the asset's
    ``generation_metadata.applied_parameters`` and the asset read say ``prompt_source:
    consumer``, and the asset's ``source`` is ``generated_from_consumer_prompt``. The prompt
    never reaches a log line. Refused at ``body.prompt`` before any job: with ``style``, blank,
    over 4,000 characters, a control character, or nothing left once its ages are removed.

    Complement asserted: without ``prompt`` the ``image_prompt`` writer runs and the asset reads
    ``prompt_source: engine`` (``source: generated``), as every image made before #1017 does."""
    caplog.set_level(logging.DEBUG)
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        app.guard.reset()
        before = await subject_jobs(engine)
        for refused in (
            body(prompt=CONSUMER_PROMPT, style="oil painting"),
            body(prompt="   "),
            body(prompt="p" * 4_001),
            body(prompt="a quay\x07at night"),
            body(prompt=f"{TRUE_AGE}"),
        ):
            status, code, details = error_of(await app.request(refused))
            assert (status, code, details["field"]) == (422, "validation_error", "body.prompt")
            assert CANARY not in json.dumps(details)
        assert await subject_jobs(engine) == before
        assert app.generator.requests == []

        job_id, job = await app.generated(
            Outputs(n=1, size=(64, 96)), prompt=CONSUMER_PROMPT, aspect_ratio="2:3"
        )
        assert ip.prompt_calls(app.guard) == []
        sent = app.generator.requests[-1].request
        assert sent.prompt == redact_ages(CONSUMER_PROMPT, TRUE_AGE)
        assert str(TRUE_AGE) not in sent.prompt
        assert "year" not in sent.prompt.lower()
        assert sent.prompt.endswith(CANARY)
        assert sent.negative_prompt is None
        assert sent.aspect_ratio == "2:3"
        checked = [payload(c) for c in safety_calls(app.guard)]
        assert checked
        assert all(f"Prompt: {sent.prompt}" in p for p in checked)
        assert all(f"Subject description: {DESCRIPTION}" in p for p in checked)
        assert (await job_origin(engine, job_id))["prompt"] == CONSUMER_PROMPT

        asset = await app.asset(job["asset_ids"][0])
        assert (asset["source"], asset["prompt_source"]) == (
            "generated_from_consumer_prompt",
            "consumer",
        )
        assert asset["generation_metadata"]["applied_parameters"]["prompt_source"] == "consumer"
        assert asset["generation_metadata"]["prompt"] == sent.prompt

        app.guard.reset()
        _, engine_job = await app.generated(Outputs(n=1, size=(64, 97)))
        assert ip.prompt_calls(app.guard)
        written = await app.asset(engine_job["asset_ids"][0])
        assert (written["source"], written["prompt_source"]) == ("generated", "engine")
        assert written["generation_metadata"]["applied_parameters"]["prompt_source"] == "engine"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_consumer_prompt_refused(
    image_settings: Settings, engine: AsyncEngine
) -> None:
    """#1017: a consumer prompt that breaks an image-prompt policy is refused exactly as the
    same text sent as ``style`` is: 422 ``guardrail_violation`` naming the same policy, with no
    job and no generation call. A consumer prompt's output that fails the output check on every
    attempt is never stored: the job ends ``failed`` (``guardrail_violation``) with no asset.

    Complement asserted: the same prompt without the policy's word is accepted (202)."""
    word = word_of("basilisk")
    checks = ScriptedChecks(fail_closed=False)
    async with subject_app(image_settings, engine, checks) as app:
        await app.configure()
        app.guard.reset()
        made = await global_policy(app.lib, words(word), applies_to=["image_prompt"])
        try:
            before = await subject_jobs(engine)
            bad = f"A fisherman on a quay holding a {word}."
            as_prompt = await app.request(body(prompt=bad))
            as_style = await app.request(body(style=bad))
            assert refused_by(as_prompt) == refused_by(as_style) == [made.id]
            assert error_of(as_prompt)[:2] == error_of(as_style)[:2]
            assert await subject_jobs(engine) == before
            assert app.generator.requests == []
            fine = await app.request(body(prompt="A fisherman on a quay."))
            assert fine.status_code == 202, fine.text
        finally:
            await app.lib.policies_retire(POLICY_OPERATOR, made.id)

        checks.script = [MINORS_POLICY_ID]
        app.generator.script.extend(Outputs(n=1, size=(91, 60)) for _ in range(12))
        accepted = await app.request(body(prompt="A fisherman on a quay at dusk."))
        assert accepted.status_code == 202, accepted.text
        failed_id = accepted.json()["job_id"]
        assert await app.run(failed_id) == {"image_job": "failed", "reason": "guardrail_violation"}
        assert await job_assets(engine, failed_id) == []
        app.generator.script.clear()


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_consumer_prompt_minor(
    image_settings: Settings, engine: AsyncEngine
) -> None:
    """#1017: a consumer prompt for a subject declared under the minor threshold (15) is not
    refused for that age alone: a benign prompt is accepted (202), and the minors check judged
    it with the subject at age 15. A sexualizing prompt at 15 is refused by the minors policy
    before any generation.

    Complement asserted: the same sexualizing prompt for a subject declared 30 passes the minors
    policy (202)."""
    benign = "A child in a red raincoat jumping in puddles, whole figure in frame."
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        app.guard.reset()
        recognise_sexual(app.guard, UNSAFE)
        young = {"subject": subject(true_age=MINOR_AGE), "prompt": benign}
        accepted = await app.request(young)
        assert accepted.status_code == 202, accepted.text
        minors_checks = [
            payload(c)
            for c in safety_calls(app.guard)
            if SUBJECTS_HEADING in payload(c) and benign in payload(c)
        ]
        assert minors_checks
        assert all(f"- subject_1: age {MINOR_AGE}" in p for p in minors_checks)

        before = await subject_jobs(engine)
        unsafe = {"subject": subject(true_age=MINOR_AGE), "prompt": f"A figure {UNSAFE}."}
        assert refused_by(await app.request(unsafe)) == [MINORS_POLICY_ID]
        assert await subject_jobs(engine) == before
        adult = {"subject": subject(true_age=30), "prompt": f"A figure {UNSAFE}."}
        assert (await app.request(adult)).status_code == 202
        assert app.generator.requests == []


# -- lifetime ---------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_kept_and_reusable(image_settings: Settings, engine: AsyncEngine) -> None:
    """A live subject image survives the media sweep (run a week later), while an unreferenced
    upload in the same sweep is purged. It can be named in a story character's ``images``.
    Deleting it with ``DELETE /media/{id}`` retires it, and a later sweep purges it once nothing
    references it.

    Complement asserted: before the sweep, the upload is live and past nothing; a deleted subject
    image still used on a character keeps its row until the character no longer needs it (the
    sweep leaves a referenced retired asset)."""
    async with subject_app(image_settings, engine) as app:
        loose = await uploaded(app.client, png(nonce=secrets.token_hex(6)))
        await app.configure()
        _, job = await app.generated(Outputs(n=2, size=(97, 53)))
        kept, used = job["asset_ids"]
        assert await asset_state(engine, loose["id"]) == "live"

        later = clock.now() + timedelta(days=7)
        await media_sweep.sweep(engine, app.lib.media, now=later)
        assert await asset_state(engine, loose["id"]) is None, "an unreferenced upload is purged"
        assert await asset_state(engine, kept) == "live"
        assert await asset_state(engine, used) == "live"

        story = await create(app.client, story_body("Ada", "Wren-830"))
        added = await app.client.post(
            f"/stories/{story['story_id']}/characters",
            json={"name": "Dale", "definition": "A fisherman.", "true_age": 52, "images": [used]},
            headers=WRITE,
        )
        assert added.status_code == 201, added.text

        for asset_id in (kept, used):
            dropped = await app.client.delete(f"/media/{asset_id}", headers=HEADERS)
            assert dropped.status_code == 204, dropped.text
            assert await asset_state(engine, asset_id) == "retired"
        await media_sweep.sweep(engine, app.lib.media, now=later + timedelta(days=1))
        assert await asset_state(engine, kept) is None, "a deleted subject image is purged"
        assert await asset_state(engine, used) == "retired", "a character still shows it"


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_offboarding(pg_url: str, tmp_path: Path) -> None:
    """Offboarding the owner follows the operator's ``media`` choice for subject images:
    ``delete`` retires the owner's subject image (a later sweep purges it), and ``transfer``
    moves it, still live and kept by the sweep, to the new owner. A job still running when its
    owner's offboarding (``media: delete``) is asked ends ``target_gone`` with nothing stored.

    Complement asserted: an owner offboarded with ``media: keep`` keeps the image live."""
    with migrated_database(pg_url, f"epistrel_subject_{secrets.token_hex(4)}") as url:
        settings = image_settings_for(url, tmp_path)
        db = make_engine(settings)
        try:
            async with subject_app(settings, db) as app:
                await app.configure()
                owners = {a: f"p-subject-{a}-{secrets.token_hex(3)}" for a in "dtkg"}
                images: dict[str, str] = {}
                for n, (action, owner) in enumerate(owners.items()):
                    if action == "g":
                        continue
                    _, job = await app.generated(Outputs(n=1, size=(101, 50 + n)), principal=owner)
                    images[action] = job["asset_ids"][0]
                accepted = await app.request(body(), principal=owners["g"])
                assert accepted.status_code == 202, accepted.text
                pending = accepted.json()["job_id"]

                receiver = f"p-receiver-{secrets.token_hex(3)}"
                plan = (("d", "delete"), ("t", "transfer"), ("k", "keep"), ("g", "delete"))
                for action, media in plan:
                    request: dict[str, Any] = {
                        "principal": owners[action],
                        "actions": {"media": media},
                    }
                    if media == "transfer":
                        request["transfer_to"] = receiver
                    queued = await app.client.post(
                        OFFBOARDING, json=request, headers=headers("p-operator")
                    )
                    assert queued.status_code == 202, queued.text
                async with db.begin() as conn:
                    await conn.execute(text("DELETE FROM jobs WHERE kind = 'image_job'"))
                app.generator.script.append(Outputs(n=1, size=(101, 60)))
                assert await app.run(pending) == {"image_job": "target_gone"}
                assert await job_assets(db, pending) == []
                assert await run_jobs(db) >= 4

                assert await asset_state(db, images["d"]) == "retired"
                assert await asset_state(db, images["k"]) == "live"
                moved = await app.asset(images["t"])
                assert (moved["state"], moved["owner_principal"], moved["origin"]) == (
                    "live",
                    receiver,
                    "subject",
                )
                await media_sweep.sweep(db, app.lib.media, now=clock.now() + timedelta(days=7))
                assert await asset_state(db, images["d"]) is None
                assert await asset_state(db, images["t"]) == "live"
                assert await asset_state(db, images["k"]) == "live"
        finally:
            await db.dispose()


# -- reference images (#1020) -----------------------------------------------------------------

SCENE_PROMPT = "The same fisherman on the beach at night, lighting a fire, head to toe in frame."


class ReferenceStub(StubImageGenerator):
    """A stub image adapter that reports what it did with reference images, as the OpenRouter
    adapter does: applied when its endpoint takes them (``takes_references``), else dropped
    (``input_references`` named, ``references_applied: false``) with the image still made."""

    def __init__(self) -> None:
        super().__init__()
        self.takes_references = True

    async def submit(self, request: ImageRequest, idempotency_key: str | None) -> SubmitResult:
        result = await super().submit(request, idempotency_key)
        refs = request.reference_images
        takes = self.takes_references
        applied = AppliedParameters(
            requested={"n": request.n, "reference_image_ids": list(refs)},
            sent={"input_references": len(refs)} if refs and takes else {},
            references_applied=None if not refs else takes,
            dropped_parameters=("input_references",) if refs and not takes else (),
        )
        return replace(result, applied_parameters=applied)


async def run_completed(app: SubjectApp, payload: dict[str, Any], size: tuple[int, int]) -> str:
    """Request, run and read a one-output job that completes; its asset id."""
    app.generator.script.append(Outputs(n=1, size=size))
    accepted = await app.request(payload)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    assert (await app.run(job_id))["image_job"] == "completed"
    [asset_id] = (await app.job(job_id))["asset_ids"]
    return str(asset_id)


async def clone_asset(
    engine: AsyncEngine,
    asset_id: str,
    *,
    job_kind: str = "subject",
    story_id: str | None = None,
    **changes: Any,
) -> str:
    """A hand-built copy of ``asset_id``'s row (and of its job, as ``job_kind``) with
    ``changes``: the story, catalog, candidate and age-unknown rows the guardrails refuse."""
    from epistrel.core.ids import new_ulid
    from epistrel.core.tables.images import image_jobs
    from epistrel.core.tables.media import media_assets

    status = {"subject": "completed", "story": "promoted", "catalog": "candidates_ready"}
    async with engine.begin() as conn:
        asset = dict(
            (await conn.execute(media_assets.select().where(media_assets.c.id == asset_id)))
            .one()
            ._mapping
        )
        job = dict(
            (
                await conn.execute(
                    image_jobs.select().where(image_jobs.c.id == asset["source_job_id"])
                )
            )
            .one()
            ._mapping
        )
        job.update(
            id=new_ulid(),
            kind=job_kind,
            status=status[job_kind],
            story_id=story_id,
            idempotency_key=None,
        )
        await conn.execute(image_jobs.insert().values(**job))
        asset.update(id=new_ulid(), source_job_id=job["id"], **changes)
        await conn.execute(media_assets.insert().values(**asset))
    return str(asset["id"])


def reference_errors(response: httpx.Response) -> list[tuple[str, str]]:
    status, code, details = error_of(response)
    assert (status, code) == (422, "validation_error"), response.text
    found = [(str(e["field"]), str(e["type"])) for e in details["errors"]]
    assert details["field"] == found[0][0]
    assert details["reason"] == found[0][1]
    return found


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_references(
    image_settings: Settings, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """#1020: a subject request naming an earlier subject image in ``reference_image_ids`` sends
    it to the image model as a reference (the stub records the ids it was handed, in order),
    and the new asset records ``generation_metadata.reference_image_ids`` and
    ``applied_parameters.references_applied: true``; it keeps the reference's subject id, so
    one subject's images list together; the job's ``origin`` holds the ids. It works on both
    prompt paths (with a consumer ``prompt`` and ``aspect_ratio``, and with ``style``), and
    the ``image_prompt`` call carries no id. With an image model that cannot take references the
    image is still made, and the record says ``references_applied: false`` with
    ``input_references`` dropped. A reference deleted after the check is refused by the
    resolver (``reference_not_found``). Only ids and counts reach the logs.

    Complement asserted: a request without references mints a new subject id and sends none
    (``references_applied`` null)."""
    caplog.set_level(logging.DEBUG)
    generator = ReferenceStub()
    async with subject_app(image_settings, engine, generator=generator) as app:
        await app.configure()
        base = await run_completed(app, body(prompt=CONSUMER_PROMPT, aspect_ratio="2:3"), (64, 96))
        base_asset = await app.asset(base)
        assert base_asset["generation_metadata"]["applied_parameters"]["references_applied"] is None
        subject_id = base_asset["depicted_subjects"][0]["id"]
        second = await run_completed(app, body(prompt=SCENE_PROMPT, aspect_ratio="2:3"), (64, 97))
        other_subject = (await app.asset(second))["depicted_subjects"][0]["id"]
        assert other_subject != subject_id

        app.guard.reset()
        accepted = await app.request(
            body(prompt=SCENE_PROMPT, aspect_ratio="2:3", reference_image_ids=[base])
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        app.generator.script.append(Outputs(n=1, size=(64, 98)))
        assert (await app.run(job_id))["image_job"] == "completed"
        sent = app.generator.requests[-1].request
        assert list(sent.reference_images) == [base]
        assert sent.aspect_ratio == "2:3"
        assert ip.prompt_calls(app.guard) == []
        assert (await job_origin(engine, job_id))["reference_image_ids"] == [base]
        [made] = (await app.job(job_id))["asset_ids"]
        asset = await app.asset(made)
        assert asset["depicted_subjects"][0]["id"] == subject_id
        assert asset["depicted_subjects"][0]["true_age"] == TRUE_AGE
        metadata = asset["generation_metadata"]
        assert metadata["reference_image_ids"] == [base]
        assert metadata["applied_parameters"]["references_applied"] is True

        # The engine-written path, with two references of one subject, in the order sent.
        app.guard.reset()
        both = await run_completed(
            app, body(style="oil painting", reference_image_ids=[made, base]), (64, 99)
        )
        assert list(app.generator.requests[-1].request.reference_images) == [made, base]
        [written] = ip.prompt_calls(app.guard)
        assert base not in ip.rendered(written)
        assert made not in ip.rendered(written)
        assert (await app.asset(both))["depicted_subjects"][0]["id"] == subject_id

        # No endpoint takes references: made all the same, recorded as not applied.
        generator.takes_references = False
        dropped = await run_completed(app, body(reference_image_ids=[base]), (64, 100))
        found = (await app.asset(dropped))["generation_metadata"]
        assert found["reference_image_ids"] == [base]
        assert found["applied_parameters"]["references_applied"] is False
        assert "input_references" in found["applied_parameters"]["dropped_parameters"]
        generator.takes_references = True

        # The library takes the field too.
        ctx = RequestContext(principal=OWNER, idempotency_key=f"ref-lib-{secrets.token_hex(4)}")
        request = SubjectImageRequest(
            subject=SubjectSpec(**subject()), reference_image_ids=[base], n=1
        )
        queued = await app.lib.request_subject_images(ctx, request)
        app.generator.script.append(Outputs(n=1, size=(64, 101)))
        assert (await app.run(queued.job_id))["image_job"] == "completed"
        assert list(app.generator.requests[-1].request.reference_images) == [base]

        # A reference deleted after the request was checked is not sent.
        resolver = app.lib.models.images.references
        assert resolver is not None
        assert (await resolver.resolve(second)).startswith("data:image/png;base64,")
        deleted = await app.client.delete(f"/media/{second}", headers=HEADERS)
        assert deleted.status_code == 204, deleted.text
        with pytest.raises(ImageRequestInvalid) as gone:
            await resolver.resolve(second)
        assert gone.value.reason == "reference_not_found"

        plain = await run_completed(app, body(), (64, 102))
        assert list(app.generator.requests[-1].request.reference_images) == []
        plain_asset = await app.asset(plain)
        assert plain_asset["depicted_subjects"][0]["id"] not in (subject_id, other_subject)
        assert (
            plain_asset["generation_metadata"]["applied_parameters"]["references_applied"] is None
        )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged
    assert LABEL not in logged
    assert "data:image" not in logged


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_references_refused(image_settings: Settings, engine: AsyncEngine) -> None:
    """#1020: a reference that is not the caller's live subject image is 422
    ``reference_not_subject_image`` at ``body.reference_image_ids.<n>``, with **no** job,
    ``image_prompt`` call or generation call: an unknown id, a story image's asset, a catalog
    image's asset, a candidate, an upload, another principal's subject image and a deleted one.
    A true age other than the reference's is ``reference_age_mismatch``; a subject asset with no
    recorded true age is ``reference_age_unknown``; two references of different subjects are
    ``reference_subject_mismatch`` at the second. Every failing index is reported, in order.
    ``[]``, five ids and a duplicate are 422 too.

    Complement asserted: the same request naming a valid reference is accepted (202)."""
    async with subject_app(image_settings, engine) as app:
        upload = (await uploaded(app.client, png(nonce=secrets.token_hex(6)), headers=HEADERS))[
            "id"
        ]
        await app.configure()
        base = await run_completed(app, body(), (70, 90))
        other = await run_completed(app, body(), (70, 91))
        theirs = await run_completed(app, body(), (70, 92))
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE media_assets SET owner_principal = 'p-someone-else' WHERE id = :a"),
                {"a": theirs},
            )
        deleted = await run_completed(app, body(), (70, 93))
        assert (await app.client.delete(f"/media/{deleted}", headers=HEADERS)).status_code == 204
        story = await create(app.client, story_body("Ada", "Wren-1020"))
        story_image = await clone_asset(
            engine, base, job_kind="story", story_id=story["story_id"], origin=None
        )
        catalog_image = await clone_asset(engine, base, job_kind="catalog", origin=None)
        candidate = await clone_asset(engine, base, state="candidate")
        declared = (await app.asset(base))["depicted_subjects"][0]
        ageless = await clone_asset(
            engine, base, depicted_subjects=[{k: v for k, v in declared.items() if k != "true_age"}]
        )

        app.guard.reset()
        jobs_before = await subject_jobs(engine)
        calls_before = len(app.generator.requests)
        not_subject = [
            "01NOSUCHASSET0000000000000",
            story_image,
            catalog_image,
            candidate,
            upload,
        ]
        refused = await app.request(body(reference_image_ids=not_subject[:4]))
        assert reference_errors(refused) == [
            (f"body.reference_image_ids.{n}", "reference_not_subject_image") for n in range(4)
        ]
        for one in (upload, theirs, deleted):
            assert reference_errors(await app.request(body(reference_image_ids=[one]))) == [
                ("body.reference_image_ids.0", "reference_not_subject_image")
            ]
        older = {"subject": subject(true_age=20), "reference_image_ids": [base]}
        assert reference_errors(await app.request(older)) == [
            ("body.reference_image_ids.0", "reference_age_mismatch")
        ]
        assert reference_errors(await app.request(body(reference_image_ids=[ageless]))) == [
            ("body.reference_image_ids.0", "reference_age_unknown")
        ]
        mixed = await app.request(body(reference_image_ids=[base, other]))
        assert reference_errors(mixed) == [
            ("body.reference_image_ids.1", "reference_subject_mismatch")
        ]
        several = await app.request(body(reference_image_ids=[base, story_image, base, ageless]))
        assert reference_errors(several) == [
            ("body.reference_image_ids.1", "reference_not_subject_image"),
            ("body.reference_image_ids.2", "reference_duplicate"),
            ("body.reference_image_ids.3", "reference_age_unknown"),
        ]
        # The ageless row is not a valid MediaAsset: a media listing in a later test on the
        # same database would fail to read it.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM media_assets WHERE id = :a"), {"a": ageless})
        consumer = await app.request(body(prompt=SCENE_PROMPT, reference_image_ids=[story_image]))
        assert reference_errors(consumer)[0][1] == "reference_not_subject_image"
        for ids, kind in (([], "too_short"), ([base] * 5, "too_long")):
            status, code, details = error_of(await app.request(body(reference_image_ids=ids)))
            assert (status, code, details["field"]) == (
                422,
                "validation_error",
                "body.reference_image_ids",
            )
            assert details["errors"][0]["type"] == kind
        assert await subject_jobs(engine) == jobs_before
        assert len(app.generator.requests) == calls_before
        assert ip.prompt_calls(app.guard) == []

        accepted = await app.request(body(reference_image_ids=[base]))
        assert accepted.status_code == 202, accepted.text


@pytest.mark.integration
@pytest.mark.part("M9", "FR-IMG-14")
async def test_i_30__m9_references_minor(image_settings: Settings, engine: AsyncEngine) -> None:
    """#1020: a subject declared 15 with a reference generated at 15 is not refused for its
    age: the request is accepted (202), the minors check judged it with the subject at age 15,
    and the reference is sent. The safety path is unchanged: a sexualizing description at 15
    with the same reference is refused by the minors policy before any generation.

    Complement asserted: the same reference under a declared age of 16 is
    ``reference_age_mismatch``."""
    benign = "A child in a red raincoat jumping in puddles, whole figure in frame."
    async with subject_app(image_settings, engine) as app:
        await app.configure()
        young = {"subject": subject(true_age=MINOR_AGE), "prompt": benign}
        base = await run_completed(app, young, (72, 90))
        app.guard.reset()
        recognise_sexual(app.guard, UNSAFE)
        again = await run_completed(app, {**young, "reference_image_ids": [base]}, (72, 91))
        assert list(app.generator.requests[-1].request.reference_images) == [base]
        assert (await app.asset(again))["depicted_subjects"][0]["true_age"] == MINOR_AGE
        minors_checks = [
            payload(c)
            for c in safety_calls(app.guard)
            if SUBJECTS_HEADING in payload(c) and benign in payload(c)
        ]
        assert minors_checks
        assert all(f"- subject_1: age {MINOR_AGE}" in p for p in minors_checks)

        calls = len(app.generator.requests)
        unsafe = {
            "subject": subject(true_age=MINOR_AGE),
            "prompt": f"A figure {UNSAFE}.",
            "reference_image_ids": [base],
        }
        assert refused_by(await app.request(unsafe)) == [MINORS_POLICY_ID]
        older = {"subject": subject(true_age=16), "prompt": benign, "reference_image_ids": [base]}
        assert reference_errors(await app.request(older)) == [
            ("body.reference_image_ids.0", "reference_age_mismatch")
        ]
        assert len(app.generator.requests) == calls
