"""Guardrail metrics (#56): counts only, never text (invariant 3, NFR-SEC-4).

``epistrel_guardrail_violations_total{kind,stage}`` counts violated policies: ``kind`` is
``deterministic`` or ``model_assisted``, ``stage`` is ``input`` (#56) or ``output`` (#68).
"""

from __future__ import annotations

from prometheus_client import Counter

GUARDRAIL_VIOLATIONS = Counter(
    "epistrel_guardrail_violations",
    "Policies violated, by policy kind and stage (input or output).",
    ("kind", "stage"),
)

__all__ = ["GUARDRAIL_VIOLATIONS"]
