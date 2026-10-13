"""The catalog listings' query builder, request rules and cursors, without a database (#110).

``test_a_filter_narrows_only_when_named`` is the repository check of the story's third
acceptance criterion: it compiles the one query every listing runs (``search.statement``) with
and without each of the four filters a list could gate on (owner, relation, label, listing
status) and finds the filter's column or path in the SQL exactly when the request named it. The
behaviour over a database is ``tests/integration/test_i_21.py``'s ``test_i_21__listing_filters``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from epistrel.core.catalog import characters, scenarios, search
from epistrel.core.catalog.search import CatalogQuery, Position, catalog_query
from epistrel.core.catalog.service import CatalogService
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import CursorInvalid, InvalidRequest
from epistrel.core.ownership import RelationFilter

SERVICES = (characters.SERVICE, scenarios.SERVICE)

FILTERS: dict[str, tuple[dict[str, Any], str]] = {
    "owner": ({"owner": "p-owner"}, "owner_principal"),
    "relation": ({"relations": ("editor:p-editor",)}, "entity_relations"),
    "label": ({"labels": ("family",)}, "'labels'"),
    "listing_status": ({"listing_status": "public"}, "listing_status"),
}
"""Each filter a list could gate on: how a request names it, and what its predicate reads."""


def _where(service: CatalogService, query: CatalogQuery) -> str:
    """The compiled WHERE clause of ``query``'s page over ``service``'s entries."""
    clause = search.statement(service, query).whereclause
    assert clause is not None
    dialect: Any = postgresql.dialect
    return str(clause.compile(dialect=dialect()))


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.entity_type.value)
@pytest.mark.parametrize("name", sorted(FILTERS))
def test_a_filter_narrows_only_when_named(service: CatalogService, name: str) -> None:
    """Compiled without the filter's parameter, the listing's WHERE clause reads nothing the
    filter would (its column, the relations table, the labels path), whichever other filters
    are named; with it, it does. So no code path filters a list by owner, relation, label or
    listing status unless the request asks.

    Complement asserted: with the parameter, the predicate is there, so its absence is the
    request's doing, not a check that cannot see it."""
    named, read = FILTERS[name]
    others = {
        key: value
        for other, (wanted, _) in FILTERS.items()
        if other != name
        for key, value in wanted.items()
    }
    for absent in ({}, others):
        assert read not in _where(service, catalog_query(**absent)), (name, absent)
    for present in (named, {**others, **named}):
        assert read in _where(service, catalog_query(**present)), (name, present)


def test_the_default_list_reads_status_and_nothing_a_filter_reads() -> None:
    """A request naming no filter lists published entries (a scenario list leaves companions
    out) and nothing else narrows it: no owner, relation, label, listing status, rating or text
    predicate. Complement: ``status`` and ``include_companions`` change only their own
    predicates."""
    character_where = _where(characters.SERVICE, catalog_query())
    scenario_where = _where(scenarios.SERVICE, catalog_query())
    for where in (character_where, scenario_where):
        assert "status" in where
        for read in ("owner_principal", "entity_relations", "'labels'", "listing_status"):
            assert read not in where
        assert "content_rating_id" not in where
        assert "@@" not in where
    assert "companion_of IS NULL" in scenario_where
    assert "companion_of" not in character_where
    every = _where(scenarios.SERVICE, catalog_query(include_companions=True))
    assert "companion_of" not in every


def test_requests_are_normalised_and_checked() -> None:
    """Labels are trimmed, lower-cased and kept once; a listing status is trimmed; relations
    split at the first colon; no status means ``published``. A blank or overlong ``q``, ``q``
    with ``status=draft``, a malformed label, rating id or listing status, an unknown status and
    more than 30 labels are each 422 at their query field."""
    query = catalog_query(
        labels=(" Family ", "family", "NOIR"),
        listing_status="  in review ",
        relations=("editor:team/ana:1", "player:"),
        owner=" p-a ",
    )
    assert query.labels == ("family", "noir")
    assert query.listing_status == "in review"
    assert query.relations == (
        RelationFilter("editor", "team/ana:1"),
        RelationFilter("player", None),
    )
    assert (query.owner, query.status, query.q) == ("p-a", EntryStatus.PUBLISHED, None)
    refused: list[tuple[dict[str, Any], str]] = [
        ({"q": ""}, "query.q"),
        ({"q": "x" * 201}, "query.q"),
        ({"q": "garden", "status": "draft"}, "query.q"),
        ({"labels": ("ok", "no:colon")}, "query.label[1]"),
        ({"labels": ("   ",)}, "query.label[0]"),
        ({"labels": tuple(f"l{n}" for n in range(31))}, "query.label"),
        ({"content_rating": "not-a-rating"}, "query.content_rating"),
        ({"listing_status": "a\x01b"}, "query.listing_status"),
        ({"listing_status": "x" * 41}, "query.listing_status"),
        ({"status": "deleted"}, "query.status"),
        ({"q": "a\x00b"}, "query.q"),
        ({"relations": ("editor",)}, "query.relation[0]"),
    ]
    for request, field in refused:
        with pytest.raises(InvalidRequest) as caught:
            catalog_query(**request)
        assert caught.value.details["field"] == field, request
    assert catalog_query(q="x" * 200).q == "x" * 200


def test_cursors_bind_their_filters() -> None:
    """A cursor carries the last item's position and a hash of the kind and the filters (never
    ``limit``): it decodes to the same position under the same filters, in any order of the
    repeatable ones, and is 422 ``cursor_invalid`` under other filters (``filters_changed``),
    for the other kind, or when it is not one a listing gave (``malformed``: garbage, a list's
    cursor sent to a search, a position without a time zone)."""
    query = catalog_query(labels=("family", "noir"), relations=("editor:p-c", "player:"))
    same = catalog_query(labels=("noir", "family"), relations=("player:", "editor:p-c"))
    moment = datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=UTC)
    position = Position(None, moment, "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3")
    fingerprint = query.fingerprint(characters.SERVICE.entity_type)
    assert same.fingerprint(characters.SERVICE.entity_type) == fingerprint
    cursor = search.encode_cursor(position, fingerprint)
    assert search.decode_cursor(cursor, fingerprint, ranked=False) == position

    def refused(token: str, bound: str, *, ranked: bool = False) -> str:
        with pytest.raises(CursorInvalid) as caught:
            search.decode_cursor(token, bound, ranked=ranked)
        assert caught.value.details["field"] == "query.cursor"
        reason: str = caught.value.details["reason"]
        return reason

    other = catalog_query(labels=("family",)).fingerprint(characters.SERVICE.entity_type)
    assert refused(cursor, other) == "filters_changed"
    kind = query.fingerprint(scenarios.SERVICE.entity_type)
    assert refused(cursor, kind) == "filters_changed"
    assert refused(cursor, fingerprint, ranked=True) == "malformed"
    assert refused("not a cursor!", fingerprint) == "malformed"
    naive = search.encode_cursor(
        Position(None, moment.replace(tzinfo=None), position.id), fingerprint
    )
    assert refused(naive, fingerprint) == "malformed"
    ranked = search.encode_cursor(Position(0.25, moment, position.id), fingerprint)
    assert search.decode_cursor(ranked, fingerprint, ranked=True).rank == 0.25
