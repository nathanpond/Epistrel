"""The catalog listings' query builder, request rules and cursors, without a database (#110).

``test_a_filter_narrows_only_when_named`` is the repository check of the story's third
acceptance criterion: it compiles the one query every listing runs (``search.statement``) with
and without each of the four filters a list could gate on (owner, relation, label, listing
status) and finds the filter's column or path in the SQL exactly when the request named it. The
behaviour over a database is ``tests/integration/test_i_21.py``'s ``test_i_21__listing_filters``.
"""

from __future__ import annotations

import re
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
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.settings_schema import LISTING_STATUS_PATTERN, listing_status_value

SERVICES = (characters.SERVICE, scenarios.SERVICE)
ENTRY = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
"""An entry id for the cursors these tests make."""

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
    split at the first colon; no status means ``published``; a plain string label or relation
    is one value (as one query parameter is), never its characters. A blank or overlong ``q``,
    ``q`` with ``status=draft``, a malformed label or rating id, a listing status that is
    overlong or not printable text (a C0 or C1 control character, a no-break or zero-width
    space), an unknown status and more than 30 labels are each 422 at their query field.
    Complement: a listing status of printable text outside ASCII is kept, trimmed."""
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
    single = catalog_query(labels=" Family ", relations="editor:p-c")
    assert single.labels == ("family",)
    assert single.relations == (RelationFilter("editor", "p-c"),)
    assert single == catalog_query(labels=("family",), relations=("editor:p-c",))
    refused: list[tuple[dict[str, Any], str]] = [
        ({"q": ""}, "query.q"),
        ({"q": "x" * 201}, "query.q"),
        ({"q": "garden", "status": "draft"}, "query.q"),
        ({"labels": ("ok", "no:colon")}, "query.label[1]"),
        ({"labels": "no:colon"}, "query.label[0]"),
        ({"labels": "a\x00b"}, "query.label[0]"),
        ({"labels": ("   ",)}, "query.label[0]"),
        ({"labels": tuple(f"l{n}" for n in range(31))}, "query.label"),
        ({"content_rating": "not-a-rating"}, "query.content_rating"),
        ({"listing_status": "a\x01b"}, "query.listing_status"),
        ({"listing_status": "pub\x85lic"}, "query.listing_status"),
        ({"listing_status": "public\xa0"}, "query.listing_status"),
        ({"listing_status": "pub\u200blic"}, "query.listing_status"),
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
    assert catalog_query(listing_status="\ten révision ✓ ").listing_status == "en révision ✓"


def test_the_documented_pattern_refuses_every_control_character() -> None:
    """``LISTING_STATUS_PATTERN``, which the OpenAPI document gives every listing status field,
    refuses a control character (C0, DEL or C1) anywhere in a value, so a client that checks
    against the schema refuses one too. The rest of the rule, printable text, is
    ``listing_status_value``'s: a no-break space passes the pattern, and it refuses it."""
    for control in ("\x00", "\x1f", "\x7f", "\x80", "\x85", "\x9f"):
        for value in (f"{control}public", f"pub{control}lic", f"public{control}"):
            assert re.fullmatch(LISTING_STATUS_PATTERN, value) is None, repr(value)
    assert re.fullmatch(LISTING_STATUS_PATTERN, "public\xa0") is not None
    with pytest.raises(ValueError, match="printable"):
        listing_status_value("public\xa0")
    assert listing_status_value(" \tin review ") == "in review"


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


BOUND: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "q": ({"q": "garden"}, {"q": "orchard"}),
    "label": ({}, {"labels": ("family",)}),
    "content_rating": ({}, {"content_rating": EVERYONE_ID}),
    "listing_status": ({}, {"listing_status": "public"}),
    "owner": ({}, {"owner": "p-owner"}),
    "relation": ({}, {"relations": ("editor:p-editor",)}),
    "status": ({}, {"status": "archived"}),
    "include_companions": ({}, {"include_companions": True}),
}
"""Two listing requests that differ in exactly one filter, by the filter."""


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.entity_type.value)
@pytest.mark.parametrize("name", sorted(BOUND))
def test_a_cursor_binds_every_filter(service: CatalogService, name: str) -> None:
    """A page's cursor, sent again with exactly one filter changed (whichever it is: ``q``'s
    words, ``label``, ``content_rating``, ``listing_status``, ``owner``, ``relation``,
    ``status`` or ``include_companions``), is 422 ``cursor_invalid`` (``filters_changed``):
    every filter is in the hash the cursor carries.

    Complement asserted: the same filters decode the same cursor to its position."""
    first, second = (catalog_query(**request) for request in BOUND[name])
    ranked = first.q is not None
    kind = service.entity_type
    bound = first.fingerprint(kind)
    assert second.fingerprint(kind) != bound
    position = Position(0.5 if ranked else None, datetime(2026, 10, 13, tzinfo=UTC), ENTRY)
    cursor = search.encode_cursor(position, bound)
    assert search.decode_cursor(cursor, bound, ranked=ranked) == position
    with pytest.raises(CursorInvalid) as caught:
        search.decode_cursor(cursor, second.fingerprint(kind), ranked=ranked)
    assert caught.value.details["reason"] == "filters_changed"
