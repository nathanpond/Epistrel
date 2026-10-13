"""What the catalog's listings and listing-status routes share (FR-API-6, FR-CAT-11; #110).

``routes.catalog_characters`` and ``routes.catalog_scenarios`` each serve, for their kind:

- ``GET /catalog/{characters|scenarios}``: a page of cards (``search_catalog_<kind>``). The
  query parameters are declared here once. Each optional one is documented without a ``null``
  alternative: a parameter that is absent is simply not sent (#104's lesson, see
  ``routes.stories.OWNER_FILTER_SCHEMA``).
- ``PUT /catalog/{characters|scenarios}/{id}/listing-status`` ``{value}``
  (``set_listing_status_<kind>``): 200 ``ListingStatusBody``.

Both kinds' routes make one ``Engine`` call each, with the kind as ``entity_type``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Query
from pydantic import WithJsonSchema

from epistrel.api.responses import DeploymentMutationResponse
from epistrel.api.routes.stories import (
    OWNER_FILTER_SCHEMA,
    RELATION_FILTERS_SCHEMA,
    RelationFilterParam,
)
from epistrel.core.catalog.listing import ListingStatusChange
from epistrel.core.catalog.scenario_models import LABEL_PATTERN, Label
from epistrel.core.catalog.search import (
    CURSOR_MAX,
    DEFAULT_PAGE,
    LABEL_FILTERS_MAX,
    MAX_PAGE,
    Q_MAX,
)
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.enums import EntryStatus
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.settings_schema import LISTING_STATUS_PATTERN

GATES_NOTHING = (
    "Listing status, owners, relations and labels gate nothing (FR-AUTH-8): a list is narrowed "
    "by one only when the request names it, whoever asks."
)
"""The sentence every listing route's description ends with."""


class ListingStatusBody(DeploymentMutationResponse):
    """A catalog entry's listing status after a ``PUT …/listing-status`` (FR-CAT-11): one of
    the deployment's ``listing_status_values``, never interpreted by Epistrel.
    ``config_revision`` is the deployment's, unchanged."""

    id: str
    listing_status: str

    @classmethod
    def of(cls, change: ListingStatusChange) -> ListingStatusBody:
        return cls(
            id=change.id,
            listing_status=change.listing_status,
            config_revision=change.config_revision,
        )


SET_LISTING_STATUS = (
    "Set the entry's listing status (FR-CAT-11): `value` must be one of the deployment's "
    "`listing_status_values` (`GET /admin/settings`; default `private`, `in_review`, `public`), "
    "else 422 `invalid_listing_status` (`details.value`, `details.allowed_values`). Values are "
    "case-sensitive; surrounding spaces and tabs are trimmed. The change is recorded with the "
    "acting principal and time; setting the current value changes and records nothing (200). "
    "Allowed whatever the entry's status. The value is the consumer's own publishing workflow: "
    "Epistrel restricts no read, no story creation and no transition because of it (FR-AUTH-8)."
)


def list_description(kind: str, searched: str, extra: str = "") -> str:
    """A listing route's description, for ``kind`` (``catalog characters``, ``scenarios``),
    whose ``q`` searches ``searched``."""
    return (
        f"A page of {kind} as cards (each exactly the card a single read answers, plus "
        "`owner_principal` and `updated_at`), newest first: the `published` ones of every owner "
        "and listing status, or with `status` the `draft` (never published, shown by their "
        "draft) or `archived` ones. `q` searches the latest published version's public card "
        f"({searched}; never a private field, and drafts are not searchable) with "
        "`websearch_to_tsquery` (quoted phrases, `or`, `-word`), words as written: best match "
        "first, each card with its `rank`. `label` (repeatable; every one must hold), "
        "`content_rating` (exactly that rating), `listing_status`, `owner` and `relation` "
        "(`name:principal`, or `name:` for any principal; repeatable) narrow the list. "
        + extra
        + f"`limit` (1 to {MAX_PAGE}, default {DEFAULT_PAGE}) cards at a time; `next_cursor` "
        "continues, with the same filters (else 422 `cursor_invalid`). Cards only: "
        "`projection=full` is 422. " + GATES_NOTHING
    )


QParam = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=Q_MAX,
        description="Full-text search over each entry's latest published public card.",
    ),
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": Q_MAX}),
]
LabelParam = Annotated[
    list[Label] | None,
    Query(
        max_length=LABEL_FILTERS_MAX,
        description="Only entries with this label (repeatable; all must hold). Matched exactly, "
        "trimmed and lower-cased.",
    ),
    WithJsonSchema(
        {
            "type": "array",
            "items": {"type": "string", "pattern": LABEL_PATTERN},
            "maxItems": LABEL_FILTERS_MAX,
        }
    ),
]
ContentRatingParam = Annotated[
    str | None,
    Query(pattern=ULID_PATTERN, description="Only entries with exactly this content rating."),
    WithJsonSchema({"type": "string", "pattern": ULID_PATTERN}),
]
ListingStatusParam = Annotated[
    str | None,
    Query(
        pattern=LISTING_STATUS_PATTERN,
        description="Only entries with this listing status (one of the deployment's values).",
    ),
    WithJsonSchema({"type": "string", "pattern": LISTING_STATUS_PATTERN}),
]
OwnerParam = Annotated[
    str | None,
    Query(pattern=PRINCIPAL_PATTERN, description="Only the entries this principal owns."),
    WithJsonSchema(OWNER_FILTER_SCHEMA),
]
RelationParam = Annotated[
    list[RelationFilterParam] | None,
    Query(
        description="Only the entries with this relation: `name:principal`, or `name:` for any "
        "principal (repeatable; all must hold)."
    ),
    WithJsonSchema(RELATION_FILTERS_SCHEMA),
]
StatusParam = Annotated[
    EntryStatus | None,
    Query(description="`published` (the default), `draft` or `archived`."),
    WithJsonSchema({"type": "string", "enum": [status.value for status in EntryStatus]}),
]
LimitParam = Annotated[int, Query(ge=1, le=MAX_PAGE)]
CursorParam = Annotated[
    str | None,
    Query(max_length=CURSOR_MAX, description="The previous page's `next_cursor`."),
    WithJsonSchema({"type": "string", "maxLength": CURSOR_MAX}),
]
ProjectionParam = Annotated[
    Literal["card"],
    Query(description="Lists show cards only (FR-SCN-10): `card`."),
]


def page_fields(page: Any) -> dict[str, Any]:
    """A ``CatalogPage``'s items and cursor, for a list body."""
    return {"items": list(page.items), "next_cursor": page.next_cursor}


__all__ = [
    "GATES_NOTHING",
    "SET_LISTING_STATUS",
    "ContentRatingParam",
    "CursorParam",
    "LabelParam",
    "LimitParam",
    "ListingStatusBody",
    "ListingStatusParam",
    "OwnerParam",
    "ProjectionParam",
    "QParam",
    "RelationParam",
    "StatusParam",
    "list_description",
    "page_fields",
]
