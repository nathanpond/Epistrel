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

**Ambiguous sends** (#268; FR-IMG-11, part C: provider delivery is at least once, promotion at
most once). The ``sending`` commit stores the attempt's idempotency key
(``store.idempotency_key_of``), the request it sends (``request_snapshot``) and the adapter's
``{submission, idempotency_keys}`` (``capabilities_snapshot``) before ``submit`` is called. A
lease expiry that finds the attempt still ``sending`` (no request identifier or result
recorded: a sync call, or an async ``submit`` not yet acknowledged) runs ``recover_attempt``:

- the snapshot declares ``idempotency_keys`` and the key was re-sent fewer than
  ``MAX_RESENDS`` times: ``resend``. The job is due at once, and its handler (never the reaper)
  re-sends the snapshot with the stalled attempt's key under the job's new token, on a row of its
  own that reserves nothing (``resend_of``, ``requested = 0``, no ``counter_keys``). The
  result settles the stalled attempt's record (``resent``: counted once), and its outputs are
  checked and promoted under the new token. A re-send is no ambiguous send;
- otherwise: ``ambiguous``. The record (the stalled attempt's, for a re-send) is marked
  ``send_state = ambiguous`` and held to its horizon (the timer above), then settled
  **presumed** (``limits.presume``: ``used = requested``, cost ``unknown``), and the job
  retries under a new token and reservation. An ending, a cancel or a lost target keeps it held
  to that horizon all the same.

A presumed record accepts exactly one correction: the actual result's ``m`` outputs make it what
a ``held`` settlement would have (``limits.correct``: ``used = m``, ``overage = extra_outputs =
max(0, m - requested)``, ``basis = corrected``), the counters move by the difference, the bytes
are deleted unpromoted, and it is reported ``late_after_presumed``; any later delivery is
``late_duplicate``. A result before the horizon settles it ``held → settled``
(``late_discarded``) and it is never presumed. A result whose record accounting retention purged
is ``late_unaccounted`` (logged and counted; #269 records its cost) and changes no counter.
Every presumption and re-send counts as a failed attempt of the ``jobs`` row (#70's
``max_attempts``), so a job that keeps crashing mid-send ends ``failed``/``provider_unavailable``.
``recover_attempts`` is the start-up recovery hook that classifies records an earlier version
fenced in ``sending``.

Logs and metrics carry ids, outcomes and counts only (invariant 3): the events are
``image_attempt_ambiguous``, ``image_attempt_presumed``, ``image_attempt_resent`` and
``image_attempt_late``.
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
    TERMINAL_STATUSES,
    AttemptOutcome,
    ReservationState,
    SendState,
    SettlementBasis,
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
UNRESOLVED: frozenset[SendState] = IN_FLIGHT | {SendState.AMBIGUOUS}
"""A fenced attempt that may still return: in flight, or an ambiguous send (#268)."""
MAX_RESENDS = 2
"""Re-sends of one idempotency key before the attempt is treated as ambiguous (#268)."""
LATE_UNACCOUNTED = "late_unaccounted"
"""A result whose reservation record accounting retention purged (#268; never stored)."""
RESEND = "resend"
AMBIGUOUS = "ambiguous"
RESOLVED = "resolved"
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
    job_id = _job_id(payload)
    if job_id is None:
        return None
    job = await store.read_job(conn, job_id, lock=True)
    if job is None or job.terminal:
        return None
    return await fence_in(conn, job, row_id=row_id)


async def fence_in(
    conn: AsyncConnection, job: ImageJob, *, row_id: str | None = None
) -> datetime | None:
    """Fence the job's current attempt (``job`` read under lock), in ``conn``'s transaction:
    its token advanced, and an attempt in flight held to its horizon (an ``accepted`` one, or
    an ambiguous send, #268) or left for an idempotent re-send; the horizon the job waits for,
    or ``None`` when it is due at once. ``row_id`` (the lease-expiry hook's ``jobs`` row) has a
    ``sending`` attempt counted as a failed attempt of that row."""
    from epistrel.orchestrator.jobs import timers  # here: the jobs package imports this module

    job_id = job.id
    current = await store.read_reservation(conn, job_id, job.attempt_token)
    in_flight = current is not None and current.send_state in IN_FLIGHT and current.outcome is None
    now = clock.now()
    await conn.execute(
        update(image_jobs)
        .where(image_jobs.c.id == job_id)
        .values(attempt_token=store.new_token(), updated_at=now)
    )
    held: Reservation | None = current if in_flight else None
    resend = False
    if current is not None and in_flight and current.send_state is SendState.SENDING:
        # #268: an ambiguous send (held to its horizon), or one an idempotent re-send recovers
        # (the job is due at once; the timer still guards the record if nothing re-sends it).
        decided, held = await recover_attempt(conn, job, current)
        resend = decided == RESEND
        if row_id is not None:
            await count_failure(conn, row_id)
    horizon = held.horizon_at if held is not None else None
    waiting = horizon is not None and horizon > now and not resend
    await set_waiting(conn, job_id, waiting)
    if held is not None:
        await timers.schedule(conn, HORIZON_TIMER, f"{job_id}:{held.attempt_token}", horizon or now)
    logger.info(
        "image job %s (row %s): attempt %s fenced%s",
        job_id,
        row_id or "-",
        job.attempt_token,
        " until its horizon" if waiting else "",
    )
    return horizon if waiting else None


# -- ambiguous sends (#268) -----------------------------------------------------------------


async def count_failure(conn: AsyncConnection, row_id: str) -> None:
    """A presumed or re-sent attempt counts against #70's ``max_attempts``: the ``jobs`` row's
    ``failures`` rises by one (in the reaper's transaction)."""
    await conn.execute(
        update(jobs_table)
        .where(jobs_table.c.id == row_id)
        .values(failures=jobs_table.c.failures + 1)
    )


async def resends_of(conn: AsyncConnection, job_id: str, original: str) -> int:
    """How many times the attempt ``original``'s key was re-sent."""
    r = image_reservations.c
    found = await conn.scalar(
        select(func.count()).where(r.job_id == job_id, r.resend_of == original)
    )
    return int(found or 0)


async def recover_attempt(
    conn: AsyncConnection, job: ImageJob, record: Reservation, *, resend: bool = True
) -> tuple[str, Reservation | None]:
    """Classify a fenced attempt left in ``sending`` (module docstring), in ``conn``'s
    transaction: ``(RESEND, original)`` when its key can be re-sent (``resend`` allows it, the
    snapshot declares ``idempotency_keys`` and fewer than ``MAX_RESENDS`` re-sends were made);
    ``(AMBIGUOUS, original)`` once the original record is marked ``ambiguous`` (held to its
    horizon); ``(RESOLVED, None)`` when the original record no longer holds anything (a late
    return settled it). ``original`` is the record that carries the reservation: the stalled
    attempt's own, or for a re-send the attempt it re-sent. A superseded re-send's row is
    ``discarded`` (its late return still settles the original)."""
    token = record.resend_of or record.attempt_token
    original = (
        record
        if token == record.attempt_token
        else await store.read_reservation(conn, job.id, token)
    )
    accounting = await limits.read_record(conn, job.id, token, lock=True)
    if original is None or accounting is None or accounting.state is not ReservationState.HELD:
        await supersede(conn, job.id, record)
        return RESOLVED, None
    if (
        resend
        and original.resendable
        and original.request_snapshot is not None
        and await resends_of(conn, job.id, token) < MAX_RESENDS
    ):
        return RESEND, original
    await supersede(conn, job.id, record)
    if original.send_state is not SendState.AMBIGUOUS:
        await store.update_reservation(conn, job.id, token, send_state=SendState.AMBIGUOUS.value)
        logger.info(
            "image job %s attempt %s: image_attempt_ambiguous", job.id, original.attempt_token
        )
    found = await store.read_reservation(conn, job.id, token)
    return AMBIGUOUS, found


async def supersede(conn: AsyncConnection, job_id: str, record: Reservation) -> None:
    """A re-send's row whose recovery moved on is ``discarded`` (and its unreserved record
    released, which moves no counter)."""
    if record.resend_of is None or record.outcome is not None:
        return
    found = await limits.read_record(conn, job_id, record.attempt_token, lock=True)
    if found is not None and found.state is ReservationState.HELD:
        await limits.release(conn, found)
    await store.update_reservation(
        conn, job_id, record.attempt_token, outcome=AttemptOutcome.DISCARDED.value
    )


async def recover_attempts(conn: AsyncConnection) -> int:
    """The start-up recovery hook (#70's recovery pass): every fenced attempt left in
    ``sending`` that no idempotent re-send can recover (one an earlier version fenced, #267) is
    marked ``ambiguous`` and held to its horizon; how many. Idempotent: a record already
    classified is skipped, and one that can be re-sent is left to its job's run."""
    from epistrel.orchestrator.jobs import timers  # here: the jobs package imports this module

    r, j = image_reservations.c, image_jobs.c
    rows = (
        await conn.execute(
            select(r.job_id, r.attempt_token)
            .select_from(image_reservations.join(image_jobs, j.id == r.job_id))
            .where(
                r.send_state == SendState.SENDING.value,
                r.outcome.is_(None),
                r.attempt_token != j.attempt_token,
                j.status.not_in([s.value for s in TERMINAL_STATUSES]),
            )
            .order_by(r.job_id, r.created_at)
        )
    ).all()
    marked = 0
    for row in rows:
        job = await store.read_job(conn, str(row.job_id), lock=True)
        record = await store.read_reservation(conn, str(row.job_id), str(row.attempt_token))
        if (
            job is None
            or job.terminal
            or record is None
            or record.send_state is not SendState.SENDING
        ):
            continue
        decided, held = await recover_attempt(conn, job, record)
        if decided == AMBIGUOUS and held is not None:
            await timers.schedule(
                conn,
                HORIZON_TIMER,
                f"{job.id}:{held.attempt_token}",
                held.horizon_at or clock.now(),
            )
            marked += 1
    return marked


# -- the horizon ----------------------------------------------------------------------------


async def release_at_horizon_in(
    conn: AsyncConnection, job_id: str, token: str
) -> AttemptOutcome | None:
    """At the horizon, the attempt's record if it is still ``held``, in ``conn``'s transaction:
    an ambiguous send's is presumed (#268: ``presumed``), any other's released
    (``released_at_horizon``), cost ``unknown`` unless one is known. ``None`` when there was
    nothing to do (settled, released, or no record)."""
    record = await limits.read_record(conn, job_id, token, lock=True)
    if record is None or record.state is not ReservationState.HELD:
        return None
    if record.horizon_at is not None and record.horizon_at > clock.now():
        return None  # a re-send moved the horizon on (#268); its timer was moved with it
    if record.send_state is SendState.SENDING:
        # #268: still ``sending`` at its horizon, no re-send in flight: an ambiguous send.
        await store.update_reservation(conn, job_id, token, send_state=SendState.AMBIGUOUS.value)
        record = await limits.read_record(conn, job_id, token, lock=True)
        assert record is not None  # noqa: S101 - locked above
        logger.info("image job %s attempt %s: image_attempt_ambiguous", job_id, token)
    if record.send_state is SendState.AMBIGUOUS:
        await limits.presume(conn, record)
        logger.info("image job %s attempt %s: image_attempt_presumed", job_id, token)
        return AttemptOutcome.PRESUMED
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
    return AttemptOutcome.RELEASED_AT_HORIZON


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
    it is still held (an ambiguous send's presumed, #268), and a job that has not ended made due.
    ``released``, ``presumed`` or ``skipped``; firing it again changes nothing."""
    job_id, _, token = target.partition(":")
    async with db.begin() as conn:
        job = await store.read_job(conn, job_id, lock=True)
        done = await release_at_horizon_in(conn, job_id, token)
        if job is not None and not job.terminal:
            await make_due(conn, job_id)
    outcome = {
        None: "skipped",
        AttemptOutcome.PRESUMED: "presumed",
    }.get(done, "released")
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
    held: AttemptOutcome = AttemptOutcome.LATE_DISCARDED,
) -> AttemptOutcome | None:
    """The settlement path (module docstring) in ``conn``'s transaction, for ``outputs``
    returned outputs and their ``cost``. A ``held`` record settles as ``held`` (by default
    ``late_discarded``; a re-send's own return passes ``resent``), a ``released`` one as
    ``released`` (by default ``late_after_horizon``; an adoption passes ``adopted``, a confirmed
    failure ``failed`` or ``cancelled``). A presumed one is corrected once
    (``late_after_presumed``, #268). A re-send's return (its row names ``resend_of``) settles
    the attempt it re-sent. ``None`` when the attempt has no record any more (#268's
    ``late_unaccounted``: logged and counted, no counter changes; #269 records its cost)."""
    own = await store.read_reservation(conn, job_id, attempt_token)
    if own is not None and own.resend_of is not None:
        # #268: a re-send's outputs count once, on the record that reserved them.
        await store.update_reservation(
            conn,
            job_id,
            attempt_token,
            send_state=SendState.RETURNED.value,
            outputs_returned=outputs,
        )
        attempt_token = own.resend_of
    record = await limits.read_record(conn, job_id, attempt_token, lock=True)
    if record is None:
        count_attempt_outcome(LATE_UNACCOUNTED)
        logger.info(
            "image job %s attempt %s: image_attempt_late %s",
            job_id,
            attempt_token,
            LATE_UNACCOUNTED,
        )
        return None
    found = await store.read_reservation(conn, job_id, attempt_token)
    assert found is not None  # noqa: S101 - locked above
    applied = dict(found.applied_parameters or {})
    if record.state is ReservationState.SETTLED and record.basis is SettlementBasis.PRESUMED:
        # #268: the presumed settlement's one correction.
        await limits.correct(conn, record, outputs)
        applied["cost"] = cost_json(cost) if cost is not None else UNKNOWN
        if usage is not None:
            applied["usage"] = usage_json(usage)
        await store.update_reservation(
            conn,
            job_id,
            attempt_token,
            applied_parameters=applied,
            outputs_returned=outputs,
            outcome=AttemptOutcome.LATE_AFTER_PRESUMED.value,
        )
        logger.info(
            "image job %s attempt %s: image_attempt_late %s",
            job_id,
            attempt_token,
            AttemptOutcome.LATE_AFTER_PRESUMED.value,
        )
        return AttemptOutcome.LATE_AFTER_PRESUMED
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
    outcome = held if record.state is ReservationState.HELD else released
    await limits.settle(conn, record, outputs)
    applied["cost"] = cost_json(cost) if cost is not None else UNKNOWN
    if usage is not None:
        applied["usage"] = usage_json(usage)
    values: dict[str, Any] = {
        "applied_parameters": applied,
        "outputs_returned": outputs,
        "outcome": outcome.value,
    }
    if found.send_state in UNRESOLVED:
        values["send_state"] = SendState.RETURNED.value
    await store.update_reservation(conn, job_id, attempt_token, **values)
    if outcome in (AttemptOutcome.LATE_DISCARDED, AttemptOutcome.LATE_AFTER_HORIZON):
        logger.info(
            "image job %s attempt %s: image_attempt_late %s", job_id, attempt_token, outcome.value
        )
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
    """The job's latest attempt a lease expiry fenced while it was in flight (an ambiguous send
    included, #268) and that is not resolved yet (no outcome, or ``released_at_horizon``), other
    than the current token's."""
    r = image_reservations.c
    token = (
        await conn.execute(
            select(r.attempt_token)
            .where(
                r.job_id == job.id,
                r.attempt_token != job.attempt_token,
                r.send_state.in_([s.value for s in UNRESOLVED]),
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
    "AMBIGUOUS",
    "CANCEL_ERROR",
    "HORIZON_TIMER",
    "IN_FLIGHT",
    "LATE_UNACCOUNTED",
    "MAX_RESENDS",
    "RESEND",
    "RESOLVED",
    "STATUS_ERROR",
    "STATUS_POLL",
    "UNKNOWN",
    "UNRESOLVED",
    "Adopt",
    "Confirmation",
    "Poll",
    "Retry",
    "confirm",
    "cost_json",
    "count_failure",
    "fence_in",
    "horizon_of",
    "horizon_reached",
    "make_due",
    "on_lease_expired",
    "recover_attempt",
    "recover_attempts",
    "release_at_horizon_in",
    "release_unsent",
    "request_timeout_s",
    "resends_of",
    "set_waiting",
    "settle_attempt",
    "settle_attempt_in",
    "stalled_record",
    "supersede",
    "usage_json",
]
