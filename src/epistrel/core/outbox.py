"""The transactional outbox's rows and the reconciliation status (FR-EXT-10,12,14,16..18;
docs/02 §4.8; #210). The dispatcher that drives them is the Orchestrator's
(``orchestrator.jobs.handlers.outbox``); every write to the outbox tables goes through here.

**Intent** (``write_intended``). A turn's effect on a value a write-capable provider owns is
never applied by Epistrel: it is written as an ``external_effect_outbox`` row, ``intended``, in
the turn's own commit transaction (its id the effect's, which is also the provider idempotency
key), at the declaring beat's position, so the turn and the row commit together or not at all
(FR-EXT-10). The row's ``effect`` (``{kind, state, delta|value, reason}``) passes the
resurrection guard first (``core.erasure.guard.check_content``) and is indexed for erasure
(``index_records``: its text leaves by position, a character scope as a declared subject).

**Claim** (``claim_row``, ``claimable``). The dispatcher locks a row ``FOR UPDATE SKIP LOCKED``
and, in the same transaction, re-reads its operation's ``membership`` (FR-EXT-12): anything but
``active`` (``discarded`` by a truncation, ``dormant`` after a regeneration or a swap away,
``superseded``) cancels it (``cancel``), never sent. Otherwise ``mark_claimed`` sets
``claimed``, ``claimed_at`` and the lease; the commit of that transaction is the in-flight
point. A row of a story ``blocked_by_erasure`` is not claimed (it stays ``intended``).

**Outcome** (``record_outcome``), in its own transaction: an ``outbox_outcomes`` row and the
row's status. From ``claimed`` or ``unknown`` the answer becomes the status (``unknown →
confirmed | failed`` included); a later answer to a settled row is recorded as ``duplicate``
(the same, or ``unknown``) or ``conflict`` (another terminal answer) and changes nothing (the
first terminal outcome wins, FR-EXT-22; #218: one settled terminal record per row, a partial
unique index, and a ``conflict`` lists the row ``outcome_conflict``). ``release`` puts a
claimed row back to ``intended`` when the adapter proved nothing was sent
(``ProviderUnavailable`` before the request). Each
recorded outcome notifies ``OUTCOME_CHANNEL`` with the row id when its transaction commits, so a
staged turn waiting on the row (#212, FR-EXT-15) wakes at once rather than at its next poll.

**The timeline and erasure** (#211; ``core.timeline_effects`` drives them): ``locked_of``
locks the rows of operations a membership flip moved, ``cancel_all`` cancels their
``intended`` rows, and ``mark`` lists a row with a reason its status does not carry (a
``reconciliation_marks`` row, open until an explicit action resolves it, #218).
``erased_since`` and ``abandon`` are the dispatcher's erasure-specific mid-flight check and
its outcome: ``unknown``, no payload recorded, ``erased_at`` set (never sent again).

**Reconciliation** (``listings``, ``count``): what the consumer must look at, one listing per
row: ``unconfirmed`` (``intended`` with its operation active, or ``claimed``), ``failed`` and
``unknown`` by status, and any row with an open mark under the mark's reason (#211:
``reconciliation_required``, ``cancelled_on_reactivation``; #216, #217, #218 add theirs). A
``confirmed`` or ``cancelled`` row without a mark is not listed; a fork's copies never are. A
row an erasure reached (``erased_at``) is listed as ``{outbox_id, provider, status}`` with
its reason and since only.
``reservation_expired`` (#216) lists a row a State hold waited on that was released while no
prerequisite had failed (its ``reservation_expiry`` lapsed, FR-STATE-14). Nothing here bumps a
revision.

**Explicit actions** (#218, FR-EXT-22; the Orchestrator's ``jobs.settlement`` drives them):
``resolve`` closes a row's open marks with the action that answered them (``settle_now``, or
``compensated`` when the compensating row a ``compensate`` wrote is confirmed); a
``late_success_after_expiry`` mark carries its re-verification (``Listing.reverification``),
and a row a compensating row answers is listed ``status: compensated`` (``compensated_by``)
until that row is confirmed.

Log lines carry ids and counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)
from sqlalchemy import Row, and_, any_, exists, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import StateChangeStatus, StoryStatus
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import outbox_record
from epistrel.core.errors import StoryNotFound
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_only, active_operation, insert_derived
from epistrel.core.states.model import REASON_MAX
from epistrel.core.tables import erasure_ledger, operations, stories
from epistrel.core.tables.outbox import (
    CANCELLED,
    CLAIMED,
    CONFIRMED,
    CONFLICT,
    DUPLICATE,
    FAILED,
    INTENDED,
    UNKNOWN,
    VIA_DISPATCHER,
    VIA_INGEST,
    external_effect_outbox,
    outbox_outcomes,
    reconciliation_marks,
)
from epistrel.core.tables.outbox import REASONS as MARK_REASONS
from epistrel.core.tables.states import state_changes
from epistrel.observability.logging import get_logger

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation

log = get_logger(__name__)

OUTCOME_CHANNEL = "epistrel_outbox"
"""The NOTIFY channel ``record_outcome`` signals (the row id as the payload, ids only)."""

TERMINAL = frozenset({CONFIRMED, FAILED})
"""A settled row's statuses: a later answer never changes them."""

REASONS: tuple[str, ...] = MARK_REASONS
"""Every reconciliation reason (the full enum; #210 lists the first three, #211 marks
``reconciliation_required`` and ``cancelled_on_reactivation``)."""

Reason = Literal[
    "unconfirmed",
    "failed",
    "unknown",
    "reconciliation_required",
    "cancelled_on_reactivation",
    "confirmed_unsettled",
    "confirmed_discarded_origin",
    "settlement_conflict",
    "late_success_after_expiry",
    "outcome_conflict",
    "reservation_expired",
]


# -- intent --------------------------------------------------------------------------------------

INTERNAL_EFFECT_KEYS = frozenset({"authorized", "pinned"})
"""#214: the effect document's keys kept for the Engine alone (what #188 authorized at intent,
re-checked at claim; #657: ``pinned``, the value the turn read, its response's ``old_value``),
never listed nor sent to a provider."""


REQUEST_PENDING = "pending"
"""#656: a requested effect no terminal outcome has answered yet."""


def request_status(outbox_status: str | None, answered: str | None) -> str:
    """#656: a requested effect's current status as a response or history lists it: an outbox
    row's settled status (``confirmed``, ``failed``, ``cancelled``), else a consumer-applied
    request's terminal outcome (``answered``), else ``pending``."""
    if outbox_status in (CONFIRMED, FAILED, CANCELLED):
        return str(outbox_status)
    if answered in TERMINAL:
        return str(answered)
    return REQUEST_PENDING


async def request_statuses(
    conn: AsyncConnection, story_id: str, effect_ids: Sequence[str]
) -> dict[str, str]:
    """#656: the current status (``request_status``) of each of ``effect_ids``, an outbox row's
    id or a consumer-applied request's ``effect_id``."""
    if not effect_ids:
        return {}
    o, x = external_effect_outbox.c, outbox_outcomes.c
    rows = {
        str(r.id): str(r.status)
        for r in await conn.execute(
            select(o.id, o.status).where(o.story_id == story_id, o.id.in_(list(effect_ids)))
        )
    }
    answers: dict[str, str] = {}
    for r in await conn.execute(
        select(x.effect_id, x.kind)
        .where(
            x.story_id == story_id,
            x.effect_id.in_(list(effect_ids)),
            x.kind.in_(sorted(TERMINAL)),
        )
        .order_by(x.received_at, x.id)
    ):
        answers.setdefault(str(r.effect_id), str(r.kind))
    return {e: request_status(rows.get(e), answers.get(e)) for e in effect_ids}


async def request_effects(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str], *, omniscient: bool
) -> dict[str, list[dict[str, Any]]]:
    """#656: the changes each of ``operation_ids`` requested and Epistrel does not apply, as
    history lists them beside the committed ones (FR-RESP-7, as the response does): its outbox
    rows (#210, ``outbox_id``) and its ``requested_effect`` events (#209), each ``owner:
    external`` with its ``effect_id`` and current status (``request_status``); ``old_value`` the
    value the turn read when the row kept it (#657), else ``null``. A ``hidden`` State's for
    ``omniscient`` only."""
    from epistrel.core.enums import StateVisibility
    from epistrel.core.tables.events import events
    from epistrel.core.tables.states import state_definitions

    if not operation_ids:
        return {}
    d = state_definitions.c
    hidden = {
        str(r.name)
        for r in await conn.execute(
            select(d.name).where(
                d.story_id == story_id, d.visibility != StateVisibility.PLAYER_VISIBLE
            )
        )
    }
    found: dict[str, list[tuple[tuple[int, int], dict[str, Any]]]] = {}

    def add(operation_id: str, at: tuple[int, int], row: dict[str, Any]) -> None:
        if omniscient or row["state"] not in hidden:
            found.setdefault(operation_id, []).append((at, row))

    def entry(effect: Mapping[str, Any], scope_key: Any, effect_id: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "state": str(effect.get("state") or ""),
            "scope_key": None if scope_key is None else str(scope_key),
            "old_value": effect.get("pinned"),
            "new_value": None,
            "reason": str(effect.get("reason") or ""),
            "source": "turn",
            "contingency": "unconditional",
            "status": REQUEST_PENDING,
            "owner": "external",
            "effect_id": effect_id,
        }
        if effect.get("kind") == "time_advance":
            row["kind"] = "time_advance"
            row["minutes"] = effect.get("minutes")
        elif effect.get("delta") is not None:
            row["delta"] = effect.get("delta")
        else:
            row["value"] = effect.get("value")
        return row

    o = external_effect_outbox.c
    for r in await conn.execute(
        select(o.id, o.operation_id, o.scope_key, o.effect, o.beat, o.step).where(
            o.story_id == story_id,
            o.operation_id.in_(list(operation_ids)),
            o.executable.is_(True),
        )
    ):
        row = entry(dict(r.effect or {}), r.scope_key, str(r.id))
        row["outbox_id"] = str(r.id)
        add(str(r.operation_id), (int(r.beat), int(r.step)), row)
    e = events.c
    for asked in await conn.execute(
        select(e.operation_id, e.payload, e.beat, e.step).where(
            e.story_id == story_id,
            e.operation_id.in_(list(operation_ids)),
            e.type == "requested_effect",
        )
    ):
        payload = dict(asked.payload or {})
        add(
            str(asked.operation_id),
            (int(asked.beat or 0), int(asked.step or 0)),
            entry(payload, payload.get("scope_key"), str(payload.get("effect_id"))),
        )
    ids = [row["effect_id"] for rows in found.values() for _, row in rows]
    statuses = await request_statuses(conn, story_id, ids)
    out: dict[str, list[dict[str, Any]]] = {}
    for operation_id, rows in found.items():
        for _, row in sorted(rows, key=lambda pair: pair[0]):
            row["status"] = statuses.get(row["effect_id"], REQUEST_PENDING)
            out.setdefault(operation_id, []).append(row)
    return out


def public_effect(effect: Mapping[str, Any] | None) -> dict[str, Any]:
    """An outbox row's effect document as a listing shows it (``INTERNAL_EFFECT_KEYS`` left
    out)."""
    return {k: v for k, v in dict(effect or {}).items() if k not in INTERNAL_EFFECT_KEYS}


@dataclass(frozen=True, slots=True)
class Intended:
    """One effect for the outbox: its id (the idempotency key), the provider and its value, the
    scope key, the ``effect`` document and the declaring beat and step."""

    id: str
    provider_name: str
    value_name: str
    scope_key: str | None
    effect: Mapping[str, Any]
    beat: int
    step: int = 0
    slot_k: int = 0
    """#218: the slot an inter-turn operation's row is written at (``0``: a turn's)."""
    compensates: str | None = None
    """#218: the row a compensating effect answers (FR-EXT-22's ``compensate``)."""


async def write_intended(conn: AsyncConnection, op: Operation, rows: Sequence[Intended]) -> int:
    """Write ``rows`` ``intended`` in ``op``'s commit transaction (see the module docstring)."""
    story_id = op.story_id
    if story_id is None or not rows:
        return 0
    await guard.check_content(
        conn,
        story_id,
        [
            guard.Content(
                tuple(str(r.effect.get("reason") or "") for r in rows if r.effect.get("reason")),
                frozenset(r.scope_key for r in rows if r.scope_key),
                op.provenance in guard.CONSUMER_PROVENANCE,
            )
        ],
        base_revision=op.base_revision,
        path=__name__,
    )
    written = [
        {
            "id": r.id,
            "story_id": story_id,
            "provider_name": r.provider_name,
            "value_name": r.value_name,
            "scope_key": r.scope_key,
            "effect": dict(r.effect),
            "status": INTENDED,
            "operation_id": op.operation_id,
            "narrative_turn": op.narrative_turn,
            "turn_variant": op.turn_variant,
            "slot_k": r.slot_k,
            "beat": r.beat,
            "step": r.step,
            "compensates": r.compensates,
        }
        for r in rows
    ]
    await insert_derived(conn, external_effect_outbox, written)
    await index_records(conn, [outbox_record(SimpleNamespace(**row)) for row in written])
    log.debug("outbox: %d intended", len(written))
    return len(written)


# -- claim ---------------------------------------------------------------------------------------


async def claimable(conn: AsyncConnection, outbox_id: str) -> Row[Any] | None:
    """Lock outbox row ``outbox_id`` when it is ``intended``, executable and not erased (``SKIP
    LOCKED``: ``None`` while another transaction holds it, or when it is not claimable)."""
    o = external_effect_outbox.c
    return (
        await conn.execute(
            select(external_effect_outbox)
            .where(
                o.id == outbox_id,
                o.status == INTENDED,
                o.executable.is_(True),
                o.erased_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()


async def locked(conn: AsyncConnection, outbox_id: str) -> Row[Any] | None:
    """Lock outbox row ``outbox_id`` whatever its status (waits for another holder)."""
    return (
        await conn.execute(
            select(external_effect_outbox)
            .where(external_effect_outbox.c.id == outbox_id)
            .with_for_update()
        )
    ).one_or_none()


async def origin_active(conn: AsyncConnection, operation_id: str) -> bool:
    """Whether the operation that wrote a row is on the active timeline, read now (FR-EXT-12;
    through ``core.membership.active_operation``)."""
    found = await conn.scalar(
        select(operations.c.operation_id).where(
            operations.c.operation_id == operation_id, active_operation(operations)
        )
    )
    return found is not None


async def story_blocked(conn: AsyncConnection, story_id: str) -> bool:
    """Whether the erasure reconciler holds the story (#125): its rows are not claimed."""
    status = await conn.scalar(select(stories.c.status).where(stories.c.id == story_id))
    return str(getattr(status, "value", status)) == StoryStatus.BLOCKED_BY_ERASURE.value


async def cancel(conn: AsyncConnection, outbox_id: str) -> None:
    """``intended → cancelled``: the origin is no longer active; never sent."""
    await conn.execute(
        update(external_effect_outbox)
        .where(external_effect_outbox.c.id == outbox_id)
        .values(status=CANCELLED, claim_lease_until=None)
    )


async def mark_claimed(
    conn: AsyncConnection, outbox_id: str, now: datetime, lease: timedelta
) -> int:
    """``→ claimed`` with a fresh lease, one more attempt; answers the attempt (the claim's
    token)."""
    attempts = await conn.scalar(
        update(external_effect_outbox)
        .where(external_effect_outbox.c.id == outbox_id)
        .values(
            status=CLAIMED,
            claimed_at=now,
            claim_lease_until=now + lease,
            attempts=external_effect_outbox.c.attempts + 1,
        )
        .returning(external_effect_outbox.c.attempts)
    )
    return int(attempts or 0)


async def release(conn: AsyncConnection, outbox_id: str, attempts: int) -> bool:
    """``claimed → intended`` for the claim ``attempts`` when nothing was sent (the adapter
    refused before any request); ``False`` when the row moved on meanwhile."""
    o = external_effect_outbox.c
    gone = await conn.scalar(
        update(external_effect_outbox)
        .where(o.id == outbox_id, o.status == CLAIMED, o.attempts == attempts)
        .values(status=INTENDED, claimed_at=None, claim_lease_until=None)
        .returning(o.id)
    )
    return gone is not None


async def mark_unknown(conn: AsyncConnection, outbox_id: str, now: datetime, why: str) -> None:
    """A claimed row with no outcome whose lease lapsed, for a provider that does not
    deduplicate: ``unknown``, listed, never re-sent (FR-EXT-18)."""
    await _insert_outcome(conn, outbox_id, UNKNOWN, {"reason": why}, "", VIA_DISPATCHER, now)
    await conn.execute(
        update(external_effect_outbox)
        .where(external_effect_outbox.c.id == outbox_id)
        .values(status=UNKNOWN, claim_lease_until=None)
    )


# -- the timeline and erasure (#211) -------------------------------------------------------------


async def locked_of(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str]
) -> list[Row[Any]]:
    """Lock the executable rows the operations ``operation_ids`` wrote (``FOR UPDATE``: waits
    for a claim in flight, then reads the row as it committed), in id order."""
    if not operation_ids:
        return []
    o = external_effect_outbox.c
    query = select(external_effect_outbox).where(
        o.operation_id.in_(list(operation_ids)), o.executable.is_(True)
    )
    if story_id is not None:
        query = query.where(o.story_id == story_id)
    return list(await conn.execute(query.order_by(o.id).with_for_update()))


async def cancel_all(conn: AsyncConnection, outbox_ids: Sequence[str]) -> int:
    """``intended → cancelled`` for the (locked) rows ``outbox_ids``: never sent."""
    if not outbox_ids:
        return 0
    o = external_effect_outbox.c
    result = await conn.execute(
        update(external_effect_outbox)
        .where(o.id.in_(list(outbox_ids)), o.status == INTENDED)
        .values(status=CANCELLED, claim_lease_until=None)
    )
    return int(result.rowcount)


async def mark(
    conn: AsyncConnection,
    story_id: str,
    outbox_id: str | None,
    reason: str,
    *,
    detail: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    resolved_by: str | None = None,
) -> bool:
    """List row ``outbox_id`` (#661: or consumer-applied effect ``request_id``) with ``reason``
    until an explicit action resolves it (#218): a ``reconciliation_marks`` row, unless one is
    open for that row and reason already. ``detail``: codes only (#218: a late success's
    re-verification); ``resolved_by``: written already resolved (#661: the resolve action on a
    derived listing). Answers whether it wrote one."""
    if reason not in REASONS:
        raise ValueError(f"{reason!r} is not a reconciliation reason")
    if (outbox_id is None) == (request_id is None):
        raise ValueError("a mark names an outbox row or a request, not both")
    m = reconciliation_marks.c
    key = m.outbox_id if outbox_id is not None else m.request_id
    where: Any = m.resolved_by.is_(None)
    if request_id is not None:
        where = where & m.request_id.is_not(None)
    insert_ = pg_insert(reconciliation_marks).values(
        id=new_ulid(),
        story_id=story_id,
        outbox_id=outbox_id,
        request_id=request_id,
        reason=reason,
        detail=None if detail is None else dict(detail),
        resolved_by=resolved_by,
    )
    if resolved_by is None:
        insert_ = insert_.on_conflict_do_nothing(index_elements=[key, m.reason], index_where=where)
    written = await conn.scalar(insert_.returning(m.id))
    return written is not None


RESOLVED_SETTLE_NOW = "settle_now"
RESOLVED_COMPENSATED = "compensated"
"""#218: a mark's ``resolved_by``: the author's settle-now applied it, or the compensating
effect the author chose was confirmed."""


async def open_marks(
    conn: AsyncConnection, story_id: str, outbox_id: str, *, request: bool = False
) -> list[str]:
    """#218: the reasons row ``outbox_id`` (#661: ``request``: the consumer-applied effect of
    that id) is marked with and no action resolved yet."""
    m = reconciliation_marks.c
    key = m.request_id if request else m.outbox_id
    return [
        str(r)
        for r in await conn.scalars(
            select(m.reason)
            .where(m.story_id == story_id, key == outbox_id, m.resolved_by.is_(None))
            .order_by(m.since, m.id)
        )
    ]


async def resolve(
    conn: AsyncConnection,
    story_id: str,
    outbox_id: str,
    by: str,
    *,
    reasons: Sequence[str] | None = None,
    request: bool = False,
) -> int:
    """#218: resolve row ``outbox_id``'s open marks (of ``reasons``; ``None``: all; #661:
    ``request``: the consumer-applied effect's) with the explicit action ``by``; answers how
    many it resolved. The row leaves the listing unless its status lists it."""
    m = reconciliation_marks.c
    key = m.request_id if request else m.outbox_id
    query = update(reconciliation_marks).where(
        m.story_id == story_id, key == outbox_id, m.resolved_by.is_(None)
    )
    if reasons is not None:
        query = query.where(m.reason.in_(list(reasons)))
    return int((await conn.execute(query.values(resolved_by=by))).rowcount)


async def erased_since(conn: AsyncConnection, story_id: str, at: datetime | None) -> bool:
    """Whether a story-scope erasure of ``story_id`` was applied after ``at`` (its ledger row,
    #124/#125): the erasure-specific mid-flight check, never ``timeline_revision``."""
    if at is None:
        return False
    e = erasure_ledger.c
    found = await conn.scalar(
        select(literal(True))
        .where(e.story_ref == story_id, e.scope == "story", e.copied_from.is_(None), e.at > at)
        .limit(1)
    )
    return bool(found)


async def abandon(conn: AsyncConnection, outbox_id: str, now: datetime) -> None:
    """An erasure landed while the row's dispatch was in flight: ``unknown``, with an outcome
    record that holds no payload, ``erased_at`` set (listed redacted, never sent again)."""
    await _insert_outcome(conn, outbox_id, UNKNOWN, {}, "", VIA_DISPATCHER, now)
    o = external_effect_outbox.c
    await conn.execute(
        update(external_effect_outbox)
        .where(o.id == outbox_id)
        .values(
            status=UNKNOWN,
            claim_lease_until=None,
            erased_at=func.coalesce(o.erased_at, func.transaction_timestamp()),
        )
    )


# -- outcomes ------------------------------------------------------------------------------------


async def _insert_outcome(
    conn: AsyncConnection,
    outbox_id: str | None,
    kind: str,
    payload: Mapping[str, Any],
    raw_hash: str,
    via: str,
    now: datetime,
    *,
    effect_id: str | None = None,
    story_id: str | None = None,
    outcome_id: str | None = None,
) -> str:
    """One ``outbox_outcomes`` row for outbox row ``outbox_id`` or (#217) consumer-applied
    effect ``effect_id`` of ``story_id`` (with id ``outcome_id`` when given); answers its id."""
    if story_id is None:
        story_id = await conn.scalar(
            select(external_effect_outbox.c.story_id).where(
                external_effect_outbox.c.id == outbox_id
            )
        )
    outcome_id = outcome_id or new_ulid()
    await conn.execute(
        insert(outbox_outcomes).values(
            id=outcome_id,
            story_id=story_id,
            outbox_id=outbox_id,
            effect_id=effect_id,
            kind=kind,
            payload=dict(payload),
            raw_hash=raw_hash,
            received_via=via,
            received_at=now,
        )
    )
    return outcome_id


@dataclass(frozen=True, slots=True)
class Recorded:
    """What ``record_outcome`` did: the outcome record's kind and the row's status after it
    (#217: and the outcome record's id)."""

    kind: str
    status: str
    outcome_id: str = ""


async def record_outcome(
    conn: AsyncConnection,
    outbox_id: str,
    status: str,
    *,
    payload: Mapping[str, Any] | None = None,
    raw_hash: str = "",
    via: str = VIA_DISPATCHER,
    now: datetime,
    outcome_id: str | None = None,
) -> Recorded:
    """Record answer ``status`` (``confirmed``, ``failed`` or ``unknown``) for the row, which
    the caller has locked (see the module docstring); ``outcome_id``: the record's id (#217's
    ``outcome_recorded`` event names it).

    #218 (FR-EXT-22) the terminal rule: the row's first ``confirmed`` or ``failed`` record is
    its settled terminal outcome (one per row, a partial unique index). A later answer is a
    ``duplicate`` (the same, or ``unknown``) and changes nothing, or a ``conflict`` (the other
    terminal answer): recorded, the row listed ``outcome_conflict``, and nothing reversed. A
    compensating row's first confirmation resolves the marks of the row it compensates."""
    row = await locked(conn, outbox_id)
    if row is None:
        raise LookupError(f"outbox row {outbox_id} does not exist")
    current = str(row.status)
    kind = status
    after = current
    settled = await settled_kind(conn, str(row.story_id), outbox_id)
    if settled is None and current in TERMINAL:
        settled = current
    if settled is not None:
        kind = DUPLICATE if status in (settled, UNKNOWN) else CONFLICT
    elif current in (CLAIMED, UNKNOWN):
        after = status
    outcome_id = await _insert_outcome(
        conn, outbox_id, kind, payload or {}, raw_hash, via, now, outcome_id=outcome_id
    )
    if kind == CONFLICT:
        await mark(conn, str(row.story_id), outbox_id, "outcome_conflict")
    elif kind == CONFIRMED and row.compensates is not None:
        await resolve(conn, str(row.story_id), str(row.compensates), RESOLVED_COMPENSATED)
    # #212: a staged turn waiting on the row hears of the outcome when this commits.
    await conn.execute(select(func.pg_notify(OUTCOME_CHANNEL, outbox_id)))
    if after != current or after in TERMINAL:
        values: dict[str, Any] = {"status": after}
        if after in TERMINAL:
            values["claim_lease_until"] = None
        await conn.execute(
            update(external_effect_outbox)
            .where(external_effect_outbox.c.id == outbox_id)
            .values(**values)
        )
    return Recorded(kind, after, outcome_id)


async def settled_kind(conn: AsyncConnection, story_id: str, outbox_id: str) -> str | None:
    """#218: row ``outbox_id``'s settled terminal outcome (its first ``confirmed`` or ``failed``
    record), or ``None``."""
    o = outbox_outcomes.c
    found = await conn.scalar(
        select(o.kind)
        .where(o.story_id == story_id, o.outbox_id == outbox_id, o.kind.in_(sorted(TERMINAL)))
        .order_by(o.received_at, o.id)
        .limit(1)
    )
    return None if found is None else str(found)


async def settled_outcome(conn: AsyncConnection, story_id: str, outbox_id: str) -> str | None:
    """#218: the id of row ``outbox_id``'s settled terminal outcome record, or ``None``."""
    o = outbox_outcomes.c
    found = await conn.scalar(
        select(o.id)
        .where(o.story_id == story_id, o.outbox_id == outbox_id, o.kind.in_(sorted(TERMINAL)))
        .order_by(o.received_at, o.id)
        .limit(1)
    )
    return None if found is None else str(found)


async def record_effect_outcome(
    conn: AsyncConnection,
    story_id: str,
    effect_id: str,
    status: str,
    *,
    payload: Mapping[str, Any] | None = None,
    raw_hash: str = "",
    via: str = VIA_INGEST,
    now: datetime,
    outcome_id: str | None = None,
) -> Recorded:
    """#217: record the consumer's answer ``status`` for consumer-applied effect ``effect_id``
    (a #209 ``requested_effect``, which has no outbox row), with the outbox's first-terminal-wins
    rule (FR-EXT-22): after a ``confirmed`` or ``failed`` record, the same answer (or
    ``unknown``) is a ``duplicate`` and another terminal one a ``conflict``. Answers the record's
    kind, the effect's settled answer after it (``status`` when none came before) and its id.
    The caller serializes intake under the story lock (an ingest's operation commit)."""
    o = outbox_outcomes.c
    earlier = await conn.scalar(
        select(o.kind)
        .where(o.story_id == story_id, o.effect_id == effect_id, o.kind.in_(sorted(TERMINAL)))
        .order_by(o.received_at, o.id)
        .limit(1)
    )
    kind, after = status, status
    if earlier is not None:
        after = str(earlier)
        kind = DUPLICATE if status in (after, UNKNOWN) else CONFLICT
    outcome_id = await _insert_outcome(
        conn,
        None,
        kind,
        payload or {},
        raw_hash,
        via,
        now,
        effect_id=effect_id,
        story_id=story_id,
        outcome_id=outcome_id,
    )
    return Recorded(kind, after, outcome_id)


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One ``outbox_outcomes`` row as settlement reads it (#217)."""

    id: str
    story_id: str
    outbox_id: str | None
    effect_id: str | None
    kind: str
    payload: dict[str, Any]
    raw_hash: str
    received_via: str
    received_at: datetime
    settled: bool

    @property
    def effect(self) -> str:
        """The effect the outcome answers: the outbox row's id (also its effect id), or the
        consumer-applied effect's."""
        found = self.outbox_id or self.effect_id
        assert found is not None  # noqa: S101 - the table's check sets exactly one
        return found


def _outcome_of(row: Any) -> OutcomeRecord:
    return OutcomeRecord(
        id=str(row.id),
        story_id=str(row.story_id),
        outbox_id=None if row.outbox_id is None else str(row.outbox_id),
        effect_id=None if row.effect_id is None else str(row.effect_id),
        kind=str(row.kind),
        payload=dict(row.payload or {}),
        raw_hash=str(row.raw_hash or ""),
        received_via=str(row.received_via),
        received_at=row.received_at,
        settled=bool(row.settled),
    )


async def outcome(
    conn: AsyncConnection, outcome_id: str, *, lock: bool = False
) -> OutcomeRecord | None:
    """Outcome record ``outcome_id`` (``lock``: ``FOR UPDATE``), or ``None``."""
    query = select(outbox_outcomes).where(outbox_outcomes.c.id == outcome_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _outcome_of(row)


async def mark_settled(
    conn: AsyncConnection, outcome_id: str, *, outbox_id: str | None, event_id: str | None
) -> None:
    """#217: the outcome record is settled (the one column its trigger lets change) and, for an
    outbox row, the settlement's slot-positioned event is its ``outcome_event_id``."""
    await conn.execute(
        update(outbox_outcomes).where(outbox_outcomes.c.id == outcome_id).values(settled=True)
    )
    if outbox_id is not None and event_id is not None:
        await conn.execute(
            update(external_effect_outbox)
            .where(external_effect_outbox.c.id == outbox_id)
            .values(outcome_event_id=event_id)
        )


async def mark_sent(conn: AsyncConnection, outbox_id: str, at: datetime) -> None:
    """When the latest send started."""
    await conn.execute(
        update(external_effect_outbox)
        .where(external_effect_outbox.c.id == outbox_id)
        .values(sent_at=at)
    )


# -- reconciliation ------------------------------------------------------------------------------


class OutcomeView(BaseModel):
    """The latest answer recorded for a listed row."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    received_via: str
    received_at: datetime


COMPENSATED = "compensated"
"""#218: a listed row's ``status`` once the author chose to compensate it: a compensating row
(``compensated_by``) is on its way; the listing stays until that row is confirmed."""


class Reverification(BaseModel):
    """#218 (FR-EXT-22): a late success's re-verification against current state: ``passed``
    or ``failed``, and why (codes: ``precondition:availability``, ``precondition:ownership``,
    ``bounds:<constraint>``)."""

    model_config = ConfigDict(extra="forbid")

    reverification: Literal["passed", "failed"]
    reasons: list[str] = Field(default_factory=list)


class Listing(BaseModel):
    """One effect the consumer must reconcile: the outbox row (its id is the provider
    idempotency key), the provider and value, its status, why it is listed, since when, the
    effect, and the latest outcome recorded for it, if any. A row an erasure redacted (#211)
    is listed as ``{outbox_id, provider, status}`` with its reason and since only. #218: a
    ``late_success_after_expiry`` carries its re-verification; a compensated row's status is
    ``compensated`` and ``compensated_by`` names the compensating row. #661: a consumer-applied
    effect (no outbox row) is listed by its ``request_id``, its status the latest answer's kind
    (``requested`` before any)."""

    model_config = ConfigDict(extra="forbid")

    outbox_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    request_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """#661: a consumer-applied effect's id (its ``requested_effect``'s ``effect_id``) instead
    of an outbox row's."""
    provider: str
    value: str | None = Field(default=None, exclude_if=lambda v: v is None)
    status: str
    reason: Reason
    since: datetime
    effect: dict[str, Any] | None = Field(default=None, exclude_if=lambda v: v is None)
    outcome: OutcomeView | None = Field(default=None, exclude_if=lambda v: v is None)
    reverification: Reverification | None = Field(default=None, exclude_if=lambda v: v is None)
    compensated_by: str | None = Field(default=None, exclude_if=lambda v: v is None)


class CompensatingEffect(BaseModel):
    """#218 (FR-EXT-22): a compensating external effect (a refund), in #187's effect shape: an
    externally owned ``state`` (by name), the character's ``scope_key`` for a per-character
    State, a signed ``delta`` or a new ``value``, and the ``reason``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"state": "gold", "delta": 500, "reason": "refund"}]},
    )

    state: str = Field(min_length=1, max_length=64)
    scope_key: str | None = Field(default=None, min_length=1, max_length=64)
    delta: StrictInt | StrictFloat | None = None
    value: StrictBool | StrictInt | StrictFloat | StrictStr | None = None
    reason: str = Field(min_length=1, max_length=REASON_MAX)

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if (self.value is None) == (self.delta is None):
            raise ValueError("give a value or a delta, not both")
        return self


class CompensateRequest(BaseModel):
    """``POST /stories/{id}/reconciliation/{outbox_id}/compensate``: the compensating
    effect."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"effect": {"state": "gold", "scope_key": "cal", "delta": -500, "reason": "refund"}}
            ]
        },
    )

    effect: CompensatingEffect


class ResolveRequest(BaseModel):
    """#661: ``POST /stories/{id}/reconciliation/{listing}/resolve``: how a listed
    consumer-applied effect is resolved (``applied_elsewhere`` or ``abandoned``)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"resolution": "applied_elsewhere"}]}
    )

    resolution: Literal["applied_elsewhere", "abandoned"]


class Reconciliation(BaseModel):
    """``GET /stories/{id}/reconciliation``: every listed effect, oldest first."""

    model_config = ConfigDict(extra="forbid")

    listings: list[Listing]


def _open_mark() -> Any:
    m = reconciliation_marks.c
    return exists().where(m.outbox_id == external_effect_outbox.c.id, m.resolved_by.is_(None))


def _unsettled() -> Any:
    """#217 (FR-EXT-21): a ``confirmed`` row whose settlement is not in the active timeline: an
    active intent row it converted (``converted``, citing the row) has no active settlement row
    (``settles_change_id``) any more, because a truncation, swap or regeneration took the
    settlement's slot out (``confirmed_unsettled``). Derived, so a swap back clears it."""
    o = external_effect_outbox.c
    intent = state_changes.alias("intent")
    settlement = state_changes.alias("settlement")
    return and_(
        o.status == CONFIRMED,
        exists().where(
            intent.c.story_id == o.story_id,
            intent.c.status == StateChangeStatus.CONVERTED.value,
            o.id == any_(intent.c.prerequisite_effect_ids),
            active_only(intent),
            ~exists().where(
                settlement.c.story_id == intent.c.story_id,
                settlement.c.settles_change_id == intent.c.id,
                active_only(settlement),
            ),
        ),
    )


def _listed() -> Any:
    """The rows the status lists, executable rows only: unconfirmed (``intended`` with an
    active origin, or ``claimed``), ``failed``, ``unknown``, every row with an open mark
    (#211: ``reconciliation_required``, ``cancelled_on_reactivation``, and later reasons) and
    (#217) a confirmed row whose settlement left the active timeline (``_unsettled``)."""
    o = external_effect_outbox.c
    active = exists().where(
        operations.c.operation_id == o.operation_id, active_operation(operations)
    )
    return and_(
        o.executable.is_(True),
        or_(
            and_(o.status == INTENDED, active),
            o.status.in_((CLAIMED, FAILED, UNKNOWN)),
            _open_mark(),
            _unsettled(),
        ),
    )


async def _unsettled_of(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> set[str]:
    """Which of the rows ``ids`` are ``confirmed_unsettled`` (``_unsettled``)."""
    if not ids:
        return set()
    o = external_effect_outbox.c
    return {
        str(i)
        for i in await conn.scalars(
            select(o.id).where(o.story_id == story_id, o.id.in_(list(ids)), _unsettled())
        )
    }


async def _require_story(conn: AsyncConnection, story_id: str) -> None:
    if await conn.scalar(select(stories.c.id).where(stories.c.id == story_id)) is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})


async def _marks(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> dict[str, Any]:
    """Each row's latest open mark."""
    if not ids:
        return {}
    m = reconciliation_marks.c
    found = await conn.execute(
        select(m.outbox_id, m.reason, m.since, m.detail)
        .where(m.story_id == story_id, m.outbox_id.in_(list(ids)), m.resolved_by.is_(None))
        .order_by(m.since, m.id)
    )
    return {str(r.outbox_id): r for r in found}


def _status_reason(status: str) -> Reason:
    return "unconfirmed" if status in (INTENDED, CLAIMED) else status  # type: ignore[return-value]


async def expired_holds(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> set[str]:
    """#216: which of the outbox rows ``ids`` an active State hold waited on that was released
    with none of its prerequisites ``failed``: the hold expired (``reservation_expired``)."""
    if not ids:
        return set()
    c = state_changes.c
    released = [
        [str(p) for p in r.prerequisite_effect_ids]
        for r in await conn.execute(
            select(c.prerequisite_effect_ids).where(
                c.story_id == story_id,
                c.status == "released",
                active_only(state_changes),
                c.prerequisite_effect_ids.overlap(list(ids)),
            )
        )
    ]
    if not released:
        return set()
    o = external_effect_outbox.c
    named = {p for r in released for p in r}
    failed = set(
        await conn.scalars(
            select(o.id).where(o.story_id == story_id, o.id.in_(sorted(named)), o.status == FAILED)
        )
    )
    return {p for r in released if not failed & set(r) for p in r if p in ids}


async def compensations_of(
    conn: AsyncConnection, story_id: str, ids: Sequence[str]
) -> dict[str, str]:
    """#218: which of the rows ``ids`` a compensating row answers, and its id (the latest)."""
    if not ids:
        return {}
    o = external_effect_outbox.c
    found = await conn.execute(
        select(o.compensates, o.id)
        .where(o.story_id == story_id, o.compensates.in_(list(ids)), o.executable.is_(True))
        .order_by(o.created_at, o.id)
    )
    return {str(r.compensates): str(r.id) for r in found}


async def listings_in(conn: AsyncConnection, story_id: str) -> Reconciliation:
    """The story's reconciliation status, read in ``conn`` (404 for an unknown story)."""
    await _require_story(conn, story_id)
    o = external_effect_outbox.c
    rows = list(
        await conn.execute(
            select(external_effect_outbox)
            .where(o.story_id == story_id, _listed())
            .order_by(o.created_at, o.id)
        )
    )
    latest: dict[str, Any] = {}
    if rows:
        c = outbox_outcomes.c
        found = await conn.execute(
            select(c.outbox_id, c.kind, c.received_via, c.received_at)
            .where(c.story_id == story_id, c.outbox_id.in_([r.id for r in rows]))
            .order_by(c.received_at, c.id)
        )
        for answer in found:
            latest[str(answer.outbox_id)] = answer
    marked = await _marks(conn, story_id, [str(r.id) for r in rows])
    expired = await expired_holds(conn, story_id, [str(r.id) for r in rows])
    unsettled = await _unsettled_of(conn, story_id, [str(r.id) for r in rows])
    compensations = await compensations_of(conn, story_id, [str(r.id) for r in rows])
    listed: list[Listing] = []
    for row in rows:
        status = str(row.status)
        compensation = compensations.get(str(row.id))
        if compensation is not None:
            status = COMPENSATED
        outcome = latest.get(str(row.id))
        found_mark = marked.get(str(row.id))
        reason: Reason
        if found_mark is not None:
            reason = found_mark.reason
            since = found_mark.since
        elif str(row.id) in expired and status != FAILED:
            # #216: the State hold that waited on this row expired (FR-STATE-14).
            reason = "reservation_expired"
            since = row.created_at
        elif str(row.id) in unsettled:
            # #217: its settlement left the active timeline (FR-EXT-21).
            reason = "confirmed_unsettled"
            since = row.created_at if outcome is None else outcome.received_at
        else:
            reason = _status_reason(status)
            if outcome is not None and status in (FAILED, UNKNOWN):
                since = outcome.received_at
            else:
                since = row.claimed_at if status == CLAIMED and row.claimed_at else row.created_at
        checked = None
        if found_mark is not None and found_mark.detail:
            checked = Reverification.model_validate(found_mark.detail)
        if row.erased_at is not None:
            # #211: an erasure redacted the row (or abandoned its dispatch): ids only.
            listed.append(
                Listing(
                    outbox_id=str(row.id),
                    provider=str(row.provider_name),
                    status=status,
                    reason=reason,
                    since=since,
                )
            )
            continue
        listed.append(
            Listing(
                outbox_id=str(row.id),
                provider=str(row.provider_name),
                value=str(row.value_name),
                status=status,
                reason=reason,
                since=since,
                effect=public_effect(row.effect),
                outcome=None
                if outcome is None
                else OutcomeView(
                    kind=str(outcome.kind),
                    received_via=str(outcome.received_via),
                    received_at=outcome.received_at,
                ),
                reverification=checked,
                compensated_by=compensation,
            )
        )
    listed += await consumer_listings(conn, story_id)
    return Reconciliation(listings=listed)


REQUESTED = "requested"
"""#661: a consumer-applied effect's listed status before any answer was recorded."""


async def expired_requests(conn: AsyncConnection, story_id: str) -> set[str]:
    """#661 (#216's rule for a consumer-applied effect): the request ids an active State hold
    waited on that was released with none of its prerequisites failed (``reservation_expired``),
    leaving out outbox rows and requests that rule already listed once (a mark of that reason,
    open or resolved)."""
    c = state_changes.c
    released = [
        [str(p) for p in r.prerequisite_effect_ids]
        for r in await conn.execute(
            select(c.prerequisite_effect_ids).where(
                c.story_id == story_id,
                c.status == "released",
                active_only(state_changes),
                func.cardinality(c.prerequisite_effect_ids) > 0,
            )
        )
    ]
    named = {p for r in released for p in r}
    if not named:
        return set()
    o, x, m = external_effect_outbox.c, outbox_outcomes.c, reconciliation_marks.c
    outbox_ids = set(await conn.scalars(select(o.id).where(o.id.in_(sorted(named)))))
    failed = set(
        await conn.scalars(
            select(x.effect_id).where(
                x.story_id == story_id, x.effect_id.in_(sorted(named)), x.kind == FAILED
            )
        )
    ) | set(
        await conn.scalars(
            select(o.id).where(o.story_id == story_id, o.id.in_(sorted(named)), o.status == FAILED)
        )
    )
    listed_once = set(
        await conn.scalars(
            select(m.request_id).where(
                m.story_id == story_id,
                m.request_id.in_(sorted(named)),
                m.reason == "reservation_expired",
            )
        )
    )
    return {
        p
        for r in released
        if not failed & set(r)
        for p in r
        if p not in outbox_ids and p not in listed_once
    }


async def consumer_listings(conn: AsyncConnection, story_id: str) -> list[Listing]:
    """#661: the consumer-applied effects the story lists, oldest first: each one with an open
    mark (``confirmed_discarded_origin``, ``settlement_conflict``,
    ``late_success_after_expiry``) under its reason, and each a released hold waited on
    (``reservation_expired``, derived), with its request (the ``requested_effect`` payload),
    the State's provider and the latest answer recorded for it."""
    from epistrel.core.tables.events import events
    from epistrel.core.tables.states import state_definitions

    m = reconciliation_marks.c
    marks: dict[str, Any] = {}
    for r in await conn.execute(
        select(m.request_id, m.reason, m.since, m.detail)
        .where(m.story_id == story_id, m.request_id.is_not(None), m.resolved_by.is_(None))
        .order_by(m.since, m.id)
    ):
        marks[str(r.request_id)] = r
    expired = await expired_requests(conn, story_id)
    ids = sorted(set(marks) | expired)
    if not ids:
        return []
    e = events.c
    requested = {
        str(r.payload["effect_id"]): r
        for r in await conn.execute(
            select(e.payload, e.valid_time).where(
                e.story_id == story_id,
                e.type == "requested_effect",
                e.payload["effect_id"].astext.in_(ids),
            )
        )
    }
    x = outbox_outcomes.c
    latest: dict[str, Any] = {}
    for answered in await conn.execute(
        select(x.effect_id, x.kind, x.received_via, x.received_at)
        .where(x.story_id == story_id, x.effect_id.in_(ids))
        .order_by(x.received_at, x.id)
    ):
        latest[str(answered.effect_id)] = answered
    d = state_definitions.c
    providers = {
        str(r.name): r
        for r in await conn.execute(
            select(d.name, d.provider_id, d.external_value_name).where(d.story_id == story_id)
        )
    }
    found: list[tuple[datetime, Listing]] = []
    for request_id in ids:
        event = requested.get(request_id)
        if event is None:
            continue
        payload = dict(event.payload or {})
        state = str(payload.get("state") or "")
        owner = providers.get(state)
        answer = latest.get(request_id)
        status = str(answer.kind) if answer is not None and answer.kind in TERMINAL else REQUESTED
        mark_ = marks.get(request_id)
        reason: Reason = mark_.reason if mark_ is not None else "reservation_expired"
        since = mark_.since if mark_ is not None else event.valid_time
        effect = {
            k: v
            for k, v in payload.items()
            if k in ("kind", "state", "scope_key", "delta", "value", "minutes", "reason")
        }
        found.append(
            (
                since,
                Listing(
                    request_id=request_id,
                    provider=str(owner.provider_id) if owner and owner.provider_id else "consumer",
                    value=None if owner is None else (owner.external_value_name or state),
                    status=status,
                    reason=reason,
                    since=since,
                    effect=effect,
                    outcome=None
                    if answer is None
                    else OutcomeView(
                        kind=str(answer.kind),
                        received_via=str(answer.received_via),
                        received_at=answer.received_at,
                    ),
                    reverification=None
                    if mark_ is None or not mark_.detail
                    else Reverification.model_validate(mark_.detail),
                ),
            )
        )
    return [listing for _, listing in sorted(found, key=lambda f: (f[0], f[1].request_id or ""))]


async def listings(db: AsyncEngine | AsyncConnection, story_id: str) -> Reconciliation:
    """The story's reconciliation status (see the module docstring)."""
    if isinstance(db, AsyncConnection):
        return await listings_in(db, story_id)
    async with db.connect() as conn:
        return await listings_in(conn, story_id)


async def count(conn: AsyncConnection, story_id: str) -> int | None:
    """How many effects the story's reconciliation status lists; ``None`` when the story has
    never written an outbox row (the respond response then carries no ``reconciliation``)."""
    o = external_effect_outbox.c
    if await conn.scalar(select(o.id).where(o.story_id == story_id).limit(1)) is None:
        return None
    found = await conn.scalar(
        select(func.count())
        .select_from(external_effect_outbox)
        .where(o.story_id == story_id, _listed())
    )
    return int(found or 0) + len(await consumer_listings(conn, story_id))


async def summary(db: AsyncEngine | AsyncConnection, story_id: str) -> dict[str, int] | None:
    """The respond response's ``reconciliation``: ``{count}`` (``count``), ``None`` when the
    story has never written an outbox row."""
    if isinstance(db, AsyncConnection):
        found = await count(db, story_id)
    else:
        async with db.connect() as conn:
            found = await count(conn, story_id)
    return None if found is None else {"count": found}


__all__ = [
    "COMPENSATED",
    "INTERNAL_EFFECT_KEYS",
    "REASONS",
    "REQUESTED",
    "REQUEST_PENDING",
    "RESOLVED_COMPENSATED",
    "RESOLVED_SETTLE_NOW",
    "TERMINAL",
    "CompensateRequest",
    "CompensatingEffect",
    "Intended",
    "Listing",
    "OutcomeRecord",
    "OutcomeView",
    "Reason",
    "Reconciliation",
    "Recorded",
    "ResolveRequest",
    "Reverification",
    "abandon",
    "cancel",
    "cancel_all",
    "claimable",
    "compensations_of",
    "consumer_listings",
    "count",
    "erased_since",
    "expired_holds",
    "expired_requests",
    "listings",
    "listings_in",
    "locked",
    "locked_of",
    "mark",
    "mark_claimed",
    "mark_sent",
    "mark_settled",
    "mark_unknown",
    "open_marks",
    "origin_active",
    "outcome",
    "public_effect",
    "record_effect_outcome",
    "record_outcome",
    "release",
    "request_effects",
    "request_status",
    "request_statuses",
    "resolve",
    "settled_kind",
    "settled_outcome",
    "story_blocked",
    "summary",
    "write_intended",
]
