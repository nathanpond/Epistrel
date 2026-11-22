"""Per-model, per-purpose call latency estimates (#602; NFR-PERF-6/7; ADR-206).

The deployment setting ``purpose_latency_estimates`` maps a model name (the bare resolved
model string a purpose's call is sent to) to a map of purpose to seconds: how long one call of
that purpose on that model is expected to take. The budget uses it twice:

- ``respond``'s pre-check: a caller's explicit ``budget.deadline_s`` below the time the turn's
  safety-minimum calls take at these estimates is refused up front with 422
  ``deadline_below_safety_minimum``, before any model call;
- the in-turn gates (``TurnBudget.can_start``): an optional call that could not start before
  ``deadline - estimate`` is skipped.

A lookup falls through ``model`` + ``purpose`` → ``model`` + ``*`` → ``*`` + ``purpose`` →
``*`` + ``*`` → ``FALLBACK_S`` (2 s, the flat estimate this setting replaces). The shipped map
(``SEEDED``) is the ``*`` fallback plus measured p90s (seconds, two decimals), recomputed by
``tools/latency_seeds.py``:

- ``claude-haiku-5-5`` (hosted): #818's baseline traces (Standard, Full and Max pooled; first
  attempts of completed case-phase calls, judge calls left out, and the first call per schema
  per session left out: #785's first-use compile). The traces hold no ``guardrail_check``
  call (the eval presets run with the policies off), so that purpose falls back.
- ``cydonia-24b-v4.3`` (local reference tier): the model matrix's Lite rows
  (``docs/model-matrix.json``), which publish p50 and p95 turn latency, not p90: ``generation``
  is the policies-off Lite turn's p95 (one generation call), ``guardrail_check`` half the
  difference between the minors and policies-off Lite turns' p95 (two checks per turn).

Core reads this module; the Orchestrator's registry validates the purpose names on a write
(``core.deployment``), so this module knows no purpose list.
"""

from __future__ import annotations

from collections.abc import Mapping

FALLBACK_S = 2.0
"""The estimate when the map has no entry at all (the flat ``DEFAULT_LATENCY_ESTIMATE_S``)."""
WILDCARD = "*"
"""The key standing for any model, or any purpose."""
MAX_S = 600.0
"""The largest estimate a write may set, in seconds."""
MODEL_KEY_MAX = 200
"""The longest model name a key may be."""
SETTING = "purpose_latency_estimates"

SEEDED: dict[str, dict[str, float]] = {
    WILDCARD: {WILDCARD: FALLBACK_S},
    "claude-haiku-5-5": {
        "consolidation": 3.54,
        "entailment": 1.43,
        "extraction_backstop": 3.65,
        "generation": 3.88,
        "importance_scoring": 1.09,
        "regeneration": 3.61,
        "salience_detection": 3.76,
        "time_estimation": 1.39,
    },
    "cydonia-24b-v4.3": {"generation": 13.2, "guardrail_check": 1.11},
}
"""The shipped default (see the module docstring); ``tools/latency_seeds.py`` recomputes it."""

Estimates = Mapping[str, Mapping[str, float]]


def seeded() -> dict[str, dict[str, float]]:
    """A fresh copy of ``SEEDED`` (a settings default)."""
    return {model: dict(purposes) for model, purposes in SEEDED.items()}


def estimate(estimates: Estimates | None, model: str | None, purpose: str) -> float:
    """The seconds one ``purpose`` call on ``model`` is expected to take (the lookup order in
    the module docstring); ``model`` ``None`` (nothing configured) reads the ``*`` entries."""
    table = estimates if estimates is not None else SEEDED
    for model_key in (model, WILDCARD):
        if model_key is None:
            continue
        by_purpose = table.get(model_key)
        if by_purpose is None:
            continue
        for purpose_key in (purpose, WILDCARD):
            if purpose_key in by_purpose:
                return float(by_purpose[purpose_key])
    return FALLBACK_S


def problems(estimates: Estimates) -> list[str]:
    """What is wrong with ``estimates``' shape (keys and values; purpose names are checked
    against the registry by the writer): empty when it is valid."""
    found: list[str] = []
    for model, by_purpose in estimates.items():
        if not 1 <= len(model) <= MODEL_KEY_MAX:
            found.append(f"a model key is 1 to {MODEL_KEY_MAX} characters ({model[:40]!r})")
        if not isinstance(by_purpose, Mapping):
            found.append(f"{model!r}: a map of purpose to seconds")
            continue
        for purpose, seconds in by_purpose.items():
            if isinstance(seconds, bool) or not isinstance(seconds, int | float):
                found.append(f"{model!r}.{purpose}: seconds as a number")
            elif not 0 < seconds <= MAX_S:
                found.append(f"{model!r}.{purpose}: seconds above 0 and at most {MAX_S:g}")
    return found


__all__ = [
    "FALLBACK_S",
    "MAX_S",
    "MODEL_KEY_MAX",
    "SEEDED",
    "SETTING",
    "WILDCARD",
    "Estimates",
    "estimate",
    "problems",
    "seeded",
]
