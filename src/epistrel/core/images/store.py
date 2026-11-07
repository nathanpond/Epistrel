"""The image jobs' persistence: ``image_jobs`` and ``image_reservations`` rows, the attempt-token
compare and the status notifications (FR-IMG-3,4; docs/02 §4.13; #256).

Every status change goes through ``set_status`` in the caller's transaction. It sends
``NOTIFY image_job_<lowercased id>, '<new status>'``, which is delivered when that transaction
commits. A long-polling read (``GET /image-jobs/{id}?wait=``) wakes on it, and polls once a
second in case a notification is missed. With ``token``, the change is made only while the job
still carries that attempt token. That is the fence: a cancelled job, or one whose attempt a
newer claim replaced, refuses every write of the old attempt. Nothing here holds image bytes
or prompt text in a log line: ids, codes and counts only.

``insert_generated_asset`` writes a promoted output's ``media_assets`` row (``source =
generated``). A job target's ``promote`` calls it inside the promote transaction, so the row and
the attachment commit together.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import cast, func, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.ids import new_ulid
from epistrel.core.tables.images import (
    TERMINAL_STATUSES,
    AttemptOutcome,
    ImageJobKind,
    ImageJobStatus,
    SendState,
    image_jobs,
    image_reservations,
)
from epistrel.core.tables.media import CheckStatus, MediaKind, MediaSource, MediaState, media_assets

CHANNEL_PREFIX = "image_job_"


def channel(job_id: str) -> str:
    """The ``NOTIFY`` channel of job ``job_id``: ``image_job_<lowercased id>``."""
    return f"{CHANNEL_PREFIX}{job_id.lower()}"


def new_token() -> str:
    """A fresh attempt token (a canonical lowercase uuid)."""
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class ImageJob:
    """An ``image_jobs`` row as the pipeline reads it."""

    id: str
    kind: ImageJobKind
    story_id: str | None
    target: dict[str, Any]
    origin: dict[str, Any]
    viewpoint: str | None
    prompt: str
    negative_prompt: str | None
    params: dict[str, Any]
    status: ImageJobStatus
    status_reason: str | None
    attempt_token: str
    attempts: int
    policy_results: list[dict[str, Any]]
    asset_ids: list[str]
    idempotency_key: str | None
    principal: str
    created_at: datetime
    updated_at: datetime
    safety_context: dict[str, Any] | None = None
    """#257: the prompt check's safety context (``core.images.safety.SafetyContext.as_json``),
    which the output check reads (#258). Never returned by a read."""
    result: dict[str, Any] | None = None
    """#260: what a successful job made beyond ``asset_ids`` (a catalog job's
    ``{candidate_ids}``)."""
    status_detail: dict[str, Any] | None = None
    """#262: a ``cap_reached`` job's ``{scope, period}``."""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def policy_ids(self) -> list[str]:
        """The policies its outputs violated or that could not be evaluated, in first-seen
        order."""
        seen: list[str] = []
        for result in self.policy_results:
            policy_id = result.get("policy_id")
            if result.get("result") != "pass" and policy_id and policy_id not in seen:
                seen.append(str(policy_id))
        return seen


@dataclass(frozen=True, slots=True)
class Reservation:
    """One ``image_reservations`` row (one provider attempt)."""

    job_id: str
    attempt_token: str
    send_state: SendState
    attempt_ref: str | None
    applied_parameters: dict[str, Any] | None
    outputs_returned: int | None
    outcome: AttemptOutcome | None
    started_at: datetime | None
    created_at: datetime


def _job(row: Any) -> ImageJob:
    m = row._mapping
    return ImageJob(
        id=m["id"],
        kind=ImageJobKind(m["kind"]),
        story_id=m["story_id"],
        target=dict(m["target"] or {}),
        origin=dict(m["origin"] or {}),
        viewpoint=m["viewpoint"],
        prompt=m["prompt"],
        negative_prompt=m["negative_prompt"],
        params=dict(m["params"] or {}),
        status=ImageJobStatus(m["status"]),
        status_reason=m["status_reason"],
        attempt_token=str(m["attempt_token"]),
        attempts=int(m["attempts"]),
        policy_results=list(m["policy_results"] or []),
        asset_ids=[str(a) for a in m["asset_ids"] or []],
        idempotency_key=m["idempotency_key"],
        principal=m["principal"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        safety_context=None if m["safety_context"] is None else dict(m["safety_context"]),
        result=None if m["result"] is None else dict(m["result"]),
        status_detail=None if m["status_detail"] is None else dict(m["status_detail"]),
    )


def _reservation(row: Any) -> Reservation:
    m = row._mapping
    return Reservation(
        job_id=m["job_id"],
        attempt_token=str(m["attempt_token"]),
        send_state=SendState(m["send_state"]),
        attempt_ref=m["attempt_ref"],
        applied_parameters=m["applied_parameters"],
        outputs_returned=m["outputs_returned"],
        outcome=None if m["outcome"] is None else AttemptOutcome(m["outcome"]),
        started_at=m["started_at"],
        created_at=m["created_at"],
    )


@dataclass(frozen=True, slots=True)
class NewJob:
    """What ``insert_job`` writes (the pipeline's ``PreparedImageJob``, flattened)."""

    kind: ImageJobKind
    story_id: str | None
    target: Mapping[str, Any]
    origin: Mapping[str, Any]
    viewpoint: str | None
    prompt: str
    negative_prompt: str | None
    params: Mapping[str, Any]
    principal: str
    idempotency_key: str | None = None
    job_id: str = field(default_factory=new_ulid)
    safety_context: Mapping[str, Any] | None = None


async def notify(conn: AsyncConnection, job_id: str, status: ImageJobStatus) -> None:
    """``NOTIFY image_job_<id>, '<status>'`` (delivered when ``conn``'s transaction commits)."""
    await conn.execute(select(func.pg_notify(channel(job_id), status.value)))


async def insert_job(conn: AsyncConnection, job: NewJob) -> tuple[str, str]:
    """Insert the job (``queued``) in ``conn``'s transaction; ``(job id, attempt token)``. Its
    first attempt's reservation record is ``core.images.limits.reserve``'s (#262), in the same
    transaction."""
    now = clock.now()
    token = new_token()
    await conn.execute(
        insert(image_jobs).values(
            id=job.job_id,
            kind=job.kind.value,
            story_id=job.story_id,
            target=dict(job.target),
            origin=dict(job.origin),
            viewpoint=job.viewpoint,
            prompt=job.prompt,
            negative_prompt=job.negative_prompt,
            params=dict(job.params),
            status=ImageJobStatus.QUEUED.value,
            attempt_token=token,
            attempts=0,
            policy_results=[],
            asset_ids=[],
            idempotency_key=job.idempotency_key,
            principal=job.principal,
            created_at=now,
            updated_at=now,
            safety_context=None if job.safety_context is None else dict(job.safety_context),
        )
    )
    await notify(conn, job.job_id, ImageJobStatus.QUEUED)
    return job.job_id, token


async def read_job(conn: AsyncConnection, job_id: str, *, lock: bool = False) -> ImageJob | None:
    """The job, or ``None``; ``lock`` takes its row ``FOR UPDATE``."""
    query = select(image_jobs).where(image_jobs.c.id == job_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _job(row)


async def read_statuses(conn: AsyncConnection, job_ids: Sequence[str]) -> dict[str, str]:
    """``{job id: status}`` of the jobs that exist among ``job_ids`` (one statement)."""
    if not job_ids:
        return {}
    rows = await conn.execute(
        select(image_jobs.c.id, image_jobs.c.status).where(image_jobs.c.id.in_(list(job_ids)))
    )
    return {str(r.id): str(r.status) for r in rows}


async def read_reservation(conn: AsyncConnection, job_id: str, token: str) -> Reservation | None:
    row = (
        await conn.execute(
            select(image_reservations).where(
                image_reservations.c.job_id == job_id,
                image_reservations.c.attempt_token == token,
            )
        )
    ).one_or_none()
    return None if row is None else _reservation(row)


async def reservations(conn: AsyncConnection, job_id: str) -> list[Reservation]:
    """Every attempt of the job, oldest first."""
    rows = await conn.execute(
        select(image_reservations)
        .where(image_reservations.c.job_id == job_id)
        .order_by(image_reservations.c.created_at, image_reservations.c.attempt_token)
    )
    return [_reservation(r) for r in rows]


async def set_status(
    conn: AsyncConnection,
    job_id: str,
    status: ImageJobStatus,
    *,
    token: str | None = None,
    allowed_from: Sequence[ImageJobStatus] | None = None,
    **values: Any,
) -> bool:
    """Move the job to ``status`` (with ``values``) and notify, in ``conn``'s transaction.
    With ``token``, only while the job still carries it; with ``allowed_from``, only from those
    statuses. A terminal job never moves. ``False`` when nothing was changed."""
    query = update(image_jobs).where(
        image_jobs.c.id == job_id,
        image_jobs.c.status.not_in([s.value for s in TERMINAL_STATUSES]),
    )
    if token is not None:
        query = query.where(image_jobs.c.attempt_token == token)
    if allowed_from is not None:
        query = query.where(image_jobs.c.status.in_([s.value for s in allowed_from]))
    changed = await conn.scalar(
        query.values(status=status.value, updated_at=clock.now(), **values).returning(
            image_jobs.c.id
        )
    )
    if changed is None:
        return False
    await notify(conn, job_id, status)
    return True


async def update_reservation(conn: AsyncConnection, job_id: str, token: str, **values: Any) -> None:
    """Write ``values`` on one attempt's row."""
    await conn.execute(
        update(image_reservations)
        .where(
            image_reservations.c.job_id == job_id,
            image_reservations.c.attempt_token == token,
        )
        .values(**values)
    )


async def next_attempt(conn: AsyncConnection, job_id: str, old_token: str) -> str | None:
    """Give the job a new attempt token, fencing ``old_token``; ``None`` when the job no longer
    carries ``old_token``. The new attempt's reservation record is the caller's
    (``core.images.limits.reserve``, #262), in the same transaction."""
    token = new_token()
    moved = await conn.scalar(
        update(image_jobs)
        .where(image_jobs.c.id == job_id, image_jobs.c.attempt_token == old_token)
        .values(attempt_token=token, updated_at=clock.now())
        .returning(image_jobs.c.id)
    )
    if moved is None:
        return None
    return token


async def set_result(conn: AsyncConnection, job_id: str, result: Mapping[str, Any]) -> None:
    """Record what the job made (``image_jobs.result``; #260), in ``conn``'s transaction."""
    await conn.execute(
        update(image_jobs).where(image_jobs.c.id == job_id).values(result=dict(result))
    )


async def append_policy_results(
    conn: AsyncConnection, job_id: str, results: Sequence[Mapping[str, Any]]
) -> None:
    """Add ``results`` (``{attempt_token, output, policy_id, result}``) to the job's list."""
    if not results:
        return
    await conn.execute(
        update(image_jobs)
        .where(image_jobs.c.id == job_id)
        .values(
            policy_results=image_jobs.c.policy_results.op("||")(
                cast([dict(r) for r in results], JSONB)
            )
        )
    )


async def insert_generated_asset(
    conn: AsyncConnection,
    *,
    checksum: str,
    mime_type: str,
    size: int,
    width: int | None,
    height: int | None,
    owner: str,
    content_rating_version_id: str,
    generation_metadata: Mapping[str, Any],
    state: MediaState,
    created_by: str,
    alt_text: str | None = None,
    caption: str | None = None,
    depicted_subjects: Sequence[Mapping[str, Any]] = (),
    source: MediaSource = MediaSource.GENERATED,
    check_status: CheckStatus = CheckStatus.UNCHECKED,
    check_model: str | None = None,
    check_endpoint: str | None = None,
    policy_results: Sequence[Mapping[str, Any]] = (),
    viewpoint_basis: str | None = None,
) -> str:
    """A promoted output's ``media_assets`` row (``kind = image``, ``source`` ``generated`` or
    ``generated_from_consumer_prompt``, with its output check's fields, #258) in ``conn``'s
    transaction (a target's promote); its id. The blob ``checksum`` is already in the
    store: until the transaction commits, nothing names it, and the media sweep's orphan step
    only removes a blob nothing names after an hour."""
    asset_id = new_ulid()
    await conn.execute(
        insert(media_assets).values(
            id=asset_id,
            owner_principal=owner,
            kind=MediaKind.IMAGE.value,
            mime_type=mime_type,
            size=size,
            width=width,
            height=height,
            duration_ms=None,
            checksum=checksum,
            content_rating_version_id=content_rating_version_id,
            alt_text=alt_text,
            caption=caption,
            generation_metadata=dict(generation_metadata),
            source=source.value,
            state=state.value,
            depicted_subjects=[dict(s) for s in depicted_subjects],
            created_at=clock.now(),
            created_by=created_by,
            poster_media_id=None,
            check_status=check_status.value,
            check_model=check_model,
            check_endpoint=check_endpoint,
            policy_results=[dict(r) for r in policy_results],
            viewpoint_basis=viewpoint_basis,
        )
    )
    return asset_id


__all__ = [
    "CHANNEL_PREFIX",
    "ImageJob",
    "NewJob",
    "Reservation",
    "append_policy_results",
    "channel",
    "insert_generated_asset",
    "insert_job",
    "new_token",
    "next_attempt",
    "notify",
    "read_job",
    "read_reservation",
    "read_statuses",
    "reservations",
    "set_result",
    "set_status",
    "update_reservation",
]
