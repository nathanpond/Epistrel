"""Automatic illustration (FR-IMG-6; #265): a story set to ``illustration_mode = auto`` gets a
picture at the points its turns' renders mark, without the consumer asking.

**The hints.** A render marks an **illustration point**: a new location, a character's first
appearance, or a moment the scene turns on (``REASONS``). Hints are trusted alone; nothing is
derived deterministically.

- *Lite* (one free-text reply, #57): the prompt asks the model to end the reply with one
  optional final line ``[[illustrate: <reason>]]`` (``LITE_ILLUSTRATE_INSTRUCTION``).
  ``strip_illustrate_tag`` examines only the final non-empty line. Any trailing line matching
  ``[[illustrate…]]`` is stripped, repeatedly until none remain, whether or not the story is
  ``auto``, before the output gate, storage and delivery. Only the reply's last tag (the final
  line) is parsed, and it counts when its value is exactly one of the reasons (trimmed,
  lowercased); else it is dropped and counted (``epistrel_image_auto_hints_dropped_total``).
  The point is placed after the reply beat. No model call is added.
- *Structured* (#150): every beat render's schema gains an optional ``illustrate: [{beat_index,
  reason}]`` (``IllustrateHint``, at most ``MAX_HINTS``; ``beat_index`` is the 0-based index
  into that render's own ``beats``) when the story is ``auto``. Entries that do not validate
  are dropped and counted and never fail the render (``lenient_hints``). The first valid hint
  on a beat is its point (``TurnBeat.illustrate``), so a beat the turn does not commit drops it.

The instructions are included only when the story is ``auto``, an image adapter is configured
and the cap is above 0 (``Illustration.instructs``). The model is never told the cap.

**The queue** (``queue_points``, a commit step of the turn: never an after-commit hook). The
first ``max_auto_images_per_turn`` points in beat order are inserted as ``story`` image jobs
with their #70 ``jobs`` rows, in the turn's own transaction, so a crash can never commit the
turn without its jobs. The rest are dropped (``epistrel_image_auto_points_dropped_total{reason=
cap}``). Each job's principal is ``system`` and its key ``auto:<operation_id>:<k>`` (``k`` the
0-based index among the accepted points). Its audience is the turn's output viewpoint and its
camera ``third_person``. Its origin names the turn (``origin.auto``: operation id, variant,
``k``, reason, beat), and its ``params.auto`` marks it not yet prepared. It has no prompt and no
reservation yet: the job prepares itself (``StoryTarget.prepare``, run by the image job runner).
That means ``require_image_checker`` first (``failed``/``image_check_unconfigured``), then
#263's prompt building, protected match and #257's check (``failed``, ``guardrail_violation``
with the policy ids), then the #262 reservation (``cap_reached``, ``image_limit_reached`` or
``images_disabled``). None of these touch the turn. A staged turn (#212) stores its accepted
points with the intent (``turn_report.illustration``) and queues them in the completion's
commit, whichever process runs it.

**The response** lists the queued jobs as ``pending_images [{job_id, position: {turn, beat}}]``
(``pending_images``, read in the commit transaction, so #61's replay returns the stored list).
Image jobs never count against the turn's budget or deadline.

Logs and metrics carry reason codes, counts and ids only (invariant 3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.ids import new_ulid
from epistrel.core.images import store
from epistrel.core.images.story_images import DEFAULT_CAMERA, PendingImage
from epistrel.core.settings_schema import StorySettings
from epistrel.core.tables import stories
from epistrel.core.tables.images import ImageJobKind, image_jobs
from epistrel.observability.logging import get_logger
from epistrel.observability.metrics import (
    count_auto_hints_dropped,
    count_auto_points_dropped,
    count_auto_queued,
)
from epistrel.orchestrator.images.registry import ImageNotConfigured

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation
    from epistrel.orchestrator.models.access import ModelAccessLayer

log = get_logger(__name__)

Reason = Literal["new_location", "first_appearance", "turning_point"]
REASONS: tuple[str, ...] = ("new_location", "first_appearance", "turning_point")
MAX_HINTS = 4
"""A structured render's ``illustrate`` list holds at most this many entries."""
AUTO = "auto"
SOURCE_AUTO = "auto"
"""``origin.source`` of an automatic job (a requested one has none)."""
KEY_PREFIX = "auto"
REPORT_KEY = "illustration"
"""``turn_report.illustration``: a staged turn's accepted points, stored with the intent."""

LITE_ILLUSTRATE_INSTRUCTION = (
    "Illustration, at most rarely and only when one clearly applies: when this reply reaches "
    "a new location, a character's first appearance, or a moment the scene turns on, end it "
    "with one final line `[[illustrate: new_location]]`, `[[illustrate: first_appearance]]` or "
    "`[[illustrate: turning_point]]`. Otherwise add nothing."
)
"""The Lite prompt's illustrate instruction (the end of #57's ``## Your part``)."""
STRUCTURED_ILLUSTRATE_INSTRUCTION = (
    "Illustration, at most rarely and only when one clearly applies: when a beat reaches a new "
    "location, a character's first appearance, or a moment the scene turns on, list it in "
    "`illustrate` as `{beat_index, reason}`: the beat's 0-based index in your `beats`, and "
    "`new_location`, `first_appearance` or `turning_point`. Otherwise leave `illustrate` out."
)
"""A structured beat render's illustrate instruction (after the beat rules)."""

_TAG = re.compile(r"^\s*\[\[\s*illustrate\s*(:[^\]]*)?\]\]\s*$", re.IGNORECASE)


# -- the hints -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IllustrationPoint:
    """One point a turn marked: after committed beat ``beat`` of the turn, for ``reason``."""

    beat: int
    reason: str


@dataclass(frozen=True, slots=True)
class StrippedReply:
    """A Lite reply with its trailing illustrate tags stripped: the text, the parsed reason
    (``None``: no tag, or an invalid one) and whether an invalid tag was dropped."""

    text: str
    reason: str | None = None
    dropped: bool = False


def strip_illustrate_tag(text: str) -> StrippedReply:
    """The Lite tag grammar (module docstring): every trailing ``[[illustrate…]]`` line
    stripped, the reply's last one parsed."""
    lines = text.split("\n")
    last: str | None = None  # the final tag's value (the first one stripped)
    while True:
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines or not _TAG.match(lines[-1]):
            break
        found = _TAG.match(lines.pop())
        if last is None and found is not None:
            last = (found.group(1) or "").removeprefix(":")
    if last is None:
        return StrippedReply(text)
    stripped = "\n".join(lines).rstrip()
    value = last.strip().lower()
    if value in REASONS:
        return StrippedReply(stripped, value)
    count_auto_hints_dropped()
    return StrippedReply(stripped, None, dropped=True)


class IllustrateHint(BaseModel):
    """One structured illustrate entry: a beat of the render (0-based) and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    beat_index: int = Field(ge=0, strict=True)
    reason: Reason


def _lenient(value: Any) -> Any:
    """Keep the entries that validate (at most ``MAX_HINTS``), counting the others dropped: an
    illustrate entry never fails a render."""
    if value is None:
        return []
    if not isinstance(value, list):
        count_auto_hints_dropped()
        return []
    kept: list[IllustrateHint] = []
    dropped = 0
    for entry in value:
        try:
            hint = (
                entry if isinstance(entry, IllustrateHint) else IllustrateHint.model_validate(entry)
            )
        except ValidationError:
            dropped += 1
            continue
        if len(kept) < MAX_HINTS:
            kept.append(hint)
        else:
            dropped += 1
    count_auto_hints_dropped(dropped)
    return kept


IllustrateHints = Annotated[
    list[IllustrateHint], BeforeValidator(_lenient), Field(max_length=MAX_HINTS)
]
"""A structured render's ``illustrate`` field (see ``_lenient``)."""


def first_hints(hints: Sequence[IllustrateHint]) -> dict[int, str]:
    """Each render beat's point: the first hint naming it, in emission order."""
    found: dict[int, str] = {}
    for hint in hints:
        found.setdefault(hint.beat_index, hint.reason)
    return found


# -- the story's setting -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Illustration:
    """A turn's automatic illustration, read at its start (#64 prospective): the story's mode
    and cap, and whether an image adapter is configured."""

    mode: str = "on_request"
    cap: int = 0
    adapter: bool = False

    @property
    def instructs(self) -> bool:
        """Whether the turn's renders are asked for illustrate hints."""
        return self.mode == AUTO and self.cap > 0 and self.adapter

    @property
    def queues(self) -> bool:
        return self.instructs


OFF = Illustration()


async def adapter_configured(models: ModelAccessLayer) -> bool:
    """Whether an image adapter is configured (no job is queued without one)."""
    try:
        await models.images.get()
    except ImageNotConfigured:
        return False
    return True


async def illustration_of(models: ModelAccessLayer, settings: StorySettings) -> Illustration:
    """The turn's ``Illustration`` from the story's settings at its start."""
    mode = str(getattr(settings, "illustration_mode", "on_request"))
    cap = int(getattr(settings, "max_auto_images_per_turn", 0) or 0)
    if mode != AUTO or cap <= 0:
        return Illustration(mode, cap, False)
    return Illustration(mode, cap, await adapter_configured(models))


# -- the queue -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Accepted:
    """The points a turn queues (at most the cap, in beat order) and how many were dropped."""

    points: tuple[IllustrationPoint, ...]
    dropped: int = 0


def accept(points: Sequence[IllustrationPoint], cap: int) -> Accepted:
    """The first ``cap`` points in beat order (one per beat, the first marked)."""
    seen: dict[int, IllustrationPoint] = {}
    for point in sorted(points, key=lambda p: p.beat):
        seen.setdefault(point.beat, point)
    ordered = list(seen.values())
    return Accepted(tuple(ordered[: max(0, cap)]), max(0, len(ordered) - max(0, cap)))


def stored(points: Sequence[IllustrationPoint]) -> list[dict[str, Any]]:
    return [{"beat": p.beat, "reason": p.reason} for p in points]


def from_stored(value: Any) -> list[IllustrationPoint]:
    found: list[IllustrationPoint] = []
    for entry in value or ():
        if isinstance(entry, Mapping) and isinstance(entry.get("beat"), int):
            found.append(IllustrationPoint(int(entry["beat"]), str(entry.get("reason") or "")))
    return found


def _origin(
    op: Operation, story_id: str, viewpoint: str, point: IllustrationPoint, k: int, revision: int
) -> dict[str, Any]:
    assert op.narrative_turn is not None  # noqa: S101 - set by the commit before its steps
    turn, variant = int(op.narrative_turn), int(op.turn_variant or 0)
    return {
        "story_id": story_id,
        "operation_id": new_ulid(),
        "turn": turn,
        "variant": variant,
        "position": {"turn": turn, "variant": variant, "slot_k": 0, "beat": point.beat, "step": 0},
        "viewpoint": viewpoint,
        "timeline_revision": revision,
        "request_position": {"turn": turn, "beat": point.beat},
        "source": SOURCE_AUTO,
        "auto": {
            "operation_id": op.operation_id,
            "variant": variant,
            "k": k,
            "reason": point.reason,
            "beat_index": point.beat,
        },
    }


def is_auto(job: Any) -> bool:
    """Whether an image job is an automatic one (``origin.source = auto``)."""
    return bool(job.origin.get("source") == SOURCE_AUTO)


def needs_preparing(job: Any) -> bool:
    """Whether an automatic job has not built its prompt yet (``params.auto.prepared``)."""
    auto = job.params.get("auto")
    return isinstance(auto, Mapping) and not auto.get("prepared")


async def queue_points(
    conn: AsyncConnection,
    op: Operation,
    points: Sequence[IllustrationPoint],
    *,
    illustration: Illustration,
    viewpoint: str,
    pipeline: str,
) -> list[str]:
    """Insert the turn's automatic image jobs in ``conn``'s transaction (the turn's commit):
    the accepted points' ``story`` jobs and their ``jobs`` rows (module docstring). Their ids,
    in beat order; nothing when the turn does not queue (``illustration.queues``)."""
    from epistrel.orchestrator.images.jobs import IMAGE_JOB_KIND
    from epistrel.orchestrator.jobs import queue  # here: the queue imports the handlers

    if not illustration.queues or not points or op.story_id is None:
        return []
    accepted = accept(points, illustration.cap)
    count_auto_points_dropped("cap", accepted.dropped)
    story_id = op.story_id
    story = (
        await conn.execute(
            select(stories.c.content_rating_id, stories.c.timeline_revision).where(
                stories.c.id == story_id
            )
        )
    ).one()
    rating = None if story.content_rating_id is None else str(story.content_rating_id)
    queued: list[str] = []
    for k, point in enumerate(accepted.points):
        new = store.NewJob(
            kind=ImageJobKind.STORY,
            story_id=story_id,
            target={
                "story_id": story_id,
                "audience_viewpoint": viewpoint,
                "camera_pov": dict(DEFAULT_CAMERA),
                "content_rating_id": rating,
            },
            origin=_origin(op, story_id, viewpoint, point, k, int(story.timeline_revision)),
            viewpoint=viewpoint,
            prompt="",
            negative_prompt=None,
            params={
                "n": 1,
                "seed": None,
                "size": None,
                "aspect_ratio": None,
                "quality": None,
                "reference_image_ids": [],
                "caption": None,
                "alt_text": None,
                "prompt_subjects": [],
                "auto": {"prepared": False},
            },
            principal=SYSTEM_PRINCIPAL,
            idempotency_key=f"{KEY_PREFIX}:{op.operation_id}:{k}",
        )
        job_id, _ = await store.insert_job(conn, new)
        await queue.enqueue(conn, IMAGE_JOB_KIND, {"image_job_id": job_id}, story_id=story_id)
        queued.append(job_id)
    count_auto_queued(pipeline, len(queued))
    log.info("turn %s queued %d automatic image job(s)", op.operation_id, len(queued))
    return queued


def points_of(beats: Sequence[Any]) -> list[IllustrationPoint]:
    """The illustration points of a structured turn's committed beats (``TurnBeat.illustrate``),
    in beat order."""
    return [
        IllustrationPoint(int(b.beat), str(b.illustrate))
        for b in sorted(beats, key=lambda b: b.beat)
        if getattr(b, "illustrate", None)
    ]


def staged_report(
    points: Sequence[IllustrationPoint], illustration: Illustration
) -> dict[str, Any]:
    """``turn_report.illustration`` of a staged turn's intent: its accepted points and the
    story's mode and cap as the turn started (#64), queued at completion (``queue_staged``)."""
    accepted = accept(points, illustration.cap)
    count_auto_points_dropped("cap", accepted.dropped)
    return {
        "points": stored(accepted.points),
        "mode": illustration.mode,
        "cap": illustration.cap,
    }


async def queue_staged(conn: AsyncConnection, *, op: Operation, pipeline: str) -> None:
    """A staged turn's completion (#212 stage 4, whichever process runs it): the points its
    intent stored (``turn_report.illustration``), queued in the completion's commit. An
    abandoned staged turn never completes, so it queues nothing."""
    from epistrel.core.tables import operations

    report = await conn.scalar(
        select(operations.c.turn_report).where(operations.c.operation_id == op.operation_id)
    )
    found = report.get(REPORT_KEY) if isinstance(report, Mapping) else None
    if not isinstance(found, Mapping) or op.viewpoint is None:
        return
    illustration = Illustration(str(found.get("mode") or AUTO), int(found.get("cap") or 0), True)
    await queue_points(
        conn,
        op,
        from_stored(found.get("points")),
        illustration=illustration,
        viewpoint=op.viewpoint,
        pipeline=pipeline,
    )


async def pending_images(conn: Any, operation_id: str) -> list[PendingImage]:
    """The automatic image jobs turn operation ``operation_id`` queued, in ``k`` order."""
    j = image_jobs.c
    rows = (
        await conn.execute(
            select(j.id, j.origin).where(
                j.kind == ImageJobKind.STORY.value,
                j.origin["source"].astext == SOURCE_AUTO,
                j.origin["auto"]["operation_id"].astext == operation_id,
            )
        )
    ).all()
    found = sorted(rows, key=lambda r: int(r.origin["auto"]["k"]))
    return [
        PendingImage(
            job_id=str(r.id),
            position={
                "turn": int(r.origin["request_position"]["turn"]),
                "beat": int(r.origin["request_position"]["beat"]),
            },
        )
        for r in found
    ]


__all__ = [
    "LITE_ILLUSTRATE_INSTRUCTION",
    "MAX_HINTS",
    "OFF",
    "REASONS",
    "REPORT_KEY",
    "SOURCE_AUTO",
    "STRUCTURED_ILLUSTRATE_INSTRUCTION",
    "Accepted",
    "IllustrateHint",
    "IllustrateHints",
    "Illustration",
    "IllustrationPoint",
    "StrippedReply",
    "accept",
    "adapter_configured",
    "first_hints",
    "from_stored",
    "illustration_of",
    "is_auto",
    "needs_preparing",
    "pending_images",
    "points_of",
    "queue_points",
    "queue_staged",
    "staged_report",
    "stored",
    "strip_illustrate_tag",
]
