"""Reading amendments (FR-REWRITE-9,10,11; #238): the list and one amendment's detail.

Omniscient reads over the shared tables, on any pipeline (they never dispatch): a Lite
amendment's ``relies_on`` and ``dependents`` are empty (it has no #220 edges).

- **list**: ``{id, kind, status, author, tx_time, parts_count, reverted_by, undoes, redoes,
  session_id}``, newest ``tx_time`` first (ties by id, newest first), optionally by ``status``;
  a cursor (the last id served) pages it, ``LIST_DEFAULT`` a page (at most ``LIST_MAX``).
- **detail**: the list fields plus ``parts`` (every part row, by ordinal), ``relies_on`` (the
  #220 edges from its parts: what it was recorded as relying on) and ``dependents`` (#220's
  ``dependents()`` at depth 1 of its parts and the facts they wrote, with ``truncated``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import edges
from epistrel.core.errors import AmendmentNotFound, StoryNotFound
from epistrel.core.tables import amendment_parts, amendments, stories
from epistrel.core.tables.edges import dependency_edges

LIST_DEFAULT = 50
LIST_MAX = 200
PART_COLUMNS = (
    "id",
    "ordinal",
    "kind",
    "status",
    "subject_id",
    "predicate_id",
    "qualifiers",
    "proposition_id",
    "state_id",
    "scope_key",
    "valid_from_minutes",
    "valid_to_minutes",
    "valid_spec",
    "written_fact_id",
    "superseded_fact_ids",
    "written_state_change_id",
    "timing_uncertain",
    "position_bound",
    "record_table",
    "record_id",
    "action",
    "strategy",
    "patch_before",
    "patch_after",
    "tx_from",
    "tx_to",
)


class AmendmentSummary(BaseModel):
    """An amendment in a list (#238)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    status: str
    author: str
    tx_time: datetime
    parts_count: int
    reverted_by: str | None = None
    undoes: str | None = None
    redoes: str | None = None
    session_id: str | None = None


class AmendmentPage(BaseModel):
    """``GET /stories/{id}/amendments``: newest first; ``next_cursor`` pages on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AmendmentSummary]
    next_cursor: str | None = None


class AmendmentDetail(AmendmentSummary):
    """``GET /stories/{id}/amendments/{aid}``: the summary, the part rows, what its parts were
    recorded as relying on (``relies_on``) and the #220 dependents of its parts and the facts
    they wrote (``dependents``, depth 1; ``truncated`` when the search's cap cut it)."""

    parts: list[dict[str, Any]] = Field(default_factory=list)
    relies_on: list[dict[str, Any]] = Field(default_factory=list)
    dependents: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False


def _jsonable(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _summary(row: Any, parts_count: int) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": str(row.kind),
        "status": str(row.status),
        "author": str(row.author),
        "tx_time": row.tx_time,
        "parts_count": parts_count,
        "reverted_by": None if row.reverted_by is None else str(row.reverted_by),
        "undoes": None if row.undoes is None else str(row.undoes),
        "redoes": None if row.redoes is None else str(row.redoes),
        "session_id": None if row.session_id is None else str(row.session_id),
    }


async def _counts(conn: AsyncConnection, ids: list[str]) -> dict[str, int]:
    p = amendment_parts.c
    found = await conn.execute(
        select(p.amendment_id, func.count()).where(p.amendment_id.in_(ids)).group_by(p.amendment_id)
    )
    return {str(a): int(n) for a, n in found}


async def list_amendments(
    conn: AsyncConnection,
    story_id: str,
    *,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = LIST_DEFAULT,
) -> AmendmentPage:
    """The story's amendments, newest first (see the module docstring); 404
    ``story_not_found``."""
    if await conn.scalar(select(stories.c.id).where(stories.c.id == story_id)) is None:
        raise StoryNotFound("no such story", {"story_id": story_id})
    a = amendments.c
    query = select(amendments).where(a.story_id == story_id)
    if status is not None:
        query = query.where(a.status == status)
    if cursor is not None:
        at = (await conn.execute(select(a.tx_time).where(a.id == cursor))).scalar_one_or_none()
        if at is not None:
            query = query.where(or_(a.tx_time < at, and_(a.tx_time == at, a.id < cursor)))
    size = max(1, min(int(limit), LIST_MAX))
    rows = list(await conn.execute(query.order_by(a.tx_time.desc(), a.id.desc()).limit(size + 1)))
    page = rows[:size]
    counts = await _counts(conn, [str(r.id) for r in page])
    return AmendmentPage(
        items=[AmendmentSummary(**_summary(r, counts.get(str(r.id), 0))) for r in page],
        next_cursor=str(page[-1].id) if len(rows) > size and page else None,
    )


async def amendment_row(conn: AsyncConnection, story_id: str, amendment_id: str) -> Any:
    """The amendment's row; 404 ``story_not_found``, or ``amendment_not_found`` when the story
    has none by that id."""
    if await conn.scalar(select(stories.c.id).where(stories.c.id == story_id)) is None:
        raise StoryNotFound("no such story", {"story_id": story_id})
    row = (
        await conn.execute(
            select(amendments).where(
                amendments.c.id == amendment_id, amendments.c.story_id == story_id
            )
        )
    ).one_or_none()
    if row is None:
        raise AmendmentNotFound("no such amendment", {"amendment_id": amendment_id})
    return row


async def part_rows(conn: AsyncConnection, amendment_id: str) -> list[dict[str, Any]]:
    p = amendment_parts.c
    return [
        dict(r._mapping)
        for r in await conn.execute(
            select(amendment_parts).where(p.amendment_id == amendment_id).order_by(p.ordinal)
        )
    ]


async def get_amendment(conn: AsyncConnection, story_id: str, amendment_id: str) -> AmendmentDetail:
    """One amendment with its parts and edges (see the module docstring)."""
    row = await amendment_row(conn, story_id, amendment_id)
    parts = await part_rows(conn, amendment_id)
    part_ids = [str(p["id"]) for p in parts]
    e = dependency_edges.c
    relies = [
        {
            "edge_type": str(getattr(r.edge_type, "value", r.edge_type)),
            "part_id": str(r.dependent_id),
            "target": {
                "kind": str(getattr(r.target_kind, "value", r.target_kind)),
                "id": str(r.target_id),
            },
        }
        for r in await conn.execute(
            select(e.edge_type, e.dependent_id, e.target_kind, e.target_id)
            .where(
                e.story_id == story_id,
                e.dependent_table == "amendment_parts",
                e.dependent_id.in_(part_ids),
            )
            .order_by(e.id)
        )
    ]
    written = tuple(str(p["written_fact_id"]) for p in parts if p.get("written_fact_id"))
    found = (
        await edges.dependents(
            conn,
            story_id,
            edges.Targets(fact_ids=written, amendment_part_ids=tuple(part_ids)),
            depth=1,
        )
        if part_ids
        else edges.DependentResult()
    )
    own = set(part_ids)
    dependents = [
        {
            "edge_type": d.edge_type.value,
            "record": {"table": d.dependent[0], "id": d.dependent[1]},
            "target": {"kind": d.target[0], "id": d.target[1]},
        }
        for d in found.items
        if not (d.dependent[0] == "amendment_parts" and d.dependent[1] in own)
    ]
    return AmendmentDetail(
        **_summary(row, len(parts)),
        parts=[{k: _jsonable(p.get(k)) for k in PART_COLUMNS} for p in parts],
        relies_on=relies,
        dependents=dependents,
        truncated=found.truncated,
    )


__all__ = [
    "LIST_DEFAULT",
    "LIST_MAX",
    "AmendmentDetail",
    "AmendmentPage",
    "AmendmentSummary",
    "amendment_row",
    "get_amendment",
    "list_amendments",
    "part_rows",
]
