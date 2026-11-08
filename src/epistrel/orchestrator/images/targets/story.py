"""The ``story`` image job target: a generated story image (FR-IMG-5,8,10; #263).

A story job's ``target`` is ``{story_id, audience_viewpoint, camera_pov, content_rating_id}``
and its ``origin`` ``{story_id, operation_id, turn, variant, position, viewpoint,
timeline_revision, request_position}`` (docs/02 §4.13): ``operation_id`` is the operation the
image will be placed in, a ULID minted in the enqueue transaction; ``position`` is the
five-column position the safety path reads (``core.images.safety.image_position``); and
``request_position`` is the request's own (``{turn, beat}`` or ``{slot_after_turn}``). The job's
``params`` also fix, at enqueue, the image's ``source`` (``generated`` or
``generated_from_consumer_prompt``), its ``viewpoint_basis`` (``filtered_inputs`` or
``protected_term_match``) and its final ``depicted_subjects``; promotion copies them verbatim and
never recomputes them. The worker sends exactly the stored prompt.

- ``validate_enqueue``: the placement's checks again (``core.images.story_images.place_at``: an
  active position, the audience, the camera), 422 as at the request.
- ``revalidate(conn, job)`` (#264; FR-IMG-4, FR-ADMIN-9): the story revalidation, point in
  time, in the caller's transaction. It takes the story row ``FOR UPDATE`` first (#47's lock
  order: the runner locks the job row after it) and answers, by precedence ``target_gone >
  erased > target_inactive > amended > policy_changed``:

  - the story deleted → ``Gone`` (an archived story is present);
  - #127's erasure guard refusing a depicted subject or the audience's character
    (``assert_not_erased``) or the stored prompt, caption or alt text (``check_content``, as a
    consumer's text when the prompt was one) → ``Stale(erased)``;
  - the story ``paused`` (#118/#120/#228), ``blocked_by_erasure`` (#125) or ``importing`` →
    ``Wait`` until ``image_pause_recheck_s`` from now, or ``Stale(paused_too_long)`` once the job
    has waited ``image_pause_max_wait_s`` since its first wait (``image_jobs.wait_started_at``);
  - the origin's ``(turn, variant)`` not active now, or (a slot) no longer the last completed
    turn → ``Stale(target_inactive)``, ``cause`` ``truncated``, ``swapped``, ``regenerated`` (a
    newer variant is active at the turn) or ``advanced`` (a later turn committed after a slot's);
  - an active amendment (#221; an ``undo``/``redo``, #238, counted as its own, an undo carrying
    the parts it reverted) of a ``rewrite_session`` operation whose ``timeline_revision`` is past
    the origin's, with a record part on an event or utterance of the target variant, or a
    proposition, State or ``facts`` part about a depicted subject or one the texts name, on any
    predicate but ``age`` (owner 2026-11-06: no visual flag) → ``Stale(amended)``;
  - a policy of the story's image-prompt and image-output set now that the job's last check did
    not run under (``image_jobs.checked_policies``) → ``NeedsEvaluation``;
  - otherwise ``Ok``.

  The runner also calls it before each provider call (only ``Gone`` counts there) and before the
  outputs are checked (``Gone``, ``Wait`` and a ``Stale`` no later state undoes count there).
- ``refresh(runtime, job)`` (#264, outside any lock, before the outputs are checked): the story's
  image policy set, then #257's prompt check again on the stored prompt (consumer prompts
  included) with a fresh safety context (presence, true ages, aliases now); both are stored on
  the job for the output check and ``revalidate``. A refused prompt is ``Stale(policy_changed)``
  naming the policies.
- ``promote`` (inside the promotion's transaction, after ``revalidate`` answered ``Ok``): the
  output's ``media_assets`` row (``live``, owned by the story's owner, under its rating's current
  version, with the job's source, viewpoint basis and depicted subjects) and the
  ``story_images`` row, under a ``story_image_added`` event in the job's own operation (anchored
  as an upload's: a beat position's ``story_image`` operation at its turn, a slot's inter-turn
  operation after the last completed turn). A placement the timeline refuses even so is
  ``TargetStale(target_inactive)``. The job's ``result`` is ``{asset_ids, image_ids}``.

A ``stale`` or ``target_gone`` ending writes an ``image_job_ended`` audit row ``{job_id,
reason}`` and nothing the story shows; an ended job is never revived (an untruncate included),
and a job staled by its turn's regeneration never queues itself again (#265 queues the new
variant's own). Logs carry ids only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.config_changes import read_section
from epistrel.core.enums import OperationKind, OperationStatus, StoryStatus
from epistrel.core.erasure.guard import Content, assert_not_erased, check_content
from epistrel.core.errors import (
    ErasedSubjectReferenced,
    GuardrailViolation,
    PositionInvalid,
    StoryNotFound,
)
from epistrel.core.images import story_images
from epistrel.core.images.safety import SafetyContext
from epistrel.core.images.store import ImageJob, read_job, set_result, update_job
from epistrel.core.membership import active_operation
from epistrel.core.policies.model import Target
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.settings_schema import ImageJobSettings
from epistrel.core.tables import (
    amendment_parts,
    amendments,
    content_ratings,
    events,
    facts,
    operations,
    stories,
    utterances,
)
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.core.tables.media import MediaSource, MediaState
from epistrel.core.tables.story_images import ViewpointBasis
from epistrel.orchestrator.images import jobs

if TYPE_CHECKING:
    from epistrel.orchestrator.images.jobs import ImageRuntime

FIELD = "body"
"""Where a generation request's fields are reported (its JSON body)."""
JOB_LOCATION = jobs.JOB_LOCATION
GUARD_PATH = "epistrel.orchestrator.images.targets.story"
"""What the erasure guard's spy records for revalidation's reads (it writes nothing)."""
AGE_PREDICATE_ID = CORE_BY_NAME["age"].id
"""A true-age amendment never stales an image through the amendment clause: it reaches it
through the policy re-check (the safety context's true ages)."""
WAITING_STATUSES = frozenset(
    {StoryStatus.PAUSED.value, StoryStatus.BLOCKED_BY_ERASURE.value, StoryStatus.IMPORTING.value}
)
"""A story in one of these waits: a rating, reactivation or backfill check, #125's reconcile,
or an import (whose jobs are not even claimed)."""
NAMING_SOURCES = frozenset({"declared", "depicted", "named"})
"""A safety-context subject the image depicts or its texts name (not one merely present)."""


@dataclass(frozen=True, slots=True)
class StoryImageQueued:
    """A story image generation request's answer: 202 ``{job_id, status: queued}`` with
    ``Location: /image-jobs/{id}``, and the story's unchanged ``timeline_revision`` (the image is
    placed, in its own operation, when the job promotes it). A replay of the same key answers
    the same job (``replayed``)."""

    job_id: str
    timeline_revision: int
    status: str = ImageJobStatus.QUEUED.value
    replayed: bool = False

    @property
    def location(self) -> str:
        return JOB_LOCATION.format(self.job_id)


def request_position(
    origin: Mapping[str, Any],
) -> story_images.BeatPosition | story_images.SlotPosition:
    """The request's position from a job's origin."""
    found = origin.get("request_position") or {}
    if "slot_after_turn" in found:
        return story_images.SlotPosition(slot_after_turn=int(found["slot_after_turn"]))
    return story_images.BeatPosition(turn=int(found["turn"]), beat=int(found["beat"]))


def origin_of(
    placement: story_images.Placement,
    *,
    operation_id: str,
    timeline_revision: int,
    position: story_images.BeatPosition | story_images.SlotPosition,
) -> dict[str, Any]:
    """A story job's origin (module docstring)."""
    return {
        "story_id": placement.story_id,
        "operation_id": operation_id,
        "turn": placement.turn,
        "variant": placement.variant,
        "position": {
            "turn": placement.turn,
            "variant": placement.variant,
            "slot_k": 1 if placement.slot else 0,
            "beat": 0 if placement.slot else placement.beat,
            "step": 0,
        },
        "viewpoint": placement.audience_viewpoint,
        "timeline_revision": timeline_revision,
        "request_position": position.model_dump(mode="json"),
    }


def target_of(placement: story_images.Placement) -> dict[str, Any]:
    """A story job's target (module docstring)."""
    return {
        "story_id": placement.story_id,
        "audience_viewpoint": placement.audience_viewpoint,
        "camera_pov": dict(placement.camera_pov),
        "content_rating_id": placement.content_rating_id,
    }


async def _owner(conn: AsyncConnection, story_id: str) -> str | None:
    found = await conn.scalar(select(stories.c.owner_principal).where(stories.c.id == story_id))
    return None if found is None else str(found)


# -- revalidation (#264) ----------------------------------------------------------------------


def _depicted_ids(job: ImageJob) -> list[str]:
    """The subjects the image depicts (``params.depicted_subjects``, fixed at enqueue)."""
    found: list[str] = []
    for subject in job.params.get("depicted_subjects") or ():
        value = subject.get("id") if isinstance(subject, Mapping) else subject
        if value:
            found.append(str(value))
    return found


def _subject_ids(job: ImageJob) -> list[str]:
    """The depicted subjects and the characters the texts name (the safety context's declared,
    depicted and named subjects): whose facts an amendment may not change under the image."""
    found = dict.fromkeys(_depicted_ids(job))
    context = SafetyContext.from_json(job.safety_context or {})
    for subject in context.characters:
        if subject.source in NAMING_SOURCES and subject.character_id is not None:
            found.setdefault(subject.character_id)
    return list(found)


def _texts(job: ImageJob) -> tuple[str, ...]:
    return tuple(
        str(t) for t in (job.prompt, job.params.get("caption"), job.params.get("alt_text")) if t
    )


async def _erased(conn: AsyncConnection, job: ImageJob) -> bool:
    """Whether #127's erasure guard refuses the image: a depicted subject or the audience's
    character, or the stored prompt, caption or alt text."""
    story_id = str(job.origin["story_id"])
    ids = _depicted_ids(job)
    audience = story_images.audience_character(str(job.target.get("audience_viewpoint") or ""))
    if audience is not None:
        ids.append(audience)
    consumer = job.params.get("source") == MediaSource.GENERATED_FROM_CONSUMER_PROMPT.value
    revision = job.origin.get("timeline_revision")
    try:
        await assert_not_erased(conn, ids, field="depicted_subjects", path=GUARD_PATH)
        await check_content(
            conn,
            story_id,
            [Content(_texts(job), frozenset(ids), consumer)],
            base_revision=None if revision is None else int(revision),
            path=GUARD_PATH,
        )
    except ErasedSubjectReferenced:
        return True
    return False


async def _wait(conn: AsyncConnection, job: ImageJob) -> jobs.Wait | jobs.Stale:
    """A paused story: ``Wait`` for ``image_pause_recheck_s``, or ``Stale(paused_too_long)``
    once the job has waited ``image_pause_max_wait_s`` since its first wait."""
    settings = await read_section(conn, ImageJobSettings)
    now = clock.now()
    started = job.wait_started_at or now
    if (now - started).total_seconds() > settings.image_pause_max_wait_s:
        return jobs.Stale("paused_too_long")
    recheck = settings.image_pause_recheck_s
    return jobs.Wait(now + timedelta(seconds=recheck), recheck)


def _turns(story_id: str, turn: int) -> Any:
    o = operations.c
    return (
        o.story_id == story_id,
        o.narrative_turn == turn,
        o.kind == OperationKind.TURN.value,
        o.status == OperationStatus.COMMITTED.value,
    )


async def _target_operation(conn: AsyncConnection, job: ImageJob) -> str | None:
    """The origin's turn variant's operation (``None`` for a slot before the first turn)."""
    turn, variant = int(job.origin["turn"]), int(job.origin.get("variant") or 0)
    if turn < 1:
        return None
    found = await conn.scalar(
        select(operations.c.operation_id).where(
            *_turns(str(job.origin["story_id"]), turn), operations.c.turn_variant == variant
        )
    )
    return None if found is None else str(found)


async def _inactive(conn: AsyncConnection, job: ImageJob) -> str | None:
    """Why the origin's position is no longer on the timeline (``truncated``, ``swapped``,
    ``regenerated``, ``advanced``), or ``None`` when it still is."""
    story_id = str(job.origin["story_id"])
    turn, variant = int(job.origin["turn"]), int(job.origin.get("variant") or 0)
    if turn >= 1:
        active = await conn.scalar(
            select(operations.c.turn_variant).where(
                *_turns(story_id, turn), active_operation(operations)
            )
        )
        if active is None:
            return "truncated"
        if int(active or 0) != variant:
            return "regenerated" if int(active or 0) > variant else "swapped"
    if isinstance(request_position(job.origin), story_images.SlotPosition):
        last = await story_images.last_completed_turn(conn, story_id)
        if last != turn:
            return "advanced"
    return None


async def _amendment(conn: AsyncConnection, job: ImageJob) -> str | None:
    """The first active amendment since the enqueue (its ``rewrite_session`` operation's
    ``timeline_revision`` past the origin's) that changed the target variant's beats or a
    non-``age`` fact of a subject (module docstring); ``None`` when there is none."""
    story_id = str(job.origin["story_id"])
    revision = int(job.origin.get("timeline_revision") or 0)
    a, o = amendments.c, operations.c
    later = (
        await conn.execute(
            select(a.id, a.undoes)
            .select_from(amendments.join(operations, o.operation_id == a.operation_id))
            .where(a.story_id == story_id, a.status == "active", o.timeline_revision > revision)
            .order_by(a.tx_time, a.id)
        )
    ).all()
    if not later:
        return None
    subjects = _subject_ids(job)
    target = await _target_operation(conn, job)
    for row in later:
        ids = [str(row.id), *([str(row.undoes)] if row.undoes is not None else [])]
        if await _touches(conn, story_id, ids, target, subjects):
            return str(row.id)
    return None


async def _touches(
    conn: AsyncConnection,
    story_id: str,
    amendment_ids: Sequence[str],
    target: str | None,
    subjects: Collection[str],
) -> bool:
    """Whether a part of ``amendment_ids`` corrects a record of operation ``target`` (an event
    or an utterance) or a non-``age`` fact of one of ``subjects``."""
    p = amendment_parts.c
    clauses: list[Any] = []
    if target is not None:
        clauses.append(
            (p.record_table == "events")
            & p.record_id.in_(select(events.c.id).where(events.c.operation_id == target))
        )
        clauses.append(
            (p.record_table == "utterances")
            & p.record_id.in_(select(utterances.c.id).where(utterances.c.operation_id == target))
        )
    if subjects:
        wanted = list(subjects)
        clauses.append(
            (p.kind == "proposition")
            & p.subject_id.in_(wanted)
            & (p.predicate_id.is_(None) | (p.predicate_id != AGE_PREDICATE_ID))
        )
        clauses.append(
            (p.kind == "proposition") & p.state_id.is_not(None) & p.scope_key.in_(wanted)
        )
        clauses.append(
            (p.record_table == "facts")
            & p.record_id.in_(
                select(facts.c.id).where(
                    facts.c.story_id == story_id,
                    facts.c.subject_id.in_(wanted),
                    facts.c.predicate_id != AGE_PREDICATE_ID,
                )
            )
        )
    if not clauses:
        return False
    found = await conn.scalar(
        select(func.count())
        .select_from(amendment_parts)
        .where(p.story_id == story_id, p.amendment_id.in_(list(amendment_ids)), or_(*clauses))
    )
    return bool(found)


async def policy_set(db: AsyncEngine, story_id: str) -> list[list[Any]]:
    """The story's effective policies that judge an image prompt or output now (the seeded
    minors policy always), as ``[[policy_id, version], …]`` in id order (``StoryNotFound``)."""
    from epistrel.orchestrator.policies.store import effective_policies  # it imports models

    found = {
        (p.id, p.version)
        for p in await effective_policies(db, story_id)
        if p.id == jobs.MINORS_POLICY_ID
        or Target.IMAGE_PROMPT in p.applies_to
        or Target.IMAGE_OUTPUT in p.applies_to
    }
    return [[policy_id, version] for policy_id, version in sorted(found)]


async def _unchecked(db: AsyncEngine, job: ImageJob) -> tuple[str, ...]:
    """The policies in force now that the job's last check did not run under."""
    checked = {(str(p[0]), p[1]) for p in job.checked_policies or ()}
    try:
        now = await policy_set(db, str(job.origin["story_id"]))
    except StoryNotFound:
        return ()
    return tuple(sorted({str(p[0]) for p in now if (str(p[0]), p[1]) not in checked}))


async def _rating_version(conn: AsyncConnection, rating_id: Any) -> str:
    """The current version of the image's rating (a retired rating still has one), else
    Everyone's."""
    found = None
    if isinstance(rating_id, str):
        found = await conn.scalar(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == rating_id)
        )
    if found is None:
        found = await conn.scalar(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == EVERYONE_ID)
        )
    return str(found)


def _placement(job: ImageJob, owner: str) -> story_images.Placement:
    origin, target = job.origin, job.target
    position = request_position(origin)
    slot = isinstance(position, story_images.SlotPosition)
    return story_images.Placement(
        story_id=str(origin["story_id"]),
        turn=int(origin["turn"]),
        variant=int(origin.get("variant") or 0),
        beat=0 if slot else int(origin["position"]["beat"]),
        slot=slot,
        audience_viewpoint=str(target["audience_viewpoint"]),
        camera_pov=dict(target.get("camera_pov") or story_images.DEFAULT_CAMERA),
        content_rating_id=str(target.get("content_rating_id") or EVERYONE_ID),
        owner=owner,
    )


class StoryTarget:
    """The story target (module docstring)."""

    success_status = ImageJobStatus.PROMOTED

    async def validate_enqueue(
        self, conn: AsyncConnection, target: Mapping[str, Any], origin: Mapping[str, Any]
    ) -> None:
        await story_images.place_at(
            conn,
            str(origin["story_id"]),
            request_position(origin),
            str(target["audience_viewpoint"]),
            target.get("camera_pov"),
            at=FIELD,
            content_rating_id=target.get("content_rating_id"),
        )

    async def revalidate(self, conn: AsyncConnection, job: ImageJob) -> jobs.Revalidation:
        """The story revalidation (module docstring), in ``conn``'s transaction."""
        story_id = str(job.origin["story_id"])
        story = (
            await conn.execute(
                select(stories.c.status).where(stories.c.id == story_id).with_for_update()
            )
        ).one_or_none()
        if story is None:
            return jobs.Gone()
        current = await read_job(conn, job.id) or job
        if await _erased(conn, current):
            return jobs.Stale("erased")
        if str(story.status) in WAITING_STATUSES:
            return await _wait(conn, current)
        cause = await _inactive(conn, current)
        if cause is not None:
            return jobs.Stale("target_inactive", {"cause": cause})
        amendment_id = await _amendment(conn, current)
        if amendment_id is not None:
            return jobs.Stale("amended", {"amendment_id": amendment_id})
        missing = await _unchecked(conn.engine, current)
        if missing:
            return jobs.NeedsEvaluation(missing)
        return jobs.OK

    async def refresh(self, runtime: ImageRuntime, job: ImageJob) -> jobs.Stale | None:
        """The prompt check again with a fresh safety context, outside any lock (module
        docstring); ``Stale(policy_changed)`` when it refuses the prompt now."""
        story_id = str(job.origin["story_id"])
        try:
            checked = await policy_set(runtime.db, story_id)
        except StoryNotFound:
            return None  # the promotion's revalidation ends the job ``target_gone``
        request = jobs.ImageJobRequest(
            prompt=job.prompt,
            negative_prompt=job.negative_prompt,
            caption=job.params.get("caption"),
            alt_text=job.params.get("alt_text"),
            depicted_subjects=tuple(
                jobs.DepictedSubject.model_validate(s)
                for s in job.params.get("prompt_subjects") or ()
            ),
        )
        check = jobs.PromptCheck(
            ImageJobKind.STORY, job.target, job.origin, request, job.principal, job.viewpoint
        )
        try:
            context = await jobs.image_checks().check_prompt(runtime, check)
        except GuardrailViolation as exc:
            violations = exc.details.get("violations") or ()
            ids = [str(v["policy_id"]) for v in violations if v.get("policy_id")]
            return jobs.Stale("policy_changed", {"policy_ids": ids})
        except StoryNotFound:
            return None
        values: dict[str, Any] = {"checked_policies": checked}
        if context is not None:
            values["safety_context"] = context.as_json()
        async with runtime.db.begin() as conn:
            await update_job(conn, job.id, **values)
        return None

    async def promote(
        self, conn: AsyncConnection, job: ImageJob, outputs: Sequence[jobs.PromotableOutput]
    ) -> list[str]:
        story_id = str(job.origin["story_id"])
        owner = await _owner(conn, story_id)
        if owner is None:  # ``revalidate`` holds the story row: never, short of a bug
            raise jobs.TargetGone(f"story {story_id}")
        placement = _placement(job, owner)
        source = MediaSource(str(job.params.get("source") or MediaSource.GENERATED.value))
        basis = ViewpointBasis(
            str(job.params.get("viewpoint_basis") or ViewpointBasis.FILTERED_INPUTS.value)
        )
        version = await _rating_version(conn, placement.content_rating_id)
        output = outputs[0]  # a story job generates one image (``n = 1``)
        asset_id = await jobs.insert_output_asset(
            conn,
            job,
            output,
            state=MediaState.LIVE,
            owner=owner,
            content_rating_version_id=version,
            depicted_subjects=list(job.params.get("depicted_subjects") or ()),
            source=source,
            viewpoint_basis=basis.value,
        )
        try:
            image_id = await story_images.place_generated(
                conn,
                placement,
                asset_id,
                operation_id=str(job.origin["operation_id"]),
                principal=job.principal,
                prompt=job.prompt,
                job_id=job.id,
                source=source,
                viewpoint_basis=basis,
            )
        except PositionInvalid:
            # ``revalidate`` checked the position under the story's lock, so the timeline
            # cannot have moved: fail safe all the same, nothing placed.
            cause = "advanced" if placement.slot else "truncated"
            raise jobs.TargetStale("target_inactive", {"cause": cause}) from None
        await set_result(conn, job.id, {"asset_ids": [asset_id], "image_ids": [image_id]})
        return [asset_id]

    async def on_terminal(self, conn: AsyncConnection, job: ImageJob) -> None:
        return None

    async def result_ids(self, conn: AsyncConnection, job: ImageJob) -> tuple[str, list[str]]:
        return "asset_ids", [str(a) for a in job.asset_ids]


TARGET = StoryTarget()
jobs.register_target(ImageJobKind.STORY, TARGET)
"""Registered on import (the Engine imports this module): tests swap it with
``testing.images.registered_target``, which restores it."""


__all__ = [
    "AGE_PREDICATE_ID",
    "FIELD",
    "JOB_LOCATION",
    "TARGET",
    "StoryImageQueued",
    "StoryTarget",
    "origin_of",
    "policy_set",
    "request_position",
    "target_of",
]
