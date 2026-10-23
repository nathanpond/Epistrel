"""Claim detection's shadow samples and their report (FR-OBS-4, FR-VERIFY-13, NFR-QUAL-10;
#148).

**A sample** (``write_sample``) is one ``extraction_shadow`` row: a ``gated`` render (or
composed message) whose extraction still ran after its turn committed, and the claims that
extraction found that no declared or detector claim covered ("missed": what gating would have
missed). It is written in the shadow job's commit with the **sampled turn's** operation, so it
follows that turn's membership (#130); a sample of a turn no longer on the active timeline is
not written. ``missed`` holds spans, categories, predicate ids and shape hashes only. The sampled
turn's ``turn_report.detection`` then records the sample's ``shadow`` status and
``shadow_missed`` count.

**The report** (``report``, ``GET /admin/evals/extraction-shadow``): over a window
(``from``/``to``, 30 days back by default) and optionally one story, it sums the active samples
(``samples``, ``failed``, ``extraction_claims``, ``missed``, ``missed_by_category`` and
``miss_rate``: missed claims over every claim the sampled extractions made) and, **separately**,
detection and checking over every active turn report in the window (``detected_total``: claims
detected, ``checked_total``: claims verified; ``turn_report.detection``). Zero unchecked detected
claims is the guarantee; complete detection is the measured rate (NFR-QUAL-10). One page of the
samples themselves follows (``limit``, ``after``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Integer, and_, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.enums import OperationKind
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import operation_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_only, active_operation, insert_derived
from epistrel.core.tables import operations
from epistrel.core.tables.detection import extraction_shadow

DEFAULT_WINDOW = timedelta(days=30)
PAGE_MAX = 200
PAGE_DEFAULT = 50
SAMPLED = "sampled"
FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Missed:
    """One claim gating missed: where it is (``beat`` ``None``: the composed message), what
    kind of claim it is, its predicate (``None``: it did not normalize) and its shape hash."""

    beat: int | None
    start: int
    end: int
    category: str
    predicate_id: str | None = None
    shape: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "beat": self.beat,
            "start": self.start,
            "end": self.end,
            "category": self.category,
            "predicate_id": self.predicate_id,
            "shape": self.shape,
        }


async def write_sample(
    conn: AsyncConnection,
    story_id: str,
    operation_id: str,
    *,
    status: str,
    extraction_claims: int,
    missed: Sequence[Missed] = (),
) -> str | None:
    """Write one sample of the turn ``operation_id`` (see the module docstring); ``None`` when
    that turn is not on the story's active timeline (nothing is written)."""
    if status not in (SAMPLED, FAILED):
        raise ValueError(f"unknown shadow sample status {status!r}")
    row = (
        await conn.execute(
            select(
                operations.c.narrative_turn, operations.c.turn_variant, operations.c.turn_report
            ).where(
                operations.c.operation_id == operation_id,
                operations.c.story_id == story_id,
                active_operation(operations),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    sample_id = new_ulid()
    await insert_derived(
        conn,
        extraction_shadow,
        [
            {
                "id": sample_id,
                "story_id": story_id,
                "operation_id": operation_id,
                "narrative_turn": row.narrative_turn,
                "turn_variant": row.turn_variant,
                "status": status,
                "extraction_claims": extraction_claims,
                "missed": [m.document() for m in missed],
                "missed_count": len(missed),
            }
        ],
    )
    report = row.turn_report
    if isinstance(report, dict) and isinstance(report.get("detection"), dict):
        # The sampled turn's report learns what its sample found (``shadow``, ``shadow_missed``).
        detection = {**report["detection"], "shadow": status, "shadow_missed": len(missed)}
        updated = {**report, "detection": detection}
        await conn.execute(
            update(operations)
            .where(operations.c.operation_id == operation_id)
            .values(turn_report=updated)
        )
        await index_records(
            conn,
            [
                operation_record(
                    SimpleNamespace(
                        operation_id=operation_id, story_id=story_id, turn_report=updated
                    )
                )
            ],
        )
    return sample_id


@dataclass(frozen=True, slots=True)
class Sample:
    id: str
    story_id: str
    operation_id: str
    narrative_turn: int | None
    status: str
    extraction_claims: int
    missed_count: int
    missed_by_category: dict[str, int]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """The report (see the module docstring)."""

    window_from: datetime
    window_to: datetime
    story_id: str | None
    samples: int
    failed: int
    extraction_claims: int
    missed: int
    missed_by_category: dict[str, int]
    miss_rate: float | None
    detected_total: int
    checked_total: int
    items: list[Sample] = field(default_factory=list)
    next: str | None = None


def _by_category(missed: Any) -> dict[str, int]:
    found: dict[str, int] = {}
    for entry in missed if isinstance(missed, list) else []:
        category = str(entry.get("category") or "other") if isinstance(entry, dict) else "other"
        found[category] = found.get(category, 0) + 1
    return found


async def report(
    conn: AsyncConnection,
    *,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    story_id: str | None = None,
    limit: int = PAGE_DEFAULT,
    after: str | None = None,
) -> ShadowReport:
    """The shadow report over the window (see the module docstring)."""
    if not 1 <= limit <= PAGE_MAX:
        raise InvalidRequest(
            f"limit is between 1 and {PAGE_MAX}", {"field": "limit", "reason": "out_of_range"}
        )
    to = window_to or clock.now()
    start = window_from or clock.window_start(to, DEFAULT_WINDOW)
    if start > to:
        raise InvalidRequest(
            "the window's from is not after its to", {"field": "from", "reason": "after_to"}
        )
    s = extraction_shadow
    where = [active_only(s), s.c.created_at >= start, s.c.created_at <= to]
    if story_id is not None:
        where.append(s.c.story_id == story_id)
    rows = (
        await conn.execute(
            select(s.c.status, s.c.extraction_claims, s.c.missed_count, s.c.missed).where(*where)
        )
    ).all()
    samples = sum(1 for r in rows if r.status == SAMPLED)
    failed = sum(1 for r in rows if r.status == FAILED)
    extraction_claims = sum(int(r.extraction_claims) for r in rows if r.status == SAMPLED)
    missed = sum(int(r.missed_count) for r in rows if r.status == SAMPLED)
    by_category: dict[str, int] = {}
    for r in rows:
        for category, n in _by_category(r.missed).items():
            by_category[category] = by_category.get(category, 0) + n
    o = operations
    detection = o.c.turn_report["detection"]
    turn_where = [
        o.c.kind == OperationKind.TURN.value,
        active_operation(o),
        o.c.committed_at.is_not(None),
        o.c.committed_at >= start,
        o.c.committed_at <= to,
        detection.is_not(None),
    ]
    if story_id is not None:
        turn_where.append(o.c.story_id == story_id)
    totals = (
        await conn.execute(
            select(
                func.coalesce(func.sum(cast(detection["detected"].astext, Integer)), 0),
                func.coalesce(func.sum(cast(detection["checked"].astext, Integer)), 0),
            ).where(and_(*turn_where))
        )
    ).one()
    page_where = list(where)
    if after is not None:
        page_where.append(s.c.id > after)
    page = (
        await conn.execute(
            select(
                s.c.id,
                s.c.story_id,
                s.c.operation_id,
                s.c.narrative_turn,
                s.c.status,
                s.c.extraction_claims,
                s.c.missed_count,
                s.c.missed,
                s.c.created_at,
            )
            .where(*page_where)
            .order_by(s.c.id)
            .limit(limit + 1)
        )
    ).all()
    items = [
        Sample(
            id=str(r.id),
            story_id=str(r.story_id),
            operation_id=str(r.operation_id),
            narrative_turn=r.narrative_turn,
            status=str(r.status),
            extraction_claims=int(r.extraction_claims),
            missed_count=int(r.missed_count),
            missed_by_category=_by_category(r.missed),
            created_at=r.created_at,
        )
        for r in page[:limit]
    ]
    return ShadowReport(
        window_from=start,
        window_to=to,
        story_id=story_id,
        samples=samples,
        failed=failed,
        extraction_claims=extraction_claims,
        missed=missed,
        missed_by_category=dict(sorted(by_category.items())),
        miss_rate=None if not extraction_claims else missed / extraction_claims,
        detected_total=int(totals[0]),
        checked_total=int(totals[1]),
        items=items,
        next=items[-1].id if len(page) > limit else None,
    )


__all__ = [
    "DEFAULT_WINDOW",
    "FAILED",
    "PAGE_DEFAULT",
    "PAGE_MAX",
    "SAMPLED",
    "Missed",
    "Sample",
    "ShadowReport",
    "report",
    "write_sample",
]
