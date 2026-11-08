"""The adapter's request, built from a value with no handle to true ages (FR-IMG-12; #257, #797).

``ImageRequest`` (``port``) is what an image adapter sends. It is built here and nowhere else in
the job pipeline, from ``RequestInputs``: the job's prompt, negative prompt and request-shaped
parameters, copied out of the ``image_jobs`` row by the caller. ``RequestInputs`` has no
``safety_context`` and no age field, and this module imports neither ``core.images.store`` nor
``orchestrator.images.jobs`` (where ``ImageJob`` and its safety context live), nor
``core.images.safety`` or ``core.catalog.ages``: the import-linter contract
``images-generation-path-has-no-ages`` lists it as a source module, and
``tests/gates/test_image_paths_contract.py`` walks its types.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from epistrel.orchestrator.images.params import (
    ImageGenerationParams,
    resolve_dimensions,
    resolve_quality,
)
from epistrel.orchestrator.images.port import ImageRequest, Quality


@dataclass(frozen=True, slots=True)
class RequestInputs:
    """What the adapter's request is built from: the job's perceivable prompt and its
    request-shaped parameters, nothing else of the job."""

    prompt: str
    negative_prompt: str | None
    size: str | None = None
    aspect_ratio: str | None = None
    quality: Quality | None = None
    n: int = 1
    reference_image_ids: tuple[str, ...] = ()

    @classmethod
    def of(
        cls, prompt: str, negative_prompt: str | None, params: Mapping[str, Any]
    ) -> RequestInputs:
        """The inputs from a job's ``prompt``, ``negative_prompt`` and ``params``."""
        references: Sequence[str] = params.get("reference_image_ids") or ()
        return cls(
            prompt=prompt,
            negative_prompt=negative_prompt,
            size=params.get("size"),
            aspect_ratio=params.get("aspect_ratio"),
            quality=params.get("quality"),
            n=int(params.get("n") or 1),
            reference_image_ids=tuple(references),
        )


def build_image_request(
    inputs: RequestInputs, seed: int, params: ImageGenerationParams
) -> ImageRequest:
    """A fresh attempt's request: the configured defaults resolved (``params``)."""
    size, ratio = resolve_dimensions(params, size=inputs.size, aspect_ratio=inputs.aspect_ratio)
    return ImageRequest(
        prompt=inputs.prompt,
        negative_prompt=inputs.negative_prompt,
        seed=seed,
        size=size,
        aspect_ratio=ratio,
        quality=resolve_quality(params, inputs.quality),
        n=inputs.n,
        reference_images=inputs.reference_image_ids,
    )


def resend_request(snapshot: Mapping[str, Any], inputs: RequestInputs) -> ImageRequest:
    """A re-send's request (#268): the stalled attempt's ``request_snapshot``, which holds no
    prompt text (accounting rows outlive their story), with the job's prompt sent again."""
    return ImageRequest.model_validate(
        {**snapshot, "prompt": inputs.prompt, "negative_prompt": inputs.negative_prompt}
    )
