"""Test seams for the image job pipeline (#256): a test target, scripted checks, and a minimal
image adapter a child engine process can register (``testing.faults serve --images``).

- ``TestImageTarget`` stands in for the ``story`` target (#263): it accepts any enqueue (a
  ``target`` of ``{"refuse": true}`` is refused), and promotes each passing output as a ``live``
  asset rated Everyone, owned by the requesting principal. Tests register it for ``story``
  (``registered_target``); the ``kind`` CHECK stays ``catalog|story``.
- ``ScriptedChecks`` stands in for #257's and #258's checks: each output's verdict comes from
  its script in order (``"pass"``, or a policy id that output violates; the last repeats), after
  the same fail-closed rule as the default (``image_check`` unset while an image-output policy
  applies raises ``ImageCheckUnconfigured``), unless ``fail_closed=False``. ``stall`` makes each
  output check wait for an event first. ``prompt_violation`` refuses every prompt.
- ``PngImageGenerator`` is a ``sync`` adapter answering ``n`` grey PNGs, written with the
  standard library alone. #268: ``submission="async"`` makes it acknowledge in ``submit`` and
  answer from ``collect``, and ``idempotency_keys=True`` makes it declare that capability (a
  child engine's crash tests need both: ``testing.faults`` registers the three variants).

Nothing in ``create_app`` or the Engine imports this module.
"""

from __future__ import annotations

import asyncio
import struct
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.errors import GuardrailViolation, InvalidRequest
from epistrel.core.images.store import ImageJob
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.tables import content_ratings
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.core.tables.media import MediaState
from epistrel.orchestrator.images import jobs
from epistrel.orchestrator.images.port import (
    Capabilities,
    CollectResult,
    Cost,
    ImageOutput,
    ImageRequest,
    ImageUsage,
    SubmitResult,
)
from epistrel.orchestrator.images.registry import ImageAdapterConfig, ensure_image_check_configured

PASS = "pass"  # noqa: S105 - a verdict, not a secret


class TestImageTarget:
    """The test target (module docstring); ``promoted`` and ``ended`` record what it saw."""

    __test__ = False

    success_status = ImageJobStatus.PROMOTED

    def __init__(self) -> None:
        self.promoted: list[tuple[str, list[str]]] = []
        self.ended: list[tuple[str, str]] = []

    async def validate_enqueue(
        self, conn: AsyncConnection, target: Mapping[str, Any], origin: Mapping[str, Any]
    ) -> None:
        if target.get("refuse"):
            raise InvalidRequest("the test target refuses", {"field": "target"})

    async def promote(
        self, conn: AsyncConnection, job: ImageJob, outputs: Sequence[jobs.PromotableOutput]
    ) -> list[str]:
        version = await conn.scalar(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == EVERYONE_ID)
        )
        ids = [
            await jobs.insert_output_asset(
                conn,
                job,
                output,
                state=MediaState.LIVE,
                owner=job.principal,
                content_rating_version_id=str(version),
            )
            for output in outputs
        ]
        self.promoted.append((job.id, ids))
        return ids

    async def on_terminal(self, conn: AsyncConnection, job: ImageJob) -> None:
        self.ended.append((job.id, job.status.value))

    async def result_ids(self, conn: AsyncConnection, job: ImageJob) -> tuple[str, list[str]]:
        return "asset_ids", list(job.asset_ids)


@contextmanager
def registered_target(
    target: TestImageTarget | None = None, kind: ImageJobKind = ImageJobKind.STORY
) -> Iterator[TestImageTarget]:
    """``target`` (a new one by default) registered for ``kind`` for the duration."""
    found = target or TestImageTarget()
    before = jobs.target_for(kind)
    jobs.register_target(kind, found)
    try:
        yield found
    finally:
        if before is None:
            jobs.unregister_target(kind)
        else:
            jobs.register_target(kind, before)


class ScriptedChecks:
    """Scripted prompt and output checks (module docstring)."""

    def __init__(
        self,
        script: Sequence[str] = (PASS,),
        *,
        fail_closed: bool = True,
        stall: asyncio.Event | None = None,
        prompt_violation: str | None = None,
        unavailable: int = 0,
    ) -> None:
        self.script = list(script) or [PASS]
        self.fail_closed = fail_closed
        self.stall = stall
        self.stalled = asyncio.Event()
        self.prompt_violation = prompt_violation
        self.unavailable = unavailable
        self.checked: list[tuple[str, int]] = []
        self.prompts: list[str] = []

    async def check_prompt(self, runtime: jobs.ImageRuntime, check: jobs.PromptCheck) -> None:
        self.prompts.append(check.request.prompt)
        if self.prompt_violation is not None:
            raise GuardrailViolation(
                "the prompt violates a policy",
                {
                    "violations": [
                        {
                            "policy_id": self.prompt_violation,
                            "policy_name": self.prompt_violation,
                            "message": "This content isn't allowed.",
                        }
                    ]
                },
            )

    async def check_output(
        self, runtime: jobs.ImageRuntime, job: ImageJob, output: jobs.StagedOutput
    ) -> jobs.OutputVerdict:
        if self.fail_closed:
            policies = await jobs.image_output_policy_set(runtime.db)
            await ensure_image_check_configured(runtime.models, policies)
        if self.stall is not None:
            self.stalled.set()
            await self.stall.wait()
        if self.unavailable > 0:
            self.unavailable -= 1
            raise jobs.ImageCheckUnavailable("scripted")
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        self.checked.append((job.id, output.index))
        policy = step if step != PASS else jobs.MINORS_POLICY_ID
        result = "pass" if step == PASS else "violation"
        return jobs.OutputVerdict(
            per_policy=(jobs.PolicyVerdict(policy, result),),  # type: ignore[arg-type]
            model="stub-vision",
            endpoint="checker.test",
        )


@contextmanager
def installed_checks(checks: ScriptedChecks) -> Iterator[ScriptedChecks]:
    """``checks`` as the pipeline's checks for the duration."""
    jobs.set_image_checks(checks)
    try:
        yield checks
    finally:
        jobs.set_image_checks(None)


def png(width: int = 16, height: int = 16, *, shade: int = 128) -> bytes:
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


class PngImageGenerator:
    """An image adapter answering ``n`` PNGs per call (``sync`` by default); its own
    factory."""

    COST = Cost(Decimal("0.01"), "USD")

    def __init__(
        self, *, submission: Literal["sync", "async"] = "sync", idempotency_keys: bool = False
    ) -> None:
        self.calls = 0
        self.submission: Literal["sync", "async"] = submission
        self.idempotency_keys = idempotency_keys
        self._pending: dict[str, int] = {}

    def __call__(self, config: ImageAdapterConfig) -> PngImageGenerator:
        return self

    def capabilities(self) -> Capabilities:
        return Capabilities(submission=self.submission, idempotency_keys=self.idempotency_keys)

    def _images(self, n: int) -> tuple[ImageOutput, ...]:
        return tuple(
            ImageOutput(png(shade=(self.calls * 37 + i * 11) % 256), "image/png") for i in range(n)
        )

    async def submit(self, request: ImageRequest, idempotency_key: str | None) -> SubmitResult:
        self.calls += 1
        ref = f"png-{self.calls}"
        if self.submission == "async":
            self._pending[ref] = request.n
            return SubmitResult(attempt_ref=ref)
        images = self._images(request.n)
        return SubmitResult(ref, images, ImageUsage(len(images)), self.COST)

    async def collect(self, attempt_ref: str) -> CollectResult:
        if self.submission != "async":
            raise NotImplementedError("a sync adapter is never collected")
        images = self._images(self._pending.pop(attempt_ref, 1))
        return CollectResult(images, ImageUsage(len(images)), self.COST)

    async def aclose(self) -> None:
        return None


__all__ = [
    "PASS",
    "PngImageGenerator",
    "ScriptedChecks",
    "TestImageTarget",
    "installed_checks",
    "png",
    "registered_target",
]
