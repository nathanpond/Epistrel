"""Escalation yield, the gate-threshold store and the promotion rule (FR-OBS-2,4, NFR-PERF-5;
#155). Model-free: the Orchestrator writes the ``escalations`` rows
(``orchestrator.verify.yields``); this module reads them and keeps ``gate_thresholds``.

**The yield report** (``yield_report``, ``GET /admin/evals/escalation-yield``): over a window
(``from`` inclusive, ``to`` exclusive; the last 30 days by default) of active ``escalations``
rows, per gate reason, per predicate shape and per (gate reason, shape): ``escalations`` (rows
the gate escalated, every verification pass), ``skipped`` (rows a live tightening skipped),
``valid`` (escalations with a valid model verdict), ``flips`` (escalations whose model answer
was taken over the tentative ``uncertain``), the ``flip_rate`` (flips over valid verdicts) and
the ``weighted_flip_rate`` (each flip weighted by its consequence, ``repair_kept`` 1,
``repair_discarded`` 0.25, ``none`` 0, over the same denominator). Rates are rounded to 4
decimals, ``None`` when the denominator is 0. Then the overall ``escalation_rate``: the claims
escalated over every claim verified, from ``turn_report.verify`` (and ``verify_follow_up``) of
the active turns committed in the window, with ``reference_baseline`` 0.2 and ``is_gate``
false (NFR-PERF-5: ~20 % is a reference baseline, not a launch gate).

**The threshold store.** ``put_threshold`` writes a shape's proposal: ``shadow`` keeps the live
gate as it is (a live threshold already in force stays) and starts a new shadow window;
``live`` puts the threshold in force at once. ``promote`` makes the proposal live; ``delete``
removes the shape's row (no tightening). Every write appends a ``gate_threshold_history`` row.
``live_thresholds`` is what #142's gate reads per verify. A shape key is
``layer|cardinality|temporal|polarity`` (``PredicateShape.key()``), written ``~``-separated in a
URL path.

**The promotion rule** (``promotion``): the shadow window holds the shape's escalations since its
``window_start``; promotion needs at least ``gate_promotion_min_cases`` of them, and of those the
tightened gate would have skipped (``shadow_decision`` false), a share of at least
``gate_promotion_min_agreement`` whose skip agreed with the model (``shadow_agreed``). A window
in which the tightening would skip nothing has no agreement (``None``) and is not promotable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import Integer, and_, case, cast, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.config_changes import (
    bump_config_revision,
    lock_deployment,
    read_section,
    read_settings,
    record_config_change,
)
from epistrel.core.enums import OperationKind
from epistrel.core.errors import GateNotPromotable, GateThresholdNotFound, InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_only, active_operation
from epistrel.core.settings_schema import ConfigClass, VerifySettings
from epistrel.core.tables import operations
from epistrel.core.tables.escalations import (
    escalations,
    gate_threshold_history,
    gate_thresholds,
)

DEFAULT_WINDOW = timedelta(days=30)
REFERENCE_BASELINE = 0.2
"""NFR-PERF-5's ~20 % overall escalation rate: a reference, never a gate."""
CONSEQUENCE_WEIGHTS: Mapping[str, float] = {
    "repair_kept": 1.0,
    "repair_discarded": 0.25,
    "none": 0.0,
}
DIGITS = 4
SHAPE_PATTERN = r"^[a-z0-9_]{1,40}(\|[a-z0-9_]{1,40}){3}$"
PATH_SEPARATOR = "~"
SHAPE_SEPARATOR = "|"

Mode = Literal["shadow", "live"]


def check_shape(shape: str, field_name: str = "shape") -> str:
    """``shape`` when it is a shape key (``layer|cardinality|temporal|polarity``), else 422."""
    if not re.fullmatch(SHAPE_PATTERN, shape):
        raise InvalidRequest(
            "a shape is four lower-case parts joined by '|': layer|cardinality|temporal|polarity",
            {"field": field_name, "reason": "invalid_shape"},
        )
    return shape


def shape_of_path(segment: str) -> str:
    """A shape key from its URL form (``|`` written ``~``)."""
    return check_shape(segment.replace(PATH_SEPARATOR, SHAPE_SEPARATOR), "path.shape")


def path_of_shape(shape: str) -> str:
    return shape.replace(SHAPE_SEPARATOR, PATH_SEPARATOR)


# -- the threshold store ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateThreshold:
    """One shape's row (see the module docstring)."""

    shape: str
    threshold: float
    mode: Mode
    live_threshold: float | None
    proposed_at: datetime
    promoted_at: datetime | None
    window_start: datetime


def _threshold_of(row: Any) -> GateThreshold:
    return GateThreshold(
        shape=str(row.shape),
        threshold=float(row.threshold),
        mode="live" if row.mode == "live" else "shadow",
        live_threshold=None if row.live_threshold is None else float(row.live_threshold),
        proposed_at=row.proposed_at,
        promoted_at=row.promoted_at,
        window_start=row.window_start,
    )


async def thresholds(conn: AsyncConnection) -> dict[str, GateThreshold]:
    """Every shape's row, by shape key."""
    found = await conn.execute(select(gate_thresholds).order_by(gate_thresholds.c.shape))
    return {str(r.shape): _threshold_of(r) for r in found}


async def threshold(
    conn: AsyncConnection, shape: str, *, lock: bool = False
) -> GateThreshold | None:
    query = select(gate_thresholds).where(gate_thresholds.c.shape == shape)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _threshold_of(row)


async def live_thresholds(conn: AsyncConnection) -> dict[str, float]:
    """The thresholds in force, by shape key (#142's gate reads them per verify)."""
    found = await conn.execute(
        select(gate_thresholds.c.shape, gate_thresholds.c.live_threshold).where(
            gate_thresholds.c.live_threshold.is_not(None)
        )
    )
    return {str(r.shape): float(r.live_threshold) for r in found}


async def _history(
    conn: AsyncConnection, action: str, shape: str, found: GateThreshold | None, actor: str
) -> None:
    await conn.execute(
        insert(gate_threshold_history).values(
            id=new_ulid(),
            shape=shape,
            action=action,
            threshold=None if found is None else found.threshold,
            mode=None if found is None else found.mode,
            live_threshold=None if found is None else found.live_threshold,
            window_start=None if found is None else found.window_start,
            actor=actor,
        )
    )


async def put_threshold(
    conn: AsyncConnection,
    shape: str,
    value: float,
    mode: Mode,
    *,
    actor: str,
    now: datetime | None = None,
) -> tuple[GateThreshold | None, GateThreshold]:
    """Write ``shape``'s proposal (see the module docstring); the row before and after."""
    if not 0.0 <= value <= 1.0:
        raise InvalidRequest(
            "a gate threshold is between 0 and 1",
            {"field": "threshold", "reason": "out_of_range"},
        )
    moment = now or clock.now()
    before = await threshold(conn, check_shape(shape), lock=True)
    live = value if mode == "live" else (None if before is None else before.live_threshold)
    promoted = moment if mode == "live" else (None if before is None else before.promoted_at)
    values = {
        "threshold": value,
        "mode": mode,
        "live_threshold": live,
        "proposed_at": moment,
        "promoted_at": promoted,
        "window_start": moment,
    }
    await conn.execute(
        pg_insert(gate_thresholds)
        .values(shape=shape, **values)
        .on_conflict_do_update(index_elements=[gate_thresholds.c.shape], set_=values)
    )
    after = GateThreshold(shape=shape, **values)  # type: ignore[arg-type]
    await _history(conn, "put", shape, after, actor)
    return before, after


async def promote(
    conn: AsyncConnection, found: GateThreshold, *, actor: str, now: datetime | None = None
) -> GateThreshold:
    """Make ``found``'s proposal live (the caller checked the rule)."""
    moment = now or clock.now()
    await conn.execute(
        update(gate_thresholds)
        .where(gate_thresholds.c.shape == found.shape)
        .values(mode="live", live_threshold=found.threshold, promoted_at=moment)
    )
    after = GateThreshold(
        shape=found.shape,
        threshold=found.threshold,
        mode="live",
        live_threshold=found.threshold,
        proposed_at=found.proposed_at,
        promoted_at=moment,
        window_start=found.window_start,
    )
    await _history(conn, "promote", found.shape, after, actor)
    return after


async def remove(conn: AsyncConnection, found: GateThreshold, *, actor: str) -> None:
    """Remove ``found``'s row: the shape is not tightened any more."""
    await conn.execute(delete(gate_thresholds).where(gate_thresholds.c.shape == found.shape))
    await _history(conn, "delete", found.shape, None, actor)


# -- the promotion rule -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Promotion:
    """A shape's shadow window against the promotion rule (see the module docstring)."""

    shape: str
    cases: int
    required_cases: int
    skipped: int
    agreed: int
    required_agreement: float

    @property
    def agreement(self) -> float | None:
        return None if not self.skipped else round(self.agreed / self.skipped, DIGITS)

    @property
    def promotable(self) -> bool:
        agreement = None if not self.skipped else self.agreed / self.skipped
        return (
            self.cases >= self.required_cases
            and agreement is not None
            and agreement >= self.required_agreement
        )

    def details(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "cases": self.cases,
            "required_cases": self.required_cases,
            "agreement": self.agreement,
            "required_agreement": self.required_agreement,
        }


async def promotion(
    conn: AsyncConnection,
    found: GateThreshold,
    *,
    min_cases: int,
    min_agreement: float,
) -> Promotion:
    """``found``'s shadow window: its escalations, the would-have-skipped ones and how many of
    those agreed with the model."""
    e = escalations
    would_skip = e.c.shadow_decision.is_(False)
    row = (
        await conn.execute(
            select(
                func.count(),
                func.count().filter(would_skip),
                func.count().filter(and_(would_skip, e.c.shadow_agreed.is_(True))),
            ).where(
                active_only(e),
                e.c.predicate_shape == found.shape,
                e.c.skipped_by_gate.is_(False),
                e.c.at >= found.window_start,
            )
        )
    ).one()
    return Promotion(
        shape=found.shape,
        cases=int(row[0]),
        required_cases=min_cases,
        skipped=int(row[1]),
        agreed=int(row[2]),
        required_agreement=min_agreement,
    )


# -- the yield report ---------------------------------------------------------------------------


def _rate(numerator: float, denominator: int) -> float | None:
    return None if not denominator else round(numerator / denominator, DIGITS)


@dataclass(slots=True)
class YieldCluster:
    """One cluster's counts and rates (see the module docstring)."""

    gate_reason: str | None = None
    shape: str | None = None
    escalations: int = 0
    skipped: int = 0
    valid: int = 0
    flips: int = 0
    weighted_flips: float = 0.0

    @property
    def flip_rate(self) -> float | None:
        return _rate(self.flips, self.valid)

    @property
    def weighted_flip_rate(self) -> float | None:
        return _rate(self.weighted_flips, self.valid)

    def add(self, other: YieldCluster) -> None:
        self.escalations += other.escalations
        self.skipped += other.skipped
        self.valid += other.valid
        self.flips += other.flips
        self.weighted_flips += other.weighted_flips


@dataclass(frozen=True, slots=True)
class YieldReport:
    """The report (see the module docstring)."""

    window_from: datetime
    window_to: datetime
    by_gate_reason: list[YieldCluster]
    by_shape: list[YieldCluster]
    by_gate_reason_shape: list[YieldCluster]
    escalations: int
    claims: int
    escalated: int
    escalation_rate: float | None
    reference_baseline: float = REFERENCE_BASELINE
    is_gate: bool = False
    totals: YieldCluster = field(default_factory=YieldCluster)


def _weight() -> Any:
    e = escalations
    return case(
        *((e.c.consequence == name, weight) for name, weight in CONSEQUENCE_WEIGHTS.items()),
        else_=0.0,
    )


async def yield_report(
    conn: AsyncConnection,
    *,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> YieldReport:
    """The yield report over the window (see the module docstring)."""
    to = window_to or clock.now()
    start = window_from or (to - DEFAULT_WINDOW)
    if start > to:
        raise InvalidRequest(
            "the window's from is not after its to", {"field": "from", "reason": "after_to"}
        )
    e = escalations
    escalated = e.c.skipped_by_gate.is_(False)
    valid = and_(escalated, e.c.model_verdict.is_not(None))
    flipped = and_(valid, e.c.flipped.is_(True))
    groups = (
        await conn.execute(
            select(
                e.c.gate_reason,
                e.c.predicate_shape,
                func.count().filter(escalated),
                func.count().filter(e.c.skipped_by_gate.is_(True)),
                func.count().filter(valid),
                func.count().filter(flipped),
                func.coalesce(func.sum(case((flipped, _weight()), else_=0.0)), 0.0),
            )
            .where(active_only(e), e.c.at >= start, e.c.at < to)
            .group_by(e.c.gate_reason, e.c.predicate_shape)
            .order_by(e.c.gate_reason, e.c.predicate_shape)
        )
    ).all()
    pairs = [
        YieldCluster(
            gate_reason=str(g[0]),
            shape=str(g[1]),
            escalations=int(g[2]),
            skipped=int(g[3]),
            valid=int(g[4]),
            flips=int(g[5]),
            weighted_flips=float(g[6]),
        )
        for g in groups
    ]
    by_reason: dict[str, YieldCluster] = {}
    by_shape: dict[str, YieldCluster] = {}
    totals = YieldCluster()
    for pair, (reason, shape) in zip(pairs, ((str(g[0]), str(g[1])) for g in groups), strict=True):
        by_reason.setdefault(reason, YieldCluster(gate_reason=reason)).add(pair)
        by_shape.setdefault(shape, YieldCluster(shape=shape)).add(pair)
        totals.add(pair)
    o = operations
    claims, escalated_claims = await _verified(conn, o, start, to)
    return YieldReport(
        window_from=start,
        window_to=to,
        by_gate_reason=[by_reason[k] for k in sorted(by_reason)],
        by_shape=[by_shape[k] for k in sorted(by_shape)],
        by_gate_reason_shape=pairs,
        escalations=totals.escalations,
        claims=claims,
        escalated=escalated_claims,
        escalation_rate=_rate(escalated_claims, claims),
        totals=totals,
    )


async def _verified(
    conn: AsyncConnection, o: Any, start: datetime, to: datetime
) -> tuple[int, int]:
    """Claims verified and escalated by the active turns committed in the window."""
    where = [
        o.c.kind == OperationKind.TURN.value,
        active_operation(o),
        o.c.committed_at.is_not(None),
        o.c.committed_at >= start,
        o.c.committed_at < to,
    ]
    totals = [0, 0]
    for key in ("verify", "verify_follow_up"):
        part = o.c.turn_report[key]
        row = (
            await conn.execute(
                select(
                    func.coalesce(func.sum(cast(part["claims"].astext, Integer)), 0),
                    func.coalesce(func.sum(cast(part["escalated"].astext, Integer)), 0),
                ).where(and_(*where, part.is_not(None)))
            )
        ).one()
        totals[0] += int(row[0])
        totals[1] += int(row[1])
    return totals[0], totals[1]


# -- the operator's writes (``PUT /admin/verify/gate`` and its promote and delete) ----------------

SETTING_PREFIX = "verify_gate_threshold"
"""``config_changes.setting`` of a live threshold's change: ``verify_gate_threshold:<shape>``."""
UNVALIDATED = "unvalidated_tightening"
"""The warning of a ``live`` write without ``ignore_warnings``: the tightening skips the shadow
window, so it applies only when confirmed (#109's two-stage flow)."""


class GateRequest(BaseModel):
    """``PUT /admin/verify/gate``: a shape's proposed threshold and its mode. ``shadow`` records
    what the tightened gate would decide and changes nothing live; ``live`` applies it at once
    (an operator's cost ceiling, NFR-PERF-5), which, being unvalidated, needs
    ``ignore_warnings``: without it the answer is the warning, nothing written."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {"shape": "core|functional|fluent|affirmed", "threshold": 0.4, "mode": "shadow"}
            ]
        },
    )

    shape: str = Field(
        pattern=SHAPE_PATTERN,
        max_length=170,
        description="The predicate shape key: `layer|cardinality|temporal|polarity`.",
    )
    threshold: float = Field(
        ge=0.0,
        le=1.0,
        description="The proposed threshold: a claim of the shape escalates when its "
        "deterministic confidence is below the least of this and the profile's gate. 1.0 "
        "tightens nothing.",
    )
    mode: Mode = "shadow"
    ignore_warnings: StrictBool = False

    @field_validator("threshold", mode="before")
    @classmethod
    def _number(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("must be a number from 0 to 1")
        return float(value)


@dataclass(frozen=True, slots=True)
class GateWrite:
    """A gate write's outcome: the shape's row after it (``None`` values once removed), whether
    it was applied (a ``live`` write without ``ignore_warnings`` is not), its warnings, and the
    deployment's ``config_revision`` after the call."""

    shape: str
    applied: bool
    config_revision: int
    threshold: float | None = None
    mode: str | None = None
    live_threshold: float | None = None
    window_start: datetime | None = None
    proposed_at: datetime | None = None
    promoted_at: datetime | None = None
    warnings: tuple[str, ...] = ()
    replayed: bool = False

    @classmethod
    def of(
        cls,
        shape: str,
        found: GateThreshold | None,
        revision: int,
        *,
        applied: bool = True,
        warnings: tuple[str, ...] = (),
    ) -> GateWrite:
        if found is None:
            return cls(shape, applied, revision, warnings=warnings)
        return cls(
            shape=shape,
            applied=applied,
            config_revision=revision,
            threshold=found.threshold,
            mode=found.mode,
            live_threshold=found.live_threshold,
            window_start=found.window_start,
            proposed_at=found.proposed_at,
            promoted_at=found.promoted_at,
            warnings=warnings,
        )


async def _live_changed(
    conn: AsyncConnection, shape: str, old: float | None, new: float | None, actor: str
) -> int:
    """A change of the threshold in force: one configuration revision and its
    ``config_changes`` row (interpretive: it changes what the gate decides)."""
    revision = await bump_config_revision(conn, await lock_deployment(conn))
    await record_config_change(
        conn,
        config_revision=revision,
        setting=f"{SETTING_PREFIX}:{shape}",
        old=old,
        new=new,
        actor=actor,
        change_class=ConfigClass.interpretive,
    )
    return revision


async def set_gate(db: AsyncEngine, request: GateRequest, *, actor: str) -> GateWrite:
    """``PUT /admin/verify/gate`` (see ``GateRequest``)."""
    shape = check_shape(request.shape)
    if request.mode == "live" and not request.ignore_warnings:
        revision = (await read_settings(db)).config_revision
        async with db.connect() as conn:
            found = await threshold(conn, shape)
        return GateWrite.of(shape, found, revision, applied=False, warnings=(UNVALIDATED,))
    async with db.begin() as conn:
        before, after = await put_threshold(
            conn, shape, request.threshold, request.mode, actor=actor
        )
        old = None if before is None else before.live_threshold
        if after.live_threshold != old:
            revision = await _live_changed(conn, shape, old, after.live_threshold, actor)
        else:
            revision = (await read_settings(conn)).config_revision
    return GateWrite.of(shape, after, revision)


async def promote_gate(db: AsyncEngine, shape: str, *, actor: str) -> GateWrite:
    """``POST /admin/verify/gate/{shape}/promote``: the proposal goes live when its shadow
    window meets the rule, else ``GateNotPromotable`` with the numbers."""
    shape = check_shape(shape)
    async with db.begin() as conn:
        found = await threshold(conn, shape, lock=True)
        if found is None:
            raise GateThresholdNotFound(
                f"no gate threshold is proposed for shape {shape}", {"shape": shape}
            )
        settings = await read_section(conn, VerifySettings)
        window = await promotion(
            conn,
            found,
            min_cases=settings.gate_promotion_min_cases,
            min_agreement=settings.gate_promotion_min_agreement,
        )
        if not window.promotable:
            raise GateNotPromotable(
                f"shape {shape}'s shadow window does not meet the promotion rule",
                window.details(),
            )
        after = await promote(conn, found, actor=actor)
        if after.live_threshold != found.live_threshold:
            revision = await _live_changed(
                conn, shape, found.live_threshold, after.live_threshold, actor
            )
        else:
            revision = (await read_settings(conn)).config_revision
    return GateWrite.of(shape, after, revision)


async def remove_gate(db: AsyncEngine, shape: str, *, actor: str) -> GateWrite:
    """``DELETE /admin/verify/gate/{shape}``: the shape is not tightened any more (a live
    threshold is demoted: a configuration change)."""
    shape = check_shape(shape)
    async with db.begin() as conn:
        found = await threshold(conn, shape, lock=True)
        if found is None:
            raise GateThresholdNotFound(
                f"no gate threshold is proposed for shape {shape}", {"shape": shape}
            )
        await remove(conn, found, actor=actor)
        if found.live_threshold is not None:
            revision = await _live_changed(conn, shape, found.live_threshold, None, actor)
        else:
            revision = (await read_settings(conn)).config_revision
    return GateWrite.of(shape, None, revision)


__all__ = [
    "CONSEQUENCE_WEIGHTS",
    "DEFAULT_WINDOW",
    "REFERENCE_BASELINE",
    "SETTING_PREFIX",
    "SHAPE_PATTERN",
    "UNVALIDATED",
    "GateRequest",
    "GateThreshold",
    "GateWrite",
    "Mode",
    "Promotion",
    "YieldCluster",
    "YieldReport",
    "check_shape",
    "live_thresholds",
    "path_of_shape",
    "promote",
    "promote_gate",
    "promotion",
    "put_threshold",
    "remove",
    "remove_gate",
    "set_gate",
    "shape_of_path",
    "threshold",
    "thresholds",
    "yield_report",
]
