"""#1017 (FR-IMG-14, ADR-214): a consumer-written subject-image prompt, as units.

``SubjectImageRequest.prompt`` is optional: 1 to 4,000 characters, not blank, no control
character but newline and tab, and never with ``style``. Each refusal is 422 at ``body.prompt``,
and no message echoes the text. ``prompt_override`` hands the job ``redact_ages(prompt,
true_age)`` with nothing added, or ``None`` without a prompt. A prompt left empty by redaction is
422 at ``body.prompt``. ``prompt_source`` is ``consumer`` or ``engine``. An asset read derives it
from ``source``, so an image generated before #1017 reads ``engine``. A catalog image request
still refuses ``prompt``. The end-to-end request is ``tests/integration/test_i_30_m9.py``. Unit
level: no part marker."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from epistrel.api.errors import install
from epistrel.core.errors import InvalidRequest
from epistrel.core.images.perceived import redact_ages
from epistrel.core.tables.media import MediaSource, PromptSource, prompt_source_of
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.images.targets.catalog import CatalogImageRequest
from epistrel.orchestrator.images.targets.subject import (
    PROMPT_MAX,
    PROMPT_SOURCE,
    SubjectImageRequest,
    job_origin,
    job_params,
    prompt_override,
    prompt_source,
)

SUBJECT = {"description": "Tall woman in a green coat", "true_age": 34, "label": "W"}
SECRET = "SECRET-PROMPT-TEXT"
PROMPT = (
    "Full-length photo of a tall 34-year-old woman, aged 34, in a green wool coat on a rainy "
    f"street, head to toe in frame. {SECRET}"
)


def request(**fields: Any) -> SubjectImageRequest:
    return SubjectImageRequest.model_validate({"subject": SUBJECT, **fields})


def app() -> FastAPI:
    """A one-route app with the API's error envelope: the route takes the real body."""
    found = FastAPI()
    install(found)

    @found.post("/subject-images")
    async def take(body: SubjectImageRequest) -> dict[str, Any]:
        return {"prompt": body.prompt}

    return found


async def post(payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/subject-images", json=payload)


@pytest.mark.parametrize(
    "fields",
    [
        {"prompt": ""},
        {"prompt": "   \n\t "},
        {"prompt": SECRET + "p" * PROMPT_MAX},
        {"prompt": f"{SECRET}\x07"},
        {"prompt": f"{SECRET}\r"},
        {"prompt": f"{SECRET}\x7f"},
        {"prompt": PROMPT, "style": f"oil {SECRET}"},
        {"prompt": 7},
    ],
)
async def test_a_bad_prompt_is_refused_at_its_field(fields: dict[str, Any]) -> None:
    answered = await post({"subject": SUBJECT, **fields})
    assert answered.status_code == 422, answered.text
    error = answered.json()["error"]
    assert (error["code"], error["details"]["field"]) == ("validation_error", "body.prompt")
    assert SECRET not in answered.text


@pytest.mark.parametrize(
    "prompt", [PROMPT, "x", "p" * PROMPT_MAX, "line one\n\tline two", "  padded  "]
)
async def test_a_good_prompt_is_taken_as_sent(prompt: str) -> None:
    answered = await post({"subject": SUBJECT, "prompt": prompt})
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"prompt": prompt}


async def test_null_prompt_is_absent_and_takes_a_style() -> None:
    answered = await post({"subject": SUBJECT, "prompt": None, "style": "oil"})
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"prompt": None}


def test_a_catalog_image_request_refuses_a_prompt() -> None:
    with pytest.raises(ValidationError) as caught:
        CatalogImageRequest.model_validate({"slot": "primary", "prompt": PROMPT})
    assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [
        (("prompt",), "extra_forbidden")
    ]


def test_the_override_is_the_redacted_prompt_and_nothing_more() -> None:
    found = prompt_override(request(prompt=PROMPT, aspect_ratio="2:3"))
    assert found is not None
    assert found.prompt == redact_ages(PROMPT, 34)
    assert "34" not in found.prompt
    assert "year" not in found.prompt.lower()
    assert found.prompt.startswith("Full-length photo of a tall")
    assert found.prompt.endswith(SECRET)
    assert (found.size, found.aspect_ratio) == (None, "2:3")


def test_the_override_takes_the_deployment_dimensions_without_a_ratio() -> None:
    deployment = ImageGenerationParams(adapter="stub_sync", default_size="512x512")
    found = prompt_override(request(prompt=PROMPT), deployment)
    assert found is not None
    assert (found.size, found.aspect_ratio) == ("512x512", None)


def test_no_prompt_is_no_override() -> None:
    assert prompt_override(request(style="oil")) is None


@pytest.mark.parametrize("prompt", ["34", "aged 34", "34 years old"])
def test_a_prompt_left_empty_by_redaction_is_refused(prompt: str) -> None:
    with pytest.raises(InvalidRequest) as caught:
        prompt_override(request(prompt=prompt))
    assert caught.value.details["field"] == "body.prompt"


def test_the_description_is_still_checked_on_the_consumer_path() -> None:
    bare = SubjectImageRequest.model_validate(
        {"subject": {**SUBJECT, "description": "34 years old"}, "prompt": PROMPT}
    )
    with pytest.raises(InvalidRequest) as caught:
        prompt_override(bare)
    assert caught.value.details["field"] == "body.subject.description"


def test_prompt_source_and_the_job_fields() -> None:
    consumer, written = request(prompt=PROMPT), request()
    assert (prompt_source(consumer), prompt_source(written)) == (
        PromptSource.CONSUMER,
        PromptSource.ENGINE,
    )
    assert job_params(consumer) == {PROMPT_SOURCE: "consumer"}
    assert job_params(written) == {PROMPT_SOURCE: "engine"}
    assert job_origin("s1", consumer)["prompt"] == PROMPT
    assert job_origin("s1", written)["prompt"] is None


def test_an_asset_reads_its_prompt_source_from_its_source() -> None:
    assert prompt_source_of(MediaSource.GENERATED_FROM_CONSUMER_PROMPT.value) is (
        PromptSource.CONSUMER
    )
    assert prompt_source_of(MediaSource.GENERATED.value) is PromptSource.ENGINE
    assert prompt_source_of(MediaSource.UPLOADED.value) is None
    assert prompt_source_of(MediaSource.EXTRACTED.value) is None
