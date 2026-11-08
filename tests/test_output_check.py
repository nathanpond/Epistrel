"""Unit tests of #258's pieces: multimodal messages on the wire, the image sent to the checker,
the checker fingerprint, and how ``check_output`` reads the checker's answer (``on_uncertain``,
omitted ids, deterministic policies, certification per policy)."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image

from epistrel.core.images.safety import ResolvedSubject, SafetyContext
from epistrel.core.policies.model import Target
from epistrel.orchestrator.images import output_check
from epistrel.orchestrator.models import held
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.openai_compatible import request_body
from epistrel.orchestrator.models.prompts import render
from epistrel.orchestrator.models.registry import IMAGE_CHECK, ImageCheckVerdict
from epistrel.orchestrator.models.structured import Uncertain
from tests.policy_support import policy, words

IMAGE_TARGETS = (Target.IMAGE_OUTPUT,)


def _png(width: int, height: int, mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height)).save(out, format="PNG")
    return out.getvalue()


def _decoded(url: str) -> Image.Image:
    head, data = url.split(",", 1)
    assert head.endswith(";base64")
    return Image.open(io.BytesIO(base64.b64decode(data)))


def test_message_wire_is_unchanged_for_text_and_an_array_with_images() -> None:
    """A text message goes on the wire exactly as before (``{role, content}``), and so does its
    held-call key; a message with images goes as OpenAI's content array.

    Complement asserted: the image message's key differs from its text-only twin's."""
    plain = Message(role="user", content="hello")
    assert plain.wire() == {"role": "user", "content": "hello"}
    assert plain.wire() == plain.model_dump(exclude={"images"})
    with_image = Message(role="user", content="hello", images=("data:image/png;base64,AAAA",))
    assert with_image.wire() == {
        "role": "user",
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    body = request_body("m", [plain, with_image], ModelParams(), None)
    assert body["messages"] == [plain.wire(), with_image.wire()]
    assert held.key_of("p", [plain]) != held.key_of("p", [with_image])
    assert held.key_of("p", [plain]) == held.key_of("p", [Message(role="user", content="hello")])


def test_image_check_render_sends_the_image_as_a_part() -> None:
    """``image_check``'s rendered user message carries the image as a part, never as text.

    Complement asserted: the system message carries no image."""
    inputs = IMAGE_CHECK.input_schema.model_validate(
        {
            "image": "data:image/png;base64,QUJD",
            "prompt": "a harbour",
            "policies": [{"policy_id": "p1", "text": "no gore"}],
        }
    )
    system, user = render(IMAGE_CHECK, inputs)
    assert system.images == ()
    assert user.images == ("data:image/png;base64,QUJD",)
    assert "QUJD" not in system.content + user.content
    assert "Prompt: a harbour" in user.content


def test_data_url_keeps_small_images_and_downscales_large_ones() -> None:
    """An image that fits goes as it is; a larger one, or one of another type, is downscaled to
    at most 1024 px on its longest side (aspect kept) and sent as PNG.

    Complement asserted: the small image's bytes are unchanged."""
    small = _png(40, 30)
    url = output_check.data_url(small, "image/png")
    assert base64.b64decode(url.split(",", 1)[1]) == small
    large = output_check.data_url(_png(3000, 1500, "RGBA"), "image/png")
    assert large.startswith("data:image/png;base64,")
    assert _decoded(large).size == (1024, 512)
    other = output_check.data_url(_png(20, 20), "image/bmp")
    assert other.startswith("data:image/png;base64,")


def test_checker_fingerprint_moves_with_each_input() -> None:
    """12 hex, the same for the same inputs, and different when the host, the model, the
    reported version or the template hash changes (an edited checker prompt loses
    its certification)."""
    base = output_check.checker_fingerprint("vision.example", "v1", None, "a" * 64)
    assert len(base) == 12
    assert int(base, 16) >= 0
    assert base == output_check.checker_fingerprint("vision.example", "v1", None, "a" * 64)
    for changed in (
        ("other.example", "v1", None, "a" * 64),
        ("vision.example", "v2", None, "a" * 64),
        ("vision.example", "v1", "2026-11", "a" * 64),
        ("vision.example", "v1", None, "b" * 64),
    ):
        assert output_check.checker_fingerprint(*changed) != base
    assert output_check.template_sha256() == output_check.TEMPLATE_SHA256


@dataclass
class FakeModels:
    """Just enough of the Model Access Layer: ``structured_call`` answers ``answer``."""

    answer: Any
    calls: list[Any] = field(default_factory=list)

    async def structured_call(self, purpose: str, schema: Any, inputs: Any, **_: Any) -> Any:
        self.calls.append((purpose, inputs))
        return self.answer


CHECKER = output_check.Checker(model="vision-1", endpoint="vision.example", fingerprint="f" * 12)
CONTEXT = SafetyContext(
    subjects=(ResolvedSubject("subject_1", "c1", 17, "minor", "declared"),), threshold=18
)


async def _check(models: FakeModels, policies: list[Any], **texts: Any) -> Any:
    return await output_check.check_output(
        models,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        _png(8, 8),
        "image/png",
        prompt=texts.get("prompt"),
        caption=texts.get("caption"),
        alt_text=None,
        safety_context=CONTEXT,
        policies=policies,
        found_checker=CHECKER,
        certified=frozenset({"block"}),
    )


async def test_check_output_reads_the_answer_per_policy() -> None:
    """One call judges every model-assisted policy. ``uncertain`` (said, or an id the answer
    omits) follows ``on_uncertain``: ``violation`` blocks, ``pass`` allows with
    ``uncertain: true``. Deterministic policies run locally on the texts. Certification is per
    policy. An answer that never validates counts every policy uncertain.

    Complement asserted: an allowed uncertain result is a stored pass; the deterministic
    policy's word in the caption is a violation without any model verdict for it."""
    block = policy(
        {"type": "model_assisted", "prompt": "no gore", "on_uncertain": "violation"},
        policy_id="block",
        applies_to=IMAGE_TARGETS,
    )
    allow = policy(
        {"type": "model_assisted", "prompt": "no logos", "on_uncertain": "pass"},
        policy_id="allow",
        applies_to=IMAGE_TARGETS,
    )
    said = policy(
        {"type": "model_assisted", "prompt": "no text overlays"},
        policy_id="said",
        applies_to=IMAGE_TARGETS,
    )
    listed = policy(words("wyvern"), policy_id="listed", applies_to=IMAGE_TARGETS)
    models = FakeModels(ImageCheckVerdict(verdicts={"said": "pass", "allow": "uncertain"}))
    verdict = await _check(models, [block, allow, said, listed], caption="a wyvern at dusk")
    (call,) = models.calls
    assert call[0] == "image_check"
    assert [p.policy_id for p in call[1].policies] == ["block", "allow", "said"]
    assert call[1].subjects[0].true_age == 17
    assert call[1].image.startswith("data:image/png;base64,")
    found = {v.policy_id: (v.result, v.uncertain, v.certified) for v in verdict.per_policy}
    assert found == {
        "block": ("violation", True, True),
        "allow": ("pass", True, False),
        "said": ("pass", False, False),
        "listed": ("violation", False, False),
    }
    assert verdict.violations == ["block", "listed"]
    assert [r["policy_id"] for r in verdict.policy_results()] == ["allow", "said"]
    assert (verdict.model, verdict.endpoint) == ("vision-1", "vision.example")

    garbled = await _check(FakeModels(Uncertain("parse_error", 3)), [block, allow])
    assert [(v.policy_id, v.result) for v in garbled.per_policy] == [
        ("block", "violation"),
        ("allow", "pass"),
    ]
    clean = await _check(FakeModels(ImageCheckVerdict(verdicts={})), [listed], caption="dusk")
    assert clean.passed
    assert clean.check_status == "passed"


async def test_no_applicable_policy_makes_no_call_and_stays_unchecked() -> None:
    """With zero applicable image-output policies no call is made and the image stays
    ``unchecked``; ``output_policies`` keeps image-output policies only, a character policy
    only for a resolved subject.

    Complement asserted: a text-only policy is left out, an image-output one is kept."""
    models = FakeModels(ImageCheckVerdict())
    verdict = await _check(models, [])
    assert models.calls == []
    assert verdict.check_status == "unchecked"
    assert output_check.record_of(verdict).status.value == "unchecked"
    text_only = policy(words("x"), policy_id="t", applies_to=(Target.TEXT_OUTPUT,))
    image = policy(words("x"), policy_id="i", applies_to=IMAGE_TARGETS)
    assert [p.id for p in output_check.output_policies([text_only, image], CONTEXT)] == ["i"]


@pytest.mark.parametrize("media_type", ["image/jpeg", "image/webp"])
def test_data_url_sends_vision_types_as_they_are(media_type: str) -> None:
    """JPEG and WebP that fit go with their own type."""
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, format=media_type.split("/")[1].upper())
    url = output_check.data_url(out.getvalue(), media_type)
    assert url.startswith(f"data:{media_type};base64,")
