"""Scripted image adapters for the M8 tests (#254): ``StubImageGenerator`` and
``StubImageChecker``.

``StubImageGenerator`` is an ``ImageGenerator`` and its own factory: register it with
``register_image_adapter("stub_sync", stub, replace=True)`` (``registered``) and the
``image_generation`` row naming ``stub_sync`` builds to it. Its per-call ``script`` says what each
``submit`` does, in order:

- ``Outputs(n, size)``: ``n`` real PNGs of ``size`` (written by a stdlib zlib/struct encoder,
  no Pillow), with their usage and ``cost``. A ``sync`` stub answers them from ``submit``; an
  ``async`` stub acknowledges at once and answers them from ``collect``.
- ``Fail(exc)``: raise ``exc`` (from ``submit``; from ``collect`` with ``at="collect"``).
- ``Stall(event, then)``: wait until ``event`` is set (a test releases it), then do ``then``.
- ``StatusSeq(states, outputs)``: an ``async`` confirming stub: ``submit`` acknowledges, and each
  ``status`` call answers the next state (the last repeats), with ``outputs`` on ``succeeded``.

An exhausted script raises ``StubScriptExhausted``. ``requests`` records every
``(request, idempotency_key as received, attempt_ref)``; refs are ``stub-1``, ``stub-2`` …

``StubImageChecker`` scripts the ``image_check`` purpose on the stub model adapter
(``StubAdapter.when(…, purpose="image_check")``, #51) with ``{"verdicts": {<policy_id>:
pass|violation|uncertain}}`` and reads back the requests it received.
"""

from __future__ import annotations

import asyncio
import json
import struct
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

from epistrel.orchestrator.images.port import (
    AttemptCost,
    AttemptState,
    CancelResult,
    Capabilities,
    CollectResult,
    Cost,
    ImageOutput,
    ImageRequest,
    ImageUsage,
    StatusResult,
    Submission,
    SubmitResult,
)
from epistrel.orchestrator.images.registry import (
    ImageAdapterConfig,
    register_image_adapter,
    unregister_image_adapter,
)
from epistrel.orchestrator.models.registry import ImageVerdict
from epistrel.orchestrator.models.stub import StubAdapter, StubCall

STUB_COST = Cost(Decimal("0.04"), "USD")


def png(width: int, height: int, *, shade: int = 128) -> bytes:
    """A valid ``width`` x ``height`` grey RGB PNG, from the standard library alone."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes([shade, shade, shade]) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


@dataclass(frozen=True)
class Outputs:
    n: int = 1
    size: tuple[int, int] = (64, 64)
    cost: AttemptCost = STUB_COST

    def images(self) -> tuple[ImageOutput, ...]:
        width, height = self.size
        return tuple(
            ImageOutput(png(width, height, shade=(40 * i) % 256), "image/png")
            for i in range(self.n)
        )


@dataclass(frozen=True)
class Fail:
    exc: BaseException
    at: str = "submit"


@dataclass(frozen=True)
class Stall:
    event: asyncio.Event
    then: Outputs | Fail = field(default_factory=Outputs)


@dataclass(frozen=True)
class StatusSeq:
    states: Sequence[AttemptState]
    outputs: Outputs = field(default_factory=Outputs)


type Step = Outputs | Fail | Stall | StatusSeq


class StubScriptExhausted(AssertionError):
    """The stub was called more times than its script has entries."""


@dataclass
class Received:
    request: ImageRequest
    idempotency_key: str | None
    attempt_ref: str


class StubImageGenerator:
    """A scripted ``ImageGenerator`` (module docs); registered as its own factory."""

    def __init__(
        self,
        *,
        submission: Submission = "sync",
        confirms_completion: bool = False,
        idempotency_keys: bool = False,
        returns_request_id: bool | None = None,
        script: Sequence[Step] = (),
    ) -> None:
        self._capabilities = Capabilities(
            submission=submission,
            confirms_completion=confirms_completion,
            idempotency_keys=idempotency_keys,
            returns_request_id=(
                confirms_completion if returns_request_id is None else returns_request_id
            ),
        )
        self.script: list[Step] = list(script)
        self.requests: list[Received] = []
        self.configs: list[ImageAdapterConfig] = []
        self.collected: list[str] = []
        self.cancelled: list[str] = []
        self.closed = 0
        self._pending: dict[str, Step] = {}
        self._statuses: dict[str, list[AttemptState]] = {}

    # -- the factory ------------------------------------------------------------------------

    def __call__(self, config: ImageAdapterConfig) -> StubImageGenerator:
        self.configs.append(config)
        return self

    def capabilities(self) -> Capabilities:
        return self._capabilities

    # -- the port ---------------------------------------------------------------------------

    def _next(self) -> Step:
        if not self.script:
            raise StubScriptExhausted(f"no script entry for call {len(self.requests)}")
        return self.script.pop(0)

    async def _settle(self, step: Step) -> Outputs:
        while isinstance(step, Stall):
            await step.event.wait()
            step = step.then
        if isinstance(step, Fail):
            raise step.exc
        if isinstance(step, StatusSeq):
            return step.outputs
        return step

    async def submit(self, request: ImageRequest, idempotency_key: str | None) -> SubmitResult:
        ref = f"stub-{len(self.requests) + 1}"
        self.requests.append(Received(request, idempotency_key, ref))
        step = self._next()
        if self._capabilities.submission == "async":
            if isinstance(step, Fail) and step.at == "submit":
                raise step.exc
            if isinstance(step, StatusSeq):
                self._statuses[ref] = list(step.states)
            self._pending[ref] = step
            return SubmitResult(attempt_ref=ref)
        done = await self._settle(step)
        images = done.images()
        return SubmitResult(ref, images, ImageUsage(len(images)), done.cost)

    async def collect(self, attempt_ref: str) -> CollectResult:
        self.collected.append(attempt_ref)
        step = self._pending.pop(attempt_ref)
        done = await self._settle(step)
        images = done.images()
        return CollectResult(images, ImageUsage(len(images)), done.cost)

    async def status(self, attempt_ref: str) -> StatusResult:
        states = self._statuses.get(attempt_ref) or ["unknown"]
        state = states.pop(0) if len(states) > 1 else states[0]
        if state != "succeeded":
            return StatusResult(state)
        step = self._pending.get(attempt_ref)
        done = step.outputs if isinstance(step, StatusSeq) else Outputs()
        images = done.images()
        return StatusResult(state, images, ImageUsage(len(images)), done.cost)

    async def cancel(self, attempt_ref: str) -> CancelResult:
        self.cancelled.append(attempt_ref)
        return "accepted"

    async def aclose(self) -> None:
        self.closed += 1


@contextmanager
def registered(name: str, stub: StubImageGenerator) -> Iterator[StubImageGenerator]:
    """``stub`` registered as ``name`` for the duration (``stub_sync``/``stub_async``)."""
    register_image_adapter(name, stub, replace=True)
    try:
        yield stub
    finally:
        unregister_image_adapter(name)


IMAGE_CHECK = "image_check"
POLICIES_HEADING = "## Policies"


class StubImageChecker:
    """``image_check`` answers scripted on the stub model adapter, and the requests it saw."""

    def __init__(self, stub: StubAdapter) -> None:
        self.stub = stub

    def answer(
        self, verdicts: Mapping[str, ImageVerdict], *, when: str | tuple[str, ...] = ""
    ) -> None:
        """Answer ``verdicts`` to ``image_check`` requests whose messages contain ``when``
        (every one, by default). An earlier ``answer`` wins over a later one that also
        matches."""
        response = json.dumps({"verdicts": dict(verdicts)})
        self.stub.when(when or POLICIES_HEADING, response, purpose=IMAGE_CHECK)

    @property
    def calls(self) -> list[StubCall]:
        return [call for call in self.stub.calls if call.purpose == IMAGE_CHECK]

    @property
    def inputs(self) -> list[str]:
        """Each ``image_check`` request's messages, joined: what the checker was sent."""
        return ["\n".join(m.content for m in call.messages) for call in self.calls]


__all__ = [
    "IMAGE_TABLES_CLEANUP",
    "STUB_COST",
    "Fail",
    "Outputs",
    "Received",
    "Stall",
    "StatusSeq",
    "StubImageChecker",
    "StubImageGenerator",
    "StubScriptExhausted",
    "catalog_origin",
    "png",
    "registered",
    "story_origin",
]


# -- image jobs (#256) -----------------------------------------------------------------------


IMAGE_TABLES_CLEANUP = (
    "DELETE FROM image_reservations",
    "DELETE FROM image_jobs",
    "DELETE FROM jobs WHERE kind = 'image_job'",
)
"""What an image job test leaves on the shared database, taken back after it (a queued
``image_job`` row would otherwise reach a later test's worker)."""


def story_origin(story_id: str, **fields: object) -> dict[str, object]:
    """A story job's origin (docs/02 §4.13)."""
    return {
        "story_id": story_id,
        "operation_id": None,
        "turn": 1,
        "variant": 1,
        "position": None,
        "viewpoint": "omniscient",
        "timeline_revision": 1,
        **fields,
    }


def catalog_origin(entry_id: str) -> dict[str, object]:
    """A catalog job's origin."""
    return {
        "entry_kind": "character",
        "entry_id": entry_id,
        "draft_revision": 1,
        "image_input_fingerprint": "f" * 12,
    }
