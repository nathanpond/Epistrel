"""Image costs: one ``image_costs`` row per provider attempt, and the usage reports built on them
(FR-IMG-11, part D; docs/02 §4.13; #269).

**What is recorded.** Every provider attempt that reached the provider gets one row, unique on
``(job_id, attempt_token)``, written in the transaction that resolves the attempt: its return
(billed outputs, whatever became of them), an adapter error, its release at the horizon, its
presumption (#268) and every late return on the settlement path (#267). An idempotent re-send's
cost lands on the attempt it re-sent (#268), so it is counted once. An attempt that never
reached the provider (``prepared``) gets no row.

- ``known``: the provider reported an amount in a currency. The amount is rounded half-even to 8
  decimals (``numeric(18,8)``); the currency is upper-cased ISO 4217.
- ``zero``: the provider refused the request before generating (HTTP 400, 401, 402, 403, 413 or
  429), so nothing was billed: cost 0, no currency, ``outputs_billed = 0``.
- ``unknown``: anything else without an amount (a 5xx, a timeout, an ambiguous send, a horizon
  release, a success without a cost). It is counted, never summed as 0, and nothing is
  estimated from the endpoint's pricing.

``record_attempt`` inserts with ``ON CONFLICT DO NOTHING``, then makes the one fill-in: an
``unknown`` row becomes ``known`` or ``zero`` once (``filled_at``) and keeps its ``period_date``;
a row that is already known or zero never changes again. ``outputs_billed`` is the outputs the
provider returned, null while nobody knows (a presumption, a horizon release) until a return
fills it. ``period_date`` is the deployment counter's period in the reservation record's
``counter_keys`` (the day the attempt was reserved on, so a cost settled after local midnight
lands on its reservation's day). ``record_late_unaccounted`` writes the row for a late return
whose reservation record accounting retention purged (``accounting = late_unaccounted``, on
``recorded_at``'s local day, attributed from the job if it still exists).

**Reports.** Deployment and entry usage (``outputs_used``, ``overage``, ``extra_outputs``,
``reserved`` = currently held) come from ``image_limit_counters``; a story's from its
``image_reservations`` by ``story_id`` (there are no story counters). ``ambiguous_sends`` counts
the records whose ``send_state`` is ``ambiguous`` or whose ``basis`` is ``presumed`` or
``corrected``, by their reservation day. Costs come from ``image_costs``: per currency (never
mixed or converted) the ``known_total`` and ``known_attempts``, and the counts of ``unknown`` and
``zero`` attempts. Money is a ``Decimal`` at the stored precision.

The rows are deployment accounting with no story content: they survive erasure, principal
offboarding and the story's deletion (``story_id`` nulled, ``core.stories.STORY_ID_EXCEPTIONS``)
and are purged by ``period_date`` with the accounting retention (``purge``). Logs carry ids,
statuses and the accounting only (invariant 3): ``image_cost_recorded``, ``image_cost_filled``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Date, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from epistrel.core import clock
from epistrel.core.config_changes import read_section
from epistrel.core.errors import InvalidRange
from epistrel.core.ids import new_ulid
from epistrel.core.images import limits
from epistrel.core.images.limits import ReservationRecord
from epistrel.core.settings_schema import ImageJobSettings
from epistrel.core.tables.images import (
    CostAccounting,
    CostStatus,
    LimitScope,
    ReservationState,
    SendState,
    SettlementBasis,
    image_costs,
    image_jobs,
    image_limit_counters,
    image_reservations,
)

logger = logging.getLogger(__name__)

ZERO_HTTP_STATUSES: frozenset[int] = frozenset({400, 401, 402, 403, 413, 429})
"""Provider answers that refuse a request before anything is generated: recorded at ``zero``."""
QUANTUM = Decimal("0.00000001")
MAX_AMOUNT = Decimal("10000000000")
"""``numeric(18,8)`` holds less than 10^10; a larger amount is recorded ``unknown``."""
RANGE_DEFAULT_DAYS = 30
RANGE_MAX_DAYS = 366
PURGE_BATCH = limits.RETENTION_BATCH


# -- what is recorded -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostEntry:
    """One attempt's cost as recorded: its status, and for ``known`` the amount and currency."""

    status: CostStatus
    amount: Decimal | None = None
    currency: str | None = None


UNKNOWN = CostEntry(CostStatus.UNKNOWN)
ZERO = CostEntry(CostStatus.ZERO, Decimal(0))


def known(amount: Any, currency: Any) -> CostEntry:
    """A provider-reported cost, rounded half-even to 8 decimals with its currency upper-cased;
    ``UNKNOWN`` when either is unusable (not a number, negative, too large for the column, or
    not a three-letter currency)."""
    try:
        value = Decimal(str(amount))
    except InvalidOperation, ValueError:
        return UNKNOWN
    if not value.is_finite() or value < 0:
        return UNKNOWN
    rounded = value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
    code = str(currency).strip().upper() if isinstance(currency, str) else ""
    if rounded >= MAX_AMOUNT or len(code) != 3 or not code.isascii() or not code.isalpha():
        return UNKNOWN
    return CostEntry(CostStatus.KNOWN, rounded, code)


def money(value: Decimal) -> str:
    """A cost as a decimal string at the stored precision (``0.04000000``), never in exponent
    form."""
    return format(value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN), "f")


def of_error(status: int | None, *, ambiguous: bool) -> CostEntry:
    """An adapter error's cost: ``zero`` for a refusal before generation
    (``ZERO_HTTP_STATUSES``, never an ambiguous one), ``unknown`` otherwise."""
    if not ambiguous and status in ZERO_HTTP_STATUSES:
        return ZERO
    return UNKNOWN


def _period_of(record: ReservationRecord) -> date | None:
    for key in record.counter_keys:
        if key.scope is LimitScope.DEPLOYMENT:
            return key.period
    return None


async def _today(conn: AsyncConnection) -> date:
    return date.fromisoformat(await limits.current_period(conn))


def _values(entry: CostEntry, outputs_billed: int | None) -> dict[str, Any]:
    if entry.status is CostStatus.ZERO:
        return {"cost": Decimal(0), "currency": None, "outputs_billed": 0}
    if entry.status is CostStatus.KNOWN:
        return {"cost": entry.amount, "currency": entry.currency, "outputs_billed": outputs_billed}
    return {"cost": None, "currency": None, "outputs_billed": outputs_billed}


async def _write(
    conn: AsyncConnection,
    *,
    job_id: str,
    attempt_token: str,
    attribution: tuple[str | None, str | None, str | None],
    entry: CostEntry,
    outputs_billed: int | None,
    accounting: CostAccounting,
    period: date,
) -> str:
    """Insert, or fill in once; ``recorded``, ``filled`` or ``unchanged``."""
    now = clock.now()
    story_id, entry_kind, entry_id = attribution
    values = _values(entry, outputs_billed)
    inserted = await conn.execute(
        pg_insert(image_costs)
        .values(
            id=new_ulid(),
            job_id=job_id,
            attempt_token=attempt_token,
            story_id=story_id,
            entry_kind=entry_kind,
            entry_id=entry_id,
            cost_status=entry.status.value,
            accounting=accounting.value,
            period_date=period,
            recorded_at=now,
            **values,
        )
        .on_conflict_do_nothing(index_elements=["job_id", "attempt_token"])
        .returning(image_costs.c.id)
    )
    c = image_costs.c
    if inserted.first() is not None:
        logger.info(
            "image job %s attempt %s: image_cost_recorded %s %s",
            job_id,
            attempt_token,
            entry.status.value,
            accounting.value,
        )
        return "recorded"
    where = and_(c.job_id == job_id, c.attempt_token == attempt_token)
    if entry.status is not CostStatus.UNKNOWN:
        filled = await conn.execute(
            update(image_costs)
            .where(where, c.cost_status == CostStatus.UNKNOWN.value)
            .values(
                cost=values["cost"],
                currency=values["currency"],
                cost_status=entry.status.value,
                outputs_billed=(
                    0
                    if entry.status is CostStatus.ZERO
                    else func.coalesce(c.outputs_billed, outputs_billed)
                ),
                filled_at=now,
            )
        )
        if filled.rowcount:
            logger.info(
                "image job %s attempt %s: image_cost_filled %s %s",
                job_id,
                attempt_token,
                entry.status.value,
                accounting.value,
            )
            return "filled"
    elif outputs_billed is not None:
        # The cost stays unknown, but the outputs the provider returned are now known.
        await conn.execute(
            update(image_costs)
            .where(where, c.outputs_billed.is_(None), c.cost_status == CostStatus.UNKNOWN.value)
            .values(outputs_billed=outputs_billed)
        )
    logger.info(
        "image job %s attempt %s: image cost unchanged (%s reported)",
        job_id,
        attempt_token,
        entry.status.value,
    )
    return "unchanged"


async def record_attempt(
    conn: AsyncConnection,
    record: ReservationRecord,
    entry: CostEntry,
    outputs_billed: int | None,
    *,
    accounting: CostAccounting = CostAccounting.NORMAL,
) -> str:
    """Record the cost of the attempt whose reservation ``record`` this is, in ``conn``'s
    transaction (module docstring): ``recorded`` (a new row), ``filled`` (``unknown`` became
    known or zero) or ``unchanged``. Attribution comes from the record's columns and the day
    from its ``counter_keys``."""
    period = _period_of(record)
    if period is None:  # an unreserved row (#268): its own creation day
        tz = limits.zone(await read_section(conn, ImageJobSettings))
        period = limits.period_of(record.created_at, tz)
    return await _write(
        conn,
        job_id=record.job_id,
        attempt_token=record.attempt_token,
        attribution=(record.story_id, record.entry_kind, record.entry_id),
        entry=entry,
        outputs_billed=outputs_billed,
        accounting=accounting,
        period=period,
    )


async def record_late_unaccounted(
    conn: AsyncConnection,
    job_id: str,
    attempt_token: str,
    entry: CostEntry,
    outputs_billed: int | None,
) -> str:
    """A late return whose reservation record accounting retention purged (#268's
    ``late_unaccounted``): a fresh row on ``recorded_at``'s local day, attributed from
    ``image_jobs`` while the job exists (story and entry null otherwise). A row the purge left
    is filled in as ``record_attempt`` would."""
    job = (
        await conn.execute(
            select(
                image_jobs.c.kind, image_jobs.c.story_id, image_jobs.c.origin, image_jobs.c.target
            ).where(image_jobs.c.id == job_id)
        )
    ).one_or_none()
    attribution: tuple[str | None, str | None, str | None] = (None, None, None)
    if job is not None:
        if job.kind == "catalog":
            origin, target = dict(job.origin or {}), dict(job.target or {})
            kind = origin.get("entry_kind") or target.get("entry_kind")
            entry_id = origin.get("entry_id") or target.get("entry_id")
            attribution = (
                None,
                None if kind is None else str(kind),
                None if entry_id is None else str(entry_id),
            )
        else:
            attribution = (None if job.story_id is None else str(job.story_id), None, None)
    return await _write(
        conn,
        job_id=job_id,
        attempt_token=attempt_token,
        attribution=attribution,
        entry=entry,
        outputs_billed=outputs_billed,
        accounting=CostAccounting.LATE_UNACCOUNTED,
        period=await _today(conn),
    )


async def record_unknown_if_sent(conn: AsyncConnection, record: ReservationRecord) -> None:
    """An attempt released while its call was in flight (an ending, a cancel): cost
    ``unknown`` with no outputs known, which a later return fills in."""
    if record.send_state in (SendState.SENDING, SendState.ACCEPTED, SendState.AMBIGUOUS):
        await record_attempt(conn, record, UNKNOWN, None)


# -- reading --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttemptCostRow:
    """One attempt's ``image_costs`` row as a job read reports it."""

    cost: Decimal | None
    cost_status: CostStatus
    currency: str | None
    outputs_billed: int | None
    accounting: CostAccounting


async def attempt_costs(conn: AsyncConnection, job_id: str) -> dict[str, AttemptCostRow]:
    """The job's cost rows by attempt token."""
    c = image_costs.c
    rows = await conn.execute(
        select(
            c.attempt_token, c.cost, c.cost_status, c.currency, c.outputs_billed, c.accounting
        ).where(c.job_id == job_id)
    )
    return {
        str(r.attempt_token): AttemptCostRow(
            cost=r.cost,
            cost_status=CostStatus(r.cost_status),
            currency=r.currency,
            outputs_billed=r.outputs_billed,
            accounting=CostAccounting(r.accounting),
        )
        for r in rows
    }


@dataclass(frozen=True, slots=True)
class CurrencyTotal:
    """Known costs in one currency: their sum and how many attempts they are."""

    currency: str
    known_total: Decimal
    known_attempts: int


@dataclass(frozen=True, slots=True)
class CostTotals:
    """Costs per currency (never mixed or converted), and the ``unknown`` and ``zero``
    attempts counted apart."""

    costs: tuple[CurrencyTotal, ...] = ()
    unknown_attempts: int = 0
    zero_attempts: int = 0


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """One report's totals (``ImageUsageTotals``): outputs used, overage, extra outputs,
    currently reserved, ambiguous sends, and the costs."""

    outputs_used: int = 0
    overage: int = 0
    extra_outputs: int = 0
    reserved: int = 0
    ambiguous_sends: int = 0
    cost: CostTotals = field(default_factory=CostTotals)

    @property
    def active(self) -> bool:
        c = self.cost
        return bool(
            self.outputs_used
            or self.overage
            or self.extra_outputs
            or self.reserved
            or self.ambiguous_sends
            or c.costs
            or c.unknown_attempts
            or c.zero_attempts
        )


@dataclass(frozen=True, slots=True)
class UsageDay:
    day: date
    totals: UsageTotals


@dataclass(frozen=True, slots=True)
class DeploymentUsage:
    """``GET /admin/image-usage``: every retained day of the range, ascending, and its totals."""

    from_date: date
    to_date: date
    retained_from: date
    days: tuple[UsageDay, ...]
    totals: UsageTotals


@dataclass(frozen=True, slots=True)
class EntryLimits:
    image_limit_entry_day: int
    today_used: int
    today_reserved: int
    today_remaining: int


@dataclass(frozen=True, slots=True)
class ScopedUsage:
    """A story's or a catalog entry's usage: all-time totals and the active days of the range."""

    from_date: date
    to_date: date
    retained_from: date
    all_time: UsageTotals
    days: tuple[UsageDay, ...]
    limits: EntryLimits | None = None


def _merge_costs(rows: Iterable[Any]) -> CostTotals:
    per: dict[str, tuple[Decimal, int]] = {}
    unknown = zero = 0
    for r in rows:
        status = CostStatus(r.cost_status)
        if status is CostStatus.UNKNOWN:
            unknown += int(r.attempts)
        elif status is CostStatus.ZERO:
            zero += int(r.attempts)
        else:
            total, n = per.get(str(r.currency), (Decimal(0), 0))
            per[str(r.currency)] = (total + Decimal(r.total or 0), n + int(r.attempts))
    return CostTotals(
        costs=tuple(CurrencyTotal(cur, t, n) for cur, (t, n) in sorted(per.items())),
        unknown_attempts=unknown,
        zero_attempts=zero,
    )


def _add(a: UsageTotals, b: UsageTotals) -> UsageTotals:
    per: dict[str, tuple[Decimal, int]] = {}
    for item in (*a.cost.costs, *b.cost.costs):
        total, n = per.get(item.currency, (Decimal(0), 0))
        per[item.currency] = (total + item.known_total, n + item.known_attempts)
    return UsageTotals(
        outputs_used=a.outputs_used + b.outputs_used,
        overage=a.overage + b.overage,
        extra_outputs=a.extra_outputs + b.extra_outputs,
        reserved=a.reserved + b.reserved,
        ambiguous_sends=a.ambiguous_sends + b.ambiguous_sends,
        cost=CostTotals(
            costs=tuple(CurrencyTotal(cur, t, n) for cur, (t, n) in sorted(per.items())),
            unknown_attempts=a.cost.unknown_attempts + b.cost.unknown_attempts,
            zero_attempts=a.cost.zero_attempts + b.cost.zero_attempts,
        ),
    )


def total_of(days: Iterable[UsageTotals]) -> UsageTotals:
    found = UsageTotals()
    for day in days:
        found = _add(found, day)
    return found


_RECORD_DAY = func.cast(image_reservations.c.counter_keys[0]["period"].astext, Date())
_AMBIGUOUS = (image_reservations.c.send_state == SendState.AMBIGUOUS.value) | (
    image_reservations.c.basis.in_(
        [SettlementBasis.PRESUMED.value, SettlementBasis.CORRECTED.value]
    )
)


async def _counter_days(
    conn: AsyncConnection, scope: LimitScope, scope_key: str
) -> dict[date, UsageTotals]:
    c = image_limit_counters.c
    rows = await conn.execute(
        select(c.period, c.used, c.overage, c.extra_outputs, c.reserved).where(
            c.scope == scope.value, c.scope_key == scope_key
        )
    )
    return {
        r.period: UsageTotals(
            outputs_used=int(r.used),
            overage=int(r.overage),
            extra_outputs=int(r.extra_outputs),
            reserved=int(r.reserved),
        )
        for r in rows
    }


async def _record_days(
    conn: AsyncConnection, where: ColumnElement[bool]
) -> dict[date, UsageTotals]:
    r = image_reservations.c
    settled = r.state == ReservationState.SETTLED.value
    rows = await conn.execute(
        select(
            _RECORD_DAY.label("day"),
            func.coalesce(func.sum(r.used).filter(settled), 0).label("used"),
            func.coalesce(func.sum(r.overage).filter(settled), 0).label("overage"),
            func.coalesce(func.sum(r.extra_outputs).filter(settled), 0).label("extra"),
            func.coalesce(
                func.sum(r.requested).filter(r.state == ReservationState.HELD.value), 0
            ).label("reserved"),
        )
        .where(where, func.jsonb_array_length(r.counter_keys) > 0)
        .group_by(_RECORD_DAY)
    )
    return {
        row.day: UsageTotals(
            outputs_used=int(row.used),
            overage=int(row.overage),
            extra_outputs=int(row.extra),
            reserved=int(row.reserved),
        )
        for row in rows
    }


async def _ambiguous_days(conn: AsyncConnection, where: ColumnElement[bool]) -> dict[date, int]:
    r = image_reservations.c
    rows = await conn.execute(
        select(_RECORD_DAY.label("day"), func.count().label("n"))
        .where(where, _AMBIGUOUS, func.jsonb_array_length(r.counter_keys) > 0)
        .group_by(_RECORD_DAY)
    )
    return {row.day: int(row.n) for row in rows}


async def _cost_days(conn: AsyncConnection, where: ColumnElement[bool]) -> dict[date, CostTotals]:
    c = image_costs.c
    rows = await conn.execute(
        select(
            c.period_date,
            c.cost_status,
            c.currency,
            func.sum(c.cost).label("total"),
            func.count().label("attempts"),
        )
        .where(where)
        .group_by(c.period_date, c.cost_status, c.currency)
    )
    grouped: dict[date, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.period_date, []).append(row)
    return {day: _merge_costs(found) for day, found in grouped.items()}


def _combine(
    usage: Mapping[date, UsageTotals],
    ambiguous: Mapping[date, int],
    costs: Mapping[date, CostTotals],
) -> dict[date, UsageTotals]:
    days = set(usage) | set(ambiguous) | set(costs)
    return {
        day: replace(
            usage.get(day, UsageTotals()),
            ambiguous_sends=ambiguous.get(day, 0),
            cost=costs.get(day, CostTotals()),
        )
        for day in days
    }


def _true() -> ColumnElement[bool]:
    return image_reservations.c.job_id.is_not(None)


async def check_range(
    conn: AsyncConnection, from_date: date | None, to_date: date | None
) -> tuple[date, date]:
    """A report's range: local ISO dates, both inclusive, by default the last 30 days through
    today; 422 ``invalid_range`` when ``from`` is after ``to`` or the range is longer than 366
    days."""
    today = await _today(conn)
    to_date = to_date or (today if from_date is None or from_date <= today else from_date)
    from_date = from_date or to_date - timedelta(days=RANGE_DEFAULT_DAYS - 1)
    details = {"from": from_date.isoformat(), "to": to_date.isoformat()}
    if from_date > to_date:
        raise InvalidRange("from is after to", details)
    if (to_date - from_date).days + 1 > RANGE_MAX_DAYS:
        raise InvalidRange(f"a range covers at most {RANGE_MAX_DAYS} days", details)
    return from_date, to_date


async def retained_from(conn: AsyncConnection) -> date:
    """The first day the accounting retention keeps (``limits.retention_cutoff``)."""
    return limits.retention_cutoff(await read_section(conn, ImageJobSettings))


async def deployment_usage(
    conn: AsyncConnection, from_date: date | None = None, to_date: date | None = None
) -> DeploymentUsage:
    """The deployment's usage and cost per local day of the range (every retained day, zeros
    included), ascending, and the range's totals."""
    start, end = await check_range(conn, from_date, to_date)
    kept = await retained_from(conn)
    found = _combine(
        await _counter_days(conn, LimitScope.DEPLOYMENT, limits.DEPLOYMENT_KEY),
        await _ambiguous_days(conn, _true()),
        await _cost_days(conn, image_costs.c.id.is_not(None)),
    )
    days: list[UsageDay] = []
    day = max(start, kept)
    while day <= end:
        days.append(UsageDay(day, found.get(day, UsageTotals())))
        day += timedelta(days=1)
    return DeploymentUsage(start, end, kept, tuple(days), total_of(d.totals for d in days))


async def _scoped(
    conn: AsyncConnection,
    usage: Mapping[date, UsageTotals],
    record_filter: ColumnElement[bool],
    cost_filter: ColumnElement[bool],
    from_date: date | None,
    to_date: date | None,
) -> tuple[date, date, date, UsageTotals, tuple[UsageDay, ...]]:
    start, end = await check_range(conn, from_date, to_date)
    kept = await retained_from(conn)
    found = _combine(
        usage,
        await _ambiguous_days(conn, record_filter),
        await _cost_days(conn, cost_filter),
    )
    days = tuple(
        UsageDay(day, totals)
        for day, totals in sorted(found.items())
        if start <= day <= end and totals.active
    )
    return start, end, kept, total_of(found.values()), days


async def story_usage(
    conn: AsyncConnection,
    story_id: str,
    from_date: date | None = None,
    to_date: date | None = None,
) -> ScopedUsage:
    """A story's usage and cost from its records and cost rows by ``story_id``: all-time totals
    and the range's active days. There are no story limits (per-story admission is the
    consumer's, #262)."""
    where = image_reservations.c.story_id == story_id
    start, end, kept, all_time, days = await _scoped(
        conn,
        await _record_days(conn, where),
        where,
        image_costs.c.story_id == story_id,
        from_date,
        to_date,
    )
    return ScopedUsage(start, end, kept, all_time, days)


async def entry_usage(
    conn: AsyncConnection,
    entry_kind: str,
    entry_id: str,
    from_date: date | None = None,
    to_date: date | None = None,
) -> ScopedUsage:
    """A catalog entry's usage (its entry counters) and cost (its catalog jobs' cost rows):
    all-time totals, the range's active days, and today's entry limit."""
    r, c = image_reservations.c, image_costs.c
    key = limits.entry_key(entry_kind, entry_id)
    usage = await _counter_days(conn, LimitScope.ENTRY, key)
    start, end, kept, all_time, days = await _scoped(
        conn,
        usage,
        and_(r.entry_kind == entry_kind, r.entry_id == entry_id),
        and_(c.entry_kind == entry_kind, c.entry_id == entry_id),
        from_date,
        to_date,
    )
    settings = await read_section(conn, ImageJobSettings)
    today = usage.get(await _today(conn), UsageTotals())
    limit = settings.image_limit_entry_day
    return ScopedUsage(
        start,
        end,
        kept,
        all_time,
        days,
        EntryLimits(
            image_limit_entry_day=limit,
            today_used=today.outputs_used,
            today_reserved=today.reserved,
            today_remaining=max(0, limit - today.outputs_used - today.reserved),
        ),
    )


@dataclass(frozen=True, slots=True)
class JobUsage:
    """One job's usage (``GET /image-jobs/{id}``'s ``usage``) and cost totals."""

    outputs_used: int
    overage: int
    extra_outputs: int
    ambiguous_sends: int
    cost: CostTotals


async def job_usage(conn: AsyncConnection, job_id: str) -> JobUsage:
    """Summed from the job's records and cost rows, on read."""
    r, c = image_reservations.c, image_costs.c
    settled = r.state == ReservationState.SETTLED.value
    row = (
        await conn.execute(
            select(
                func.coalesce(func.sum(r.used).filter(settled), 0).label("used"),
                func.coalesce(func.sum(r.overage).filter(settled), 0).label("overage"),
                func.coalesce(func.sum(r.extra_outputs).filter(settled), 0).label("extra"),
                func.count().filter(_AMBIGUOUS).label("ambiguous"),
            ).where(r.job_id == job_id)
        )
    ).one()
    cost_rows = await conn.execute(
        select(
            c.cost_status,
            c.currency,
            func.sum(c.cost).label("total"),
            func.count().label("attempts"),
        )
        .where(c.job_id == job_id)
        .group_by(c.cost_status, c.currency)
    )
    return JobUsage(
        outputs_used=int(row.used),
        overage=int(row.overage),
        extra_outputs=int(row.extra),
        ambiguous_sends=int(row.ambiguous),
        cost=_merge_costs(cost_rows),
    )


# -- retention ------------------------------------------------------------------------------


async def purge(db: AsyncEngine, *, now: datetime | None = None) -> int:
    """Delete the cost rows whose ``period_date`` is before the accounting retention's cutoff
    (no state condition), in batches of ``PURGE_BATCH``, each in its own transaction; how
    many."""
    settings = await read_section(db, ImageJobSettings)
    cutoff = limits.retention_cutoff(settings, now=now)
    removed = 0
    c = image_costs.c
    while True:
        async with db.begin() as conn:
            batch = select(c.id).where(c.period_date < cutoff).limit(PURGE_BATCH).subquery()
            result = await conn.execute(image_costs.delete().where(c.id.in_(select(batch.c.id))))
        removed += result.rowcount
        if result.rowcount < PURGE_BATCH:
            return removed


async def detach_story(conn: AsyncConnection, story_id: str) -> None:
    """A story's deletion keeps its cost rows with ``story_id`` nulled (deployment
    accounting)."""
    await conn.execute(
        update(image_costs).where(image_costs.c.story_id == story_id).values(story_id=None)
    )


__all__ = [
    "MAX_AMOUNT",
    "RANGE_DEFAULT_DAYS",
    "RANGE_MAX_DAYS",
    "UNKNOWN",
    "ZERO",
    "ZERO_HTTP_STATUSES",
    "AttemptCostRow",
    "CostEntry",
    "CostTotals",
    "CurrencyTotal",
    "DeploymentUsage",
    "EntryLimits",
    "InvalidRange",
    "JobUsage",
    "ScopedUsage",
    "UsageDay",
    "UsageTotals",
    "attempt_costs",
    "check_range",
    "deployment_usage",
    "detach_story",
    "entry_usage",
    "job_usage",
    "known",
    "money",
    "of_error",
    "purge",
    "record_attempt",
    "record_late_unaccounted",
    "record_unknown_if_sent",
    "retained_from",
    "story_usage",
    "total_of",
]
