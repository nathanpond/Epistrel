"""A principal lookup's related items keep the erasure-held ``name: null`` rule (#526): an entry
a pending catalog-scope erasure reaches loses its name whether the principal owns it or is
related to it, and nothing else does. Without a database: ``without_names`` over a built
lookup. The integration side (names read, a held story) is in
``tests/integration/test_offboarding.py``."""

from __future__ import annotations

from epistrel.core.principals import (
    TYPES,
    OwnedEntities,
    OwnedItem,
    OwnedPage,
    PrincipalLookup,
    RelatedEntities,
    RelatedItem,
    RelatedPage,
    without_names,
)


def _related(entity_id: str) -> RelatedItem:
    return RelatedItem(
        id=entity_id,
        relations=["editor"],
        name=f"name of {entity_id}",
        status="draft",
        version=None,
        listing_status="private",
    )


def _owned(entity_id: str) -> OwnedItem:
    return OwnedItem(
        id=entity_id, name=f"name of {entity_id}", status="draft", version=None, listing_status=None
    )


def _lookup() -> PrincipalLookup:
    owned = {
        key: OwnedPage(count=1, items=[_owned(f"own-{key}")], next_cursor=None) for key in TYPES
    }
    related = {
        key: RelatedPage(
            count=2, items=[_related(f"rel-{key}"), _related(f"kept-{key}")], next_cursor=None
        )
        for key in TYPES
    }
    return PrincipalLookup(
        principal="p-x", owned=OwnedEntities(**owned), related=RelatedEntities(**related)
    )


def test_a_reached_related_entry_loses_its_name() -> None:
    reached = {("character", "rel-characters"), ("media", "rel-media"), ("persona", "own-personas")}
    found = without_names(_lookup(), lambda kind, entity_id: (kind, entity_id) in reached)
    names = {
        item.id: item.name
        for section in (found.owned, found.related)
        for key in TYPES
        for item in getattr(section, key).items
    }
    assert names["rel-characters"] is None
    assert names["rel-media"] is None
    assert names["own-personas"] is None
    assert {k: v for k, v in names.items() if v is not None} == {
        k: f"name of {k}" for k in names if k not in {"rel-characters", "rel-media", "own-personas"}
    }
    (held,) = [i for i in found.related.characters.items if i.id == "rel-characters"]
    assert (held.relations, held.status, held.listing_status) == (["editor"], "draft", "private")
