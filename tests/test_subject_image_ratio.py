"""#1012 (FR-IMG-14): a subject image's ``aspect_ratio`` and its default framing, as units.

The request takes an optional ``aspect_ratio`` from exactly ``SUBJECT_ASPECT_RATIOS`` (anything
else is 422 at ``body.aspect_ratio``); ``subject_view`` passes it to ``resolve_dimensions`` as
the request's own ratio, so it wins over both deployment defaults, and leaving it out gives
exactly the old dimensions. The rendered ``image_prompt`` of a subject view carries the
head-in-frame line and neither "portrait" nor "plain background"; a story or catalog view's
does not carry the line. The OpenRouter adapter sends the ratio and records it, or names it as
dropped when the endpoint cannot apply it. The end-to-end request is
``tests/integration/test_i_30_m9.py``. Unit level: no part marker."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI

from epistrel.api.errors import install
from epistrel.orchestrator.images.adapters import openrouter
from epistrel.orchestrator.images.params import SUBJECT_ASPECT_RATIOS, ImageGenerationParams
from epistrel.orchestrator.images.port import ImageRequest
from epistrel.orchestrator.images.prompt_builder import (
    PURPOSE_TEXT,
    PerceivedSubject,
    PerceivedView,
    prompt_inputs,
)
from epistrel.orchestrator.images.targets.subject import (
    SCENE,
    SubjectImageRequest,
    subject_view,
)
from epistrel.orchestrator.models.prompts import render
from epistrel.orchestrator.models.registry import get_purpose

HEAD_IN_FRAME = "Keep the subject's whole head in frame unless the style asks for a crop."
SUBJECT = {"description": "Tall woman in a green coat", "true_age": 34, "label": "W"}
GUIDANCE = "All-ages imagery only."


def request(**fields: Any) -> SubjectImageRequest:
    return SubjectImageRequest.model_validate({"subject": SUBJECT, **fields})


def app() -> FastAPI:
    """A one-route app with the API's error envelope: the route takes the real body."""
    found = FastAPI()
    install(found)

    @found.post("/subject-images")
    async def take(body: SubjectImageRequest) -> dict[str, Any]:
        return {"aspect_ratio": body.aspect_ratio}

    return found


async def post(payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/subject-images", json=payload)


def test_the_ratio_set() -> None:
    assert SUBJECT_ASPECT_RATIOS == ("1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9")


@pytest.mark.parametrize("ratio", SUBJECT_ASPECT_RATIOS)
async def test_each_allowed_ratio_validates(ratio: str) -> None:
    answered = await post({"subject": SUBJECT, "aspect_ratio": ratio})
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"aspect_ratio": ratio}


@pytest.mark.parametrize("payload", [None, {}])
async def test_omitted_or_null_is_no_ratio(payload: dict[str, Any] | None) -> None:
    body: dict[str, Any] = {"subject": SUBJECT}
    if payload is None:
        body["aspect_ratio"] = None
    answered = await post(body)
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"aspect_ratio": None}


@pytest.mark.parametrize("ratio", ["5:4", "", "2x3", 2, " 2:3", "2:3 "])
async def test_any_other_ratio_is_refused_at_its_field(ratio: object) -> None:
    """Exact match only: a well-formed unlisted ratio, an empty or malformed string, a number
    and a padded value are each 422 at ``body.aspect_ratio``. A string's message lists the
    set; a non-string is a type error at the same field."""
    answered = await post({"subject": SUBJECT, "aspect_ratio": ratio})
    assert answered.status_code == 422, answered.text
    error = answered.json()["error"]
    assert (error["code"], error["details"]["field"]) == ("validation_error", "body.aspect_ratio")
    message = str(error["details"]["errors"][0]["message"])
    if isinstance(ratio, str):
        assert all(r in message for r in SUBJECT_ASPECT_RATIOS), message
    else:
        assert error["details"]["errors"][0]["type"] == "string_type"


DEFAULT_RATIO = ImageGenerationParams(adapter="stub", default_aspect_ratio="1:1")
DEFAULT_SIZE = ImageGenerationParams(adapter="stub", default_size="512x512")
UNSET = ImageGenerationParams(adapter="stub")


@pytest.mark.parametrize("deployment", [DEFAULT_RATIO, DEFAULT_SIZE, UNSET, None])
def test_a_given_ratio_wins_over_the_deployment(deployment: ImageGenerationParams | None) -> None:
    """The request's ratio is the request ratio: it beats a configured ``1:1`` and a configured
    size, and no size is sent beside it."""
    view = subject_view(request(aspect_ratio="2:3"), GUIDANCE, deployment)
    assert (view.size, view.aspect_ratio) == (None, "2:3")


@pytest.mark.parametrize(
    ("deployment", "expected"),
    [
        (DEFAULT_RATIO, (None, "1:1")),
        (ImageGenerationParams(adapter="stub", default_aspect_ratio="16:9"), (None, "16:9")),
        (DEFAULT_SIZE, ("512x512", None)),
        (UNSET, (None, "1:1")),
        (None, (None, "1:1")),
    ],
)
def test_no_ratio_keeps_todays_dimensions(
    deployment: ImageGenerationParams | None, expected: tuple[str | None, str | None]
) -> None:
    """The complement: an omitted ratio is exactly the deployment default's, else ``1:1``;
    it never becomes ``2:3``."""
    view = subject_view(request(), GUIDANCE, deployment)
    assert (view.size, view.aspect_ratio) == expected


def rendered(view: PerceivedView) -> str:
    messages = render(get_purpose("image_prompt"), prompt_inputs(view))
    return "\n".join(m.content for m in messages)


def test_subject_prompt_has_the_head_line_and_no_portrait() -> None:
    assert SCENE == "single subject"
    assert "portrait" not in PURPOSE_TEXT["subject"]
    text = rendered(subject_view(request(style="photo, full-body"), GUIDANCE))
    assert f"## Framing\n{HEAD_IN_FRAME}" in text
    assert "portrait" not in text.lower()
    assert "plain background" not in text.lower()


def _story_view() -> PerceivedView:
    return PerceivedView(
        subjects=(
            PerceivedSubject(
                description="a tall woman",
                age_band="mid thirties",
                base_appearance_prompt=None,
                reference_image_ids=(),
                seed=None,
            ),
        ),
        scene="a tavern",
        visual_style=None,
        rating_image_guidance=GUIDANCE,
        purpose="story",
        beats=("She raises a cup.",),
        location="the tavern",
        camera_framing="third-person view of the scene",
        aspect_ratio="16:9",
    )


@pytest.mark.parametrize("purpose", ["story", "primary", "gallery", "cover"])
def test_other_images_have_no_head_line(purpose: str) -> None:
    view = _story_view()
    if purpose != "story":
        view = PerceivedView(
            subjects=view.subjects,
            scene="single-subject portrait, plain background",
            visual_style=None,
            rating_image_guidance=GUIDANCE,
            purpose=purpose,
            size="512x512",
        )
    text = rendered(view)
    assert HEAD_IN_FRAME not in text
    if purpose == "story":
        assert "## Framing\nthird-person view of the scene" in text


def _endpoint(*parameters: str) -> openrouter.Endpoint:
    return openrouter.Endpoint(slug="e", parameters=frozenset(parameters))


def test_openrouter_sends_and_records_the_ratio() -> None:
    """The adapter's body carries ``aspect_ratio: "2:3"`` and no size, and its applied
    parameters record the ratio as requested and sent."""
    ratio = ImageRequest(prompt="p", aspect_ratio="2:3")
    endpoints = [_endpoint("aspect_ratio")]
    route = openrouter.route_request(ratio, endpoints)
    planned = openrouter.plan_body("m/x", ratio, route)
    assert planned.body["aspect_ratio"] == "2:3"
    assert "size" not in planned.body
    applied = openrouter.applied_of(ratio, planned, endpoints[0])
    assert applied.requested["aspect_ratio"] == "2:3"
    assert applied.sent["aspect_ratio"] == "2:3"
    assert "aspect_ratio" not in applied.dropped_parameters


def test_openrouter_names_an_unapplied_ratio_as_dropped() -> None:
    """An endpoint that cannot apply the ratio still generates: the ratio is requested and
    named as dropped, never refused."""
    ratio = ImageRequest(prompt="p", aspect_ratio="2:3")
    endpoints = [_endpoint("seed")]
    planned = openrouter.plan_body("m/x", ratio, openrouter.route_request(ratio, endpoints))
    assert "aspect_ratio" not in planned.body
    applied = openrouter.applied_of(ratio, planned, endpoints[0])
    assert applied.requested["aspect_ratio"] == "2:3"
    assert "aspect_ratio" in applied.dropped_parameters
