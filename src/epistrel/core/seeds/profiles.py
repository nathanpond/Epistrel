"""The Lite preset (FR-PROF-3; #106): the engine profile every deployment starts with and its
default.

Migration ``0025_engine_profiles`` inserts it with the fixed id below, owned by ``system``
(FR-AUTH-5: presets shipped with Epistrel are owned by ``system``), at version 1, and makes it
the deployment's ``default_engine_profile_id``. It writes no ``config_changes`` row and leaves
the ``config_revision`` at its value. A preset is an ordinary profile: an operator may edit it
(a new version), copy it, or delete it once another profile is the default; ``preset`` only
records where it came from. The Standard, Full and Max presets arrive with M7. Once a database
is seeded, editing this module changes nothing in it: change seeds through a new migration.

Lite's settings name every setting M3 delivers:

- ``pipeline: lite``; the rolling summary on, with a 30-message window and a 6,000-token
  compression threshold; pinned memories on, at most 50; the polish pass on, regenerating a
  reply that broke an output policy at most twice (M2's ``guardrail_retry_budget``);
- budgets ``{model_calls: null, deadline_ms: 45000}``: ``null`` is NFR-PERF-6's formula (9 + G
  in M3, a total cap that includes the guardrail calls G), and 45 s is M2's ``turn_deadline``
  (the owner's decision of 2026-10-12, so a Lite story's budget is unchanged from M2);
- models: each delivered purpose routed to the deployment's model configuration (``null``:
  inherit ``/admin/model-config``, #52), and the generation defaults of M2's
  ``lite_generation_defaults`` (temperature 0.8, top_p 0.95, ``medium`` length).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

LITE_PRESET_ID = "00000000000000000000PRF001"
"""The Lite preset's id (26 Crockford characters, like the seeded ratings')."""

LITE_NAME = "Lite"
SEED_PRINCIPAL = "system"
"""The preset's owner and its version's ``created_by`` (the workers' principal,
``core.context``)."""

LITE_SETTINGS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "pipeline": "lite",
        "memory": {
            "rolling_summary": {
                "enabled": True,
                "window_messages": 30,
                "compress_threshold_tokens": 6_000,
            }
        },
        "pins": {"enabled": True, "max_pinned": 50},
        "repair": {"polish": {"enabled": True, "max_regenerations": 2}},
        "budgets": {"model_calls": None, "deadline_ms": 45_000},
        "models": {
            "purposes": {
                "generation": {"model": None},
                "guardrail_check": {"model": None},
                "lite_summary": {"model": None},
            },
            "generation": {"temperature": 0.8, "top_p": 0.95, "response_length": "medium"},
        },
    }
)
"""Lite's version 1 settings, as stored (JSON)."""


def lite_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``LITE_SETTINGS`` (nested dicts copied too)."""

    def thaw(value: Any) -> Any:
        if isinstance(value, dict | MappingProxyType):
            return {key: thaw(item) for key, item in value.items()}
        return value

    copied: dict[str, Any] = thaw(LITE_SETTINGS)
    return copied


__all__ = ["LITE_NAME", "LITE_PRESET_ID", "LITE_SETTINGS", "SEED_PRINCIPAL", "lite_settings"]
