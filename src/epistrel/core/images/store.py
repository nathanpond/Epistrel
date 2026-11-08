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

**The subject index** (FR-ADMIN-6, FR-IMG-9; #266). A story job is indexed in its story
(``records.image_job_record``: its prompts, caption and alt text, and its depicted subjects) when
it is inserted and whenever ``update_job`` rewrites its prompt or params (an automatic job's
preparation, #265); a catalog job is not indexed. A generated catalog asset (a candidate's) is
indexed as a medium (``records.media_record``) and its declared subjects pass the erasure guard
(``assert_not_erased``); a story image's asset is indexed with its ``story_images`` row instead.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import cast, func, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.ids import new_ulid
from epistrel.core.images.errors import FencedToken
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
from epistrel.observability.metrics import count_attempt_outcome

logger = logging.getLogger(__name__)

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
    """#262: a ``cap_reached`` job's ``{scope, period}``; #264: a ``stale`` job's ``{cause,
    policy_ids, amendment_id}``, a waiting one's ``{waiting_on, next_check_at}``."""
    wait_started_at: datetime | None = None
    """#264: when the job first waited on a paused story (set once)."""
    checked_policies: list[list[Any]] | None = None
    """#264: the story's image policy set (``[[policy_id, version], …]``) at the job's last
    prompt and output check; ``None`` before the first."""
    waiting_for_horizon: bool = False
    """#267: a lease expiry fenced an attempt in flight; no attempt starts before its horizon."""
    confirm_deadline_at: datetime | None = None
    """#267: when a ``confirms_completion`` adapter's status polling gives up."""
    confirm_fallback: bool = False
    """#267 (sticky): the job fell back to the non-confirming rule after
    ``image_confirm_wait_max_s``."""

    @property
    def waiting(self) -> bool:
        """Whether the job is waiting on a paused story (#264)."""
        return (
            self.status is ImageJobStatus.CHECKING
            and self.status_detail is not None
            and self.status_detail.get("waiting_on") is not None
        )

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
    horizon_at: datetime | None = None
    """#267: ``started_at + request_timeout_s + image_attempt_grace_s``."""
    cancel_result: str | None = None
    last_status: str | None = None
    idempotency_key: str | None = None
    """#268: ``sha256("<job_id>:<attempt_token>")`` (lowercase hex), written at the ``sending``
    commit; a re-send's is its stalled attempt's."""
    resend_of: str | None = None
    """#268: the stalled attempt an idempotent re-send re-sent (its record takes the
    settlement)."""
    request_snapshot: dict[str, Any] | None = None
    """#268: the request sent, as JSON (``ImageRequest.model_dump``), without its prompt text."""
    capabilities_snapshot: dict[str, Any] | None = None
    """#268: the adapter's ``{submission, idempotency_keys}`` at the ``sending`` commit."""

    @property
    def resendable(self) -> bool:
        """Whether its adapter declared ``idempotency_keys`` when it was sent (#268)."""
        return bool((self.capabilities_snapshot or {}).get("idempotency_keys"))


def idempotency_key_of(job_id: str, attempt_token: str) -> str:
    """An attempt's provider idempotency key (#268; FR-IMG-11): the lowercase hex SHA-256 of
    ``"<job_id>:<attempt_token>"`` (the token as canonical uuid text)."""
    return hashlib.sha256(f"{job_id}:{uuid.UUID(attempt_token)!s}".encode()).hexdigest()


def _job(row: Any) -> ImageJob:
    m = row._mapping
    return ImageJob(
        id=m["id"],
        kind=ImageJobKind(m["kind"]),
        story_id=m["story_id"],
        target=dict(m["target"] or {}),
        origin=dict(m["origin"] or {}),
        viewpoint=m["viewpoint"],
        prompt=m["prompt"] or "",  # null: erased (#266); such a job has ended
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
        wait_started_at=m["wait_started_at"],
        checked_policies=None
        if m["checked_policies"] is None
        else [list(p) for p in m["checked_policies"]],
        waiting_for_horizon=bool(m["waiting_for_horizon"]),
        confirm_deadline_at=m["confirm_deadline_at"],
        confirm_fallback=bool(m["confirm_fallback"]),
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
        horizon_at=m["horizon_at"],
        cancel_result=m["cancel_result"],
        last_status=m["last_status"],
        idempotency_key=m["idempotency_key"],
        resend_of=None if m["resend_of"] is None else str(m["resend_of"]),
        request_snapshot=None if m["request_snapshot"] is None else dict(m["request_snapshot"]),
        capabilities_snapshot=None
        if m["capabilities_snapshot"] is None
        else dict(m["capabilities_snapshot"]),
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
    if job.story_id is not None:
        from epistrel.core.erasure.index import index_records
        from epistrel.core.erasure.records import image_job_record

        row = SimpleNamespace(
            id=job.job_id,
            story_id=job.story_id,
            target=dict(job.target),
            prompt=job.prompt,
            negative_prompt=job.negative_prompt,
            params=dict(job.params),
        )
        await index_records(conn, [image_job_record(row)], fresh=True)
    await notify(conn, job.job_id, ImageJobStatus.QUEUED)
    return job.job_id, token


INDEXED_JOB_COLUMNS = frozenset({"prompt", "negative_prompt", "params", "target"})
"""The columns whose change re-indexes a story job (``records.image_job_record`` reads them)."""


async def index_job(conn: AsyncConnection, job_id: str) -> None:
    """(Re)write a story job's ``subject_references`` rows from its row (a catalog job has
    none), in ``conn``'s transaction."""
    from epistrel.core.erasure.index import index_records
    from epistrel.core.erasure.records import image_job_record

    j = image_jobs.c
    row = (
        await conn.execute(
            select(j.id, j.story_id, j.target, j.prompt, j.negative_prompt, j.params).where(
                j.id == job_id, j.story_id.is_not(None)
            )
        )
    ).one_or_none()
    if row is not None:
        await index_records(conn, [image_job_record(row)])


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
    """Write ``values`` on one attempt's row. A written ``outcome`` is counted
    (``epistrel_image_attempt_outcomes_total{outcome}``, #267) and logged with ids only."""
    written = await conn.scalar(
        update(image_reservations)
        .where(
            image_reservations.c.job_id == job_id,
            image_reservations.c.attempt_token == token,
        )
        .values(**values)
        .returning(image_reservations.c.job_id)
    )
    outcome = values.get("outcome")
    if written is not None and isinstance(outcome, str):
        count_attempt_outcome(outcome)
        logger.info("image job %s attempt %s: %s", job_id, token, outcome)


async def require_token(conn: AsyncConnection, job_id: str, token: str) -> None:
    """Refuse a fenced caller (#267): the job's row, locked, must still carry ``token``
    (``UPDATE … WHERE id = :id AND attempt_token = :t`` with a rowcount check); otherwise
    ``FencedToken``. Promotion, attachment and every counter change except the settlement path
    call it first, in their own transaction."""
    found = await conn.execute(
        update(image_jobs)
        .where(image_jobs.c.id == job_id, image_jobs.c.attempt_token == token)
        .values(attempt_token=image_jobs.c.attempt_token)
    )
    if found.rowcount != 1:
        raise FencedToken(job_id, token)


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


async def update_job(conn: AsyncConnection, job_id: str, **values: Any) -> None:
    """Write ``values`` on the job's row without moving its status (#264's safety context
    refresh, checked policy set and wait), in ``conn``'s transaction."""
    await conn.execute(
        update(image_jobs).where(image_jobs.c.id == job_id).values(updated_at=clock.now(), **values)
    )
    if INDEXED_JOB_COLUMNS & values.keys():
        await index_job(conn, job_id)


QUEUE_KIND = "image_job"
"""The ``jobs`` row kind that runs an image job (``orchestrator.jobs.worker.IMAGE_JOB_KIND``)."""
ERASED_REASON = "erased"
"""``status_reason`` of a job an erasure cancelled (#266)."""


async def cancel_in(conn: AsyncConnection, job_id: str, *, reason: str = ERASED_REASON) -> bool:
    """Cancel a job that has not ended inside ``conn``'s transaction, as
    ``orchestrator.images.jobs.cancel_job`` does (an erasure's, #266, holding the story lock): a
    new attempt token fences any worker at promote, the current attempt is discarded, every
    reservation it holds is released (#262), a queued ``jobs`` row is finished and an
    ``image_job_ended`` audit row is written. The staged bytes are the staging sweep's. ``False``
    for a job that has already ended."""
    from epistrel.core import audit
    from epistrel.core.context import SYSTEM_PRINCIPAL
    from epistrel.core.images import limits
    from epistrel.core.tables import jobs as jobs_table
    from epistrel.core.tables.audit_log import AuditKind

    job = await read_job(conn, job_id, lock=True)
    if job is None or job.terminal:
        return False
    await set_status(
        conn, job_id, ImageJobStatus.CANCELLED, status_reason=reason, attempt_token=new_token()
    )
    await update_reservation(
        conn, job_id, job.attempt_token, outcome=AttemptOutcome.DISCARDED.value
    )
    # #267: an attempt a lease expiry fenced stays held until its horizon (its timer releases it).
    await limits.release_all(conn, job_id, current_token=job.attempt_token)
    await conn.execute(
        update(jobs_table)
        .where(
            jobs_table.c.kind == QUEUE_KIND,
            jobs_table.c.status == "queued",
            jobs_table.c.payload["image_job_id"].astext == job_id,
        )
        .values(
            status="done",
            result={"image_job": ImageJobStatus.CANCELLED.value},
            updated_at=clock.now(),
        )
    )
    await audit.record(
        conn,
        AuditKind.IMAGE_JOB_ENDED,
        principal=SYSTEM_PRINCIPAL,
        story_id=job.story_id,
        details={
            "job_id": job_id,
            "attempt_token": job.attempt_token,
            "outcome": ImageJobStatus.CANCELLED.value,
            "reason": reason,
        },
    )
    return True


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
    source_job_id: str | None = None,
    source_attempt_token: str | None = None,
    output_index: int | None = None,
) -> str:
    """A promoted output's ``media_assets`` row (``kind = image``, ``source`` ``generated`` or
    ``generated_from_consumer_prompt``, with its output check's fields, #258) in ``conn``'s
    transaction (a target's promote); its id. The blob ``checksum`` is already in the
    store: until the transaction commits, nothing names it, and the media sweep's orphan step
    only removes a blob nothing names after an hour. ``source_job_id``, ``source_attempt_token``
    and ``output_index`` name the output (#268): their unique index refuses a second promotion
    of it."""
    from epistrel.core.erasure.guard import assert_not_erased

    # No new medium depicts a subject a catalog-scope erasure removed (FR-ADMIN-9; #127, #266).
    await assert_not_erased(
        conn,
        [str(s.get("id")) for s in depicted_subjects if s.get("id")],
        field="depicted_subjects",
        path=__name__,
    )
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
            source_job_id=source_job_id,
            source_attempt_token=source_attempt_token,
            output_index=output_index,
        )
    )
    if viewpoint_basis is None:
        # A catalog candidate's asset is a catalog medium (#266); a story image's is indexed
        # with its ``story_images`` row, in its story.
        from epistrel.core.erasure.index import index_records
        from epistrel.core.erasure.records import media_record

        row = (await conn.execute(select(media_assets).where(media_assets.c.id == asset_id))).one()
        await index_records(conn, [media_record(row)], fresh=True)
    return asset_id


__all__ = [
    "CHANNEL_PREFIX",
    "ERASED_REASON",
    "QUEUE_KIND",
    "ImageJob",
    "NewJob",
    "Reservation",
    "append_policy_results",
    "cancel_in",
    "channel",
    "idempotency_key_of",
    "index_job",
    "insert_generated_asset",
    "insert_job",
    "new_token",
    "next_attempt",
    "notify",
    "read_job",
    "read_reservation",
    "read_statuses",
    "require_token",
    "reservations",
    "set_result",
    "set_status",
    "update_job",
    "update_reservation",
]
