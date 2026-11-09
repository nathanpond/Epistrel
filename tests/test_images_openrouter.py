"""Unit tests for the OpenRouter adapter's pure parts and the reference-image resolver (#255):
endpoint parsing, cost reading, provider error codes, which hosts a provider can fetch, and
data URLs. The adapter's HTTP contract is in ``tests/integration/test_i_23.py``."""

from __future__ import annotations

import base64
import io
from decimal import Decimal

import pytest
from PIL import Image

from epistrel.orchestrator.images.adapters import openrouter
from epistrel.orchestrator.images.errors import (
    ImageProviderMalformed,
    ImageRequestInvalid,
    provider_code_of,
)
from epistrel.orchestrator.images.params import ImageGenerationParams, default_base_url
from epistrel.orchestrator.images.port import Cost, ImageRequest
from epistrel.orchestrator.images.references import (
    DATA_URL_MAX_SIDE,
    data_url,
    provider_can_fetch,
)
from epistrel.orchestrator.models.config import endpoint_of
from tests.image_support import png


def test_parse_endpoints_shapes() -> None:
    """Endpoints parse from ``data.endpoints``, ``data`` as a list, or ``endpoints``;
    ``supported_parameters`` as a list or a mapping; a record without it supports nothing,
    and one without a name gets a positional slug."""
    records = [
        {"tag": "a", "supported_parameters": ["seed", 3]},
        {"provider_name": "B", "supported_parameters": {"size": {}}},
        {},
    ]
    for body in ({"data": {"endpoints": records}}, {"data": records}, {"endpoints": records}):
        parsed = openrouter.parse_endpoints(body)
        assert [(e.slug, e.parameters) for e in parsed] == [
            ("a", frozenset({"seed"})),
            ("B", frozenset({"size"})),
            ("endpoint-2", frozenset()),
        ]
    for bad in ({"data": {"id": "x"}}, "text", {"endpoints": [{"tag": 4}]}):
        with pytest.raises(ImageProviderMalformed):
            openrouter.parse_endpoints(bad)


def test_serving_endpoint_matches_provider_names() -> None:
    """The response's ``provider`` names the serving endpoint by slug or provider name, in any
    case; an unknown one with several candidates is unknown."""
    endpoints = openrouter.parse_endpoints(
        {"endpoints": [{"tag": "alpha", "provider_name": "Alpha AI"}, {"tag": "beta"}]}
    )
    route = openrouter.route_request(ImageRequest(prompt="p"), endpoints)
    assert openrouter.serving_endpoint(route, "alpha ai") == endpoints[0]
    assert openrouter.serving_endpoint(route, "BETA") == endpoints[1]
    assert openrouter.serving_endpoint(route, "gamma") is None
    assert openrouter.serving_endpoint(route, {"name": "alpha"}) is None


def test_unresolvable_references_never_narrow_routing() -> None:
    """With no resolver, references are dropped and do not narrow routing; ``n`` that no
    chosen endpoint lists is dropped (sent as one)."""
    endpoints = openrouter.parse_endpoints(
        {"endpoints": [{"tag": "r", "supported_parameters": ["input_references"]}, {"tag": "o"}]}
    )
    request = ImageRequest(prompt="p", n=2, reference_images=("m",))
    route = openrouter.route_request(request, endpoints, can_reference=False)
    assert route.only is None
    planned = openrouter.plan_body("m/x", request, route)
    assert planned.body == {"model": "m/x", "prompt": "p"}
    assert planned.dropped == ("n", "input_references")


def test_n_is_clamped_to_the_endpoints_max() -> None:
    """#801: ``n`` above an endpoint's ``n.max`` is clamped to the smallest max the chosen
    endpoints list (one call; the job settles the outputs it gets). Clamped to one, it is not
    sent and is recorded dropped, as an unlisted ``n`` is. The recorded model lists
    ``n: {max: 1}``, where ``n = 2`` used to be a non-retryable 400.

    Complement asserted: an ``n`` within every max is sent as asked, and a listed ``n`` with no
    max is not clamped."""
    recorded = openrouter.parse_endpoints(
        {"endpoints": [{"tag": "bfl", "supported_parameters": {"n": {"max": 1, "min": 1}}}]}
    )
    request = ImageRequest(prompt="p", n=2)
    planned = openrouter.plan_body("m/x", request, openrouter.route_request(request, recorded))
    assert "n" not in planned.body
    assert planned.dropped == ("n",)
    assert planned.requested["n"] == 2

    two = openrouter.parse_endpoints(
        {
            "endpoints": [
                {"tag": "a", "supported_parameters": {"n": {"max": 4}}},
                {"tag": "b", "supported_parameters": {"n": {"max": 2}}},
                {"tag": "c", "supported_parameters": {"size": {}}},
            ]
        }
    )
    many = ImageRequest(prompt="p", n=4)
    planned = openrouter.plan_body("m/x", many, openrouter.route_request(many, two))
    assert planned.body["n"] == planned.sent["n"] == 2
    assert planned.dropped == ()
    assert openrouter.applied_of(many, planned, two[1]).dropped_parameters == ()

    within = ImageRequest(prompt="p", n=2)
    planned = openrouter.plan_body("m/x", within, openrouter.route_request(within, two))
    assert planned.body["n"] == 2
    unbounded = openrouter.parse_endpoints(
        {"endpoints": [{"tag": "u", "supported_parameters": ["n"]}]}
    )
    planned = openrouter.plan_body("m/x", many, openrouter.route_request(many, unbounded))
    assert planned.body["n"] == 4


def test_resolution_is_not_advertised() -> None:
    """#801: no ``ImageRequest`` field carries ``resolution``, so it is not a canonical
    parameter. On the recorded model (``resolution`` and ``aspect_ratio``, no ``size``) a
    configured ``size`` is dropped and recorded under ``dropped_parameters``: sizes are not
    translated (#255).

    Complement asserted: a ratio on that model is sent."""
    assert "resolution" not in openrouter.CANONICAL_PARAMETERS
    endpoints = openrouter.parse_endpoints(
        {
            "endpoints": [
                {"tag": "bfl", "supported_parameters": {"resolution": {}, "aspect_ratio": {}}}
            ]
        }
    )
    sized = ImageRequest(prompt="p", size="1024x1024")
    planned = openrouter.plan_body("m/x", sized, openrouter.route_request(sized, endpoints))
    assert "size" not in planned.body
    assert planned.dropped == ("size",)
    ratio = ImageRequest(prompt="p", aspect_ratio="1:1")
    planned = openrouter.plan_body("m/x", ratio, openrouter.route_request(ratio, endpoints))
    assert planned.body["aspect_ratio"] == "1:1"


@pytest.mark.parametrize(
    ("usage", "cost"),
    [
        ({"cost": 0.04}, Cost(Decimal("0.04"), "USD")),
        ({"cost": "0.5"}, Cost(Decimal("0.5"), "USD")),
        ({"cost": 0}, Cost(Decimal(0), "USD")),
        ({}, "unknown"),
        (None, "unknown"),
        ({"cost": None}, "unknown"),
        ({"cost": True}, "unknown"),
        ({"cost": "free"}, "unknown"),
        ({"cost": float("nan")}, "unknown"),
        ({"cost": -1}, "unknown"),
    ],
)
def test_cost_of(usage: dict[str, object] | None, cost: object) -> None:
    """``usage.cost`` is a USD cost; anything missing or not a number is ``unknown``, never 0."""
    assert openrouter.cost_of(usage) == cost


def test_provider_codes_are_tokens_only() -> None:
    """A provider error code is repeated only as a short token or an integer, never as text
    that could carry the prompt."""
    assert provider_code_of(429) == "429"
    assert provider_code_of("rate_limited") == "rate_limited"
    assert provider_code_of("the prompt was: a castle") is None
    assert provider_code_of(True) is None
    assert provider_code_of({"x": 1}) is None
    assert provider_code_of("x" * 65) is None


def test_decode_image_reads_the_bytes() -> None:
    """A ``data:`` prefix is stripped, and width, height and type come from the bytes."""
    encoded = base64.b64encode(png(20, 10)).decode()
    output = openrouter.decode_image(f"data:image/jpeg;base64,{encoded}")
    assert (output.media_type, output.width, output.height) == ("image/png", 20, 10)


@pytest.mark.parametrize(
    ("url", "fetchable"),
    [
        ("https://epistrel.example/media/x", True),
        ("http://203.0.113.7:8080/m", False),  # reserved for documentation
        ("https://8.8.8.8/m", True),
        ("http://localhost:8000/m", False),
        ("http://media.localhost/m", False),
        ("http://127.0.0.1/m", False),
        ("http://10.1.2.3/m", False),
        ("http://192.168.1.9/m", False),
        ("http://169.254.1.1/m", False),
        ("http://[::1]/m", False),
        ("http://0.0.0.0/m", False),
        ("http://epistrel/m", False),
        ("http://box.internal/m", False),
        ("ftp://epistrel.example/m", False),
        ("/media/x", False),
    ],
)
def test_provider_can_fetch(url: str, fetchable: bool) -> None:
    """Only a public host is handed to the provider as a URL; anything else gets a data URL."""
    assert provider_can_fetch(url) is fetchable


def test_data_url_downscales_and_keeps_the_type() -> None:
    """A large image becomes a data URL at most 1024 px on its longest side, in its stored
    type; a small one is passed through as it is; bytes that are no image are refused."""
    big = io.BytesIO()
    Image.new("RGB", (2000, 1000), (10, 20, 30)).save(big, format="JPEG")
    url = data_url(big.getvalue(), "image/jpeg")
    assert url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(url.partition(",")[2]))) as image:
        assert (image.format, image.size) == ("JPEG", (DATA_URL_MAX_SIDE, 512))
    small = png(8, 8)
    assert data_url(small, "image/png") == (
        "data:image/png;base64," + base64.b64encode(small).decode()
    )
    with pytest.raises(ImageRequestInvalid) as refused:
        data_url(b"not an image", "image/png")
    assert refused.value.reason == "reference_not_image"


def test_default_base_url_names_the_endpoint() -> None:
    """With no ``base_url``, the OpenRouter row's endpoint is its default host."""
    params = ImageGenerationParams(adapter="openrouter")
    assert endpoint_of(default_base_url(params)) == "openrouter.ai"
    assert default_base_url(ImageGenerationParams()) is None
    assert default_base_url(ImageGenerationParams(adapter="stub_sync")) is None
