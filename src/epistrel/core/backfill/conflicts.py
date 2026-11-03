"""A backfill's contradictions under ``backfill_conflict_policy = ask`` (FR-PROF-10; #230).

Reconciliation (``core.backfill.reconcile``) leaves each contradicting key of an ``ask`` backfill
``open``, its later statements unwritten, and finishes its pass; the backfill then ends its run
``paused_for_conflicts`` and the story stays paused (409 ``story_paused``, ``cause:
backfill_conflicts``, ``conflicts_open``) until the author settles them:

- **listing** (``listing``): each conflict with its statements (position, speaker, the quoted
  transcript text, the value) and a **recommended** answer (owner decision 4): the
  ``last_mention`` statement, ``reason: "last_mention"``;
- **resolve** (``resolve``: ``choose`` a statement, a ``value``, or ``accept_recommendation``):
  a ``value`` none of the statements states is normalized against the conflict's key (#708: a
  literal of the statements' kind and unit, or the story's entity by id, name or alias; 422
  ``invalid_resolution`` otherwise), and ``valid_from``/``valid_to`` (story minutes) bound the
  value written; the chosen statement is canon for the overlap (written under the
  backfill's operation with provenance ``author``: a principal chose it) and the others, when
  a character spoke them, are recorded as in-world claims. Resolving the last open conflict
  runs the backfill's completion in the same request (``complete``);
- **resume** (``resume``, ``POST /stories/{id}/backfill`` with ``ignore_warnings``): every
  still-open conflict becomes ``uncertain`` and the backfill completes. Where the target
  configuration runs commitment, each uncertain key is an open thunk (#146) listing the
  conflicting values, so a later claim resolves it through reconciliation instead of being
  accepted as novel; otherwise the row is the only record. The earlier value the pass wrote
  stays canon (the later ones were never written).

Statements hold no text: the listing quotes the transcript, so an erasure's redaction holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal, Self

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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.canon import facts as canon
from epistrel.core.enums import Membership, OpenSlot, RecordProvenance
from epistrel.core.errors import (
    BackfillConflictClosed,
    BackfillConflictNotFound,
    BackfillNotAwaitingConflicts,
    BackfillNotFound,
    InvalidResolution,
)
from epistrel.core.membership import flip_membership
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.tables import (
    backfill_conflicts,
    entities,
    entity_aliases,
    story_backfills,
    transcript_messages,
)
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

AWAITING = "paused_for_conflicts"
"""The backfill's status (and the pause's cause, ``backfill_conflicts``) while conflicts are
open."""
CAUSE = "backfill_conflicts"
QUOTE_MAX = 500
LIST_MAX = 200
LIST_DEFAULT = 50
RECOMMENDED_REASON = "last_mention"

Which = Literal["open", "all"]
Complete = Callable[[AsyncConnection, Any], Awaitable[None]]
"""``complete(conn, row)``: #228's completion transaction for the backfill ``row``."""


class Statement(BaseModel):
    """One statement of a conflict: where it is, who said it, the quoted text and its value."""

    statement_id: str
    event_id: str | None
    turn: int
    position: int
    speaker: str | None
    source: str
    quote: str | None
    value: Any


class Recommended(BaseModel):
    """The engine's recommended answer (owner decision 4): the latest statement."""

    choose: str
    reason: str = RECOMMENDED_REASON


class Conflict(BaseModel):
    """A contradiction the backfill met (``GET /stories/{id}/backfill``'s ``conflict_list``)."""

    id: str
    status: str
    subject: dict[str, Any]
    statements: list[Statement]
    recommended: Recommended | None
    resolution: dict[str, Any] | None
    resolved_by: str | None
    resolved_at: datetime | None


class ResolveRequest(BaseModel):
    """``POST /stories/{id}/backfill/conflicts/{cid}``: exactly one of ``choose`` (a statement
    id), ``value`` (a normalized value: one a statement states, or a new one, #708) or
    ``accept_recommendation``; ``valid_from``/``valid_to`` (story minutes) bound a ``choose``
    or ``value`` resolution."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"accept_recommendation": True}]}
    )

    choose: StrictStr | None = Field(default=None, max_length=200)
    value: StrictStr | StrictInt | StrictFloat | None = None
    valid_from: StrictStr | StrictInt | None = None
    valid_to: StrictStr | StrictInt | None = None
    accept_recommendation: StrictBool = False

    @model_validator(mode="after")
    def _one(self) -> Self:
        given = [
            self.choose is not None,
            self.value is not None,
            self.accept_recommendation,
        ]
        if sum(given) != 1:
            raise ValueError("send exactly one of choose, value and accept_recommendation")
        return self


@dataclass(frozen=True, slots=True)
class Resolved:
    """A resolution's answer: the conflict, and the backfill's status and open count."""

    conflict: Conflict
    status: str
    conflicts_open: int
    timeline_revision: int = 0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Resumed:
    """``POST /stories/{id}/backfill`` on a backfill awaiting conflicts: without
    ``ignore_warnings`` the open conflicts (nothing changes), with it the completed backfill and
    how many conflicts were left uncertain."""

    applied: bool
    status: str
    conflicts_open: int
    conflicts: list[Conflict]
    conflicts_made_uncertain: int = 0


# -- reads ---------------------------------------------------------------------------------------


def _ordered(statements: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(statements, key=lambda s: (int(s.get("turn") or 0), int(s.get("beat") or 0)))


def recommended(statements: Sequence[dict[str, Any]]) -> Recommended | None:
    """The ``last_mention`` statement (the latest; a same-position tie goes to the later)."""
    ordered = _ordered(statements)
    return Recommended(choose=str(ordered[-1]["statement_id"])) if ordered else None


async def _quotes(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> dict[str, str]:
    t = transcript_messages.c
    rows = await conn.execute(
        select(t.message_id, t.text).where(t.story_id == story_id, t.message_id.in_(list(ids)))
    )
    return {str(r.message_id): str(r.text)[:QUOTE_MAX] for r in rows}


async def view(conn: AsyncConnection, row: Any) -> Conflict:
    statements = list(row.statements or [])
    quotes = await _quotes(
        conn, str(row.story_id), [str(s["event_id"]) for s in statements if s.get("event_id")]
    )
    return Conflict(
        id=str(row.id),
        status=str(row.status),
        subject=dict(row.subject or {}),
        statements=[
            Statement(
                statement_id=str(s.get("statement_id")),
                event_id=s.get("event_id"),
                turn=int(s.get("turn") or 0),
                position=int(s.get("position") or s.get("turn") or 0),
                speaker=s.get("speaker"),
                source=str(s.get("source") or "transcript"),
                quote=quotes.get(str(s.get("event_id"))),
                value=s.get("value"),
            )
            for s in _ordered(statements)
        ],
        recommended=recommended(statements) if row.status == "open" else None,
        resolution=None if row.resolution is None else dict(row.resolution),
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
    )


async def listing(
    conn: AsyncConnection, backfill_id: str, which: Which = "open", limit: int = LIST_DEFAULT
) -> list[Conflict]:
    """The backfill's conflicts (``open`` ones, or ``all``), oldest first, at most ``limit``."""
    t = backfill_conflicts.c
    query = select(backfill_conflicts).where(t.backfill_id == backfill_id)
    if which == "open":
        query = query.where(t.status == "open")
    rows = await conn.execute(query.order_by(t.id).limit(min(limit, LIST_MAX)))
    return [await view(conn, r) for r in rows]


async def open_count(conn: AsyncConnection, backfill_id: str) -> int:
    t = backfill_conflicts.c
    return int(
        await conn.scalar(
            select(func.count())
            .select_from(backfill_conflicts)
            .where(t.backfill_id == backfill_id, t.status == "open")
        )
        or 0
    )


# -- writes --------------------------------------------------------------------------------------


def _shim(row: Any, turn: int, variant: int) -> Any:
    return SimpleNamespace(
        operation_id=str(row.operation_id),
        story_id=str(row.story_id),
        principal=str(row.requested_by),
        narrative_turn=turn,
        turn_variant=variant,
        base_revision=None,
    )


async def _locked_backfill(conn: AsyncConnection, story_id: str) -> Any:
    from epistrel.core.backfill import latest_row

    latest = await latest_row(conn, story_id)
    if latest is None:
        raise BackfillNotFound("the story has no backfill", {"story_id": story_id})
    return (
        await conn.execute(
            select(story_backfills).where(story_backfills.c.id == latest.id).with_for_update()
        )
    ).one()


def _awaiting(row: Any) -> None:
    if str(row.status) != AWAITING:
        raise BackfillNotAwaitingConflicts(
            "the story's backfill is not paused for conflicts",
            {"backfill_id": str(row.id), "status": str(row.status)},
        )


def _minutes(value: str | int | None, field: str) -> int | None:
    """A window bound: story minutes (an int, or its digits as a string)."""
    if value is None or isinstance(value, int):
        return value
    text_value = value.strip()
    if text_value.lstrip("-").isdigit():
        return int(text_value)
    raise InvalidResolution("a resolution's window is in story minutes", {"field": f"body.{field}"})


async def _entity(conn: AsyncConnection, story_id: str, value: str) -> str | None:
    """The story's entity ``value`` names: its id, or its one canonical name or alias
    (case-insensitive); ``None`` when none or several match."""
    e, a = entities.c, entity_aliases.c
    found = await conn.scalar(select(e.id).where(e.story_id == story_id, e.id == value))
    if found is not None:
        return str(found)
    named = {
        str(i)
        for (i,) in await conn.execute(
            select(e.id).where(
                e.story_id == story_id, func.lower(e.canonical_name) == value.lower()
            )
        )
    } | {
        str(i)
        for (i,) in await conn.execute(
            select(a.entity_id).where(a.story_id == story_id, func.lower(a.alias) == value.lower())
        )
    }
    return next(iter(named)) if len(named) == 1 else None


async def _normalized(
    conn: AsyncConnection, story_id: str, statements: Sequence[dict[str, Any]], value: Any
) -> dict[str, Any]:
    """#708: a resolution's new value, normalized against the conflict's key (a literal of the
    statements' kind and unit, or an entity of the story named by id, name or alias): a
    statement of no event, at the latest statement's position. 422 ``invalid_resolution`` when
    it does not normalize."""
    from pydantic import ValidationError

    from epistrel.core.propositions.model import TypedLiteral

    latest = _ordered(statements)[-1]
    base = Proposition.model_validate(latest["proposition"])
    invalid = InvalidResolution(
        "the value does not normalize against the conflict's predicate", {"field": "body.value"}
    )
    if base.literal is not None:
        try:
            literal = TypedLiteral(kind=base.literal.kind, value=value, unit=base.literal.unit)
        except ValidationError:
            raise invalid from None
        made = base.model_copy(update={"literal": literal})
    elif base.object_id is not None and isinstance(value, str):
        entity = await _entity(conn, story_id, value)
        if entity is None:
            raise invalid
        made = base.model_copy(update={"object_id": entity})
    else:
        raise invalid
    return {
        **latest,
        "statement_id": None,
        "event_id": None,
        "speaker": None,
        "fact_id": None,
        "value": value,
        "proposition": made.model_dump(mode="json"),
    }


def _windowed(chosen: dict[str, Any], request: ResolveRequest) -> dict[str, Any]:
    """#708: the chosen value over the request's ``valid_from``/``valid_to`` window (story
    minutes; an open bound stays the statement's), always written as a new fact."""
    start = _minutes(request.valid_from, "valid_from")
    end = _minutes(request.valid_to, "valid_to")
    if start is None and end is None:
        return chosen
    if start is not None and end is not None and end <= start:
        raise InvalidResolution("the window ends before it starts", {"field": "body.valid_to"})
    proposition = Proposition.model_validate(chosen["proposition"])
    possible = proposition.valid_time.possible()
    window = Interval.of(
        start if start is not None else possible[0], end if end is not None else possible[1]
    )
    made = proposition.model_copy(update={"valid_time": window})
    return {**chosen, "fact_id": None, "proposition": made.model_dump(mode="json")}


def _chosen(statements: Sequence[dict[str, Any]], request: ResolveRequest) -> dict[str, Any]:
    if request.accept_recommendation:
        found = recommended(statements)
        wanted = None if found is None else found.choose
        return next(s for s in statements if s.get("statement_id") == wanted)
    for one in _ordered(statements):
        if request.choose is not None and one.get("statement_id") == request.choose:
            return one
        if request.value is not None and str(one.get("value")) == str(request.value):
            return one
    raise InvalidResolution(
        "the resolution names none of the conflict's statements",
        {"field": "body.choose" if request.choose is not None else "body.value"},
    )


async def resolve(
    db: AsyncEngine,
    story_id: str,
    conflict_id: str,
    request: ResolveRequest,
    *,
    principal: str,
    complete: Complete,
) -> Resolved:
    """Settle one open conflict (see the module docstring)."""
    from epistrel.core.backfill.reconcile import in_world_claim

    async with db.begin() as conn:
        row = await _locked_backfill(conn, story_id)
        t = backfill_conflicts.c
        found = (
            await conn.execute(
                select(backfill_conflicts)
                .where(t.id == conflict_id, t.story_id == story_id)
                .with_for_update()
            )
        ).one_or_none()
        if found is None:
            raise BackfillConflictNotFound(
                "the story's backfill has no such conflict", {"conflict_id": conflict_id}
            )
        _awaiting(row)
        if str(found.status) != "open":
            raise BackfillConflictClosed(
                "the conflict is not open",
                {"conflict_id": conflict_id, "status": str(found.status)},
            )
        statements = list(found.statements or [])
        try:
            chosen = _chosen(statements, request)
        except InvalidResolution:
            if request.value is None:
                raise
            chosen = await _normalized(conn, story_id, statements, request.value)
        if request.value is not None or request.choose is not None:
            chosen = _windowed(chosen, request)
        op = str(row.operation_id)
        await flip_membership(conn, [op], Membership.ACTIVE, story_id=story_id)
        if chosen.get("fact_id") is None:
            await canon.commit_fact(
                conn,
                story_id,
                Proposition.model_validate(chosen["proposition"]),
                provenance=RecordProvenance.AUTHOR,
                operation=_shim(row, int(chosen["turn"]), int(chosen.get("variant") or 0)),
                source_event_ids=[chosen["event_id"]] if chosen.get("event_id") else [],
                beat=int(chosen.get("beat") or 1),
            )
        for loser in statements:
            if loser.get("statement_id") != chosen.get("statement_id"):
                await in_world_claim(conn, story_id, op, loser)
        via = (
            "recommendation"
            if request.accept_recommendation
            else ("choose" if request.choose is not None else "value")
        )
        await conn.execute(
            update(backfill_conflicts)
            .where(t.id == conflict_id)
            .values(
                status="resolved",
                resolution={
                    "choice": {
                        "statement_id": chosen.get("statement_id"),
                        **({"value": request.value} if chosen.get("statement_id") is None else {}),
                        **(
                            {
                                "valid_from": _minutes(request.valid_from, "valid_from"),
                                "valid_to": _minutes(request.valid_to, "valid_to"),
                            }
                            if request.valid_from is not None or request.valid_to is not None
                            else {}
                        ),
                    },
                    "via": via,
                    "by": principal,
                },
                resolved_by=principal,
                resolved_at=clock.now(),
            )
        )
        left = await open_count(conn, str(row.id))
        status = AWAITING
        if left == 0:
            await complete(conn, row)
            status = "completed"
        else:
            await flip_membership(conn, [op], Membership.DORMANT, story_id=story_id)
        log.info("backfill conflict resolved", backfill_id=str(row.id), left=left)
        settled = (await conn.execute(select(backfill_conflicts).where(t.id == conflict_id))).one()
        return Resolved(await view(conn, settled), status, left)


async def _commitment_on(conn: AsyncConnection, row: Any) -> bool:
    """Whether the backfill's target configuration runs commitment."""
    from epistrel.core.commit import lifecycle
    from epistrel.core.profiles import effective

    story_id = str(row.story_id)
    current = await effective.effective_configuration(conn, story_id, validate=False)
    target = dict(row.target or {})
    document = current.document
    if target.get("profile_id"):
        held = await effective.assignable(
            conn, str(target["profile_id"]), version=target.get("pin")
        )
        document = held.document
    settings = effective.merged_settings(
        current.defaults, document, target.get("overrides", current.overrides)
    )
    return lifecycle.commitment_enabled(settings)


async def _thunk(conn: AsyncConnection, row: Any, conflict: Any) -> None:
    """An uncertain key's open thunk, listing the conflicting values."""
    from epistrel.core.commit import lifecycle
    from epistrel.core.commit.sketch import Candidate, ThunkSketch

    statements = _ordered(list(conflict.statements or []))
    first = Proposition.model_validate(statements[0]["proposition"])
    sketch = ThunkSketch(
        predicate_id=first.predicate_id,
        subject_id=first.subject_id,
        relation_text=first.predicate_id,
        open_slot=OpenSlot.OBJECT if first.object_id is not None else OpenSlot.LITERAL,
        candidates=tuple(
            Candidate(text=f"{s.get('value')} (turn {s.get('turn')})"[:200]) for s in statements
        ),
    )
    await lifecycle.introduce(
        conn,
        _shim(row, int(statements[0]["turn"]), 0),
        sketch,
        source_event_ids=[str(s["event_id"]) for s in statements if s.get("event_id")],
        on=True,
    )


async def resume(
    conn: AsyncConnection,
    row: Any,
    *,
    ignore_warnings: bool,
    principal: str,
    complete: Complete,
) -> Resumed:
    """``POST /stories/{id}/backfill`` on a backfill awaiting conflicts (see the module
    docstring); the caller holds the backfill's row lock."""
    _awaiting(row)
    found = await listing(conn, str(row.id), "open", LIST_MAX)
    if not ignore_warnings:
        return Resumed(False, AWAITING, len(found), found)
    t = backfill_conflicts.c
    rows = list(
        await conn.execute(
            select(backfill_conflicts).where(t.backfill_id == row.id, t.status == "open")
        )
    )
    op = str(row.operation_id)
    await flip_membership(conn, [op], Membership.ACTIVE, story_id=str(row.story_id))
    thunks = await _commitment_on(conn, row)
    for conflict in rows:
        if thunks:
            await _thunk(conn, row, conflict)
        await conn.execute(
            update(backfill_conflicts)
            .where(t.id == conflict.id)
            .values(
                status="uncertain",
                resolution={"ignore_warnings": True, "thunk": thunks},
                resolved_by=principal,
                resolved_at=clock.now(),
            )
        )
    await complete(conn, row)
    log.info("backfill resumed past conflicts", backfill_id=str(row.id), uncertain=len(rows))
    return Resumed(True, "completed", 0, [], conflicts_made_uncertain=len(rows))


__all__ = [
    "AWAITING",
    "CAUSE",
    "Conflict",
    "Recommended",
    "ResolveRequest",
    "Resolved",
    "Resumed",
    "Statement",
    "listing",
    "open_count",
    "recommended",
    "resolve",
    "resume",
    "view",
]
