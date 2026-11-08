"""Image limits: output-image reservations against the deployment and catalog-entry daily
limits (FR-IMG-11 as amended by #262; docs/02 §4.13).

**Unit and scopes.** The unit is the output image: every image a provider returns counts once.
There are two scopes, in lock order. ``deployment`` (key ``deployment``) charges every job.
``entry`` (key ``<entry_kind>:<entry_id>``) charges a catalog job. A story job charges the
deployment only and records its ``story_id`` for attribution: per-story and per-turn admission is
the consumer's to manage. Each period is the local date in ``deployment_time_zone`` at the
record's ``created_at``, from the injectable clock (``core.clock``). The limits are
``image_limit_deployment_day`` and ``image_limit_entry_day``: 0 disables the scope, and there is
no unlimited value.

**Records and counters.** One ``image_reservations`` row per provider attempt carries an
immutable snapshot of the counters it charges (``counter_keys``, in lock order) and the outputs
it ``requested``. ``image_limit_counters`` stores, per key, the sums of the records that name
it: ``reserved`` = Σ ``requested`` over ``held`` records, and ``used``, ``overage`` and
``extra_outputs`` = Σ over ``settled`` ones. Each counter change is made by ``counter_deltas``,
through ``reserve``, ``settle`` and ``release`` only, in the transaction of the record change it
belongs to, and on the keys of the record's snapshot (never the current time or target).

**Reserve.** ``reserve`` checks every applicable limit for 0 first (``ImagesDisabled``). It then
upserts and locks each counter in lock order and admits only if ``used + reserved + k ≤ limit``
everywhere (``ImageLimitReached`` names the first failing scope). Then it inserts the ``held``
record and adds ``k`` to every counter's ``reserved``. A failure writes nothing the caller's
transaction (or savepoint) keeps.

**Reconcile and retain.** ``reconcile`` recomputes every counter from the records in one
REPEATABLE READ snapshot and finds ``held`` records whose job is terminal or missing.
``fix_counters`` rewrites drifted sums and creates missing rows in lock order, and it releases
stuck records through ``release``. Neither touches ``last_record_at``. ``retain`` deletes records,
then counters, of periods closed for ``image_accounting_retention_days``, but never while a
``held`` record names them.

Nothing here logs content: ids, scopes, periods and counts only (invariant 3).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Date, and_, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.config_changes import read_section
from epistrel.core.errors import ImageLimitReached, ImagesDisabled
from epistrel.core.images import store
from epistrel.core.images.store import ImageJob
from epistrel.core.settings_schema import ImageJobSettings
from epistrel.core.tables.images import (
    TERMINAL_STATUSES,
    ImageJobKind,
    LimitScope,
    ReservationState,
    SendState,
    SettlementBasis,
    image_jobs,
    image_limit_counters,
    image_reservations,
)

DEPLOYMENT_KEY = "deployment"
LOCK_ORDER: tuple[LimitScope, ...] = (LimitScope.DEPLOYMENT, LimitScope.ENTRY)
RETENTION_BATCH = 1_000
STUCK_HELD = "stuck_held"
"""The drift metric's scope label for a ``held`` record whose job is terminal or missing."""


class InvalidReservationTransition(Exception):
    """``settle`` on a record that is already ``settled``, or ``release`` on one that is not
    ``held``."""


# -- keys -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class CounterKey:
    """One counter a record charges: ``(scope, scope_key, period)``."""

    scope: LimitScope
    scope_key: str
    period: date

    def as_json(self) -> dict[str, str]:
        return {
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "period": self.period.isoformat(),
        }

    @classmethod
    def of(cls, value: Mapping[str, Any]) -> CounterKey:
        return cls(
            LimitScope(str(value["scope"])),
            str(value["scope_key"]),
            date.fromisoformat(str(value["period"])),
        )

    @property
    def sort_key(self) -> tuple[int, str, date]:
        return (LOCK_ORDER.index(self.scope), self.scope_key, self.period)


def zone(settings: ImageJobSettings) -> ZoneInfo:
    """The deployment's time zone (``deployment_time_zone``)."""
    return ZoneInfo(settings.deployment_time_zone)


def period_of(moment: datetime, tz: ZoneInfo) -> date:
    """The local calendar day of ``moment`` in ``tz``."""
    return moment.astimezone(tz).date()


async def current_period(conn: AsyncConnection) -> str:
    """Today's local date in the deployment's time zone (``YYYY-MM-DD``)."""
    return period_of(clock.now(), zone(await read_section(conn, ImageJobSettings))).isoformat()


def resets_at(period: date, tz: ZoneInfo) -> datetime:
    """The next local midnight after ``period``. ``fold=0`` is normalized through UTC, so a
    midnight that does not exist (a DST gap) is the first instant of that date that does."""
    midnight = datetime.combine(period + timedelta(days=1), time(0), tzinfo=tz)
    return midnight.astimezone(UTC).astimezone(tz)


def entry_key(entry_kind: str, entry_id: str) -> str:
    return f"{entry_kind}:{entry_id}"


def attribution(job: ImageJob) -> tuple[str | None, str | None, str | None]:
    """``(story_id, entry_kind, entry_id)`` of the job (for usage reports, never scopes)."""
    if job.kind is ImageJobKind.CATALOG:
        kind = job.origin.get("entry_kind") or job.target.get("entry_kind")
        entry = job.origin.get("entry_id") or job.target.get("entry_id")
        return None, None if kind is None else str(kind), None if entry is None else str(entry)
    return job.story_id, None, None


def counter_keys(job: ImageJob, period: date) -> list[CounterKey]:
    """The counters a job's attempt charges, in lock order: the deployment, and a catalog
    job's entry."""
    keys = [CounterKey(LimitScope.DEPLOYMENT, DEPLOYMENT_KEY, period)]
    _story, kind, entry = attribution(job)
    if job.kind is ImageJobKind.CATALOG and kind is not None and entry is not None:
        keys.append(CounterKey(LimitScope.ENTRY, entry_key(kind, entry), period))
    return keys


def limit_for(scope: LimitScope, settings: ImageJobSettings) -> int:
    if scope is LimitScope.DEPLOYMENT:
        return settings.image_limit_deployment_day
    return settings.image_limit_entry_day


# -- deltas ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Deltas:
    """What one record change adds to each counter it names."""

    reserved: int = 0
    used: int = 0
    overage: int = 0
    extra_outputs: int = 0

    def __bool__(self) -> bool:
        return any((self.reserved, self.used, self.overage, self.extra_outputs))


def counter_deltas(
    old_state: ReservationState | None,
    new_state: ReservationState,
    requested: int,
    m: int = 0,
) -> Deltas:
    """docs/02 §4.13's counter maintenance. Inserting a ``held`` record adds its ``requested`` to
    ``reserved``. ``held → released`` takes it back. ``held → settled`` with *m* outputs takes it
    back and adds ``used = m``, ``overage = m - min(m, requested)`` and ``extra_outputs =
    max(0, m - requested)``. A presumed settlement (#268) is ``m = requested``. ``released →
    settled`` (#267: a late return after the record was released at its horizon, or a
    confirming adapter's adopted outputs) leaves ``reserved`` alone and adds ``used = m``,
    ``overage = m`` (nothing was reserved for them any more) and ``extra_outputs = max(0, m -
    requested)``. Any other transition raises ``InvalidReservationTransition``. A presumed
    settlement's one correction (#268) is ``correct``'s difference, not a state transition."""
    if old_state is None and new_state is ReservationState.HELD:
        return Deltas(reserved=requested)
    if old_state is ReservationState.HELD and new_state is ReservationState.RELEASED:
        return Deltas(reserved=-requested)
    if old_state is ReservationState.HELD and new_state is ReservationState.SETTLED:
        if m < 0:
            raise ValueError("a settlement returns at least 0 outputs")
        return Deltas(
            reserved=-requested,
            used=m,
            overage=m - min(m, requested),
            extra_outputs=max(0, m - requested),
        )
    if old_state is ReservationState.RELEASED and new_state is ReservationState.SETTLED:
        if m < 0:
            raise ValueError("a settlement returns at least 0 outputs")
        return Deltas(used=m, overage=m, extra_outputs=max(0, m - requested))
    raise InvalidReservationTransition(
        f"no counter transition from {old_state} to {new_state.value}"
    )


# -- records --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    """An ``image_reservations`` row's accounting."""

    job_id: str
    attempt_token: str
    story_id: str | None
    entry_kind: str | None
    entry_id: str | None
    counter_keys: tuple[CounterKey, ...]
    requested: int
    used: int
    overage: int
    extra_outputs: int
    basis: SettlementBasis
    state: ReservationState
    send_state: SendState
    created_at: datetime
    settled_at: datetime | None
    horizon_at: datetime | None = None
    """#267: ``started_at + request_timeout_s + image_attempt_grace_s`` once sent."""


def _record(row: Any) -> ReservationRecord:
    m = row._mapping
    keys = m["counter_keys"] or []
    if isinstance(keys, str):
        keys = json.loads(keys)
    return ReservationRecord(
        job_id=m["job_id"],
        attempt_token=str(m["attempt_token"]),
        story_id=m["story_id"],
        entry_kind=m["entry_kind"],
        entry_id=m["entry_id"],
        counter_keys=tuple(CounterKey.of(k) for k in keys),
        requested=int(m["requested"]),
        used=int(m["used"]),
        overage=int(m["overage"]),
        extra_outputs=int(m["extra_outputs"]),
        basis=SettlementBasis(m["basis"]),
        state=ReservationState(m["state"]),
        send_state=SendState(m["send_state"]),
        created_at=m["created_at"],
        settled_at=m["settled_at"],
        horizon_at=m["horizon_at"],
    )


async def read_record(
    conn: AsyncConnection, job_id: str, attempt_token: str, *, lock: bool = False
) -> ReservationRecord | None:
    query = select(image_reservations).where(
        image_reservations.c.job_id == job_id,
        image_reservations.c.attempt_token == attempt_token,
    )
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _record(row)


async def records(conn: AsyncConnection, job_id: str) -> list[ReservationRecord]:
    """The job's records, oldest first."""
    rows = await conn.execute(
        select(image_reservations)
        .where(image_reservations.c.job_id == job_id)
        .order_by(image_reservations.c.created_at, image_reservations.c.attempt_token)
    )
    return [_record(r) for r in rows]


async def _apply(
    conn: AsyncConnection,
    keys: Iterable[CounterKey],
    deltas: Deltas,
    *,
    at: datetime | None,
) -> None:
    """Add ``deltas`` to each counter in ``keys`` (in lock order); ``at`` stamps
    ``last_record_at`` (``None``: left as it is, the reconciler's fix)."""
    if not deltas:
        return
    c = image_limit_counters.c
    values: dict[str, Any] = {
        "reserved": c.reserved + deltas.reserved,
        "used": c.used + deltas.used,
        "overage": c.overage + deltas.overage,
        "extra_outputs": c.extra_outputs + deltas.extra_outputs,
    }
    if at is not None:
        values["last_record_at"] = at
    for key in sorted(keys, key=lambda k: k.sort_key):
        await conn.execute(
            update(image_limit_counters)
            .where(
                c.scope == key.scope.value,
                c.scope_key == key.scope_key,
                c.period == key.period,
            )
            .values(**values)
        )


# -- reserve, settle, release ---------------------------------------------------------------


async def reserve(
    conn: AsyncConnection,
    job: ImageJob,
    attempt_token: str,
    k: int,
    *,
    settings: ImageJobSettings | None = None,
) -> ReservationRecord:
    """Reserve ``k`` outputs for the job's attempt ``attempt_token`` against every applicable
    limit (module docstring), in ``conn``'s transaction: the ``held`` record. Raises
    ``ImagesDisabled`` (422) or ``ImageLimitReached`` (409), having written nothing the caller
    should keep (its transaction or savepoint rolls the upserts back). ``k ≥ 1``."""
    if k < 1:
        raise ValueError("a reservation reserves at least one output")
    # #267: a counter change for a fenced attempt token is refused (``FencedToken``).
    await store.require_token(conn, job.id, attempt_token)
    found = settings if settings is not None else await read_section(conn, ImageJobSettings)
    tz = zone(found)
    now = clock.now()
    period = period_of(now, tz)
    keys = counter_keys(job, period)
    limits = [limit_for(key.scope, found) for key in keys]
    for key, limit in zip(keys, limits, strict=True):
        if limit == 0:
            raise ImagesDisabled(
                f"image generation is disabled at the {key.scope.value} scope",
                {"scope": key.scope.value},
            )
    c = image_limit_counters.c
    for key, limit in zip(keys, limits, strict=True):
        await conn.execute(
            pg_insert(image_limit_counters)
            .values(
                scope=key.scope.value,
                scope_key=key.scope_key,
                period=key.period,
                limit_value=limit,
            )
            .on_conflict_do_nothing(index_elements=["scope", "scope_key", "period"])
        )
        row = (
            await conn.execute(
                select(c.reserved, c.used)
                .where(
                    c.scope == key.scope.value,
                    c.scope_key == key.scope_key,
                    c.period == key.period,
                )
                .with_for_update()
            )
        ).one()
        used, reserved = int(row.used), int(row.reserved)
        if used + reserved + k > limit:
            raise ImageLimitReached(
                f"the {key.scope.value} image limit for {key.period.isoformat()} is reached",
                {
                    "scope": key.scope.value,
                    "limit": limit,
                    "used": used,
                    "reserved": reserved,
                    "requested": k,
                    "period": key.period.isoformat(),
                    "resets_at": resets_at(key.period, tz).isoformat(),
                },
            )
    story_id, entry_kind, entry_id = attribution(job)
    await conn.execute(
        insert(image_reservations).values(
            job_id=job.id,
            attempt_token=attempt_token,
            send_state=SendState.PREPARED.value,
            created_at=now,
            story_id=story_id,
            entry_kind=entry_kind,
            entry_id=entry_id,
            counter_keys=[key.as_json() for key in keys],
            requested=k,
            used=0,
            overage=0,
            extra_outputs=0,
            basis=SettlementBasis.ACTUAL.value,
            state=ReservationState.HELD.value,
        )
    )
    for key, limit in zip(keys, limits, strict=True):
        await conn.execute(
            update(image_limit_counters)
            .where(
                c.scope == key.scope.value,
                c.scope_key == key.scope_key,
                c.period == key.period,
            )
            .values(reserved=c.reserved + k, limit_value=limit, last_record_at=now)
        )
    record = await read_record(conn, job.id, attempt_token)
    assert record is not None  # noqa: S101 - inserted above
    return record


async def insert_unreserved_record(
    conn: AsyncConnection, job: ImageJob, attempt_token: str
) -> ReservationRecord:
    """An attempt row that reserves nothing (``requested = 0``, no ``counter_keys``): #268's
    idempotent re-send, whose outputs count on the original attempt's record. It takes no
    counter lock and applies no delta."""
    story_id, entry_kind, entry_id = attribution(job)
    await conn.execute(
        insert(image_reservations).values(
            job_id=job.id,
            attempt_token=attempt_token,
            send_state=SendState.PREPARED.value,
            created_at=clock.now(),
            story_id=story_id,
            entry_kind=entry_kind,
            entry_id=entry_id,
            counter_keys=[],
            requested=0,
            basis=SettlementBasis.ACTUAL.value,
            state=ReservationState.HELD.value,
        )
    )
    record = await read_record(conn, job.id, attempt_token)
    assert record is not None  # noqa: S101 - inserted above
    return record


async def settle(
    conn: AsyncConnection,
    record: ReservationRecord,
    m: int,
    *,
    basis: SettlementBasis = SettlementBasis.ACTUAL,
) -> ReservationRecord:
    """Settle a ``held`` record with ``m`` returned outputs, in ``conn``'s transaction:
    ``used = m``, ``overage = m - min(m, requested)``, ``extra_outputs = max(0, m -
    requested)``, state ``settled``, and the deltas applied to its snapshot's counters. A
    ``released`` record settles too (#267's late return or adoption: ``overage = m``).
    Raises ``InvalidReservationTransition`` when the record (re-read under lock) is already
    ``settled``."""
    current = await read_record(conn, record.job_id, record.attempt_token, lock=True)
    if current is None:
        raise InvalidReservationTransition("the reservation record does not exist")
    deltas = counter_deltas(current.state, ReservationState.SETTLED, current.requested, m)
    now = clock.now()
    await conn.execute(
        update(image_reservations)
        .where(
            image_reservations.c.job_id == current.job_id,
            image_reservations.c.attempt_token == current.attempt_token,
        )
        .values(
            used=m,
            overage=deltas.overage,
            extra_outputs=deltas.extra_outputs,
            basis=basis.value,
            state=ReservationState.SETTLED.value,
            settled_at=now,
        )
    )
    await _apply(conn, current.counter_keys, deltas, at=now)
    settled = await read_record(conn, current.job_id, current.attempt_token)
    assert settled is not None  # noqa: S101 - locked above
    return settled


async def release(
    conn: AsyncConnection, record: ReservationRecord, *, touch: bool = True
) -> ReservationRecord:
    """Release a ``held`` record (``reserved -= requested`` on its counters), in ``conn``'s
    transaction. With ``touch=False`` (the reconciler's fix) ``last_record_at`` stays as it is.
    Raises ``InvalidReservationTransition`` unless the record is ``held``."""
    current = await read_record(conn, record.job_id, record.attempt_token, lock=True)
    if current is None:
        raise InvalidReservationTransition("the reservation record does not exist")
    deltas = counter_deltas(current.state, ReservationState.RELEASED, current.requested)
    now = clock.now()
    await conn.execute(
        update(image_reservations)
        .where(
            image_reservations.c.job_id == current.job_id,
            image_reservations.c.attempt_token == current.attempt_token,
        )
        .values(state=ReservationState.RELEASED.value)
    )
    await _apply(conn, current.counter_keys, deltas, at=now if touch else None)
    released = await read_record(conn, current.job_id, current.attempt_token)
    assert released is not None  # noqa: S101 - locked above
    return released


async def presume(conn: AsyncConnection, record: ReservationRecord) -> ReservationRecord:
    """Settle an ambiguous send's ``held`` record as **presumed** (#268; FR-IMG-11), in
    ``conn``'s transaction: ``used = requested`` (the outputs count as covered), ``basis =
    presumed``, its cost ``unknown`` and its outcome ``presumed``; ``send_state`` stays
    ``ambiguous``. Raises ``InvalidReservationTransition`` unless the record is ``held``."""
    current = await read_record(conn, record.job_id, record.attempt_token, lock=True)
    if current is None or current.state is not ReservationState.HELD:
        raise InvalidReservationTransition("only a held record is presumed")
    settled = await settle(conn, current, current.requested, basis=SettlementBasis.PRESUMED)
    found = await store.read_reservation(conn, current.job_id, current.attempt_token)
    applied = dict((found.applied_parameters if found is not None else None) or {})
    applied["cost"] = "unknown"
    await store.update_reservation(
        conn,
        current.job_id,
        current.attempt_token,
        applied_parameters=applied,
        outcome="presumed",
    )
    return settled


async def correct(conn: AsyncConnection, record: ReservationRecord, m: int) -> ReservationRecord:
    """The one correction of a presumed settlement (#268; FR-IMG-11), in ``conn``'s
    transaction: the record becomes what a ``held`` settlement with ``m`` outputs would have
    recorded (``used = m``, ``overage = extra_outputs = max(0, m - requested)``), ``basis =
    corrected``, and its snapshot's counters move by the difference (``reserved`` untouched).
    Raises ``InvalidReservationTransition`` unless the record (re-read under lock) is a
    ``settled`` one with ``basis = presumed``."""
    if m < 0:
        raise ValueError("a settlement returns at least 0 outputs")
    current = await read_record(conn, record.job_id, record.attempt_token, lock=True)
    if (
        current is None
        or current.state is not ReservationState.SETTLED
        or current.basis is not SettlementBasis.PRESUMED
    ):
        raise InvalidReservationTransition("only a presumed settlement is corrected")
    excess = max(0, m - current.requested)
    deltas = Deltas(
        used=m - current.used,
        overage=excess - current.overage,
        extra_outputs=excess - current.extra_outputs,
    )
    now = clock.now()
    await conn.execute(
        update(image_reservations)
        .where(
            image_reservations.c.job_id == current.job_id,
            image_reservations.c.attempt_token == current.attempt_token,
        )
        .values(
            used=m,
            overage=excess,
            extra_outputs=excess,
            basis=SettlementBasis.CORRECTED.value,
        )
    )
    await _apply(conn, current.counter_keys, deltas, at=now)
    corrected = await read_record(conn, current.job_id, current.attempt_token)
    assert corrected is not None  # noqa: S101 - locked above
    return corrected


async def release_all(
    conn: AsyncConnection, job_id: str, *, current_token: str | None = None
) -> int:
    """Release every record the job still holds (any ending, #256's terminal transitions, and a
    story's deletion), in ``conn``'s transaction; how many. With ``current_token`` (the job's
    token before the ending), a record of another token that is still in flight before its
    horizon is kept: a lease expiry fenced it (#267), and its horizon timer releases it, so a
    late return still settles inside its reservation. An ambiguous send's record (#268) that is
    not kept is presumed (``presume``), never released: the limits err toward generating
    less."""
    now = clock.now()
    held = await conn.execute(
        select(image_reservations)
        .where(
            image_reservations.c.job_id == job_id,
            image_reservations.c.state == ReservationState.HELD.value,
        )
        .order_by(image_reservations.c.created_at, image_reservations.c.attempt_token)
        .with_for_update()
    )
    count = 0
    for row in held.all():
        record = _record(row)
        if current_token is not None and fenced_in_flight(record, current_token, now):
            continue
        if record.send_state is SendState.AMBIGUOUS:
            await presume(conn, record)
        else:
            await release(conn, record)
        count += 1
    return count


def fenced_in_flight(record: ReservationRecord, current_token: str, now: datetime) -> bool:
    """Whether ``record`` is an attempt a lease expiry fenced that may still return before its
    horizon (#267): another token than the job's current one, sent (an ambiguous send, #268,
    included), and its horizon ahead."""
    return (
        record.attempt_token != current_token
        and record.send_state in (SendState.SENDING, SendState.ACCEPTED, SendState.AMBIGUOUS)
        and record.horizon_at is not None
        and record.horizon_at > now
    )


async def release_for_story(conn: AsyncConnection, story_id: str) -> int:
    """A story's deletion (#262, decision 13): release every record its image jobs hold, then
    null ``story_id`` on all its records (they are deployment accounting and are kept; the
    counters and ``counter_keys`` are untouched). The caller has set
    ``epistrel.allow_story_delete`` to the story, which the immutability trigger reads."""
    job_ids = (
        await conn.scalars(select(image_jobs.c.id).where(image_jobs.c.story_id == story_id))
    ).all()
    released = 0
    for job_id in job_ids:
        released += await release_all(conn, str(job_id))
    await conn.execute(
        update(image_reservations)
        .where(image_reservations.c.story_id == story_id)
        .values(story_id=None)
    )
    return released


# -- reconcile ------------------------------------------------------------------------------

_SUMS = text(
    """
    SELECT k->>'scope' AS scope, k->>'scope_key' AS scope_key, (k->>'period')::date AS period,
        coalesce(sum(r.requested) FILTER (WHERE r.state = 'held'), 0) AS reserved,
        coalesce(sum(r.used) FILTER (WHERE r.state = 'settled'), 0) AS used,
        coalesce(sum(r.overage) FILTER (WHERE r.state = 'settled'), 0) AS overage,
        coalesce(sum(r.extra_outputs) FILTER (WHERE r.state = 'settled'), 0) AS extra_outputs
    FROM image_reservations AS r, jsonb_array_elements(r.counter_keys) AS k
    GROUP BY 1, 2, 3
    """
)
COUNTS = ("reserved", "used", "overage", "extra_outputs")


@dataclass(frozen=True, slots=True)
class CounterDrift:
    """One counter whose stored sums differ from its records' (a missing row or one no record
    names compares with zeros)."""

    key: CounterKey
    expected: dict[str, int]
    stored: dict[str, int] | None

    def as_json(self) -> dict[str, Any]:
        return {**self.key.as_json(), "expected": self.expected, "stored": self.stored}


@dataclass(frozen=True, slots=True)
class StuckRecord:
    """A ``held`` record whose job is terminal or missing."""

    job_id: str
    attempt_token: str
    job_status: str | None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one reconcile found (ids, keys and counts only)."""

    rows: int
    drift: tuple[CounterDrift, ...]
    stuck: tuple[StuckRecord, ...]

    @property
    def clean(self) -> bool:
        return not self.drift and not self.stuck

    def as_json(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.clean else "drift",
            "rows": self.rows,
            "drifted": [d.as_json() for d in self.drift],
            "stuck_held": [
                {"job_id": s.job_id, "attempt_token": s.attempt_token} for s in self.stuck
            ],
        }


async def _expected(conn: AsyncConnection) -> dict[CounterKey, dict[str, int]]:
    return {
        CounterKey(LimitScope(r.scope), str(r.scope_key), r.period): {
            name: int(getattr(r, name)) for name in COUNTS
        }
        for r in await conn.execute(_SUMS)
    }


async def _stored(conn: AsyncConnection) -> dict[CounterKey, dict[str, int]]:
    c = image_limit_counters.c
    rows = await conn.execute(
        select(c.scope, c.scope_key, c.period, c.reserved, c.used, c.overage, c.extra_outputs)
    )
    return {
        CounterKey(LimitScope(r.scope), str(r.scope_key), r.period): {
            name: int(getattr(r, name)) for name in COUNTS
        }
        for r in rows
    }


async def _stuck(conn: AsyncConnection) -> list[StuckRecord]:
    """``held`` records whose job is terminal or missing; a record held to its horizon (#267:
    sent, its horizon still ahead) is not stuck until then."""
    r, j = image_reservations, image_jobs
    rows = await conn.execute(
        select(r.c.job_id, r.c.attempt_token, j.c.status)
        .select_from(r.outerjoin(j, j.c.id == r.c.job_id))
        .where(
            r.c.state == ReservationState.HELD.value,
            (j.c.id.is_(None)) | j.c.status.in_([s.value for s in TERMINAL_STATUSES]),
            r.c.horizon_at.is_(None) | (r.c.horizon_at <= clock.now()),
        )
        .order_by(r.c.job_id, r.c.attempt_token)
    )
    return [StuckRecord(str(x.job_id), str(x.attempt_token), x.status) for x in rows]


async def compare(conn: AsyncConnection) -> ReconcileReport:
    """Every counter against its records' sums, and the stuck ``held`` records, in ``conn``'s
    snapshot (the caller runs it under REPEATABLE READ)."""
    expected, stored = await _expected(conn), await _stored(conn)
    zeros = dict.fromkeys(COUNTS, 0)
    drift: list[CounterDrift] = []
    for key in sorted(set(expected) | set(stored), key=lambda k: k.sort_key):
        want = expected.get(key, zeros)
        have = stored.get(key)
        if have is None and want == zeros:
            continue
        if have != want:
            drift.append(CounterDrift(key, dict(want), None if have is None else dict(have)))
    return ReconcileReport(len(stored), tuple(drift), tuple(await _stuck(conn)))


async def reconcile(db: AsyncEngine) -> ReconcileReport:
    """``compare`` in one REPEATABLE READ snapshot; never writes."""
    async with db.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            return await compare(conn)


async def fix_counters(conn: AsyncConnection) -> dict[str, int]:
    """Release each stuck ``held`` record through ``release``, then rewrite every drifted
    counter to its records' sums, creating missing rows in lock order (limit value: the current
    setting), in ``conn``'s transaction. ``last_record_at`` is never touched. Counts per scope
    (``stuck_held``, ``deployment``, ``entry``)."""
    counts = {STUCK_HELD: 0, **{scope.value: 0 for scope in LOCK_ORDER}}
    for stuck in await _stuck(conn):
        record = await read_record(conn, stuck.job_id, stuck.attempt_token)
        if record is not None and record.state is ReservationState.HELD:
            await release(conn, record, touch=False)
            counts[STUCK_HELD] += 1
    settings = await read_section(conn, ImageJobSettings)
    report = await compare(conn)
    c = image_limit_counters.c
    for drifted in report.drift:
        key = drifted.key
        await conn.execute(
            pg_insert(image_limit_counters)
            .values(
                scope=key.scope.value,
                scope_key=key.scope_key,
                period=key.period,
                limit_value=limit_for(key.scope, settings),
            )
            .on_conflict_do_nothing(index_elements=["scope", "scope_key", "period"])
        )
        await conn.execute(
            update(image_limit_counters)
            .where(
                c.scope == key.scope.value,
                c.scope_key == key.scope_key,
                c.period == key.period,
            )
            .values(**drifted.expected)
        )
        counts[key.scope.value] += 1
    return counts


# -- retention ------------------------------------------------------------------------------


def retention_cutoff(settings: ImageJobSettings, *, now: datetime | None = None) -> date:
    """Periods before this local date are closed and past ``image_accounting_retention_days``
    (a period closes at its next local midnight; today counts in the current zone)."""
    today = period_of(now or clock.now(), zone(settings))
    return today - timedelta(days=settings.image_accounting_retention_days)


async def retain(db: AsyncEngine, *, now: datetime | None = None) -> dict[str, int]:
    """Delete the accounting of periods before ``retention_cutoff``: first the records that are
    settled or released, then the counters no record names any more. Each batch is up to
    ``RETENTION_BATCH`` rows, in its own transaction. Counts only."""
    settings = await read_section(db, ImageJobSettings)
    cutoff = retention_cutoff(settings, now=now)
    tz = settings.deployment_time_zone
    removed = {"records": 0, "counters": 0}
    record_period = func.coalesce(
        func.cast(image_reservations.c.counter_keys[0]["period"].astext, Date()),
        func.cast(func.timezone(tz, image_reservations.c.created_at), Date()),
    )
    while True:
        async with db.begin() as conn:
            batch = (
                select(image_reservations.c.job_id, image_reservations.c.attempt_token)
                .where(
                    image_reservations.c.state != ReservationState.HELD.value,
                    record_period < cutoff,
                )
                .limit(RETENTION_BATCH)
                .subquery()
            )
            result = await conn.execute(
                image_reservations.delete().where(
                    and_(
                        image_reservations.c.job_id == batch.c.job_id,
                        image_reservations.c.attempt_token == batch.c.attempt_token,
                    )
                )
            )
        removed["records"] += result.rowcount
        if result.rowcount < RETENTION_BATCH:
            break
    c = image_limit_counters.c
    named = (
        select(image_reservations.c.job_id)
        .where(
            image_reservations.c.counter_keys.contains(
                func.jsonb_build_array(
                    func.jsonb_build_object(
                        "scope",
                        c.scope,
                        "scope_key",
                        c.scope_key,
                        "period",
                        func.to_char(c.period, "YYYY-MM-DD"),
                    )
                )
            )
        )
        .exists()
    )
    while True:
        async with db.begin() as conn:
            batch = (
                select(c.scope, c.scope_key, c.period)
                .where(c.period < cutoff, ~named)
                .limit(RETENTION_BATCH)
                .subquery()
            )
            result = await conn.execute(
                image_limit_counters.delete().where(
                    and_(
                        c.scope == batch.c.scope,
                        c.scope_key == batch.c.scope_key,
                        c.period == batch.c.period,
                    )
                )
            )
        removed["counters"] += result.rowcount
        if result.rowcount < RETENTION_BATCH:
            break
    return removed


__all__ = [
    "COUNTS",
    "DEPLOYMENT_KEY",
    "LOCK_ORDER",
    "RETENTION_BATCH",
    "STUCK_HELD",
    "CounterDrift",
    "CounterKey",
    "Deltas",
    "InvalidReservationTransition",
    "ReconcileReport",
    "ReservationRecord",
    "StuckRecord",
    "attribution",
    "compare",
    "counter_deltas",
    "counter_keys",
    "current_period",
    "entry_key",
    "fenced_in_flight",
    "fix_counters",
    "insert_unreserved_record",
    "limit_for",
    "period_of",
    "read_record",
    "reconcile",
    "records",
    "release",
    "release_all",
    "release_for_story",
    "reserve",
    "resets_at",
    "retain",
    "retention_cutoff",
    "settle",
    "zone",
]
