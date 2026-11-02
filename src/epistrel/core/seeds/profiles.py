"""The seeded engine profiles: the Lite preset (FR-PROF-3; #106), the profile every deployment
starts with and its default, and the system-owned ``Structured`` profile (#129 seeded it as
``Structured (M4)``; #181 renamed it and made its version 2 the full M5 set, #203 its version 3
the full M6 set); and the deployment's profile defaults they are read over.

Migration ``0025_engine_profiles`` inserts it with the fixed id below, owned by ``system``
(FR-AUTH-5: presets shipped with Epistrel are owned by ``system``), at version 1, and makes it
the deployment's ``default_engine_profile_id``. It writes no ``config_changes`` row and leaves
the ``config_revision`` at its value. A preset is an ordinary profile: an operator may edit it
(a new version), copy it, or delete it once another profile is the default; ``preset`` only
records where it came from. M7 (#227) adds the Standard and Max presets and converts
``Structured`` in place into Full. Once a database
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


# -- the Structured profile, version 1: Structured (M4) (#129) ------------------------------------

STRUCTURED_M4_PROFILE_ID = "00000000000000000000PRF002"
"""The ``Structured (M4)`` profile's id, beside ``LITE_PRESET_ID``."""
STRUCTURED_M4_NAME = "Structured (M4)"
"""The name ``0047`` seeded the profile under, and the label of its version 1 (#181 renamed the
row ``Structured``)."""

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


STRUCTURED_M4_VERSION = 1
"""The version ``STRUCTURED_M4_SETTINGS`` is: what every M4 fixture pins (#181), and the label
the committed M4 baseline reports keep (``Structured (M4) v1``)."""


# -- the Structured profile, version 2 (#181; frozen by #203) ------------------------------------

STRUCTURED_PROFILE_ID = STRUCTURED_M4_PROFILE_ID
"""The seeded structured profile's id: the same row as ``Structured (M4)``, renamed in place
(the owner's decision), so every M4 fixture and pin keeps working."""
STRUCTURED_NAME = "Structured"

STRUCTURED_RENDERS = 2
"""The renders (B) a turn of the seeded profile is budgeted for: two primaries."""
STRUCTURED_GUARDRAIL_CALLS = 2
"""The guardrail calls (G) its explicit cap leaves room for: one model-assisted policy on the
input and the output."""
STRUCTURED_MODEL_CALLS = 4 + 5 * STRUCTURED_RENDERS + STRUCTURED_GUARDRAIL_CALLS
"""NFR-PERF-6's ``4 + 5 per primary + G`` (#156's structured default) at B = 2 and G = 2: a
total cap that includes the reserved guardrail calls, as every explicit cap is (#113)."""
STRUCTURED_DEADLINE_MS = 60_000
"""A two-render turn's deadline: 45 s (M4's) raised to 60 s (#454 leaves model speed to M9)."""

STRUCTURED_M5_SETTINGS: MappingProxyType[str, Any] = MappingProxyType(
    {
        **STRUCTURED_M4_SETTINGS,
        "character_knowledge": {
            "partitions": True,
            "nested_beliefs": {"enabled": True, "depth": 2},
            "lazy_resolution": True,
            "testimony": "model",
            "max_adjudications_per_turn": 2,
            "max_lazy_resolutions_per_turn": 20,
        },
        "perception": {"level": "full"},
        "multi_character": {"renders": "per_character", "interjection_max_words": 6},
        "budgets": {"model_calls": STRUCTURED_MODEL_CALLS, "deadline_ms": STRUCTURED_DEADLINE_MS},
    }
)
"""``Structured``'s version 2 settings, as stored (JSON): everything version 1 turns on
(``STRUCTURED_M4_SETTINGS``), plus every module M5 delivers (#181, the owner's decision of an
in-place update): per-character partitions with nested beliefs to depth 2, lazy resolution and
testimony adjudicated by the model (at most 2 adjudications and 20 lazy resolutions a turn),
``full`` perception routing, and per-character renders (interjections of at most 6 words where
a story allows them). Budgets: ``STRUCTURED_MODEL_CALLS`` calls and a 60 s deadline. Frozen by
#203: migration ``0080_structured_profile_m5`` inserts exactly these."""

STRUCTURED_M5_VERSION = 2
"""The version ``STRUCTURED_M5_SETTINGS`` is (``0080_structured_profile_m5``), and the label the
committed M5 reports keep (``Structured v2``)."""


def structured_m5_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``STRUCTURED_M5_SETTINGS``."""
    copied: dict[str, Any] = _thaw(STRUCTURED_M5_SETTINGS)
    return copied


# -- the Structured profile, version 3 (#203) -----------------------------------------------------

STRUCTURED_VERSION = 3
"""The version migration ``0104_structured_profile_m6`` adds: ``STRUCTURED_SETTINGS``."""

STRUCTURED_SETTINGS: MappingProxyType[str, Any] = MappingProxyType(
    {
        **STRUCTURED_M5_SETTINGS,
        "clock": {"level": "on", "time_claims": True, "scheduled_stops": True},
        "states": {"level": "on"},
    }
)
"""``Structured``'s version 3 settings, as stored (JSON): everything version 2 turns on
(``STRUCTURED_M5_SETTINGS``), plus both modules M6 delivers (#203, the owner's decision of an
in-place update): the world clock ``on`` with time-claim verification and scheduled-event
stops, and States ``on``. The clock's mode (``paced``, ``manual`` or ``external``) is a story
setting, not a profile dial. Budgets stay version 2's (the owner's decision of 2026-10-28):
NFR-PERF-6's 4 already counts time estimation, and a story with a synchronous provider's
outcome step (#214's O) is reserved inside the explicit cap."""


def structured_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``STRUCTURED_SETTINGS``."""
    copied: dict[str, Any] = _thaw(STRUCTURED_SETTINGS)
    return copied


# -- the M7 presets: Standard, Full and Max (#227) ------------------------------------------------

FULL_PRESET_ID = STRUCTURED_M4_PROFILE_ID
"""The Full preset's id: the seeded ``Structured`` row, converted in place (the owner's decision
1 on #227), so every M4 to M6 fixture and pin keeps working."""
FULL_NAME = "Full"
FULL_VERSION = 4
"""The version migration ``0110_m7_presets`` adds to the ``Structured`` row on a deployment that
never edited it (the next after its current one)."""
STANDARD_PRESET_ID = "00000000000000000000PRF003"
STANDARD_NAME = "Standard"
MAX_PRESET_ID = "00000000000000000000PRF004"
MAX_NAME = "Max"

M7_PROFILE_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "verification": {"shape_tightening": "on"},
        "commitment": {"salience_bias": "balanced"},
    }
)
"""The two dials #227 adds, at the schema's defaults: migration ``0110_m7_presets`` adds each one
the deployment's ``profile_defaults`` does not name yet, so the document stays complete."""

FULL_SETTINGS: MappingProxyType[str, Any] = MappingProxyType(
    {
        **STRUCTURED_SETTINGS,
        "verification": {
            **STRUCTURED_SETTINGS["verification"],
            "shape_tightening": "on",
        },
        "commitment": {**STRUCTURED_SETTINGS["commitment"], "salience_bias": "balanced"},
    }
)
"""Full's settings (FR-PROF-3; #227), as stored (JSON): ``Structured``'s version 3, which already
meets every Full clause (the structured pipeline; every module on: episodic memory with hybrid
search, consolidation, commitment, the clock with time claims and scheduled stops, States;
verification at ``entailment`` with extraction ``always`` and gate 1.0; partitions with nested
beliefs to depth 2; ``full`` perception; per-character renders), with #227's two new dials named
at their defaults. Budgets stay version 2's (16 calls, 60 s); the rolling summary stays off (the
memory module is on through episodic memory and consolidation)."""

STANDARD_MODEL_CALLS = 8
STANDARD_DEADLINE_MS = 45_000

STANDARD_SETTINGS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "pipeline": "structured",
        "memory": {
            "rolling_summary": {"enabled": False},
            "episodic": {"enabled": True, "k": 40, "hybrid_search": True, "reranking": False},
            "consolidation": {"enabled": True, "cadence_turns": 20},
        },
        "pins": {"enabled": True, "max_pinned": 50},
        "character_knowledge": {
            "partitions": True,
            "nested_beliefs": {"enabled": False},
            "lazy_resolution": True,
            "testimony": "defaulted",
        },
        "perception": {"level": "co_location"},
        "verification": {
            "level": "extraction",
            "extraction_mode": "gated",
            "entailment_gate": 1.0,
            "contradiction_checking": True,
            "shape_tightening": "on",
        },
        "commitment": {"enabled": True, "salience": "rules", "salience_bias": "balanced"},
        "clock": {"level": "on", "time_claims": False, "scheduled_stops": True},
        "states": {"level": "off"},
        "multi_character": {"renders": "per_character"},
        "repair": {"max_attempts": 1, "polish": {"enabled": False}},
        "budgets": {"model_calls": STANDARD_MODEL_CALLS, "deadline_ms": STANDARD_DEADLINE_MS},
        "models": {
            "generation": {"temperature": 0.8, "top_p": 0.95, "response_length": "medium"},
        },
    }
)
"""Standard's version 1 settings (FR-PROF-3; #227), as stored (JSON): the structured pipeline,
per-character partitions (no nested beliefs; lazy resolution; testimony ``defaulted``),
``co_location`` perception, declared-claim verification with gated extraction and contradiction
checking, commitment with rules salience, the world clock with scheduled stops (no time-claim
checks), consolidation over episodic memory, States off, per-character renders, one repair
attempt, the polish pass off, and moderate budgets (8 calls, 45 s)."""

MAX_MODEL_CALLS = 24
MAX_DEADLINE_MS = 120_000
MAX_ENTAILMENT_MIN_CONFIDENCE = 0.7
"""Max's "stricter checks" (#227's Claude's Discretion): an entailment verdict needs 0.7
confidence to count, above Full's 0.6."""
MAX_STRONGEST_PURPOSES: tuple[str, ...] = (
    "entailment",
    "extraction_backstop",
    "belief_adjudication",
    "salience_detection",
    "time_estimation",
)
"""The verification purposes Max routes to ``"@strongest"`` (``core.profiles.tiers``)."""

MAX_SETTINGS: MappingProxyType[str, Any] = MappingProxyType(
    {
        **FULL_SETTINGS,
        "memory": {
            **STRUCTURED_SETTINGS["memory"],
            "consolidation": {"enabled": True, "cadence_turns": 10},
        },
        "verification": {
            **FULL_SETTINGS["verification"],
            "entailment_gate": 1.0,
            "shape_tightening": "off",
            "entailment_min_confidence": MAX_ENTAILMENT_MIN_CONFIDENCE,
        },
        "commitment": {**FULL_SETTINGS["commitment"], "salience_bias": "recall"},
        "repair": {**STRUCTURED_SETTINGS["repair"], "max_attempts": 3},
        "budgets": {"model_calls": MAX_MODEL_CALLS, "deadline_ms": MAX_DEADLINE_MS},
        "models": {
            **STRUCTURED_SETTINGS["models"],
            "purposes": {purpose: {"model": "@strongest"} for purpose in MAX_STRONGEST_PURPOSES},
        },
    }
)
"""Max's version 1 settings (FR-PROF-3; #227), as stored (JSON): Full's, plus every uncertain
verdict escalated (gate 1.0 with the per-shape tightening off), recall-biased salience, the
strongest configured model for each verification purpose (``"@strongest"``), three repair
attempts, consolidation every 10 turns, a stricter entailment confidence, and generous budgets
(24 calls, 120 s)."""


def full_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``FULL_SETTINGS``."""
    copied: dict[str, Any] = _thaw(FULL_SETTINGS)
    return copied


def standard_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``STANDARD_SETTINGS``."""
    copied: dict[str, Any] = _thaw(STANDARD_SETTINGS)
    return copied


def max_settings() -> dict[str, Any]:
    """A fresh, mutable copy of ``MAX_SETTINGS``."""
    copied: dict[str, Any] = _thaw(MAX_SETTINGS)
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
            "entailment_gate": 1.0,
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


HOT_SET_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "memory": {
            "hot_set": {
                "importance_floor": 0.2,
                "retirement_max_lag": 50,
                "retain_retired_vectors": False,
                "retain_dormant_vectors": False,
            }
        }
    }
)
"""The hot set's retirement dials (#147), at the schema's defaults: migration
``0061_retrieval_tier`` adds them to a deployment's ``profile_defaults`` that does not name them
yet."""


ESCALATION_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {"verification": {"entailment_min_confidence": 0.6, "safe_disposition": "repair"}}
)
"""The escalation dials (#142), at the schema's defaults: migration
``0057_verify_escalation_defaults`` adds them to a deployment's ``profile_defaults`` that does
not name them yet. ``verification.entailment_gate``'s default moved from 0.5 to 1.0 in the same
story (``M4_PROFILE_DEFAULTS``)."""

PREVIOUS_ENTAILMENT_GATE = 0.5
"""The ``verification.entailment_gate`` default the unreleased ``0047`` seeded before #142:
``0057`` moves a stored 0.5 to the new default."""


CONSOLIDATION_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {"memory": {"consolidation": {"window_memories": 50}}}
)
"""The consolidation window dial (#143), at the schema's default: migration
``0058_consolidation`` adds it to a deployment's ``profile_defaults`` that does not name it
yet."""


IMPLICATION_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {"repair": {"implication_threshold": 2}}
)
"""The auto-quarantine dial (#145, FR-GUARD-5), at the schema's default: migration
``0059_mutation_primitives`` adds it to a deployment's ``profile_defaults`` that does not name
it yet."""


RENDER_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {"render": {"max_beats": 6, "working_memory_beats": 20, "prompt_token_budget": 6_000}}
)
"""The structured renders' dials (#150), at the schema's defaults: migration
``0063_structured_turn`` adds them to a deployment's ``profile_defaults`` that does not name them
yet."""


M5_PROFILE_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "character_knowledge": {
            "partitions": False,
            "nested_beliefs": {"enabled": False, "depth": 2},
            "lazy_resolution": False,
            "testimony": "off",
            "max_adjudications_per_turn": 2,
            "max_lazy_resolutions_per_turn": 20,
        },
        "perception": {"level": "none"},
        "multi_character": {"renders": "combined", "interjection_max_words": 6},
    }
)
"""The settings M5 delivers (#181), at the schema's defaults, every module off: migration
``0080_structured_profile_m5`` adds each one the deployment's ``profile_defaults`` does not name
yet, so the document stays complete."""


M6_PROFILE_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType(
    {
        "clock": {"level": "off", "time_claims": False, "scheduled_stops": False},
        "states": {"level": "off"},
    }
)
"""The settings M6 delivers (#203), at the schema's defaults, both modules off: migration
``0104_structured_profile_m6`` adds each one the deployment's ``profile_defaults`` does not name
yet, so the document stays complete."""


def profile_defaults() -> dict[str, Any]:
    """A deployment's seeded ``profile_defaults``: Lite's settings with ``M4_PROFILE_DEFAULTS``,
    ``VERIFY_DEPTH_DEFAULTS``, ``RETRIEVAL_DEFAULTS``, ``ESCALATION_DEFAULTS``,
    ``CONSOLIDATION_DEFAULTS``, ``IMPLICATION_DEFAULTS``, ``HOT_SET_DEFAULTS``,
    ``RENDER_DEFAULTS``, ``M5_PROFILE_DEFAULTS``, ``M6_PROFILE_DEFAULTS`` and
    ``M7_PROFILE_DEFAULTS`` under them (a complete document of every delivered setting)."""
    base = _merged(_merged(_thaw(M4_PROFILE_DEFAULTS), VERIFY_DEPTH_DEFAULTS), RETRIEVAL_DEFAULTS)
    base = _merged(_merged(base, ESCALATION_DEFAULTS), CONSOLIDATION_DEFAULTS)
    base = _merged(_merged(base, IMPLICATION_DEFAULTS), HOT_SET_DEFAULTS)
    base = _merged(_merged(base, RENDER_DEFAULTS), M5_PROFILE_DEFAULTS)
    base = _merged(_merged(base, M6_PROFILE_DEFAULTS), M7_PROFILE_DEFAULTS)
    return _merged(base, LITE_SETTINGS)


__all__ = [
    "CONSOLIDATION_DEFAULTS",
    "ESCALATION_DEFAULTS",
    "FULL_NAME",
    "FULL_PRESET_ID",
    "FULL_SETTINGS",
    "FULL_VERSION",
    "HOT_SET_DEFAULTS",
    "IMPLICATION_DEFAULTS",
    "LITE_NAME",
    "LITE_PRESET_ID",
    "LITE_SETTINGS",
    "M4_PROFILE_DEFAULTS",
    "M5_PROFILE_DEFAULTS",
    "M6_PROFILE_DEFAULTS",
    "M7_PROFILE_DEFAULTS",
    "MAX_DEADLINE_MS",
    "MAX_ENTAILMENT_MIN_CONFIDENCE",
    "MAX_MODEL_CALLS",
    "MAX_NAME",
    "MAX_PRESET_ID",
    "MAX_SETTINGS",
    "MAX_STRONGEST_PURPOSES",
    "PREVIOUS_ENTAILMENT_GATE",
    "RENDER_DEFAULTS",
    "RETRIEVAL_DEFAULTS",
    "SEED_PRINCIPAL",
    "STANDARD_DEADLINE_MS",
    "STANDARD_MODEL_CALLS",
    "STANDARD_NAME",
    "STANDARD_PRESET_ID",
    "STANDARD_SETTINGS",
    "STRUCTURED_DEADLINE_MS",
    "STRUCTURED_GUARDRAIL_CALLS",
    "STRUCTURED_M4_NAME",
    "STRUCTURED_M4_PROFILE_ID",
    "STRUCTURED_M4_SETTINGS",
    "STRUCTURED_M4_VERSION",
    "STRUCTURED_M5_SETTINGS",
    "STRUCTURED_M5_VERSION",
    "STRUCTURED_MODEL_CALLS",
    "STRUCTURED_NAME",
    "STRUCTURED_PROFILE_ID",
    "STRUCTURED_RENDERS",
    "STRUCTURED_SETTINGS",
    "STRUCTURED_VERSION",
    "VERIFY_DEPTH_DEFAULTS",
    "full_settings",
    "lite_settings",
    "max_settings",
    "profile_defaults",
    "standard_settings",
    "structured_m4_settings",
    "structured_m5_settings",
    "structured_settings",
]
