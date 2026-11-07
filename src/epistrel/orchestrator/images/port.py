"""The ``ImageGenerator`` port (FR-IMG-1; docs/02 §4.13; #254).

Every image adapter implements this protocol. The engine never calls an adapter directly: it
calls the ``ImageGeneratorHandle`` that ``registry.get_adapter`` builds from the model
configuration, which strips the idempotency key for an adapter that does not declare
``idempotency_keys`` and closes a superseded instance once its in-flight calls finish.

**Submission** (``Capabilities.submission``):

- ``async``: the provider acknowledges each request with its identifier before generating.
  ``submit`` returns at once with the ``attempt_ref`` and no outputs; the worker commits the
  acknowledgement (#256, ``send_state = accepted``) and only then calls ``collect``.
- ``sync``: the provider returns its identifier only with the results. ``submit`` returns when
  generation has finished, with the outputs; ``collect`` is not used, and the whole call is the
  window in which a crash leaves the send ambiguous (FR-IMG-11).

**Completion confirmation** (``confirms_completion``, optional): ``status(attempt_ref)`` and
``cancel(attempt_ref)``. Only a terminal status (``succeeded``, ``failed``, ``cancelled``) is
definitive; an ``accepted`` cancel is not, and ``unknown`` never is. Registration
(``registry.register_image_adapter``) refuses an adapter that declares it with ``sync``
submission, without both methods, or without a provider request identifier per call
(``returns_request_id``).

**Idempotency keys** (``idempotency_keys``): an adapter whose provider deduplicates requests by
a client-supplied key declares it, and ``submit`` then receives the attempt's key; otherwise it
receives ``None``.

**Cost** is a ``Cost`` in the provider's currency, or ``"unknown"`` when the provider is silent:
never ``0`` for an unpriced call. ``SubmitResult.cost`` is ``None`` only when the cost comes
later, with ``collect`` (an ``async`` adapter).

Outputs are held in memory (``ImageOutput.data``) until #256's worker writes them to private
staging; the media type is the adapter's reading of the bytes, and #256 records the dimensions
from the image itself, never from the request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_IMAGES_PER_REQUEST = 10
MAX_REFERENCE_IMAGES = 4
MAX_SEED = 2**32 - 1
MAX_PROMPT_CHARS = 8_000
SIZE_PATTERN = r"^\d{2,5}x\d{2,5}$"
"""``<width>x<height>`` in pixels (``1024x1024``)."""
ASPECT_RATIO_PATTERN = r"^\d{1,2}:\d{1,2}$"
"""``<w>:<h>`` (``16:9``)."""
DEFAULT_REQUEST_TIMEOUT_S = 120

type Submission = Literal["async", "sync"]
type Quality = Literal["low", "medium", "high", "auto"]
type AttemptState = Literal["running", "succeeded", "failed", "cancelled", "unknown"]
type CancelResult = Literal["accepted", "not_supported"]
"""What a ``cancel`` answered. Neither answer is definitive (module docstring)."""

TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})
"""The only definitive ``StatusResult.state`` values."""

Size = Annotated[str, StringConstraints(pattern=SIZE_PATTERN)]
AspectRatio = Annotated[str, StringConstraints(pattern=ASPECT_RATIO_PATTERN)]


class Capabilities(BaseModel):
    """What an adapter declares about itself (a closed field set: ``extra="forbid"``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    submission: Submission
    confirms_completion: bool = False
    idempotency_keys: bool = False
    returns_request_id: bool = False
    """Whether every call yields the provider's identifier for that request, so ``status`` can
    name it. ``confirms_completion`` needs it (registration rule ``no_request_id``)."""
    request_timeout_s: int = Field(default=DEFAULT_REQUEST_TIMEOUT_S, ge=1, le=3_600)
    """The adapter's own bound on one provider call; the ``image_generation`` row's
    ``params.request_timeout_s`` overrides it."""


class ImageRequest(BaseModel):
    """One generation request: what the adapter sends. ``size`` or ``aspect_ratio`` (at most one:
    the caller resolves the configured defaults before submitting, ``params.resolve_dimensions``),
    ``n`` images, and up to four reference images as media asset ids, which the adapter
    resolves (#255: signed URLs or data URLs)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    negative_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED)
    size: Size | None = None
    aspect_ratio: AspectRatio | None = None
    quality: Quality | None = None
    n: int = Field(default=1, ge=1, le=MAX_IMAGES_PER_REQUEST)
    reference_images: tuple[str, ...] = Field(default=(), max_length=MAX_REFERENCE_IMAGES)

    @model_validator(mode="after")
    def _one_dimension(self) -> Self:
        if self.size is not None and self.aspect_ratio is not None:
            raise ValueError("give size or aspect_ratio, not both")
        return self


@dataclass(frozen=True)
class Cost:
    """What one attempt cost, in the provider's currency (``"USD"`` for OpenRouter)."""

    amount: Decimal
    currency: str


type AttemptCost = Cost | Literal["unknown"]
"""A ``Cost``, or ``"unknown"`` when the provider said nothing: never a ``0`` it did not say."""


@dataclass(frozen=True)
class ImageUsage:
    """How many images the provider says it produced, and its usage block as it sent it."""

    images: int
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageOutput:
    """One generated image: its bytes, in memory, and their media type (``image/png``), with
    the width and height an adapter read from the bytes themselves (#255: Pillow), never from
    the request; ``None`` when the adapter did not decode them."""

    data: bytes = field(repr=False)
    media_type: str
    width: int | None = None
    height: int | None = None


class AppliedParameters(BaseModel):
    """What one attempt actually asked of the provider (FR-IMG-1; #255), stored with its
    generation metadata so that it never claims a seed or a reference image that was not used.

    ``endpoint`` is the provider endpoint that served the call, when known. ``requested`` is
    what the request asked for (reference images by media id), and ``sent`` is what went to
    the provider (reference images by count, never their URLs). ``seed_applied`` and
    ``references_applied`` are ``None`` when nothing of the kind was requested, and otherwise
    say whether it was sent to an endpoint that supports it. ``negative_prompt`` is
    ``"folded"`` when the negative prompt went into the prompt text. ``dropped_parameters``
    names the parameters that were requested but not applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str | None = None
    requested: dict[str, Any] = Field(default_factory=dict)
    sent: dict[str, Any] = Field(default_factory=dict)
    seed_applied: bool | None = None
    references_applied: bool | None = None
    negative_prompt: Literal["folded"] | None = None
    dropped_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubmitResult:
    """``submit``'s answer. An ``async`` adapter: the ``attempt_ref`` alone. A ``sync`` adapter:
    the ``attempt_ref`` with the outputs, usage and cost."""

    attempt_ref: str
    outputs: tuple[ImageOutput, ...] | None = None
    usage: ImageUsage | None = None
    cost: AttemptCost | None = None
    applied_parameters: AppliedParameters | None = None
    """What the attempt actually asked of the provider, when the adapter reports it (#255)."""


@dataclass(frozen=True)
class CollectResult:
    """``collect``'s answer for an ``async`` adapter's accepted attempt."""

    outputs: tuple[ImageOutput, ...]
    usage: ImageUsage | None
    cost: AttemptCost


@dataclass(frozen=True)
class StatusResult:
    """``status``'s answer. ``outputs`` only on ``succeeded``; usage and cost when the
    provider supplies them."""

    state: AttemptState
    outputs: tuple[ImageOutput, ...] | None = None
    usage: ImageUsage | None = None
    cost: AttemptCost | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@runtime_checkable
class ImageGenerator(Protocol):
    """One image adapter instance, built for one configuration (``registry`` builds it).

    ``capabilities`` is a declaration, the same for every instance of the adapter, so the
    registry can check it before any instance exists. An adapter that declares
    ``confirms_completion`` also implements ``ConfirmingImageGenerator``'s ``status`` and
    ``cancel``.
    """

    def capabilities(self) -> Capabilities: ...

    async def submit(self, request: ImageRequest, idempotency_key: str | None) -> SubmitResult: ...

    async def collect(self, attempt_ref: str) -> CollectResult: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ConfirmingImageGenerator(ImageGenerator, Protocol):
    """An adapter that declares ``confirms_completion``: it can look up and cancel one
    request by its ``attempt_ref``."""

    async def status(self, attempt_ref: str) -> StatusResult: ...

    async def cancel(self, attempt_ref: str) -> CancelResult: ...


def outputs_of(result: SubmitResult | CollectResult | StatusResult) -> Sequence[ImageOutput]:
    """The outputs a result carries (none for an ``async`` submit or a non-terminal status)."""
    return result.outputs or ()


__all__ = [
    "ASPECT_RATIO_PATTERN",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "MAX_IMAGES_PER_REQUEST",
    "MAX_REFERENCE_IMAGES",
    "MAX_SEED",
    "SIZE_PATTERN",
    "TERMINAL_STATES",
    "AppliedParameters",
    "AspectRatio",
    "AttemptCost",
    "AttemptState",
    "CancelResult",
    "Capabilities",
    "CollectResult",
    "ConfirmingImageGenerator",
    "Cost",
    "ImageGenerator",
    "ImageOutput",
    "ImageRequest",
    "ImageUsage",
    "Quality",
    "Size",
    "StatusResult",
    "Submission",
    "SubmitResult",
    "outputs_of",
]
