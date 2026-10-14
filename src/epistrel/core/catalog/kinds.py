"""The catalog's kinds of entry, by entity type: the service each one's generic operations
(listing status, listings; #110) run on."""

from __future__ import annotations

from epistrel.core.catalog import characters, scenarios
from epistrel.core.catalog.service import CatalogService
from epistrel.core.errors import InvalidRequest
from epistrel.core.tables.ownership import EntityType

SERVICES: dict[EntityType, CatalogService] = {
    EntityType.CHARACTER: characters.SERVICE,
    EntityType.SCENARIO: scenarios.SERVICE,
}
"""Every catalog kind's service: catalog characters (#103) and scenarios (#108)."""


def service_of(entity_type: EntityType | str) -> CatalogService:
    """The service of ``entity_type``; 422 ``validation_error`` (at ``entity_type``) for a type
    that is not a catalog kind."""
    try:
        return SERVICES[EntityType(entity_type)]
    except ValueError, KeyError:
        kinds = ", ".join(kind.value for kind in SERVICES)
        raise InvalidRequest(
            f"{entity_type!r} is not a catalog entry type; one of: {kinds}",
            {"field": "entity_type"},
        ) from None


__all__ = ["SERVICES", "service_of"]
