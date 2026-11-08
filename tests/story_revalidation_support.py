"""Helpers for the story image revalidation tests (#264): a Lite story on
``tests.story_image_generation_support``'s app (stub model adapter, stub image adapter, scripted
checks), a consumer-prompt generation request, and a job run in the background with its provider
call stalled until the test releases it, so a race can happen while the image is being made.

Every stalled output gets a size of its own (``sg.next_size()``), so no two tests share a
checksum on the shared database."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock
from epistrel.core.amendments.store import PropositionPartDraft, RecordPartDraft
from epistrel.core.context import RequestContext
from epistrel.core.ids import new_ulid
from epistrel.core.images.store import new_token
from epistrel.core.policies.model import Policy
from epistrel.core.propositions import registry
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.jobs.handlers.image_job import (
    IMAGE_JOB,
    IMAGE_JOB_HANDLER,
    ImageJobPayload,
)
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext, JobOutput
from epistrel.orchestrator.policies.schemas import PolicyFields
from tests import story_image_generation_support as sg
from tests import story_image_support as si
from tests.amendment_support import amend
from tests.image_job_support import staged
from tests.image_support import Outputs, Stall
from tests.policy_support import unique
from tests.respond_support import until

PROMPT = "A quiet parlor at dusk, two figures by the window."
OPERATOR_PRINCIPAL = "p-operator"
OPERATOR = RequestContext(principal=OPERATOR_PRINCIPAL)


async def lite_scene(app: sg.GenerationApp, turns: int = 2) -> tuple[str, dict[str, str]]:
    """The Lite whisper scene (Bobby the player, Jenni, Marcus) with ``turns`` turns played and
    the image adapter and checker configured."""
    story, ids = await si.scene(app.client, turns)
    await app.configure()
    return story, ids


def consumer(
    turn: int, subjects: Sequence[str] = (), *, beat: int = 2, slot: bool = False, **fields: Any
) -> dict[str, Any]:
    """A consumer-prompt generation request for ``omniscient`` at turn ``turn``'s ``beat`` (or
    the slot after it), depicting ``subjects``."""
    position = {"slot_after_turn": turn} if slot else {"turn": turn, "beat": beat}
    return {
        "position": position,
        "audience_viewpoint": "omniscient",
        "prompt": PROMPT,
        "depicted_subjects": [{"kind": "character", "id": s} for s in subjects],
        **fields,
    }


@dataclass
class Running:
    """A job run in the background, its provider call stalled until ``release``."""

    job_id: str
    release: asyncio.Event
    task: asyncio.Task[dict[str, Any]]

    async def finished(self) -> dict[str, Any]:
        """Release the provider call; the run's result."""
        self.release.set()
        return await asyncio.wait_for(self.task, 60)


def run_in_background(app: sg.GenerationApp, job_id: str) -> asyncio.Task[dict[str, Any]]:
    return asyncio.create_task(image_jobs.run_image_job(app.lib.image_runtime(), job_id, 0))


async def started(app: sg.GenerationApp, story_id: str, **fields: Any) -> Running:
    """Request a job and run it until its provider call, which stalls."""
    job_id = await sg.queued(app, story_id, **fields)
    release = asyncio.Event()
    app.generator.script.append(Stall(release, Outputs(size=sg.next_size())))
    before = len(app.generator.requests)
    task = run_in_background(app, job_id)
    await until(lambda: len(app.generator.requests) > before, "the provider call")
    return Running(job_id, release, task)


async def rerun(app: sg.GenerationApp, job_id: str) -> dict[str, Any]:
    """Run the job's handler again (as #70 would once its ``requeue_in`` came due)."""
    return await image_jobs.run_image_job(app.lib.image_runtime(), job_id, 0)


async def handled(app: sg.GenerationApp, job_id: str) -> JobOutput:
    """Run the job through the ``image_job`` handler, the app's runtime bound for the call."""
    claimed = ClaimedJob(
        id=new_ulid(),
        kind=IMAGE_JOB,
        story_id=None,
        payload={"image_job_id": job_id},
        attempt=1,
        failures=0,
        due_at=clock.now(),
        lease_until=None,
        worker_id=None,
    )
    image_jobs.bind(app.lib.image_runtime())
    try:
        return await IMAGE_JOB_HANDLER.run(
            JobContext(app.lib.db, claimed, ImageJobPayload(image_job_id=job_id))
        )
    finally:
        image_jobs.bind(None)


async def job(engine: AsyncEngine, job_id: str) -> dict[str, Any]:
    return await sg.job_row(engine, job_id)


def staging(app: sg.GenerationApp, job_id: str) -> list[Any]:
    """The files in the job's private staging."""
    return staged(app.lib.image_runtime().media, job_id)


async def reservations(engine: AsyncEngine, job_id: str) -> list[str]:
    """The states of the job's reservation records."""
    async with engine.connect() as conn:
        found = await conn.scalars(
            text("SELECT state FROM image_reservations WHERE job_id = :j ORDER BY created_at"),
            {"j": job_id},
        )
        return [str(s) for s in found]


async def audit_reason(engine: AsyncEngine, job_id: str) -> dict[str, Any] | None:
    """The ``image_job_ended`` audit row's details for the job."""
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT details FROM audit_log WHERE kind = 'image_job_ended' "
                "AND details->>'job_id' = :j"
            ),
            {"j": job_id},
        )
    return None if found is None else dict(found)


async def story_predicate(engine: AsyncEngine, story_id: str, name: str) -> str:
    """A story predicate ``name`` with a string object (an auto predicate)."""
    async with engine.begin() as conn:
        return (await registry.auto_register(conn, story_id, name, object_type="str")).id


async def amend_fact(
    engine: AsyncEngine, story_id: str, subject_id: str, predicate_id: str, value: str | int
) -> str:
    """An author amendment (#221) saying ``subject_id``'s ``predicate_id`` was ``value`` all
    along; its id."""
    kind = LiteralKind.INT if isinstance(value, int) else LiteralKind.STR
    proposition = Proposition(
        subject_id=subject_id,
        predicate_id=predicate_id,
        literal=TypedLiteral(kind=kind, value=value),
        valid_time=Interval.of(None, None),
    )
    _, made, _ = await amend(engine, story_id, [PropositionPartDraft(proposition)])
    return made.amendment_id


async def rated(engine: AsyncEngine, story_id: str, rating_id: str) -> None:
    """The story moved to ``rating_id`` (its policies in force from now on)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE stories SET content_rating_id = :r WHERE id = :s"),
            {"r": rating_id, "s": story_id},
        )


async def reply_event(engine: AsyncEngine, story_id: str, turn: int) -> str:
    """The id of an event with a ``text`` payload in turn ``turn``'s active variant."""
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT e.id FROM events e JOIN operations o ON o.operation_id = e.operation_id "
                "WHERE o.story_id = :s AND o.narrative_turn = :t AND o.kind = 'turn' "
                "AND o.membership = 'active' AND e.payload ? 'text' ORDER BY e.seq DESC LIMIT 1"
            ),
            {"s": story_id, "t": turn},
        )
    assert found is not None, (story_id, turn)
    return str(found)


async def amend_record(engine: AsyncEngine, story_id: str, table: str, record_id: str) -> str:
    """An author amendment (#221) replacing one record's text; its id."""
    part = RecordPartDraft(table, record_id, "replace", {"text": "It was otherwise."})
    _, made, _ = await amend(engine, story_id, [part])
    return made.amendment_id


async def ended_leftover(app: sg.GenerationApp, story_id: str, ids: dict[str, str]) -> str:
    """A job that promoted, with a staging prefix left behind as a crash would leave it; its
    id."""
    job_id = await sg.queued(app, story_id, **consumer(2, [ids["Jenni"]]))
    done = await sg.run(app, job_id)
    assert done["image_job"] == "promoted", done
    directory = app.lib.image_runtime().media.image_staging_dir(job_id, new_token())
    directory.mkdir(parents=True)
    (directory / "0").write_bytes(b"left behind")
    return job_id


async def point_at_missing_story(engine: AsyncEngine, job_id: str) -> None:
    """Point the job's origin at a story that does not exist (a story gone under its job)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE image_jobs SET origin = jsonb_set(origin, '{story_id}', "
                "to_jsonb(CAST(:s AS text))) WHERE id = :j"
            ),
            {"s": new_ulid(), "j": job_id},
        )


async def set_status(engine: AsyncEngine, story_id: str, status: str) -> None:
    """The story's ``status`` as a rating, reactivation or backfill check leaves it."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE stories SET status = :t WHERE id = :s"), {"t": status, "s": story_id}
        )


async def image_policy_rating(lib: Engine, engine: AsyncEngine) -> tuple[str, Policy]:
    """A new rating (inheriting from nothing) listing one new image-output policy (a word
    list): the stricter rating a tightening moves a story to."""
    rating = await create_rating(
        engine, RatingFields(name=unique("rating")), principal=OPERATOR_PRINCIPAL
    )
    body = PolicyFields.model_validate(
        {
            "scope": "rating",
            "scope_id": rating.rating.id,
            "name": unique("image-policy"),
            "rule": {"type": "wordlist", "words": [unique("never")]},
            "message": "Not at this rating.",
            "applies_to": ["image_output"],
        }
    )
    made = (await lib.policies_create(OPERATOR, body)).policy
    current = rating.rating
    await update_rating(
        engine,
        current.id,
        RatingReplacement(
            name=current.name,
            description=current.description,
            display_order=current.display_order,
            prompt_guidance=current.prompt_guidance,
            image_guidance=current.image_guidance,
            policy_ids=[made.id],
            inherits_from=[],
        ),
        principal=OPERATOR_PRINCIPAL,
    )
    return current.id, made


async def tightened(engine: AsyncEngine, story_id: str, rating_id: str) -> None:
    """What a tightening leaves while its history check runs: the stricter rating, the story
    paused."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE stories SET content_rating_id = :r, status = 'paused' WHERE id = :s"),
            {"r": rating_id, "s": story_id},
        )


__all__ = [
    "PROMPT",
    "Running",
    "amend_fact",
    "amend_record",
    "audit_reason",
    "consumer",
    "ended_leftover",
    "handled",
    "image_policy_rating",
    "job",
    "lite_scene",
    "point_at_missing_story",
    "rated",
    "reply_event",
    "rerun",
    "reservations",
    "run_in_background",
    "set_status",
    "staging",
    "started",
    "story_predicate",
    "tightened",
]
