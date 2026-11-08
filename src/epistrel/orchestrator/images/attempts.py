"""Fenced image attempts: horizons, the settlement path and completion confirmation (FR-IMG-11,
part B; NFR-REL-5; docs/02 §4.13; #267).

**Tokens and fencing.** Every provider attempt carries the job's ``attempt_token``
(``image_jobs``, a uuid independent of the ``jobs`` row's integer fence). It advances on every
new attempt and on lease expiry. #70's reaper calls ``on_lease_expired`` through the image job's
lease-expiry hook (``JobHandler.on_lease_expired``), in its lease-flip transaction, and the hook
only fences and schedules. It advances the token. With an attempt in flight (``sending`` or
``accepted``), it sets ``waiting_for_horizon``, schedules the ``image_attempt_horizon`` timer
(target ``<job_id>:<old token>``) at the attempt's horizon, and parks the ``jobs`` row there, so
no worker claims the job before then. With no attempt in flight (the lease was lost during the
checks or the promotion), the job is due at once. Promotion and every counter change except
settlement compare the caller's token with the job's (``core.images.store.require_token``,
``FencedToken``).

**Horizon.** An attempt's ``horizon_at`` is fixed when its ``sending`` commits: ``started_at +
request_timeout_s`` (the ``image_generation`` row's override, else the adapter's) ``+
image_attempt_grace_s``. Until then, a run of the job starts no attempt (``waiting_for_horizon``;
it is due again at the horizon). At the horizon a still-``held`` record is released with cost
``unknown`` (``released_at_horizon``), by the timer or by the job's own run, whichever comes
first. Endings keep the record held until then (``limits.release_all(current_token=…)``).

**The settlement path** (``settle_attempt``) is the one thing a fenced worker may do. It runs in
one transaction keyed by ``(job_id, attempt_token)``, locking that record, and never touches
another:

- ``held`` → settled as in #262 (``late_discarded``);
- ``released`` → settled with ``used += m``, ``overage += m`` and ``extra_outputs += max(0, m -
  requested)`` (``late_after_horizon``);
- already ``settled`` → nothing changes (``late_duplicate``, reported, never stored), except that
  a cost still ``unknown`` is filled in once.

The fenced worker's bytes are deleted unpromoted once it commits.

**Confirming adapters.** After the horizon, for an adapter that declares
``confirms_completion``, the job's run (not the reaper) calls ``cancel`` once and then polls
``status`` (``confirm``). Each poll is due ``image_status_poll_s`` later through the
``image_status_poll`` timer (target ``job_id``). Polling stops at ``confirm_deadline_at`` (the
horizon plus ``image_confirm_wait_max_s``, stored when the wait starts). The status decides:

- ``succeeded`` with outputs: ``Adopt``. The old record is settled through the settlement path
  (``adopted``: it was released, so every output counts as used and overage), and the job
  checks, revalidates and promotes the first ``n`` outputs under its current token;
- ``failed``, ``cancelled``, or ``succeeded`` with none: the old record is settled with no
  outputs (cost from the status or ``unknown``), then ``Retry``;
- ``running``, ``unknown`` or a ``status`` that errors: ``Poll``. An ``accepted`` cancel alone
  never ends the wait;
- the deadline passes: ``confirm_fallback`` is set (sticky), the record becomes
  ``abandoned_fallback``, and the job ``Retry``s by the non-confirming rule. A later output from
  the old call is ``late_after_horizon``, never adopted.

Logs and metrics carry ids, outcomes and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.images import limits, store
from epistrel.core.images.store import ImageJob, Reservation
from epistrel.core.settings_schema import ImageJobSettings
from epistrel.core.tables import jobs as jobs_table
from epistrel.core.tables.images import (
    AttemptOutcome,
    ReservationState,
    SendState,
    image_jobs,
    image_reservations,
)
from epistrel.observability.metrics import count_attempt_outcome
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.images.port import (
    AttemptCost,
    Capabilities,
    Cost,
    ImageUsage,
    StatusResult,
    outputs_of,
)
from epistrel.orchestrator.images.registry import ImageGeneratorHandle

logger = logging.getLogger(__name__)

IMAGE_JOB_KIND = "image_job"
HORIZON_TIMER = "image_attempt_horizon"
"""The timer (and job) kind that releases a fenced attempt at its horizon: target
``<job_id>:<attempt_token>``."""
STATUS_POLL = "image_status_poll"
"""The timer (and job) kind that runs the next ``status`` poll of a confirming adapter's stalled
attempt: target ``job_id``."""
IN_FLIGHT: frozenset[SendState] = frozenset({SendState.SENDING, SendState.ACCEPTED})
UNKNOWN = "unknown"
CANCEL_ERROR = "error"
"""``image_reservations.cancel_result`` when the one ``cancel`` call raised."""
STATUS_ERROR = "error"
"""``image_reservations.last_status`` when a ``status`` call raised (it keeps polling)."""


# -- horizons -------------------------------------------------------------------------------


def request_timeout_s(params: ImageGenerationParams, capabilities: Capabilities) -> int:
    """The bound on one provider call: the ``image_generation`` row's ``request_timeout_s``,
    else the adapter's own (``Capabilities.request_timeout_s``)."""
    return int(params.request_timeout_s or capabilities.request_timeout_s)


def horizon_of(started_at: datetime, timeout_s: int, settings: ImageJobSettings) -> datetime:
    """``started_at + request_timeout_s + image_attempt_grace_s``."""
    return started_at + timedelta(seconds=timeout_s + settings.image_attempt_grace_s)


def cost_json(cost: AttemptCost | None) -> Any:
    """A cost as the records hold it: ``{amount, currency}``, ``"unknown"``, or ``None``."""
    if isinstance(cost, Cost):
        return {"amount": str(cost.amount), "currency": cost.currency}
    return cost


def usage_json(usage: ImageUsage | None) -> Any:
    return None if usage is None else {"images": usage.images, **dict(usage.raw)}


def _job_id(payload: Mapping[str, Any]) -> str | None:
    found = payload.get("image_job_id") or payload.get("target")
    return None if found is None else str(found)


# -- the lease-expiry hook ------------------------------------------------------------------


async def on_lease_expired(
    conn: AsyncConnection, row_id: str, payload: Mapping[str, Any]
) -> datetime | None:
    """#70's lease-expiry hook for ``image_job`` and ``image_status_poll`` rows (module
    docstring), in the reaper's transaction: the job's token advanced, and with an attempt in
    flight its horizon timer scheduled and its horizon returned (the row's new ``due_at``).
    ``None`` (due as before) for a job that ended or has nothing in flight."""
    from epistrel.orchestrator.jobs import timers  # here: the jobs package imports this module

    job_id = _job_id(payload)
    if job_id is None:
        return None
    job = await store.read_job(conn, job_id, lock=True)
    if job is None or job.terminal:
        return None
    current = await store.read_reservation(conn, job_id, job.attempt_token)
    in_flight = current is not None and current.send_state in IN_FLIGHT and current.outcome is None
    now = clock.now()
    horizon = current.horizon_at if current is not None and in_flight else None
    waiting = horizon is not None and horizon > now
    await conn.execute(
        update(image_jobs)
        .where(image_jobs.c.id == job_id)
        .values(attempt_token=store.new_token(), waiting_for_horizon=waiting, updated_at=now)
    )
    if in_flight:
        await timers.schedule(conn, HORIZON_TIMER, f"{job_id}:{job.attempt_token}", horizon or now)
    logger.info(
        "image job %s (row %s): lease expired, attempt %s fenced%s",
        job_id,
        row_id,
        job.attempt_token,
        " until its horizon" if waiting else "",
    )
    return horizon if waiting else None


# -- the horizon ----------------------------------------------------------------------------


async def release_at_horizon_in(conn: AsyncConnection, job_id: str, token: str) -> bool:
    """Release the attempt's record if it is still ``held`` (``released_at_horizon``, cost
    ``unknown`` unless one is known), in ``conn``'s transaction; ``False`` when there was
    nothing to release (settled, released, or no record)."""
    record = await limits.read_record(conn, job_id, token, lock=True)
    if record is None or record.state is not ReservationState.HELD:
        return False
    await limits.release(conn, record)
    found = await store.read_reservation(conn, job_id, token)
    assert found is not None  # noqa: S101 - locked above
    applied = dict(found.applied_parameters or {})
    if applied.get("cost") is None:
        applied["cost"] = UNKNOWN
    values: dict[str, Any] = {"applied_parameters": applied}
    if found.outcome is None:
        values["outcome"] = AttemptOutcome.RELEASED_AT_HORIZON.value
    await store.update_reservation(conn, job_id, token, **values)
    return True


async def make_due(conn: AsyncConnection, job_id: str) -> None:
    """The job's parked ``image_job`` row is due now (the horizon was reached)."""
    now = clock.now()
    await conn.execute(
        update(jobs_table)
        .where(
            jobs_table.c.kind == IMAGE_JOB_KIND,
            jobs_table.c.status == "queued",
            jobs_table.c.payload["image_job_id"].astext == job_id,
            jobs_table.c.due_at > now,
        )
        .values(due_at=now, updated_at=now)
    )


async def horizon_reached(db: AsyncEngine, target: str) -> str:
    """The ``image_attempt_horizon`` timer (target ``<job_id>:<token>``): the record released if
    it is still held, and a job that has not ended made due. ``released`` or ``skipped``; firing
    it again changes nothing."""
    job_id, _, token = target.partition(":")
    async with db.begin() as conn:
        job = await store.read_job(conn, job_id, lock=True)
        released = await release_at_horizon_in(conn, job_id, token)
        if job is not None and not job.terminal:
            await make_due(conn, job_id)
    outcome = "released" if released else "skipped"
    logger.info("image job %s attempt %s: horizon %s", job_id, token, outcome)
    return outcome


# -- the settlement path --------------------------------------------------------------------


async def settle_attempt_in(
    conn: AsyncConnection,
    job_id: str,
    attempt_token: str,
    outputs: int,
    *,
    cost: AttemptCost | None,
    usage: ImageUsage | None = None,
    released: AttemptOutcome = AttemptOutcome.LATE_AFTER_HORIZON,
) -> AttemptOutcome | None:
    """The settlement path (module docstring) in ``conn``'s transaction, for ``outputs``
    returned outputs and their ``cost``. A ``released`` record settles as ``released`` (by
    default ``late_after_horizon``; an adoption passes ``adopted``, a confirmed failure
    ``failed`` or ``cancelled``). ``None`` when the attempt has no record (#268's
    ``late_unaccounted``)."""
    record = await limits.read_record(conn, job_id, attempt_token, lock=True)
    if record is None:
        logger.info("image job %s attempt %s: a return with no record", job_id, attempt_token)
        return None
    found = await store.read_reservation(conn, job_id, attempt_token)
    assert found is not None  # noqa: S101 - locked above
    applied = dict(found.applied_parameters or {})
    if record.state is ReservationState.SETTLED:
        if record.used != outputs:
            logger.info(
                "image job %s attempt %s: a duplicate return of %d output(s) for %d settled",
                job_id,
                attempt_token,
                outputs,
                record.used,
            )
        known = cost_json(cost)
        if applied.get("cost") in (None, UNKNOWN) and known not in (None, UNKNOWN):
            # The one-time cost fill-in: an unknown cost becomes known, and never changes again.
            applied["cost"] = known
            await store.update_reservation(conn, job_id, attempt_token, applied_parameters=applied)
        count_attempt_outcome(AttemptOutcome.LATE_DUPLICATE.value)
        logger.info("image job %s attempt %s: late_duplicate", job_id, attempt_token)
        return AttemptOutcome.LATE_DUPLICATE
    outcome = AttemptOutcome.LATE_DISCARDED if record.state is ReservationState.HELD else released
    await limits.settle(conn, record, outputs)
    applied["cost"] = cost_json(cost) if cost is not None else UNKNOWN
    if usage is not None:
        applied["usage"] = usage_json(usage)
    values: dict[str, Any] = {
        "applied_parameters": applied,
        "outputs_returned": outputs,
        "outcome": outcome.value,
    }
    if found.send_state in IN_FLIGHT:
        values["send_state"] = SendState.RETURNED.value
    await store.update_reservation(conn, job_id, attempt_token, **values)
    return outcome


async def settle_attempt(
    db: AsyncEngine,
    job_id: str,
    attempt_token: str,
    outputs: int,
    *,
    cost: AttemptCost | None,
    usage: ImageUsage | None = None,
    staged: Sequence[Path] = (),
) -> AttemptOutcome | None:
    """``settle_attempt_in`` in a transaction of its own; the fenced worker's ``staged`` bytes
    are deleted once it commits (the staging sweep backs that up)."""
    async with db.begin() as conn:
        outcome = await settle_attempt_in(
            conn, job_id, attempt_token, outputs, cost=cost, usage=usage
        )
    for path in staged:
        path.unlink(missing_ok=True)
    return outcome


# -- the stalled attempt --------------------------------------------------------------------


async def stalled_record(conn: AsyncConnection, job: ImageJob) -> Reservation | None:
    """The job's latest attempt a lease expiry fenced while it was in flight and that is not
    resolved yet (no outcome, or ``released_at_horizon``), other than the current token's."""
    r = image_reservations.c
    token = (
        await conn.execute(
            select(r.attempt_token)
            .where(
                r.job_id == job.id,
                r.attempt_token != job.attempt_token,
                r.send_state.in_([s.value for s in IN_FLIGHT]),
                r.outcome.is_(None) | (r.outcome == AttemptOutcome.RELEASED_AT_HORIZON.value),
            )
            .order_by(desc(r.created_at), desc(r.attempt_token))
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if token is None else await store.read_reservation(conn, job.id, str(token))


async def release_unsent(conn: AsyncConnection, job: ImageJob) -> int:
    """Release every held record of another token that was never sent (a lease expiry fenced it
    while ``prepared``): it retries with no presumption (``discarded``); how many."""
    count = 0
    for record in await limits.records(conn, job.id):
        if (
            record.attempt_token != job.attempt_token
            and record.state is ReservationState.HELD
            and record.send_state is SendState.PREPARED
        ):
            await limits.release(conn, record)
            await store.update_reservation(
                conn, job.id, record.attempt_token, outcome=AttemptOutcome.DISCARDED.value
            )
            count += 1
    return count


async def set_waiting(conn: AsyncConnection, job_id: str, waiting: bool) -> None:
    await conn.execute(
        update(image_jobs)
        .where(image_jobs.c.id == job_id, image_jobs.c.waiting_for_horizon.is_not(waiting))
        .values(waiting_for_horizon=waiting, updated_at=func.now())
    )


# -- confirmation ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Adopt:
    """``status`` said ``succeeded`` with outputs: the job takes them over."""

    status: StatusResult


@dataclass(frozen=True, slots=True)
class Retry:
    """The old attempt is resolved (``failed``, ``cancelled``) or abandoned (``fallback``): the
    job retries under a new reservation."""

    reason: str


@dataclass(frozen=True, slots=True)
class Poll:
    """No definitive status yet: polled again at ``next_at``."""

    next_at: datetime


type Confirmation = Adopt | Retry | Poll


async def confirm(
    db: AsyncEngine,
    job: ImageJob,
    record: Reservation,
    handle: ImageGeneratorHandle,
    settings: ImageJobSettings,
) -> Confirmation:
    """One confirmation step for the stalled ``record`` after its horizon (module docstring):
    ``cancel`` once, then one ``status`` poll, then what it decides. A poll schedules the next
    one through the ``image_status_poll`` timer."""
    from epistrel.orchestrator.jobs import timers  # here: the jobs package imports this module

    ref = str(record.attempt_ref)
    token = record.attempt_token
    if record.cancel_result is None:
        try:
            answer: str = await handle.cancel(ref)
        except Exception as exc:
            logger.warning("image job %s: cancel failed: %s", job.id, type(exc).__name__)
            answer = CANCEL_ERROR
        fixed = (record.horizon_at or clock.now()) + timedelta(
            seconds=settings.image_confirm_wait_max_s
        )
        async with db.begin() as conn:
            await store.update_reservation(conn, job.id, token, cancel_result=answer)
            await store.update_job(conn, job.id, confirm_deadline_at=fixed)
    try:
        found: StatusResult | None = await handle.status(ref)
    except Exception as exc:
        logger.warning("image job %s: status failed: %s", job.id, type(exc).__name__)
        found = None
    state = STATUS_ERROR if found is None else found.state
    async with db.begin() as conn:
        await store.update_reservation(conn, job.id, token, last_status=state)
        current = await store.read_job(conn, job.id)
    if found is not None and found.state == "succeeded" and outputs_of(found):
        return Adopt(found)
    if found is not None and found.state in ("succeeded", "failed", "cancelled"):
        # A ``succeeded`` with no outputs is a failure.
        settled = AttemptOutcome.CANCELLED if found.state == "cancelled" else AttemptOutcome.FAILED
        async with db.begin() as conn:
            await settle_attempt_in(
                conn, job.id, token, 0, cost=found.cost, usage=found.usage, released=settled
            )
        return Retry(settled.value)
    now = clock.now()
    deadline = None if current is None else current.confirm_deadline_at
    if deadline is not None and now >= deadline:
        async with db.begin() as conn:
            await store.update_job(conn, job.id, confirm_fallback=True)
            await store.update_reservation(
                conn, job.id, token, outcome=AttemptOutcome.ABANDONED_FALLBACK.value
            )
        logger.info("image job %s: no definitive status by its deadline, falling back", job.id)
        return Retry("fallback")
    next_at = now + timedelta(seconds=settings.image_status_poll_s)
    if deadline is not None:
        next_at = min(next_at, deadline)
    async with db.begin() as conn:
        await timers.schedule(conn, STATUS_POLL, job.id, next_at)
    logger.info("image job %s: status %s, polling again", job.id, state)
    return Poll(next_at)


__all__ = [
    "CANCEL_ERROR",
    "HORIZON_TIMER",
    "IN_FLIGHT",
    "STATUS_ERROR",
    "STATUS_POLL",
    "UNKNOWN",
    "Adopt",
    "Confirmation",
    "Poll",
    "Retry",
    "confirm",
    "cost_json",
    "horizon_of",
    "horizon_reached",
    "make_due",
    "on_lease_expired",
    "release_at_horizon_in",
    "release_unsent",
    "request_timeout_s",
    "set_waiting",
    "settle_attempt",
    "settle_attempt_in",
    "stalled_record",
    "usage_json",
]
