"""The image-generation port and its registry (#254): the port's types, the registration
rules, the scripted stub adapters every later M8 test uses, and the model guide's image rows
(FR-IMG-1,13). The database-backed half (configuration, ``not_configured``, the key) is
``tests/integration/test_images_config.py``."""

from __future__ import annotations

import asyncio
import struct
import zlib
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from epistrel.orchestrator.images import registry as images
from epistrel.orchestrator.images.params import ImageGenerationParams, resolve_dimensions
from epistrel.orchestrator.images.port import (
    Capabilities,
    CollectResult,
    ImageRequest,
    SubmitResult,
)
from epistrel.orchestrator.models import registry
from epistrel.orchestrator.models.registry import (
    HOSTED_SAFETY_RETENTION,
    ContentCategory,
    field_categories,
)
from epistrel.tools import model_guide
from tests.image_support import (
    Fail,
    Outputs,
    Stall,
    StatusSeq,
    StubImageGenerator,
    StubScriptExhausted,
    png,
    registered,
)

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "models.md"
REQUEST = ImageRequest(prompt="a lighthouse at dusk", aspect_ratio="16:9", n=2)


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Each test starts and ends with this process's adapter table as it found it."""
    before = images.registered_adapters()
    yield
    for name in images.registered_adapters() - before:
        images.unregister_image_adapter(name)


def test_port_types_are_closed_and_bounded() -> None:
    """``Capabilities`` is a closed field set (``returns_request_id`` defaults to ``False``);
    an ``ImageRequest`` takes a size or an aspect ratio but not both, 1 to 10 images, a seed in
    ``0..2**32-1`` and up to four reference images. Complement: a request inside every bound is
    accepted as given."""
    assert Capabilities(submission="sync").returns_request_id is False
    with pytest.raises(ValidationError):
        Capabilities.model_validate({"submission": "sync", "streams": True})
    for bad in (
        {"size": "1024x1024", "aspect_ratio": "1:1"},
        {"n": 0},
        {"n": 11},
        {"seed": -1},
        {"seed": 2**32},
        {"size": "big"},
        {"aspect_ratio": "wide"},
        {"quality": "ultra"},
        {"reference_images": ["a", "b", "c", "d", "e"]},
        {"prompt": ""},
    ):
        with pytest.raises(ValidationError):
            ImageRequest.model_validate({"prompt": "p", **bad})
    request = ImageRequest(
        prompt="p", seed=2**32 - 1, size="1024x768", n=10, reference_images=("a", "b", "c", "d")
    )
    assert (request.size, request.aspect_ratio, request.n) == ("1024x768", None, 10)


def test_configured_defaults_resolve_size_then_ratio() -> None:
    """A request's own size or ratio wins; else the configured size, else the configured
    ratio, else ``1:1``. Complement: with both configured, the size is used."""
    both = ImageGenerationParams(default_size="512x512", default_aspect_ratio="4:3")
    assert resolve_dimensions(both, aspect_ratio="16:9") == (None, "16:9")
    assert resolve_dimensions(both, size="64x64") == ("64x64", None)
    assert resolve_dimensions(both) == ("512x512", None)
    assert resolve_dimensions(ImageGenerationParams(default_aspect_ratio="4:3")) == (None, "4:3")
    assert resolve_dimensions(ImageGenerationParams()) == (None, "1:1")
    for bad in ({"default_size": "x"}, {"default_quality": "ultra"}, {"timeout": 5}):
        with pytest.raises(ValidationError):
            ImageGenerationParams.model_validate(bad)


def _dimensions(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


async def test_stub_generators_follow_their_scripts() -> None:
    """The scripted stubs (``tests/image_support.py``) answer what their scripts say: N real
    PNGs of the given size, a failure, a stall until released, a status sequence, and every
    request recorded with its key and ``stub-<n>`` ref. Complement: an exhausted script raises,
    and the PNG writer's bytes decompress to the declared size."""
    released = asyncio.Event()
    sync_stub = StubImageGenerator(
        script=[Outputs(2, (32, 16)), Fail(RuntimeError("provider down")), Stall(released)]
    )
    first = await sync_stub.submit(REQUEST, "key-1")
    assert isinstance(first, SubmitResult)
    assert first.attempt_ref == "stub-1"
    assert [o.media_type for o in first.outputs or ()] == ["image/png", "image/png"]
    assert {_dimensions(o.data) for o in first.outputs or ()} == {(32, 16)}
    assert first.cost is not None
    assert first.cost != "unknown"
    assert first.cost.amount == Decimal("0.04")
    with pytest.raises(RuntimeError, match="provider down"):
        await sync_stub.submit(REQUEST, None)
    stalled = asyncio.create_task(sync_stub.submit(REQUEST, None))
    await asyncio.sleep(0.01)
    assert not stalled.done()
    released.set()
    assert len((await stalled).outputs or ()) == 1
    with pytest.raises(StubScriptExhausted):
        await sync_stub.submit(REQUEST, None)
    assert [(r.idempotency_key, r.attempt_ref) for r in sync_stub.requests] == [
        ("key-1", "stub-1"),
        (None, "stub-2"),
        (None, "stub-3"),
        (None, "stub-4"),
    ]

    async_stub = StubImageGenerator(
        submission="async",
        confirms_completion=True,
        script=[Outputs(1, (8, 8), cost="unknown"), StatusSeq(["running", "succeeded"])],
    )
    acknowledged = await async_stub.submit(REQUEST, None)
    assert acknowledged.outputs is None
    assert acknowledged.cost is None
    collected = await async_stub.collect(acknowledged.attempt_ref)
    assert isinstance(collected, CollectResult)
    assert collected.cost == "unknown"  # a silent provider is unknown, never 0
    polled = await async_stub.submit(REQUEST, None)
    assert (await async_stub.status(polled.attempt_ref)).state == "running"
    done = await async_stub.status(polled.attempt_ref)
    assert done.terminal
    assert len(done.outputs or ()) == 1
    assert await async_stub.cancel(polled.attempt_ref) == "accepted"

    raw = png(3, 2)
    idat = raw[raw.index(b"IDAT") + 4 : raw.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == 2 * (1 + 3 * 3)


async def test_handle_sends_the_key_only_when_declared() -> None:
    """The handle ``ImageAdapters`` hands out strips the idempotency key for an adapter that
    does not declare ``idempotency_keys`` and refuses ``status``/``cancel`` for one that does
    not confirm completion. Complement: an adapter that declares keys receives the key."""

    def handle(stub: StubImageGenerator) -> images.ImageGeneratorHandle:
        owner = images.ImageAdapters.__new__(images.ImageAdapters)
        owner.closed = 0
        live = images._Live(stub, stub.capabilities(), ())
        return images.ImageGeneratorHandle("stub", "m", live, owner)

    plain = StubImageGenerator(script=[Outputs()])
    await handle(plain).submit(REQUEST, "attempt-key")
    assert plain.requests[0].idempotency_key is None
    with pytest.raises(images.ConfirmationUnsupported):
        await handle(plain).status("stub-1")
    keyed = StubImageGenerator(idempotency_keys=True, script=[Outputs()])
    await handle(keyed).submit(REQUEST, "attempt-key")
    assert keyed.requests[0].idempotency_key == "attempt-key"


def test_image_check_fails_closed_naming_every_policy_type() -> None:
    """``ImageCheckUnconfigured`` names every image-output policy it could not run, sorted;
    ``image_output_policies`` picks only the policies that target ``image_output``."""

    class Synthetic:
        def __init__(self, policy_id: str, *targets: str) -> None:
            self.id = policy_id
            self.applies_to = targets

    policies = [
        Synthetic("minors", "text_output", "image_output"),
        Synthetic("text_only", "text_output"),
        Synthetic("gore", "image_output"),
    ]
    assert images.image_output_policies(policies) == ["minors", "gore"]
    error = images.ImageCheckUnconfigured(images.image_output_policies(policies))
    assert error.policy_ids == ("gore", "minors")
    assert error.code == "image_check_unconfigured"
    assert images.image_output_policies([Synthetic("text_only", "text_output")]) == []


def _section(guide: str, name: str) -> str:
    return guide.split(f"### `{name}`", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]


def _rows(section: str) -> list[str]:
    receives = section.split("**Receives**", 1)[1].split("**Never receives:**", 1)[0]
    return [line.split("|")[1].strip().strip("`") for line in receives.splitlines()[4:] if line]


@pytest.mark.part("M8", "FR-IMG-13")
def test_i_24__m8_data_flow_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generated guide's rows for ``guardrail_check`` and ``image_check`` list exactly the
    inputs their registry schemas declare (subject labels, true ages and the prompt's visual
    descriptors in both; the image, prompt, caption and alt text in ``image_check``'s) and
    carry the hosted-provider retention sentence verbatim; the image adapter has a row too,
    listing the prompt, its parameters and reference images and never the safety context.

    Complement, run first: with the sentence removed from the guide's row template, rendering
    fails and ``--check`` exits 1, so the sentence cannot silently drop out."""
    monkeypatch.setattr(model_guide, "SAFETY_RETENTION_LINE", "- **Hosted providers:** see terms")
    with pytest.raises(model_guide.GuideError, match="retention sentence"):
        model_guide.render()
    assert model_guide.main(["--check"]) == 1
    monkeypatch.undo()

    guide = GUIDE.read_text(encoding="utf-8")
    assert guide == model_guide.render()
    assert model_guide.main(["--check"]) == 0
    for name in registry.SAFETY_PURPOSES:
        section = _section(guide, name)
        purpose = registry.DELIVERED[name]
        assert _rows(section) == [
            path
            for path in field_categories(purpose.input_schema)
            if not any(
                other.startswith(f"{path}.") for other in field_categories(purpose.input_schema)
            )
        ]
        assert HOSTED_SAFETY_RETENTION in section
    check = _section(guide, "image_check")
    for row in (
        "| `image` | `image` |",
        "| `prompt` | `image_prompt` |",
        "| `caption` | `caption_alt_text` |",
        "| `alt_text` | `caption_alt_text` |",
        "| `subjects.label` | `subject_label` |",
        "| `subjects.true_age` | `subject_true_age` |",
        "| `subjects.visual_descriptors` | `visual_descriptor` |",
        "| `policies.text` | `policy_text` |",
    ):
        assert row in check, row
    assert "names the prompt lacks" in check
    assert "none: its own `image_check` row" in check
    guardrail = _section(guide, "guardrail_check")
    assert "| `image_subjects.true_age` | `subject_true_age` |" in guardrail
    assert "| `image_subjects.visual_descriptors` | `visual_descriptor` |" in guardrail
    adapter = _section(guide, "image_generation")
    assert "| `prompt` | `image_prompt` |" in adapter
    assert "| `reference_images` | `reference_image` |" in adapter
    never = adapter.split("**Never receives:**", 1)[1]
    assert "`subject_true_age`" in never
    assert "`subject_label`" in never
    assert "go to the image provider" in adapter
    prompt = _section(guide, "image_prompt")
    assert "`subject_true_age`" in prompt.split("**Never receives:**", 1)[1]
    assert ContentCategory.SUBJECT_TRUE_AGE not in registry.schema_categories(
        registry.DELIVERED["image_prompt"].input_schema
    )
    assert registry.SAFETY_PURPOSES == ("guardrail_check", "image_check")


def test_registered_stub_context() -> None:
    """``registered`` adds a stub under a name and removes it afterwards."""
    stub = StubImageGenerator()
    with registered("stub_sync", stub):
        assert "stub_sync" in images.registered_adapters()
    assert "stub_sync" not in images.registered_adapters()
