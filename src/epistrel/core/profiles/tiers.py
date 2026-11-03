"""The ``"@strongest"`` routing sentinel (FR-PROF-3's Max preset; #227).

A profile (or a story override) may route any story purpose to ``"@strongest"`` instead of a
model id: ``models.purposes.<purpose>.model = "@strongest"``. It names the deployment's
strongest configured model, the deployment setting ``model_tiers.strongest`` (a model id that
``/admin/model-config`` lists; ``null`` by default). The sentinel is resolved when a story's
effective configuration is computed (``resolved``): the purpose routes to that model, or, when
the setting is ``null``, to the purpose's own configured model (the route's model is dropped).
So the effective configuration, the routes a turn uses and the fingerprint (#109) only ever
carry the resolved id, never the sentinel, and a change of ``model_tiers.strongest`` (it moves
the deployment's ``config_revision``) reaches every story at its next turn boundary and changes
its fingerprint.

The sentinel is valid only in profile routing: ``/admin/model-config`` refuses it as a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from epistrel.core.profiles.schema import ProfileSettings, PurposeRoute

STRONGEST = "@strongest"
"""The sentinel a profile routes a purpose to for the deployment's strongest model."""
MODEL_TIERS = "model_tiers"
STRONGEST_SETTING = f"{MODEL_TIERS}.strongest"


def strongest_of(document: Mapping[str, Any] | None) -> str | None:
    """``model_tiers.strongest`` from the deployment's ``settings`` document."""
    tiers = (document or {}).get(MODEL_TIERS)
    found = tiers.get("strongest") if isinstance(tiers, Mapping) else None
    return found if isinstance(found, str) and found else None


def uses_strongest(settings: ProfileSettings) -> bool:
    """Whether any purpose of ``settings`` routes to the sentinel."""
    return any(route.model == STRONGEST for route in settings.models.purposes.values())


def resolved(settings: ProfileSettings, strongest: str | None) -> ProfileSettings:
    """``settings`` with every ``"@strongest"`` route resolved to ``strongest`` (``None``: the
    route's model dropped, so the purpose inherits its own configured model)."""
    if not uses_strongest(settings):
        return settings
    purposes: dict[str, PurposeRoute] = {}
    for purpose, route in settings.models.purposes.items():
        if route.model == STRONGEST:
            route = route.model_copy(update={"model": strongest})
        purposes[purpose] = route
    models = settings.models.model_copy(update={"purposes": purposes})
    return settings.model_copy(update={"models": models})


__all__ = [
    "MODEL_TIERS",
    "STRONGEST",
    "STRONGEST_SETTING",
    "resolved",
    "strongest_of",
    "uses_strongest",
]
