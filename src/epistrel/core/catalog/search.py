"""Listing and searching the catalog (FR-API-6, FR-CAT-11, FR-AUTH-8; docs/02 §4.10; #110).

``GET /catalog/characters`` and ``GET /catalog/scenarios`` list one kind's entries, a page at a
time, as its ``list_item``: exactly the card a single read answers (FR-SCN-10), plus
``owner_principal``, ``updated_at`` and, on a search, ``rank``. ``search`` serves both, through
``statement``, the one query builder.

**What a list holds.** The ``published`` entries, unless ``status`` asks for the ``draft`` ones
(never published; each shown by its draft, ``version`` ``null``) or the ``archived`` ones (shown
by their latest published version, else their draft). A scenario list leaves companions out
(``companion_of`` set; #116) unless ``include_companions``: browsing shows authored scenarios,
while a companion still reads directly and creates stories.

**Filters**, each applied only when the request names it, never otherwise (FR-AUTH-8): no list
is narrowed to the asker, by owner, relation, label or listing status.

- ``q``: ``websearch_to_tsquery('simple', q)`` against the latest published version's ``search``
  vector (``core.tables.catalog``: the public card's name, labels, aliases or role public labels,
  and public description; never a private field). Drafts are not searchable, so ``q`` with
  ``status=draft`` is 422; a ``q`` that holds no word matches nothing. Items carry their
  ``rank`` (``ts_rank``, default weights, normalization 0).
- ``label`` (repeatable; every one must hold): an exact, normalised label (trimmed,
  lower-cased) the shown document's ``metadata.labels`` contains.
- ``content_rating``: the rating id the shown document names (``metadata.content_rating_id``),
  exactly: no inheritance (422 for a rating that does not exist).
- ``listing_status``: one of the deployment's values (422 ``invalid_listing_status`` otherwise).
- ``owner`` and ``relation`` (``name:principal``, or ``name:`` for any principal; repeatable,
  AND): ``core.ownership.list_filters``.

The "shown document" is the latest published version, or the draft of an entry never
published.

**Order and pages.** Newest first: ``updated_at`` then ``id``, both descending; a search puts
``rank`` first. Pages are keyset pages of ``limit`` (1 to 200, default 50): ``next_cursor``
(``null`` on the last page) is the last item's position, and it binds the filters (a hash of
them, ``limit`` aside), so a cursor sent with other filters, or for the other kind, is 422
``cursor_invalid``. A list carries no total.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import (
    Select,
    and_,
    cast,
    func,
    literal,
    literal_column,
    select,
    tuple_,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.expression import ColumnClause, ColumnElement

from epistrel.core import ownership
from epistrel.core.catalog import listing
from epistrel.core.catalog.scenario_models import LABELS_MAX, Label
from epistrel.core.catalog.service import CatalogService, Shown
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import CursorInvalid, InvalidRequest, refuse_nul
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.ownership import RelationFilter
from epistrel.core.settings_schema import ListingStatusValue
from epistrel.core.tables import content_ratings
from epistrel.core.tables.catalog import SEARCH_CONFIG
from epistrel.core.tables.ownership import EntityType

DEFAULT_PAGE = 50
MAX_PAGE = 200
Q_MAX = 200
"""The longest ``q``, in characters."""
LABEL_FILTERS_MAX = LABELS_MAX
"""The most ``label`` filters one request may name (as many as an entry may hold)."""
CURSOR_MAX = 256
"""The longest cursor a request may send (a listing's are well under it)."""
COMPANION_COLUMN = ownership.COMPANION_COLUMN

_LABEL: TypeAdapter[str] = TypeAdapter(Label)
_LISTING_STATUS: TypeAdapter[str] = TypeAdapter(ListingStatusValue)
_TS_CONFIG: ColumnClause[Any] = literal_column(f"'{SEARCH_CONFIG}'::regconfig")
"""The text-search configuration, written into the SQL (a constant, never a request value)."""


@dataclass(frozen=True, slots=True)
class CatalogQuery:
    """What a listing asks for (see the module docstring), validated and normalised by
    ``catalog_query``: each filter narrows the list only when it is set."""

    q: str | None = None
    labels: tuple[str, ...] = ()
    content_rating: str | None = None
    listing_status: str | None = None
    owner: str | None = None
    relations: tuple[RelationFilter, ...] = ()
    status: EntryStatus = EntryStatus.PUBLISHED
    include_companions: bool = False

    def fingerprint(self, entity_type: EntityType) -> str:
        """What a cursor binds: a hash of the kind and every filter (order-free where the
        filters are), ``limit`` aside."""
        bound = {
            "entity_type": entity_type.value,
            "q": self.q,
            "labels": sorted(self.labels),
            "content_rating": self.content_rating,
            "listing_status": self.listing_status,
            "owner": self.owner,
            "relations": sorted([r.relation, r.principal or ""] for r in self.relations),
            "status": self.status.value,
            "include_companions": self.include_companions,
        }
        canonical = json.dumps(bound, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Position:
    """Where a page ended: the last item's rank (a search's), ``updated_at`` and id."""

    rank: float | None
    updated_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class CatalogPage:
    """A page of a listing, newest first (a search: best first), and the cursor of the next
    page (``None``: the last)."""

    items: list[Any] = field(default_factory=list)
    next_cursor: str | None = None


# -- the request ----------------------------------------------------------------------------------


def _repeated(values: str | Sequence[str]) -> Sequence[str]:
    """A repeatable filter's values: a plain string is one value, as one query parameter is
    (``label="family"`` is the label ``family``, never the six labels of its letters)."""
    return (values,) if isinstance(values, str) else values


def _labels(values: Sequence[str]) -> tuple[str, ...]:
    if len(values) > LABEL_FILTERS_MAX:
        raise InvalidRequest(f"at most {LABEL_FILTERS_MAX} label filters", {"field": "query.label"})
    found: list[str] = []
    for index, value in enumerate(values):
        try:
            found.append(_LABEL.validate_python(value))
        except ValidationError as exc:
            raise InvalidRequest.from_errors(
                exc.errors(), prefix=("query", "label", index)
            ) from None
    return tuple(dict.fromkeys(found))


def catalog_query(
    *,
    q: str | None = None,
    labels: str | Sequence[str] = (),
    content_rating: str | None = None,
    listing_status: str | None = None,
    owner: str | None = None,
    relations: str | Sequence[str] = (),
    status: EntryStatus | str | None = None,
    include_companions: bool = False,
) -> CatalogQuery:
    """The listing a request asks for, each filter checked and normalised: 422
    ``validation_error`` at ``query.<name>`` for a value no entry could match (a blank or too
    long ``q``, a malformed label, rating id or listing status, an unknown ``status``), and for
    ``q`` with ``status=draft`` (drafts are not searchable). ``labels`` and ``relations`` are
    each one value (a plain string) or several."""
    label_values, relation_values = _repeated(labels), _repeated(relations)
    refuse_nul(
        {
            "q": q,
            "label": list(label_values),
            "content_rating": content_rating,
            "listing_status": listing_status,
            "owner": owner,
            "relation": list(relation_values),
        },
        ("query",),
    )
    if q is not None and not 1 <= len(q) <= Q_MAX:
        raise InvalidRequest(f"q must be 1 to {Q_MAX} characters", {"field": "query.q"})
    try:
        wanted = EntryStatus.PUBLISHED if status is None else EntryStatus(status)
    except ValueError:
        statuses = ", ".join(s.value for s in EntryStatus)
        raise InvalidRequest(
            f"status must be one of: {statuses}", {"field": "query.status"}
        ) from None
    if q is not None and wanted is EntryStatus.DRAFT:
        raise InvalidRequest(
            "drafts are not searchable: q searches each entry's latest published version",
            {"field": "query.q"},
        )
    if content_rating is not None and not re.fullmatch(ULID_PATTERN, content_rating):
        raise InvalidRequest(
            "content_rating must be a content rating's id", {"field": "query.content_rating"}
        )
    value = None
    if listing_status is not None:
        try:
            value = _LISTING_STATUS.validate_python(listing_status)
        except ValidationError:
            raise InvalidRequest(
                "listing_status must be 1 to 40 printable characters (no control or format "
                "character, and no separator but the space)",
                {"field": "query.listing_status"},
            ) from None
    return CatalogQuery(
        q=q,
        labels=_labels(label_values),
        content_rating=content_rating,
        listing_status=value,
        owner=ownership.owner_filter(owner),
        relations=tuple(
            ownership.parse_relation_filter(item, field=f"query.relation[{index}]")
            for index, item in enumerate(relation_values)
        ),
        status=wanted,
        include_companions=include_companions,
    )


# -- cursors ------------------------------------------------------------------------------------


def encode_cursor(position: Position, fingerprint: str) -> str:
    """``position`` and the filters' ``fingerprint``, as an opaque URL-safe token."""
    raw = json.dumps(
        [position.rank, position.updated_at.isoformat(), position.id, fingerprint],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _malformed() -> CursorInvalid:
    return CursorInvalid(
        "not a cursor this listing gave; list again without it",
        {"field": "query.cursor", "reason": "malformed"},
    )


def decode_cursor(cursor: str, fingerprint: str, *, ranked: bool) -> Position:
    """The position ``cursor`` names: 422 ``cursor_invalid`` for one no listing gave
    (``malformed``: or a search's for a list, or the reverse) or one given for other filters
    or another kind (``filters_changed``)."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        rank, moment, entry_id, bound = json.loads(raw)
        updated_at = datetime.fromisoformat(moment)
    except ValueError, TypeError, binascii.Error:
        raise _malformed() from None
    if (
        not isinstance(entry_id, str)
        or not re.fullmatch(ULID_PATTERN, entry_id)
        or not isinstance(bound, str)
        or updated_at.tzinfo is None
    ):
        raise _malformed()
    if ranked != (rank is not None) or (
        rank is not None
        and (isinstance(rank, bool) or not isinstance(rank, int | float) or not math.isfinite(rank))
    ):
        raise _malformed()
    if bound != fingerprint:
        raise CursorInvalid(
            "this cursor was given for other filters (or the other kind of entry); list again "
            "without it",
            {"field": "query.cursor", "reason": "filters_changed"},
        )
    return Position(None if rank is None else float(rank), updated_at, entry_id)


# -- the query ----------------------------------------------------------------------------------


def _path(document: ColumnElement[Any], *keys: str, text: bool = False) -> ColumnElement[Any]:
    """``document -> 'a' -> 'b'`` (``->>`` last when ``text``), the keys written into the SQL."""
    found = document
    for index, key in enumerate(keys):
        operator = "->>" if text and index == len(keys) - 1 else "->"
        found = found.op(operator)(literal_column(f"'{key}'"))
    return found


def statement(
    service: CatalogService,
    query: CatalogQuery,
    *,
    after: Position | None = None,
    limit: int = DEFAULT_PAGE,
) -> Select[Any]:
    """The page's query (``limit + 1`` rows, so the caller knows whether another page
    follows): see the module docstring. Every filter adds its predicate only when ``query``
    sets it, and nothing else in the query reads an owner, a relation, a label or a listing
    status (FR-AUTH-8; ``tests/test_catalog_listing.py`` compiles it to check)."""
    catalog, versions = service.catalog, service.versions
    document = type_coerce(func.coalesce(versions.c.content, catalog.c.draft), JSONB)
    columns: list[Any] = [
        catalog.c.id,
        catalog.c.owner_principal,
        catalog.c.listing_status,
        catalog.c.status,
        catalog.c.current_version,
        catalog.c.created_at,
        catalog.c.updated_at,
        document.label("document"),
        versions.c.content_rating_version_id.label("version_rating"),
        versions.c.published_at.label("version_published_at"),
    ]
    companions = COMPANION_COLUMN in catalog.c
    if companions:
        columns.append(catalog.c[COMPANION_COLUMN])
    where: list[ColumnElement[bool]] = [catalog.c.status == query.status.value]
    if companions and not query.include_companions:
        where.append(catalog.c[COMPANION_COLUMN].is_(None))
    rank: ColumnElement[Any] | None = None
    if query.q is not None:
        searched = func.websearch_to_tsquery(_TS_CONFIG, query.q)
        rank = cast(func.ts_rank(versions.c.search, searched), DOUBLE_PRECISION)
        columns.append(rank.label("rank"))
        where.append(versions.c.search.bool_op("@@")(searched))
    if query.labels:
        wanted = cast(literal(json.dumps(list(query.labels))), JSONB)
        where.append(_path(document, "metadata", "labels").bool_op("@>")(wanted))
    if query.content_rating is not None:
        rating = _path(document, "metadata", "content_rating_id", text=True)
        where.append(rating == query.content_rating)
    if query.listing_status is not None:
        where.append(catalog.c.listing_status == query.listing_status)
    where.extend(
        ownership.list_filters(
            service.entity_type,
            catalog.c.id,
            catalog.c.owner_principal,
            owner=query.owner,
            relations=query.relations,
        )
    )
    keys: list[ColumnElement[Any]] = [catalog.c.updated_at, catalog.c.id]
    if rank is not None:
        keys.insert(0, rank)
    if after is not None:
        bounds: list[Any] = [after.updated_at, after.id]
        if rank is not None:
            bounds.insert(0, after.rank)
        where.append(tuple_(*keys) < tuple_(*bounds))
    latest = and_(
        versions.c.catalog_id == catalog.c.id, versions.c.version == catalog.c.current_version
    )
    shown = catalog.outerjoin(versions, latest)
    return (
        select(*columns)
        .select_from(shown)
        .where(and_(*where))
        .order_by(*(key.desc() for key in keys))
        .limit(limit + 1)
    )


@dataclass(frozen=True, slots=True)
class _Version:
    """What a card reads of the shown version's row."""

    content_rating_version_id: str | None


def _shown(row: Any) -> Shown:
    published = row.current_version is not None
    return Shown(
        entry=row,
        document=row.document,
        version=row.current_version,
        published_at=row.version_published_at,
        version_row=_Version(row.version_rating) if published else None,
    )


async def _refuse_unknown(conn: AsyncConnection, query: CatalogQuery) -> None:
    """422 for a filter value outside its known set: a rating that does not exist
    (``validation_error``), a listing status the deployment does not allow
    (``invalid_listing_status``)."""
    if query.content_rating is not None:
        found = await conn.scalar(
            select(content_ratings.c.id).where(content_ratings.c.id == query.content_rating)
        )
        if found is None:
            raise InvalidRequest(
                f"no content rating {query.content_rating}", {"field": "query.content_rating"}
            )
    if query.listing_status is not None:
        allowed = await listing.allowed_values(conn)
        listing.refuse_unknown(query.listing_status, allowed, field="query.listing_status")


async def search(
    db: AsyncEngine,
    service: CatalogService,
    query: CatalogQuery,
    *,
    limit: int = DEFAULT_PAGE,
    cursor: str | None = None,
) -> CatalogPage:
    """One page of ``service``'s entries that ``query`` asks for (see the module docstring):
    422 ``validation_error`` for ``limit`` outside 1 to 200 or a rating that does not exist,
    ``invalid_listing_status``, or ``cursor_invalid``."""
    if not 1 <= limit <= MAX_PAGE:
        raise InvalidRequest(f"limit must be 1 to {MAX_PAGE}", {"field": "query.limit"})
    fingerprint = query.fingerprint(service.entity_type)
    after = None
    if cursor is not None:
        if len(cursor) > CURSOR_MAX:
            raise _malformed()
        after = decode_cursor(cursor, fingerprint, ranked=query.q is not None)
    async with db.connect() as conn:
        await _refuse_unknown(conn, query)
        rows = (await conn.execute(statement(service, query, after=after, limit=limit))).all()
        items = [
            await service.list_card(conn, _shown(row), rank=row._mapping.get("rank"))
            for row in rows[:limit]
        ]
    following = None
    if len(rows) > limit:
        last = rows[limit - 1]
        position = Position(last._mapping.get("rank"), last.updated_at, str(last.id))
        following = encode_cursor(position, fingerprint)
    return CatalogPage(items=items, next_cursor=following)


__all__ = [
    "CURSOR_MAX",
    "DEFAULT_PAGE",
    "LABEL_FILTERS_MAX",
    "MAX_PAGE",
    "Q_MAX",
    "CatalogPage",
    "CatalogQuery",
    "Position",
    "catalog_query",
    "decode_cursor",
    "encode_cursor",
    "search",
    "statement",
]
