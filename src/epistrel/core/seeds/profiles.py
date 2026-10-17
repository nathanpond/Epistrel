"""The seeded engine profiles: the Lite preset (FR-PROF-3; #106), the profile every deployment
starts with and its default, and the system-owned ``Structured (M4)`` profile (#129); and the
deployment's profile defaults they are read over.

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


def _thaw(value: Any) -> Any:
    if isinstance(value, dict | MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    return value


def _merged(base: dict[str, Any], over: Any) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict | MappingProxyType) and isinstance(out.get(key), dict):
            out[key] = _merged(out[key], value)
        else:
            out[key] = _thaw(value)
    return out


def lite_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``LITE_SETTINGS`` (nested dicts copied too)."""
    copied: dict[str, Any] = _thaw(LITE_SETTINGS)
    return copied


# -- the Structured (M4) profile (#129) -----------------------------------------------------------

STRUCTURED_M4_PROFILE_ID = "00000000000000000000PRF002"
"""The ``Structured (M4)`` profile's id, beside ``LITE_PRESET_ID``."""
STRUCTURED_M4_NAME = "Structured (M4)"

STRUCTURED_M4_SETTINGS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "pipeline": "structured",
        "memory": {
            "rolling_summary": {"enabled": False},
            "episodic": {"enabled": True, "k": 40, "hybrid_search": True, "reranking": False},
            "consolidation": {"enabled": True, "cadence_turns": 20},
        },
        "pins": {"enabled": True, "max_pinned": 50},
        "verification": {
            "level": "entailment",
            "extraction_mode": "always",
            "entailment_gate": 1.0,
            "contradiction_checking": True,
        },
        "commitment": {"enabled": True, "salience": "rules_and_model"},
        "repair": {"max_attempts": 1, "polish": {"enabled": True, "max_regenerations": 2}},
        "budgets": {"model_calls": None, "deadline_ms": 45_000},
        "models": {
            "generation": {"temperature": 0.8, "top_p": 0.95, "response_length": "medium"},
        },
    }
)
"""``Structured (M4)``'s version 1 settings, as stored (JSON): migration
``0047_structured_profile_seed`` inserts them, owned by ``system`` and not a preset (FR-PROF-3's
presets arrive with M7). Every module M4 delivers is on: the ``structured`` pipeline, episodic
retrieval (40 memories a retrieval, hybrid search), consolidation (every 20 turns),
verification at ``entailment`` with extraction ``always`` and every uncertain verdict escalated
(gate 1.0), contradiction checking, commitment with rules-and-model salience, and one repair
attempt (the schema's default stays 0). The rolling summary is allowed under ``structured`` and
off here; pinned memories and the polish pass as Lite has them. Budgets ``{model_calls: null,
deadline_ms: 45000}``: ``null`` is NFR-PERF-6's formula (an explicit cap is a total that
includes the guardrail calls). No model purpose is routed (each inherits the deployment's
model configuration); the generation defaults are Lite's."""


def structured_m4_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``STRUCTURED_M4_SETTINGS``."""
    copied: dict[str, Any] = _thaw(STRUCTURED_M4_SETTINGS)
    return copied


# -- the deployment's profile defaults (#113, #129) -----------------------------------------------

M4_PROFILE_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "memory": {
            "episodic": {"enabled": False, "k": 8, "hybrid_search": True, "reranking": False},
            "consolidation": {"enabled": False, "cadence_turns": 20},
            "hot_set": {"memories_per_character": 2_000, "vectors_per_story": 50_000},
        },
        "verification": {
            "level": "off",
            "extraction_mode": "gated",
            "entailment_gate": 0.5,
            "contradiction_checking": False,
        },
        "commitment": {"enabled": False, "salience": "rules"},
        "repair": {"max_attempts": 0},
    }
)
"""The settings M4 delivers (#129), at the schema's defaults, every module off: migration
``0047_structured_profile_seed`` adds each one the deployment's ``profile_defaults`` does not
name yet, so the document stays complete (it names every delivered setting)."""


VERIFY_DEPTH_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {"verification": {"implication_max_depth": 3}}
)
"""The implication closure's depth dial (#140), at the schema's default: migration
``0055_implication_depth_default`` adds it to a deployment's ``profile_defaults`` that does not
name it yet."""


RETRIEVAL_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "memory": {
            "episodic": {
                "weight_similarity": 0.6,
                "weight_recency": 0.2,
                "weight_importance": 0.2,
                "recency_half_life_turns": 100,
                "retrieval_token_budget": 3_000,
            }
        }
    }
)
"""The retrieval ranking and budget dials (#141), at the schema's defaults: migration
``0056_retrieval`` adds them to a deployment's ``profile_defaults`` that does not name them
yet."""


def profile_defaults() -> dict[str, Any]:
    """A deployment's seeded ``profile_defaults``: Lite's settings with ``M4_PROFILE_DEFAULTS``,
    ``VERIFY_DEPTH_DEFAULTS`` and ``RETRIEVAL_DEFAULTS`` under them (a complete document of
    every delivered setting)."""
    base = _merged(_merged(_thaw(M4_PROFILE_DEFAULTS), VERIFY_DEPTH_DEFAULTS), RETRIEVAL_DEFAULTS)
    return _merged(base, LITE_SETTINGS)


__all__ = [
    "LITE_NAME",
    "LITE_PRESET_ID",
    "LITE_SETTINGS",
    "M4_PROFILE_DEFAULTS",
    "RETRIEVAL_DEFAULTS",
    "SEED_PRINCIPAL",
    "STRUCTURED_M4_NAME",
    "STRUCTURED_M4_PROFILE_ID",
    "STRUCTURED_M4_SETTINGS",
    "VERIFY_DEPTH_DEFAULTS",
    "lite_settings",
    "profile_defaults",
    "structured_m4_settings",
]
